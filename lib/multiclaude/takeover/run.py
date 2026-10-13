"""The take-over: the page served through the tunnel and its link sent, until the person taps Done
or I can't do it, the time runs out, the browser closes, or the command is stopped. Then the
tunnel and the page go, the browser's viewport is put back, and the command says how it ended, in
a line on stdout and its exit status."""
import argparse
import contextlib
import fcntl
import os
import secrets
import signal
import sys
import threading
import time
from dataclasses import dataclass

from multiclaude import paths
from multiclaude.log import log
from multiclaude.takeover.browser import NoBrowser, Session
from multiclaude.takeover.fields import Fields
from multiclaude.takeover.notice import PrintedLink, TelegramLink
from multiclaude.takeover.page import PageServer, page_html
from multiclaude.takeover.stream import Stream
from multiclaude.takeover.tunnel import Tunnel, TunnelError
from multiclaude.takeover.viewport import Fitting
from multiclaude.telegram import TelegramError
from multiclaude.websocket import WebSocketClosed

DONE, GAVE_UP, EXPIRED, CLOSED, STOPPED = "done", "gave-up", "expired", "closed", "stopped"
FAILED = 1
EXIT_STATUS = {DONE: 0, GAVE_UP: 3, EXPIRED: 4, CLOSED: FAILED, STOPPED: FAILED}
RESULTS = {DONE: "done: they tapped Done; carry on in the browser",
           GAVE_UP: "gave up: they tapped I can't do it",
           EXPIRED: "expired: no Done within {minutes} minutes",
           CLOSED: "closed: the browser closed during the take-over",
           STOPPED: "stopped: the take-over was stopped before they tapped Done"}
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)  # a stopped background task, a closed terminal, Ctrl-C
DEFAULT_TASK = "Please do what this page needs from you, then tap Done."
DEFAULT_MINUTES, MOST_MINUTES = 15, 60
TOKEN_BYTES = 32
CHECK_EVERY = 1  # seconds between looks at the clock and the browser while waiting


class Busy(Exception):
    """Someone is already taking over this browser."""


class Stopped(Exception):
    """The command was told to stop (STOP_SIGNALS)."""


@dataclass(frozen=True)
class Browser:
    """The browser taken over: its stream (frames in, input out), its viewport and its text fields."""
    stream: Stream
    viewport: Fitting
    fields: Fields

    def hand_back(self):
        """Put the viewport back as it was and let go of the stream. A browser that has closed is
        left alone, since any agent-browser command for its session would start it again."""
        if not self.stream.closed:
            try:
                self.viewport.restore()
            except NoBrowser as e:
                log(f"takeover: couldn't put the viewport back: {e}")
        self.stream.close()


class Ending:
    """How a take-over ends, decided once: by the page (Done, I can't do it), the clock, the
    browser or a stop."""

    def __init__(self):
        self.outcome = None
        self.decided = threading.Event()
        self.lock = threading.Lock()

    @property
    def over(self):
        return self.decided.is_set()

    def finish(self, outcome):
        with self.lock:
            if not self.over:
                self.outcome = outcome
                self.decided.set()

    def wait(self, seconds):
        return self.decided.wait(seconds)


class TakeOver:
    def __init__(self, browser, tunnel, notice, seconds):
        """browser: the Browser taken over; tunnel: opens the way in (Tunnel); notice: sends the
        link and says how it ended (TelegramLink, PrintedLink); seconds: how long the link works."""
        self.browser, self.tunnel, self.notice, self.seconds = browser, tunnel, notice, seconds
        self.ending = Ending()

    def run(self, task):
        """Hand the browser over until the take-over ends; how it ended."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        server = PageServer(token, self.browser, self.ending, page_html(task, time.time() + self.seconds))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            address = self.tunnel.open(server.port)
            self.notice.send(f"{address}/{token}/")
            outcome = self.wait()
        finally:
            self.tunnel.close()
            server.shutdown()
            server.server_close()
            self.browser.hand_back()
        self.notice.close(outcome)
        return outcome

    def wait(self):
        deadline = time.monotonic() + self.seconds
        try:
            while not self.ending.wait(CHECK_EVERY):
                if self.browser.stream.closed:
                    self.ending.finish(CLOSED)
                elif time.monotonic() >= deadline:
                    self.ending.finish(EXPIRED)
        except Stopped:
            self.ending.finish(STOPPED)
        return self.ending.outcome


@contextlib.contextmanager
def only_take_over(port):
    """Hold the lock on the take-overs of the browser whose stream is on port; Busy if it's held."""
    path = paths.TAKEOVERS / f"{port}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("this browser is already being taken over") from None
        yield


def main(argv):
    options = parse(argv)
    for signum in STOP_SIGNALS:
        signal.signal(signum, stop)
    try:
        notice = PrintedLink() if options.print_link else TelegramLink(chat_for(options), options.task, options.minutes)
        session = Session(options.session, options.home)
        port = session.stream_port()
        with only_take_over(port):
            browser = Browser(Stream.connect(port), Fitting(session), Fields(session))
            outcome = TakeOver(browser, Tunnel(), notice, options.minutes * 60).run(options.task)
    except (Busy, Stopped, NoBrowser, TunnelError, TelegramError, WebSocketClosed, OSError) as e:
        print(f"takeover: {e}", file=sys.stderr)
        return FAILED
    print(RESULTS[outcome].format(minutes=options.minutes), flush=True)
    return EXIT_STATUS[outcome]


def stop(signum, frame):
    raise Stopped(f"stopped by {signal.Signals(signum).name}")


def chat_for(options):
    chat = options.chat or os.environ.get("TG_CHAT_ID", "")
    if not chat:
        raise TelegramError("no chat: set TG_CHAT_ID or pass --chat ID (or --print-link)")
    return chat


def parse(argv):
    parser = argparse.ArgumentParser(
        prog="takeover", description="Hand an agent-browser session to the person: a private link, live, until they tap Done.",
        epilog="Exit status: 0 Done, 3 I can't do it, 4 the time ran out, 1 failed, stopped or the browser closed "
               "(stderr or the line on stdout says which).")
    parser.add_argument("--session", required=True, help="the agent-browser session (agent-browser --session NAME)")
    parser.add_argument("--for", dest="task", default=DEFAULT_TASK, help="what the person should do there, in Markdown")
    parser.add_argument("--minutes", type=link_minutes, default=DEFAULT_MINUTES,
                        help=f"how long the link works (default {DEFAULT_MINUTES}, at most {MOST_MINUTES})")
    parser.add_argument("--home", help="the HOME the session was started with, if not this one (a plugin's own)")
    parser.add_argument("--chat", help="the Telegram chat for the link (default: $TG_CHAT_ID)")
    parser.add_argument("--print-link", action="store_true", help="print the link instead of sending it")
    return parser.parse_args(argv)


def link_minutes(text):
    value = int(text)
    if not 1 <= value <= MOST_MINUTES:
        raise argparse.ArgumentTypeError(f"1 to {MOST_MINUTES}")
    return value
