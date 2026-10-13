"""Browser take-overs (bin/takeover, lib/multiclaude/takeover): finding the browser's stream, the
bridge to it, fitting its viewport to the person's screen, its text fields for a phone's keyboard,
the page's server, the tunnel, and how a take-over ends. agent-browser, its stream server, cloudflared and Telegram are fakes; nothing leaves
the machine."""
import base64
import hashlib
import json
import os
import queue
import re
import signal
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
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
    from multiclaude.takeover import browser, controls, fields, notice, page, run, stream, tunnel, viewport
    monkeypatch.setattr(page, "FRAME_WAIT", 0.2)
    monkeypatch.setattr(run, "CHECK_EVERY", 0.02)
    return SimpleNamespace(paths=paths, telegram=telegram, websocket=websocket, browser=browser, controls=controls,
                           fields=fields, notice=notice, page=page, run=run, stream=stream, tunnel=tunnel, viewport=viewport)


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

    assert tk.browser.Session("booking").stream_port() == 41713
    assert [args for args, _ in calls] == ["session list", "--session booking stream status"]


def test_a_stream_turned_off_is_turned_on_again(tk, monkeypatch):
    calls = fake_agent_browser(monkeypatch, tk.browser, {
        "session list": {"sessions": ["booking"]},
        "--session booking stream status": status(enabled=False),
        "--session booking stream enable": status(40001)})

    assert tk.browser.Session("booking").stream_port() == 40001
    assert calls[-1][0] == "--session booking stream enable"


def test_a_session_that_isnt_running_is_never_started(tk, monkeypatch):
    """Any agent-browser command for a session starts it, so only `session list` is asked."""
    calls = fake_agent_browser(monkeypatch, tk.browser, {"session list": {"sessions": ["shop"]}})

    with pytest.raises(tk.browser.NoBrowser, match="no agent-browser session 'booking'"):
        tk.browser.Session("booking").stream_port()
    assert [args for args, _ in calls] == ["session list"]


def test_a_session_without_a_browser_open_cant_be_taken_over(tk, monkeypatch):
    fake_agent_browser(monkeypatch, tk.browser, {
        "session list": {"sessions": ["booking"]},
        "--session booking stream status": status(connected=False)})

    with pytest.raises(tk.browser.NoBrowser, match="no browser open"):
        tk.browser.Session("booking").stream_port()


def test_a_session_a_plugin_runs_under_its_own_home_is_asked_there(tk, monkeypatch):
    calls = fake_agent_browser(monkeypatch, tk.browser, {
        "session list": {"sessions": ["dei"]}, "--session dei stream status": status(36371)})

    assert tk.browser.Session("dei", home="/ws/alice/.agent-browser").stream_port() == 36371
    assert {home for _, home in calls} == {"/ws/alice/.agent-browser"}


VIEWPORT_EVAL = "--session booking eval [innerWidth, innerHeight, devicePixelRatio]"


def test_the_viewport_is_read_as_the_page_sees_it_and_set_at_its_scale(tk, monkeypatch):
    calls = fake_agent_browser(monkeypatch, tk.browser, {
        VIEWPORT_EVAL: {"origin": "https://example.com/", "result": [1280, 633, 2]},
        "--session booking set viewport 1440 789 2": {"width": 1440, "height": 789, "deviceScaleFactor": 2.0}})
    session = tk.browser.Session("booking")

    assert session.viewport() == tk.browser.Viewport(1280, 633, 2.0)
    session.set_viewport(tk.browser.Viewport(1440, 789, 2.0))
    assert calls[-1][0] == "--session booking set viewport 1440 789 2"


def test_a_viewport_that_cant_be_read_is_no_browser(tk, monkeypatch):
    fake_agent_browser(monkeypatch, tk.browser, {VIEWPORT_EVAL: {"origin": "about:blank", "result": "nope"}})

    with pytest.raises(tk.browser.NoBrowser, match="no viewport"):
        tk.browser.Session("booking").viewport()


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

    assert frame.css_pixels(0.5, 0.5) == (640, 316)
    assert frame.css_pixels(1, 1) == (1280, 632)


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


# --- the viewport ----------------------------------------------------------------------------------

class FakeSession:
    """An agent-browser session as a take-over sees it: its stream's port, its viewport, each
    viewport it was given (set), and what its page answers to an eval of FIELDS_SCRIPT (found), with
    each script it was given (evals). fail: the error each command fails with, if any."""

    def __init__(self, tk, port=41713, viewport=(1280, 633, 1.0)):
        self.browser, self.port = tk.browser, port
        self.current, self.set, self.fail = tk.browser.Viewport(*viewport), [], None
        self.found, self.evals = {"width": 1280, "height": 633, "fields": [], "focus": None}, []

    def stream_port(self):
        return self.port

    def viewport(self):
        self.check()
        return self.current

    def eval(self, script):
        self.check()
        self.evals.append(script)
        return self.found

    def set_viewport(self, viewport):
        self.check()
        self.set.append(viewport)
        self.current = viewport

    def check(self):
        if self.fail:
            raise self.browser.NoBrowser(self.fail)


def sizes(viewports):
    return [(viewport.width, viewport.height) for viewport in viewports]


def test_the_viewport_takes_the_room_the_page_has_at_the_pages_own_scale(tk):
    session = FakeSession(tk, viewport=(1280, 633, 2.0))
    fitting = tk.viewport.Fitting(session)

    fitting.fit({"width": 1440, "height": 789.4})
    fitting.fit({"width": 1440, "height": 789})  # the same size again changes nothing

    assert session.set == [tk.browser.Viewport(1440, 789, 2.0)]


def test_a_fitted_viewport_stays_within_sane_sizes(tk):
    session = FakeSession(tk)
    fitting = tk.viewport.Fitting(session)

    fitting.fit({"width": 200, "height": 150})
    fitting.fit({"width": 5000, "height": 3000})

    assert sizes(session.set) == [(320, 320), (2560, 1600)]


@pytest.mark.parametrize("size", [None, {}, {"width": 390}, {"width": "wide", "height": 700}, {"width": [390], "height": 700},
                                  {"width": float("nan"), "height": 700}, {"width": 1e400, "height": 700}])
def test_a_size_that_isnt_one_is_refused(tk, size):
    session = FakeSession(tk)

    with pytest.raises(ValueError):
        tk.viewport.Fitting(session).fit(size)
    assert session.set == []


def test_the_viewport_is_put_back_as_it_was_and_fitted_no_more(tk):
    session = FakeSession(tk, viewport=(1280, 633, 1.0))
    fitting = tk.viewport.Fitting(session)
    fitting.fit({"width": 390, "height": 664})
    fitting.fit({"width": 664, "height": 390})  # turned on its side

    fitting.restore()
    fitting.fit({"width": 1440, "height": 789})  # a late one, from a page still open

    assert sizes(session.set) == [(390, 664), (664, 390), (1280, 633)]


def test_a_viewport_never_fitted_is_left_alone(tk):
    session = FakeSession(tk)

    tk.viewport.Fitting(session).restore()

    assert session.set == []


# --- the text fields -------------------------------------------------------------------------------

EMAIL_KEYBOARD = {"mode": "email", "capitalize": "off", "correct": False}


def found(x=64, y=63.3, width=640, height=31.65, **keyboard):
    """A field as FIELDS_SCRIPT finds it on a 1280×633 page: an email address's, unless keyboard says otherwise."""
    return {"x": x, "y": y, "width": width, "height": height, "type": "email", "inputmode": None,
            "autocapitalize": None, "autocorrect": None, "spellcheck": None, **keyboard}


def found_on_the_page(*fields, focus=None):
    return {"width": 1280, "height": 633, "fields": list(fields or [found()]), "focus": focus}


def test_the_fields_in_view_are_fractions_of_the_viewport(tk):
    seen = tk.fields.in_view(found_on_the_page(found(), found(x=1200, y=600, width=200, height=100)))

    assert [(f["x"], f["y"], f["width"], f["height"]) for f in seen["fields"]] == [
        (0.05, 0.1, 0.5, 0.05), (0.9375, 0.9479, 0.0625, 0.0521)]  # the second one cut off where the viewport ends


def test_a_field_outside_the_viewport_is_left_out_unless_it_has_the_focus(tk):
    below = found(y=700)

    seen = tk.fields.in_view(found_on_the_page(below, focus=below))

    assert seen["fields"] == [] and seen["focus"]["keyboard"] == EMAIL_KEYBOARD


@pytest.mark.parametrize("field, keyboard", [
    (found(type="text"), {"mode": "text", "capitalize": "sentences", "correct": True}),
    (found(type="textarea", spellcheck="false"), {"mode": "text", "capitalize": "sentences", "correct": False}),
    (found(type="password"), {"mode": "text", "capitalize": "off", "correct": False}),
    (found(type="number"), {"mode": "decimal", "capitalize": "off", "correct": False}),
    (found(type="text", inputmode="numeric", autocapitalize="off", autocorrect="off"),
     {"mode": "numeric", "capitalize": "off", "correct": False}),
    (found(type="editable", inputmode="none", autocapitalize="shouting", autocorrect="maybe"),
     {"mode": "text", "capitalize": "sentences", "correct": True})])
def test_each_field_brings_up_the_keyboard_a_phone_would_for_it(tk, field, keyboard):
    """Its own inputmode, autocapitalize and autocorrect where it has them, a phone's defaults for its type where not."""
    assert tk.fields.in_view(found_on_the_page(field))["fields"][0]["keyboard"] == keyboard


def test_a_focus_in_a_frame_from_another_site_is_only_known_to_be_there(tk):
    assert tk.fields.in_view(found_on_the_page(focus="frame"))["focus"] == "frame"


@pytest.mark.parametrize("result", [
    None, "nope", [], {"width": 1280, "height": 633, "fields": []},
    {"width": 0, "height": 633, "fields": [], "focus": None},
    {"width": "wide", "height": 633, "fields": [], "focus": None},
    found_on_the_page(found(x=float("nan"))), found_on_the_page(found(width=-5)), found_on_the_page(found(y=True)),
    found_on_the_page(found(type=["email"])), found_on_the_page(focus=42), found_on_the_page("field")])
def test_what_isnt_the_fields_in_view_is_refused(tk, result):
    """The page's own scripts could meddle with what the script finds."""
    with pytest.raises(ValueError):
        tk.fields.in_view(result)


# --- the page --------------------------------------------------------------------------------------

@pytest.fixture
def page(tk):
    """The page's server for a stream over a socket pair and a fake session, under TOKEN."""
    stream, browser = socket_stream(tk)
    session = FakeSession(tk)
    ending = tk.run.Ending()
    taken = tk.run.Browser(stream, tk.viewport.Fitting(session), tk.fields.Fields(session))
    server = tk.page.PageServer(TOKEN, taken, ending, tk.page.page_html("Tick **the** box & <tap> Done", time.time() + 60))
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()  # quick to shut down
    yield SimpleNamespace(stream=stream, browser=browser, session=session, ending=ending,
                          url=f"http://127.0.0.1:{server.port}/{TOKEN}/", root=f"http://127.0.0.1:{server.port}")
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
    assert "<p id=\"task-full\">Tick the box &amp; &lt;tap&gt; Done</p>" in body.decode()
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"


def test_the_frame_endpoint_serves_the_latest_frame_once_it_is_newer(page):
    assert fetch(page.url + "frame?after=0")[0] == 204  # none yet, after FRAME_WAIT
    page.stream.take(frame_message(jpeg(1280, 633)))

    code, headers, body = fetch(page.url + "frame?after=0")
    assert (code, headers["Content-Type"], headers["X-Frame"], body) == (200, "image/jpeg", "1", jpeg(1280, 633))
    assert fetch(page.url + "frame?after=1")[0] == 204


def post_input(page, *actions):
    for action in actions:
        assert fetch(page.url + "input", data=action)[0] == 204


def test_a_tap_or_a_drag_reaches_the_browser_at_the_pages_css_pixels(page):
    page.stream.take(frame_message(jpeg(640, 316), device_width=1280))

    post_input(page, {"kind": "press", "x": 0.25, "y": 0.5}, {"kind": "move", "x": 0.5, "y": 0.5, "button": "left"},
               {"kind": "release", "x": 0.5, "y": 0.5})

    common = {"type": "input_mouse", "y": 316, "modifiers": 0}
    assert sent_events(page, 4) == [
        {**common, "eventType": "mouseMoved", "x": 320, "button": "none", "clickCount": 0},
        {**common, "eventType": "mousePressed", "x": 320, "button": "left", "clickCount": 1},
        {**common, "eventType": "mouseMoved", "x": 640, "button": "left", "clickCount": 0},
        {**common, "eventType": "mouseReleased", "x": 640, "button": "left", "clickCount": 1}]


def test_a_mouse_hovers_and_clicks_with_any_button_twice_over_and_keys_held(page):
    """Hovering opens a menu; a double click's second press and release count 2; Shift+click selects."""
    page.stream.take(frame_message(jpeg(1440, 789), device_width=1440))

    post_input(page, {"kind": "move", "x": 0.1, "y": 0.1},
               {"kind": "press", "x": 0.5, "y": 0.5, "button": "right", "clicks": 2, "modifiers": 8},
               {"kind": "release", "x": 0.5, "y": 0.5, "button": "right", "clicks": 2, "modifiers": 8})

    hover, _, pressed, released = sent_events(page, 4)
    assert (hover["eventType"], hover["button"], hover["x"]) == ("mouseMoved", "none", 144)
    assert [(e["eventType"], e["button"], e["clickCount"], e["modifiers"]) for e in (pressed, released)] == [
        ("mousePressed", "right", 2, 8), ("mouseReleased", "right", 2, 8)]


def test_a_wheel_turns_by_fractions_of_the_frame_where_the_pointer_is(page):
    page.stream.take(frame_message(jpeg(1280, 633)))

    post_input(page, {"kind": "wheel", "x": 0.5, "y": 0.1, "dx": 0.05, "dy": -0.8})

    assert sent_events(page, 1) == [{"type": "input_mouse", "eventType": "mouseWheel", "x": 640, "y": 63.3,
                                     "button": "none", "clickCount": 0, "modifiers": 0, "deltaX": 64, "deltaY": -506.4}]


def test_typing_presses_a_key_for_each_character_and_the_buttons_their_keys(page):
    post_input(page, {"kind": "text", "text": "a1 é\n"}, {"kind": "key", "key": "Enter"})

    downs = [(e["key"], e["text"], e["windowsVirtualKeyCode"]) for e in sent_events(page, 12) if e["eventType"] == "keyDown"]
    assert downs == [("a", "a", 65), ("1", "1", 49), (" ", " ", 32), ("é", "é", 0), ("Enter", "\r", 13), ("Enter", "\r", 13)]


def test_keys_that_type_nothing_and_shortcuts_go_with_the_keys_held(page):
    """Shift+ArrowLeft selects a character; Control+A selects everything, typing no "a"."""
    post_input(page, {"kind": "key", "key": "ArrowLeft", "modifiers": 8}, {"kind": "key", "key": "a", "modifiers": 2})

    downs = [(e["key"], e["code"], e["text"], e["windowsVirtualKeyCode"], e["modifiers"])
             for e in sent_events(page, 4) if e["eventType"] == "keyDown"]
    assert downs == [("ArrowLeft", "ArrowLeft", "", 37, 8), ("a", "KeyA", "", 65, 2)]


def test_a_key_pressed_times_over_goes_down_and_up_each_time(page):
    """A phone's keyboard deletes the word autocorrect replaces with as many Backspaces at once."""
    post_input(page, {"kind": "key", "key": "Backspace", "times": 3})

    assert [e["eventType"] for e in sent_events(page, 6)] == ["keyDown", "keyUp"] * 3


def test_a_key_is_pressed_at_most_so_many_times_at_once(tk):
    events = tk.controls.events({"kind": "key", "key": "ArrowLeft", "times": 10_000}, None)

    assert len(events) == 2 * tk.controls.MOST_PRESSES


def test_every_key_event_carries_every_field_the_stream_server_needs(tk):
    """It drops a key event that lacks one (a Backspace without "text" never arrives)."""
    for event in tk.controls.events({"kind": "key", "key": "Backspace"}, None):
        assert set(event) == {"type", "eventType", "key", "code", "text", "windowsVirtualKeyCode", "modifiers"}


@pytest.mark.parametrize("action", [
    {"kind": "click", "x": 0.5, "y": 0.5}, {"kind": "press"}, {"kind": "press", "x": "left", "y": 0},
    {"kind": "press", "x": 0, "y": 0, "button": "back"}, {"kind": "press", "x": 0, "y": 0, "modifiers": 16},
    {"kind": "move", "x": 0, "y": 0, "button": "both"}, {"kind": "wheel", "x": 0, "y": 0},
    {"kind": "text", "text": ""}, {"kind": "text", "text": "x" * 1001}, {"kind": "key", "key": "F5"},
    {"kind": "key", "key": "ab"}, {"kind": "key", "key": ["Enter"]}, {"kind": "key", "key": "Tab", "times": "two"},
    {"kind": ["press"]}, ["press"]])
def test_actions_the_browser_cant_take_get_400(page, action):
    page.stream.take(frame_message(jpeg(1280, 633)))

    assert fetch(page.url + "input", data=action)[0] == 400


def test_a_press_before_the_first_frame_gets_400(page):
    assert fetch(page.url + "input", data={"kind": "press", "x": 0.5, "y": 0.5})[0] == 400


def test_the_room_the_page_reports_fits_the_viewport(page):
    assert fetch(page.url + "screen", data={"width": 390, "height": 664})[0] == 204
    assert fetch(page.url + "screen", data={"width": "wide"})[0] == 400

    assert sizes(page.session.set) == [(390, 664)]


def test_a_fit_agent_browser_fails_is_logged_and_the_page_carries_on(page, capfd):
    page.session.fail = "Browser not launched"

    assert fetch(page.url + "screen", data={"width": 390, "height": 664})[0] == 204
    assert "couldn't fit the viewport" in capfd.readouterr().err


def test_the_page_gets_the_text_fields_the_browser_has_in_view(page, tk):
    page.session.found = found_on_the_page(focus=None)

    code, headers, body = fetch(page.url + "fields")

    assert (code, headers["Content-Type"]) == (200, "application/json")
    assert json.loads(body) == {"fields": [{"x": 0.05, "y": 0.1, "width": 0.5, "height": 0.05, "keyboard": EMAIL_KEYBOARD}],
                                "focus": None}
    assert page.session.evals == [tk.fields.FIELDS_SCRIPT]


@pytest.mark.parametrize("trouble", ["agent-browser", "answer"])
def test_fields_the_browser_cant_say_get_502(page, trouble):
    """The page asks again once the browser's page changes."""
    if trouble == "agent-browser":
        page.session.fail = "Execution context was destroyed"
    else:
        page.session.found = {"width": 1280, "height": 633, "fields": "nope", "focus": None}

    assert fetch(page.url + "fields")[0] == 502


def test_once_the_browser_has_closed_nothing_asks_agent_browser(page):
    """Any agent-browser command for its session would start it again."""
    page.stream.close()

    assert fetch(page.url + "fields")[0] == 410
    assert fetch(page.url + "screen", data={"width": 390, "height": 664})[0] == 410
    assert page.session.evals == [] and page.session.set == []


def test_done_and_i_cant_do_it_end_the_take_over_and_then_everything_answers_410(page):
    assert fetch(page.url + "end", data={"outcome": "finished"})[0] == 400
    assert fetch(page.url + "end", data={"outcome": "gave-up"})[0] == 204
    assert fetch(page.url + "end", data={"outcome": "done"})[0] == 204

    assert page.ending.outcome == "gave-up"
    assert fetch(page.url + "frame?after=0")[0] == 410
    assert fetch(page.url + "input", data={"kind": "text", "text": "late"})[0] == 410
    assert fetch(page.url + "screen", data={"width": 390, "height": 664})[0] == 410
    assert fetch(page.url + "fields")[0] == 410


def test_a_page_that_goes_away_mid_answer_is_no_error(page, capfd):
    """cloudflared hangs up the connections it holds as the tunnel closes, a frame request among them."""
    connection = socket.create_connection(("127.0.0.1", urllib.parse.urlsplit(page.root).port))
    connection.sendall(f"GET /{TOKEN}/frame?after=0 HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # hang up with a reset
    connection.close()
    time.sleep(0.5)  # past FRAME_WAIT, when the answer meets the closed connection

    assert "Exception" not in capfd.readouterr().err


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


def take_over(tk, seconds=30, then=None, session=None):
    """Run a take-over of a stream over a socket pair and session (a FakeSession); then(link,
    stream) does what the person (or the browser) does once the link is out. (outcome, the
    notice, the stream)."""
    stream, _ = socket_stream(tk)
    notice = KeptLink()

    def act():
        notice.sent.wait(5)
        if then:
            then(notice.link, stream)

    threading.Thread(target=act, daemon=True).start()
    session = session or FakeSession(tk)
    browser = tk.run.Browser(stream, tk.viewport.Fitting(session), tk.fields.Fields(session))
    outcome = tk.run.TakeOver(browser, FakeTunnel(), notice, seconds).run("Tick the box")
    return outcome, notice, stream


@pytest.mark.parametrize("tapped", ["done", "gave-up"])
def test_a_tap_on_the_page_ends_the_take_over_and_closes_the_way_in(tk, tapped):
    outcome, notice, stream = take_over(tk, then=lambda link, _: fetch(link + "end", data={"outcome": tapped}))

    assert outcome == notice.outcome == tapped
    assert FakeTunnel.made[-1].closed and stream.closed
    with pytest.raises(OSError):
        fetch(notice.link)  # the page's server is gone too


def fit_then(size, then):
    """The person's page reporting its room, then then(link, stream)."""
    def act(link, stream):
        fetch(link + "screen", data=size)
        then(link, stream)
    return act


def test_the_viewport_is_put_back_once_the_person_is_done(tk):
    session = FakeSession(tk, viewport=(1280, 633, 1.0))

    take_over(tk, session=session,
              then=fit_then({"width": 1440, "height": 789}, lambda link, _: fetch(link + "end", data={"outcome": "done"})))

    assert sizes(session.set) == [(1440, 789), (1280, 633)]


def test_a_browser_that_closed_is_left_alone(tk):
    """Any agent-browser command for its session would start it again."""
    session = FakeSession(tk)

    outcome, _, _ = take_over(tk, session=session,
                              then=fit_then({"width": 390, "height": 664}, lambda link, stream: stream.close()))

    assert outcome == "closed" and sizes(session.set) == [(390, 664)]


def test_a_viewport_that_cant_be_put_back_is_logged(tk, capfd):
    session = FakeSession(tk)

    def done_as_the_browser_fails(link, _):
        session.fail = "Browser not launched"
        fetch(link + "end", data={"outcome": "done"})

    outcome, _, _ = take_over(tk, session=session, then=fit_then({"width": 390, "height": 664}, done_as_the_browser_fails))

    assert outcome == "done" and "couldn't put the viewport back: Browser not launched" in capfd.readouterr().err


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
    monkeypatch.setattr(tk.run, "Session", lambda name, home: FakeSession(tk, port=server.port))
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
                return json.loads(fields["reply_markup"])["inline_keyboard"][0][0]["web_app"]["url"]
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
