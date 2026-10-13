"""The take-over page's web server, on 127.0.0.1 for the tunnel to reach. Every path is under a
secret token (/<token>/…), and anything else gets 404, as does plain HTTP through the tunnel. It
logs nothing, since every request line carries the token.

  GET  /<token>/                the page
  GET  /<token>/frame?after=N   the browser's latest frame once it's newer than frame N (X-Frame:
                                its number), held up to FRAME_WAIT seconds; 204 if none came
  POST /<token>/input           one action, as controls.py takes it
  POST /<token>/screen          {"width", "height"}: the CSS pixels the page has for the browser, which
                                its viewport is fitted to (viewport.py). A fit agent-browser fails is
                                logged, and answered 204 all the same: the page shows the frames scaled.
  POST /<token>/end             {"outcome": "done" | "gave-up"}: the person tapped Done or I can't do it
Once the take-over has ended, everything under the token answers 410."""
import hmac
import html
import http.server
import json
import re
import sys
import urllib.parse
from pathlib import Path

from multiclaude import bot
from multiclaude.log import log
from multiclaude.takeover import controls
from multiclaude.takeover.browser import NoBrowser

PAGE = Path(__file__).with_name("page.html")
PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
MARKDOWN_MARKS_RE = re.compile(r"\*\*|__|~~|\|\||`")  # bold, strikethrough, spoiler and code
FRAME_WAIT = 20         # seconds a frame request waits for a newer frame
BODY_MAX = 16 * 1024    # bytes in a request from the page
PAGE_OUTCOMES = ("done", "gave-up")
ALWAYS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow",
          "X-Content-Type-Options": "nosniff"}
PAGE_POLICY = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src blob:; "
               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def page_html(task, expires):
    """The page, with what the person is asked to do (the Markdown the Telegram message shows, as
    plain text) and when the link expires (a Unix time)."""
    values = {"BOT_NAME": html.escape(bot.name()), "TASK": html.escape(MARKDOWN_MARKS_RE.sub("", task)),
              "EXPIRES": str(int(expires * 1000))}
    return PLACEHOLDER_RE.sub(lambda m: values[m[1]], PAGE.read_text())


class PageServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, token, browser, ending, page):
        """browser: the Browser taken over (its stream and viewport); ending: the take-over's
        Ending, which the page's Done ends."""
        super().__init__(("127.0.0.1", 0), Handler)
        self.token, self.browser, self.ending, self.page = token, browser, ending, page.encode()

    @property
    def port(self):
        return self.server_address[1]

    def handle_error(self, request, client_address):
        """A page that went away mid-answer is no error: cloudflared hangs up the connections it
        holds as the tunnel closes, and a phone drops them as it sleeps."""
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # the page fetches frame after frame on one connection
    server_version, sys_version = "takeover", ""

    def log_message(self, format, *args):
        """Nothing is logged: request lines carry the token."""

    def do_GET(self):
        route = self.route()
        if route == "":
            self.reply(200, self.server.page, "text/html; charset=utf-8", {"Content-Security-Policy": PAGE_POLICY})
        elif route == "frame":
            self.serve_frame()
        else:
            self.reply(404)

    def do_POST(self):
        route = self.route()
        if route == "input":
            self.take_input()
        elif route == "screen":
            self.take_screen()
        elif route == "end":
            self.take_end()
        else:
            self.close_connection = True  # its body is left unread
            self.reply(404)

    def route(self):
        """The path after the token, or None for a request without the token, or one that came
        through the tunnel over plain HTTP."""
        if self.headers.get("X-Forwarded-Proto", "https") != "https":
            return None
        parts = urllib.parse.urlsplit(self.path).path.split("/", 2)  # "", token, route
        if len(parts) < 3 or not hmac.compare_digest(parts[1].encode(), self.server.token.encode()):
            return None
        return parts[2]

    def serve_frame(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            after = int(query.get("after", ["0"])[0])
        except ValueError:
            return self.reply(400)
        frame = self.server.browser.stream.next_frame(after, FRAME_WAIT)
        if self.server.ending.over:
            self.reply(410)
        elif frame is None:
            self.reply(204)
        else:
            self.reply(200, frame.jpeg, "image/jpeg", {"X-Frame": str(frame.seq)})

    def take_input(self):
        action = self.read_json()
        if self.server.ending.over:
            return self.reply(410)
        stream = self.server.browser.stream
        try:
            events = controls.events(action, stream.frame)
        except ValueError:
            return self.reply(400)
        try:
            stream.send(events)
        except OSError:
            return self.reply(502)
        self.reply(204)

    def take_screen(self):
        size = self.read_json()
        if self.server.ending.over:
            return self.reply(410)
        try:
            self.server.browser.viewport.fit(size)
        except ValueError:
            return self.reply(400)
        except NoBrowser as e:
            log(f"takeover: couldn't fit the viewport to the page: {e}")
        self.reply(204)

    def take_end(self):
        outcome = (self.read_json() or {}).get("outcome")
        if outcome not in PAGE_OUTCOMES:
            return self.reply(400)
        self.server.ending.finish(outcome)
        self.reply(204)

    def read_json(self):
        """The request's JSON object, or None. A body too big to read is left, with the connection."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = BODY_MAX + 1
        if length > BODY_MAX:
            self.close_connection = True
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def reply(self, status, body=b"", content_type=None, headers=None):
        self.send_response(status)
        for name, value in {**ALWAYS, **(headers or {})}.items():
            self.send_header(name, value)
        if content_type:
            self.send_header("Content-Type", content_type)
        if status != 204:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
