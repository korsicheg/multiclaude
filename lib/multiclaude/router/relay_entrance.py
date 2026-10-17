"""The way in from the relay (relay/, a Cloudflare Worker), which holds control panel taps, and what
the add-ins that are on take in there, until the VM takes them, so nothing is lost while it's
offline. An outgoing WebSocket (so no public address) takes envelopes,
{envelope_id, type, payload}, and acknowledges each; what wasn't acknowledged comes again on the
next connection. What goes back (a panel snapshot, an add-in's answer) is a PUT.

state/relay.json, readable by the owner only, holds {"relay_url": "wss://…/queue", "relay_token"};
`assistant relay deploy` writes it."""
import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from multiclaude import jsonfile, paths
from multiclaude.log import log
from multiclaude.websocket import WebSocket, WebSocketClosed

QUIET_PING = 2       # ping a connection that has said nothing for this long
DEAD_AFTER = 6       # and give up on it after this long without a word
RETRY_FIRST, RETRY_MAX = 2, 60  # seconds between attempts to connect, doubling
PUT_TIMEOUT = 10
GONE = 410           # what the relay answers a PUT nobody waits for any more
SETTINGS = paths.STATE / "relay.json"


def settings():
    """What state/relay.json holds (see above), or {} when there's no relay."""
    saved = jsonfile.read_json(SETTINGS, {})
    return saved if isinstance(saved, dict) and saved.get("relay_url") and saved.get("relay_token") else {}


def address(path):
    """path's https address on the relay, or None when there's no relay."""
    relay = settings().get("relay_url")
    return urllib.parse.urlsplit(relay)._replace(scheme="https", path=path).geturl() if relay else None


def token():
    """What the VM shows the relay it's the VM, or None when there's no relay."""
    return settings().get("relay_token")


def put(url, token, body):
    """PUT body (JSON bytes) to the relay. OSError when it can't, urllib's HTTPError when it refuses."""
    request = urllib.request.Request(url, data=body, method="PUT", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "multiclaude-router"})
    urllib.request.urlopen(request, timeout=PUT_TIMEOUT).close()


def answer(path, body):
    """Put body, an add-in's answer to a request the relay holds for the VM, at path on the relay:
    True once the request has it, False when nothing waits for it any more. OSError when there's no
    relay or it can't be reached, and urllib's HTTPError (an OSError too) when it refuses the answer."""
    url, relay_token = address(path), token()
    if not url or not relay_token:
        raise OSError("no relay in state/relay.json")
    try:
        put(url, relay_token, json.dumps(body, ensure_ascii=False).encode())
    except urllib.error.HTTPError as e:
        if e.code == GONE:
            return False
        raise
    return True


class RelayEntrance(threading.Thread):
    def __init__(self, relay_url, relay_token):
        super().__init__(name="relay-entrance", daemon=True)
        self.relay_url = relay_url
        self.relay_token = relay_token
        self.inbox = queue.Queue()  # envelopes: {"type": "panel", or an add-in's type, "payload"}
        self.greeted = False  # whether the relay said hello on the current connection

    def run(self):
        wait = RETRY_FIRST
        while True:
            self.greeted = False
            try:
                self.listen()
            except (WebSocketClosed, OSError, ValueError) as e:
                log(f"relay: connection lost ({e})")
            if self.greeted:
                wait = RETRY_FIRST  # it was working, so connect again at once
                continue
            log(f"relay: trying again in {wait}s")
            time.sleep(wait)
            wait = min(wait * 2, RETRY_MAX)

    def listen(self):
        """One connection, until it ends."""
        ws = WebSocket.connect(self.relay_url, headers=[("Authorization", f"Bearer {self.relay_token}")])
        ws.settimeout(QUIET_PING)
        try:
            while True:
                try:
                    envelope = json.loads(ws.recv())
                except socket.timeout:
                    if time.monotonic() - ws.heard > DEAD_AFTER:
                        raise WebSocketClosed(f"nothing heard for {DEAD_AFTER}s") from None
                    ws.ping()
                    continue
                self.take(ws, envelope)
        finally:
            ws.close()

    def take(self, ws, envelope):
        """Acknowledge and queue one envelope."""
        if envelope.get("type") == "hello":
            self.greeted = True
            log("relay: connected")
        if envelope.get("envelope_id"):
            ws.send(json.dumps({"envelope_id": envelope["envelope_id"]}))
            self.inbox.put(envelope)

    def waiting(self):
        """The envelopes that arrived since the last call."""
        found = []
        while True:
            try:
                found.append(self.inbox.get_nowait())
            except queue.Empty:
                return found
