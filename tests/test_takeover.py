"""Browser take-overs (bin/takeover, lib/multiclaude/takeover): finding the browser's stream, the
bridge to it, the page's server, the tunnel, and how a take-over ends. agent-browser, its stream
server, cloudflared and Telegram are fakes; nothing leaves the machine."""
import base64
import hashlib
import json
import os
import queue
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from conftest import ALICE_CHAT, nowhere

TOKEN = "s3cret-t0ken"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@pytest.fixture
def tk(lib, monkeypatch):
    """The takeover package, with short waits."""
    from multiclaude import paths, telegram, websocket
    from multiclaude.takeover import browser, controls, notice, page, run, stream, tunnel
    monkeypatch.setattr(page, "FRAME_WAIT", 0.2)
    monkeypatch.setattr(run, "CHECK_EVERY", 0.02)
    return SimpleNamespace(paths=paths, telegram=telegram, websocket=websocket, browser=browser, controls=controls,
                           notice=notice, page=page, run=run, stream=stream, tunnel=tunnel)


def jpeg(width, height):
    """As much of a JPEG image as its size needs: the start of image, a Huffman table segment
    (whose marker sits among the frame headers' markers) and the frame header."""
    huffman = b"\xff\xc4" + (4).to_bytes(2, "big") + b"\x00\x00"
    frame_header = (b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + height.to_bytes(2, "big")
                    + width.to_bytes(2, "big") + bytes(10))
    return b"\xff\xd8" + huffman + frame_header


def frame_message(image, device_width=1280):
    return json.dumps({"type": "frame", "data": base64.b64encode(image).decode(),
                       "metadata": {"deviceWidth": device_width, "deviceHeight": 720, "pageScaleFactor": 1,
                                    "offsetTop": 0, "scrollOffsetX": 0, "scrollOffsetY": 0}})


def fetch(url, data=None, headers=None):
    """(status, headers, body) of a GET, or of a POST of data as JSON."""
    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


# --- the browser's stream server -----------------------------------------------------------------

def fake_agent_browser(monkeypatch, browser, replies):
    """agent-browser answering each command (its args after --json) with the data in replies, or
    failing for one it has none for; the (args, HOME) of each call."""
    calls = []

    def run(argv, **options):
        args = " ".join(argv[2:])
        calls.append((args, (options.get("env") or {}).get("HOME")))
        reply = {"success": args in replies, "data": replies.get(args), "error": None if args in replies else "nope"}
        return subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")

    monkeypatch.setattr(browser.subprocess, "run", run)
    return calls


def status(port=41713, enabled=True, connected=True):
    return {"connected": connected, "enabled": enabled, "port": port if enabled else None, "screencasting": False}


def test_the_stream_port_is_the_one_stream_status_names(tk, monkeypatch):
    calls = fake_agent_browser(monkeypatch, tk.browser, {
        "session list": {"sessions": ["shop", "booking"]},
        "--session booking stream status": status(41713)})

    assert tk.browser.stream_port("booking") == 41713
    assert [args for args, _ in calls] == ["session list", "--session booking stream status"]


def test_a_stream_turned_off_is_turned_on_again(tk, monkeypatch):
    calls = fake_agent_browser(monkeypatch, tk.browser, {
        "session list": {"sessions": ["booking"]},
        "--session booking stream status": status(enabled=False),
        "--session booking stream enable": status(40001)})

    assert tk.browser.stream_port("booking") == 40001
    assert calls[-1][0] == "--session booking stream enable"


def test_a_session_that_isnt_running_is_never_started(tk, monkeypatch):
    """Any agent-browser command for a session starts it, so only `session list` is asked."""
    calls = fake_agent_browser(monkeypatch, tk.browser, {"session list": {"sessions": ["shop"]}})

    with pytest.raises(tk.browser.NoBrowser, match="no agent-browser session 'booking'"):
        tk.browser.stream_port("booking")
    assert [args for args, _ in calls] == ["session list"]


def test_a_session_without_a_browser_open_cant_be_taken_over(tk, monkeypatch):
    fake_agent_browser(monkeypatch, tk.browser, {
        "session list": {"sessions": ["booking"]},
        "--session booking stream status": status(connected=False)})

    with pytest.raises(tk.browser.NoBrowser, match="no browser open"):
        tk.browser.stream_port("booking")


def test_a_session_a_plugin_runs_under_its_own_home_is_asked_there(tk, monkeypatch):
    calls = fake_agent_browser(monkeypatch, tk.browser, {
        "session list": {"sessions": ["dei"]}, "--session dei stream status": status(36371)})

    assert tk.browser.stream_port("dei", home="/ws/alice/.agent-browser") == 36371
    assert {home for _, home in calls} == {"/ws/alice/.agent-browser"}


def test_agent_browsers_own_error_is_passed_on(tk):
    with pytest.raises(tk.browser.NoBrowser, match="stream status: Browser not launched"):
        tk.browser.reply_data('{"success": false, "data": null, "error": "Browser not launched"}', "stream status")
    with pytest.raises(tk.browser.NoBrowser, match="session list: command not found"):
        tk.browser.reply_data("command not found\n", "session list")


# --- the bridge to it --------------------------------------------------------------------------------

def test_a_jpegs_size_is_read_from_its_frame_header(tk):
    assert tk.stream.jpeg_size(jpeg(1280, 633)) == (1280, 633)
    with pytest.raises(ValueError):
        tk.stream.jpeg_size(b"\x89PNG\r\n\x1a\n" + bytes(20))


def socket_stream(tk):
    """A Stream over a socket pair, and the browser's end of it, which reads what the stream sends."""
    ours, theirs = socket.socketpair()
    theirs.settimeout(5)
    return tk.stream.Stream(tk.websocket.WebSocket(ours)), tk.websocket.WebSocket(theirs)


def test_the_stream_keeps_the_latest_frame_numbered(tk):
    stream, _ = socket_stream(tk)
    stream.take(frame_message(jpeg(1280, 633)))
    stream.take(frame_message(jpeg(640, 316)))

    frame = stream.next_frame(0, wait=0)
    assert (frame.seq, frame.width, frame.height, frame.device_width) == (2, 640, 316, 1280)
    assert stream.next_frame(2, wait=0.05) is None


def test_messages_and_frames_it_cant_read_are_left(tk):
    stream, _ = socket_stream(tk)
    stream.take(frame_message(jpeg(1280, 633)))
    for message in ('{"type": "status", "connected": true}', "not json", frame_message(b"not a jpeg"),
                    '{"type": "frame", "data": "%%%"}', '["frame"]'):
        stream.take(message)

    assert stream.frame.seq == 1


def test_a_point_on_the_frame_becomes_the_pages_css_pixel(tk):
    """The frame can be smaller than the page and shorter than the deviceHeight the stream reports
    (the window's, not the viewport's), so both axes scale with the width."""
    frame = tk.stream.Frame(1, b"", width=640, height=316, device_width=1280)

    assert frame.device_point(0.5, 0.5) == (640, 316)
    assert frame.device_point(1, 1) == (1280, 632)


class FakeStreamServer:
    """agent-browser's stream server as far as a take-over sees it: a WebSocket on 127.0.0.1 that
    sends the frames it's given and keeps the input events it gets."""

    def __init__(self, tk):
        self.tk = tk
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.events = queue.Queue()
        self.conn = None
        self.connected = threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        self.conn, _ = self.listener.accept()
        request = b""
        while b"\r\n\r\n" not in request:
            request += self.conn.recv(4096)
        key = re.search(rb"Sec-WebSocket-Key: (\S+)", request)[1].decode()
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        self.connected.set()
        client = self.tk.websocket.WebSocket(self.conn)
        try:
            while True:
                self.events.put(json.loads(client.recv()))
        except (self.tk.websocket.WebSocketClosed, OSError):
            pass

    def send(self, text):
        """A text frame, as a server sends it: unmasked, with the length its size needs."""
        payload = text.encode()
        if len(payload) < 126:
            head = bytes([0x81, len(payload)])
        else:
            head = bytes([0x81, 126]) + len(payload).to_bytes(2, "big")
        self.conn.sendall(head + payload)

    def received(self, count):
        return [self.events.get(timeout=5) for _ in range(count)]

    def close(self):
        """Hang up, as agent-browser does when its browser closes."""
        if self.conn:
            self.conn.shutdown(socket.SHUT_RDWR)  # its own reader is still waiting on it
            self.conn.close()
        self.listener.close()


def test_the_bridge_follows_the_stream_server_and_sends_it_input(tk):
    server = FakeStreamServer(tk)
    stream = tk.stream.Stream.connect(server.port)
    server.connected.wait(5)
    server.send(frame_message(jpeg(1280, 633)))

    assert stream.next_frame(0, wait=5).seq == 1
    stream.send([{"type": "input_mouse", "eventType": "mouseMoved"}, {"type": "input_mouse", "eventType": "mousePressed"}])
    assert [e["eventType"] for e in server.received(2)] == ["mouseMoved", "mousePressed"]

    server.close()
    deadline = time.monotonic() + 5
    while not stream.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stream.closed and stream.next_frame(0, wait=0) is None


# --- the page --------------------------------------------------------------------------------------

@pytest.fixture
def page(tk):
    """The page's server for a stream over a socket pair, under TOKEN."""
    stream, browser = socket_stream(tk)
    ending = tk.run.Ending()
    server = tk.page.PageServer(TOKEN, stream, ending, tk.page.page_html("Tick **the** box & <tap> Done", time.time() + 60))
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()  # quick to shut down
    yield SimpleNamespace(stream=stream, browser=browser, ending=ending, url=f"http://127.0.0.1:{server.port}/{TOKEN}/",
                          root=f"http://127.0.0.1:{server.port}")
    server.shutdown()
    server.server_close()


def sent_events(page, count):
    return [json.loads(page.browser.recv()) for _ in range(count)]


@pytest.mark.parametrize("path", ["/", f"/{TOKEN}", f"/{TOKEN}x/", "/wrong/frame?after=0", f"/x/{TOKEN}/", "/favicon.ico"])
def test_requests_without_the_token_get_404(page, path):
    assert fetch(page.root + path)[0] == 404
    assert fetch(page.root + path, data={"outcome": "done"})[0] == 404
    assert not page.ending.over


def test_plain_http_through_the_tunnel_gets_404(page):
    assert fetch(page.url, headers={"X-Forwarded-Proto": "http"})[0] == 404
    assert fetch(page.url, headers={"X-Forwarded-Proto": "https"})[0] == 200


def test_the_page_shows_the_task_as_plain_text_under_a_strict_policy(page):
    code, headers, body = fetch(page.url)

    assert code == 200 and headers["Content-Type"].startswith("text/html")
    assert "<p id=\"task\">Tick the box &amp; &lt;tap&gt; Done</p>" in body.decode()
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"


def test_the_frame_endpoint_serves_the_latest_frame_once_it_is_newer(page):
    assert fetch(page.url + "frame?after=0")[0] == 204  # none yet, after FRAME_WAIT
    page.stream.take(frame_message(jpeg(1280, 633)))

    code, headers, body = fetch(page.url + "frame?after=0")
    assert (code, headers["Content-Type"], headers["X-Frame"], body) == (200, "image/jpeg", "1", jpeg(1280, 633))
    assert fetch(page.url + "frame?after=1")[0] == 204


def test_a_tap_reaches_the_browser_at_the_pages_css_pixels(page):
    page.stream.take(frame_message(jpeg(640, 316), device_width=1280))

    assert fetch(page.url + "input", data={"kind": "press", "x": 0.25, "y": 0.5})[0] == 204
    assert fetch(page.url + "input", data={"kind": "move", "x": 0.5, "y": 0.5})[0] == 204
    assert fetch(page.url + "input", data={"kind": "release", "x": 0.5, "y": 0.5})[0] == 204

    common = {"type": "input_mouse", "y": 316, "modifiers": 0}
    assert sent_events(page, 4) == [
        {**common, "eventType": "mouseMoved", "x": 320, "button": "none", "clickCount": 0},
        {**common, "eventType": "mousePressed", "x": 320, "button": "left", "clickCount": 1},
        {**common, "eventType": "mouseMoved", "x": 640, "button": "left", "clickCount": 0},
        {**common, "eventType": "mouseReleased", "x": 640, "button": "left", "clickCount": 1}]


def test_scrolling_turns_the_wheel_where_the_person_last_touched(page):
    page.stream.take(frame_message(jpeg(1280, 633)))

    fetch(page.url + "input", data={"kind": "scroll", "direction": "up", "x": 0.5, "y": 0.1})

    assert sent_events(page, 1) == [{"type": "input_mouse", "eventType": "mouseWheel", "x": 640, "y": 63.3,
                                     "button": "none", "clickCount": 0, "modifiers": 0, "deltaX": 0, "deltaY": -400}]


def test_typing_presses_a_key_for_each_character_and_the_buttons_their_keys(page):
    assert fetch(page.url + "input", data={"kind": "text", "text": "a1 é"})[0] == 204
    assert fetch(page.url + "input", data={"kind": "key", "key": "Enter"})[0] == 204

    downs = [(e["key"], e["text"], e["windowsVirtualKeyCode"]) for e in sent_events(page, 10) if e["eventType"] == "keyDown"]
    assert downs == [("a", "a", 65), ("1", "1", 49), (" ", " ", 32), ("é", "é", 0), ("Enter", "\r", 13)]


def test_every_key_event_carries_every_field_the_stream_server_needs(tk):
    """It drops a key event that lacks one (a Backspace without "text" never arrives)."""
    for event in tk.controls.events({"kind": "key", "key": "Backspace"}, None):
        assert set(event) == {"type", "eventType", "key", "code", "text", "windowsVirtualKeyCode", "modifiers"}


@pytest.mark.parametrize("action", [
    {"kind": "click", "x": 0.5, "y": 0.5}, {"kind": "press"}, {"kind": "press", "x": "left", "y": 0},
    {"kind": "text", "text": ""}, {"kind": "text", "text": "x" * 1001}, {"kind": "key", "key": "F5"},
    {"kind": "scroll", "direction": "sideways", "x": 0, "y": 0}, ["press"]])
def test_actions_the_browser_cant_take_get_400(page, action):
    page.stream.take(frame_message(jpeg(1280, 633)))

    assert fetch(page.url + "input", data=action)[0] == 400


def test_a_press_before_the_first_frame_gets_400(page):
    assert fetch(page.url + "input", data={"kind": "press", "x": 0.5, "y": 0.5})[0] == 400


def test_done_and_i_cant_do_it_end_the_take_over_and_then_everything_answers_410(page):
    assert fetch(page.url + "end", data={"outcome": "finished"})[0] == 400
    assert fetch(page.url + "end", data={"outcome": "gave-up"})[0] == 204
    assert fetch(page.url + "end", data={"outcome": "done"})[0] == 204

    assert page.ending.outcome == "gave-up"
    assert fetch(page.url + "frame?after=0")[0] == 410
    assert fetch(page.url + "input", data={"kind": "text", "text": "late"})[0] == 410


def test_nothing_is_logged_since_every_request_carries_the_token(page, capfd):
    fetch(page.url)
    fetch(page.url + "frame?after=x")
    fetch(page.root + "/nope/")

    out, err = capfd.readouterr()
    assert TOKEN not in out + err and "GET" not in out + err


# --- the tunnel --------------------------------------------------------------------------------------

CLOUDFLARED_LOG = """\
2026-10-13T10:55:29Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-10-13T10:55:35Z INF +--------------------------------------------------------------------------------------------+
2026-10-13T10:55:35Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
2026-10-13T10:55:35Z INF |  https://casa-guides-ventures-liable.trycloudflare.com                                     |
2026-10-13T10:55:35Z INF +--------------------------------------------------------------------------------------------+
2026-10-13T10:55:35Z INF Starting metrics server on 127.0.0.1:20241/metrics
2026-10-13T10:55:36Z INF Registered tunnel connection connIndex=0 connection=271fb8da event=0 ip=2606:4700:a0::3 protocol=quic
"""
CLOUDFLARED_FAILURE = ('2026-10-13T10:55:30Z ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": '
                       "dial tcp: lookup api.trycloudflare.com: no such host\n")


def fake_cloudflared(tmp_path, monkeypatch, tk, log, then="exec sleep 30"):
    """A cloudflared that logs log, as cloudflared does on stderr, then does then; its arguments are
    kept. The address it gives answers at once."""
    monkeypatch.setattr(tk.tunnel, "answers", lambda address: True)
    script = tmp_path / "cloudflared"
    (tmp_path / "cloudflared.log").write_text(log)
    script.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/cloudflared.args"\ncat "{tmp_path}/cloudflared.log" >&2\n{then}\n')
    script.chmod(0o755)
    monkeypatch.setattr(tk.tunnel, "CLOUDFLARED", str(script))
    return tmp_path / "cloudflared.args"


def test_the_tunnels_address_is_read_from_cloudflareds_log(tk):
    found = [tk.tunnel.tunnel_address(line) for line in (CLOUDFLARED_LOG + CLOUDFLARED_FAILURE).splitlines()]

    assert [address for address in found if address] == ["https://casa-guides-ventures-liable.trycloudflare.com"]


def test_the_tunnel_opens_once_connected_and_closes(tk, tmp_path, monkeypatch):
    args = fake_cloudflared(tmp_path, monkeypatch, tk, CLOUDFLARED_LOG)
    tunnel = tk.tunnel.Tunnel()

    assert tunnel.open(8123) == "https://casa-guides-ventures-liable.trycloudflare.com"
    assert args.read_text().split() == ["tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8123"]
    tunnel.close()
    assert tunnel.proc.poll() is not None


def test_the_link_waits_until_the_new_address_answers(tk, monkeypatch):
    """A new quick tunnel's name can take some seconds to resolve after cloudflared has connected."""
    asked = []
    monkeypatch.setattr(tk.tunnel, "answers", lambda address: asked.append(address) or len(asked) == 3)
    monkeypatch.setattr(tk.tunnel.time, "sleep", lambda seconds: None)

    tk.tunnel.wait_until_answering("https://casa-guides-ventures-liable.trycloudflare.com")

    assert len(asked) == 3


def test_only_the_pages_own_server_counts_as_answering(page, tk):
    assert tk.tunnel.answers(page.root)  # its 404, for a request without the token
    assert not tk.tunnel.answers(nowhere())


def test_a_tunnel_that_fails_says_why(tk, tmp_path, monkeypatch):
    fake_cloudflared(tmp_path, monkeypatch, tk, CLOUDFLARED_FAILURE, then="exit 1")

    with pytest.raises(tk.tunnel.TunnelError, match="cloudflared stopped: .*no such host"):
        tk.tunnel.Tunnel().open(8123)


def test_without_cloudflared_the_tunnel_says_how_to_install_it(tk, tmp_path, monkeypatch):
    monkeypatch.setattr(tk.tunnel, "CLOUDFLARED", str(tmp_path / "missing"))

    with pytest.raises(tk.tunnel.TunnelError, match="assistant install"):
        tk.tunnel.Tunnel().open(8123)


# --- the take-over -----------------------------------------------------------------------------------

class FakeTunnel:
    """The way in, without Cloudflare: the page's own address on this machine."""
    made = []

    def __init__(self):
        self.closed = False
        FakeTunnel.made.append(self)

    def open(self, port):
        return f"http://127.0.0.1:{port}"

    def close(self):
        self.closed = True


class KeptLink:
    """The notice, kept rather than sent: the link, and how the take-over ended."""

    def __init__(self):
        self.link, self.outcome = None, None
        self.sent = threading.Event()

    def send(self, link):
        self.link = link
        self.sent.set()

    def close(self, outcome):
        self.outcome = outcome


def take_over(tk, seconds=30, then=None):
    """Run a take-over of a stream over a socket pair; then(link, stream) does what the person (or
    the browser) does once the link is out. (outcome, the notice, the stream)."""
    stream, _ = socket_stream(tk)
    notice = KeptLink()

    def act():
        notice.sent.wait(5)
        if then:
            then(notice.link, stream)

    threading.Thread(target=act, daemon=True).start()
    outcome = tk.run.TakeOver(stream, FakeTunnel(), notice, seconds).run("Tick the box")
    return outcome, notice, stream


@pytest.mark.parametrize("tapped", ["done", "gave-up"])
def test_a_tap_on_the_page_ends_the_take_over_and_closes_the_way_in(tk, tapped):
    outcome, notice, stream = take_over(tk, then=lambda link, _: fetch(link + "end", data={"outcome": tapped}))

    assert outcome == notice.outcome == tapped
    assert FakeTunnel.made[-1].closed and stream.closed
    with pytest.raises(OSError):
        fetch(notice.link)  # the page's server is gone too


def test_a_take_over_expires_after_its_time(tk):
    outcome, notice, _ = take_over(tk, seconds=0.2)

    assert outcome == notice.outcome == "expired"


def test_a_browser_that_closes_ends_the_take_over(tk):
    outcome, _, _ = take_over(tk, then=lambda link, stream: stream.close())

    assert outcome == "closed"


def test_only_one_take_over_of_a_browser_at_a_time(tk):
    with tk.run.only_take_over(41713):
        with pytest.raises(tk.run.Busy):
            with tk.run.only_take_over(41713):
                pass
        with tk.run.only_take_over(41714):
            pass


@pytest.fixture
def command(tk, bot_api, monkeypatch):
    """`takeover` as an assistant runs it, for chat ALICE_CHAT, against a fake stream server, the
    fake tunnel and the fake Bot API (command.api); the signal handlers it sets are put back afterwards."""
    server = FakeStreamServer(tk)
    monkeypatch.setattr(tk.run, "stream_port", lambda session, home: server.port)
    monkeypatch.setattr(tk.run, "Tunnel", FakeTunnel)
    monkeypatch.setattr(tk.notice, "BotApi", lambda timeout: tk.telegram.BotApi(timeout=timeout, base=bot_api.url))
    monkeypatch.setenv("TG_CHAT_ID", ALICE_CHAT)
    tk.paths.TELEGRAM_DIR.mkdir(parents=True)
    (tk.paths.TELEGRAM_DIR / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
    handlers = {signum: signal.getsignal(signum) for signum in tk.run.STOP_SIGNALS}
    yield SimpleNamespace(server=server, api=bot_api)
    for signum, handler in handlers.items():
        signal.signal(signum, handler)
    server.close()


def the_link(api):
    """The link the take-over sent, from its button, once it's sent."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for method, fields, _ in api.calls:
            if method == "sendRichMessage":
                return json.loads(fields["reply_markup"])["inline_keyboard"][0][0]["url"]
        time.sleep(0.02)
    raise AssertionError("no link was sent")


def person(api, then):
    """The person: once the link arrives, then(link)."""
    threading.Thread(target=lambda: then(the_link(api)), daemon=True).start()


@pytest.mark.parametrize("tapped, status, result", [("done", 0, "done: they tapped Done"),
                                                    ("gave-up", 3, "gave up: they tapped I can't do it")])
def test_the_command_sends_the_link_and_says_how_it_ended(tk, command, capsys, tapped, status, result):
    person(command.api, lambda link: fetch(link + "end", data={"outcome": tapped}))

    assert tk.run.main(["--session", "booking", "--for", "Solve the **CAPTCHA**, then tap Done", "--minutes", "5"]) == status

    assert capsys.readouterr().out.startswith(result)
    (_, sent, _), (method, edit, _) = command.api.calls
    assert sent["chat_id"] == ALICE_CHAT and "Solve the **CAPTCHA**" in json.loads(sent["rich_message"])["markdown"]
    assert "works for 5 minutes" in json.loads(sent["rich_message"])["markdown"]
    assert method == "editMessageText" and edit["message_id"] == "501" and "reply_markup" not in edit
    assert json.loads(edit["rich_message"])["markdown"].startswith(tk.notice.ENDINGS[tapped])
    assert (tk.paths.STATE / "messages" / ALICE_CHAT).read_text().startswith("501 ")


def test_the_command_stops_cleanly_when_told_to(tk, command, capsys):
    """A stopped background task gets SIGTERM: the tunnel closes and the message says so."""
    person(command.api, lambda link: os.kill(os.getpid(), signal.SIGTERM))

    assert tk.run.main(["--session", "booking"]) == 1

    assert capsys.readouterr().out.startswith("stopped:")
    assert FakeTunnel.made[-1].closed
    assert json.loads(command.api.calls[-1][1]["rich_message"])["markdown"].startswith(tk.notice.ENDINGS["stopped"])


def test_the_command_needs_a_chat_for_the_link(tk, command, monkeypatch, capsys):
    monkeypatch.delenv("TG_CHAT_ID")

    assert tk.run.main(["--session", "booking"]) == 1
    assert "no chat" in capsys.readouterr().err and command.api.calls == []


def test_the_command_fails_when_telegram_refuses_the_link(tk, command, capsys):
    command.api.fail["sendRichMessage"] = "Bad Request: chat not found"

    assert tk.run.main(["--session", "booking"]) == 1
    assert "chat not found" in capsys.readouterr().err and FakeTunnel.made[-1].closed
