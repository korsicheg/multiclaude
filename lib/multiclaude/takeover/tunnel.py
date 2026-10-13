"""A Cloudflare quick tunnel to the take-over page: `cloudflared tunnel --url` gets a random
https://….trycloudflare.com address, with no account or DNS, that lasts as long as cloudflared
runs. Cloudflare serves it over HTTPS and passes each request on to the page's server here.
cloudflared logs its address and connections, never the requests, so the token stays out of it."""
import collections
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

CLOUDFLARED = "cloudflared"
# The address it was given, as it prints it in a box. Quick tunnel names are hyphenated words;
# the one other address it may print, api.trycloudflare.com, has none.
ADDRESS_RE = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")
CONNECTED = "Registered tunnel connection"  # the edge passes requests on from then
START_WITHIN = 45  # seconds for cloudflared to get an address and connect
# A new address can take some seconds to resolve after cloudflared has connected; the link goes
# out once it answers, or after this many seconds anyway.
ANSWER_WITHIN = 30
STOP_WITHIN = 5
LAST_SAID = 3      # of cloudflared's lines, kept to say why it failed


class TunnelError(Exception):
    pass


def tunnel_address(line):
    """The quick tunnel's address in a line of cloudflared's log, or None."""
    found = ADDRESS_RE.search(line)
    return found[0] if found else None


class Tunnel:
    def __init__(self):
        self.proc = None

    def open(self, port):
        """Start a quick tunnel to http://127.0.0.1:port; its https:// address once it's connected."""
        try:
            self.proc = subprocess.Popen([CLOUDFLARED, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         text=True)
        except OSError as e:
            raise TunnelError(f"can't start cloudflared ({e}); `assistant install` installs it") from e
        lines = queue.Queue()
        threading.Thread(target=self.read_log, args=(lines,), daemon=True).start()
        address = self.wait_until_connected(lines)
        wait_until_answering(address)
        return address

    def read_log(self, lines):
        """Pass on each line cloudflared logs, then None once it exits. Reading on also keeps its
        pipe from filling up while the tunnel is open."""
        for line in self.proc.stderr:
            lines.put(line.strip())
        lines.put(None)

    def wait_until_connected(self, lines):
        deadline = time.monotonic() + START_WITHIN
        address, said = None, collections.deque(maxlen=LAST_SAID)
        while (left := deadline - time.monotonic()) > 0:
            try:
                line = lines.get(timeout=left)
            except queue.Empty:
                break
            if line is None:
                raise TunnelError(f"cloudflared stopped: {' / '.join(said) or 'no reason given'}")
            said.append(line)
            address = address or tunnel_address(line)
            if address and CONNECTED in line:
                return address
        raise TunnelError(f"cloudflared didn't connect within {START_WITHIN} seconds")

    def close(self):
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(STOP_WITHIN)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def wait_until_answering(address):
    deadline = time.monotonic() + ANSWER_WITHIN
    while not answers(address) and time.monotonic() < deadline:
        time.sleep(1)


def answers(address):
    """Whether the page's server answers at address: with its 404, for a request without the
    token. Cloudflare answers for it with an error of its own until the tunnel is through."""
    try:
        with urllib.request.urlopen(f"{address}/", timeout=5):
            return False
    except urllib.error.HTTPError as e:
        return e.code == 404
    except OSError:
        return False
