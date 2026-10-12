"""Getting going again after the VM loses its network (Network probes for it), or the Claude API
fails a turn with an error that may pass (Pickups has the assistant carry on by itself)."""
import os
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass

from multiclaude import bot, paths, telegram, users
from multiclaude.log import log
from multiclaude.router.usage import fmt_duration

PROBE_EVERY = 15          # seconds between network probes while online
PROBE_EVERY_DOWN = 5      # and while offline, to notice soon that it's back
PROBE_TIMEOUT = 10
DOWN_AFTER = 2            # failed probes in a row before the VM counts as offline, so one blip doesn't count
CLAUDE_API = "https://api.anthropic.com"  # when ~/assistant/.env sets no ANTHROPIC_BASE_URL
REPORT_AFTER = 3 * 60     # the owner hears about an outage at least this long, once it's over
RESUME_AFTER = (60, 5 * 60)  # the wait before each automatic pick-up of a dropped request...
RESUME_WINDOW = 60 * 60      # ...of which an assistant gets at most len(RESUME_AFTER) in this time
OUTAGE_GRACE = 60         # a turn that failed this soon after an outage ended still failed because of it


@dataclass(frozen=True)
class Outage:
    start: float
    end: float

    @property
    def duration(self):
        return self.end - self.start

    def covers(self, when):
        return self.start <= when <= self.end + OUTAGE_GRACE

    def report(self, chat):
        """What the person whose chat this is (the owner) hears about it once it's over."""
        return (f"🌐 The VM lost its network from {users.local_time(chat, self.start)} to "
                f"{users.local_time(chat, self.end)} ({fmt_duration(self.duration)}). "
                "Any request it interrupted is picked up again by itself.")


class Network:
    """Whether the VM can reach the network: a background probe opens a new connection to every
    target, since in an outage connections already open can keep working while no new one gets out."""

    def __init__(self, targets):
        self.targets = targets       # (host, port) pairs that must all accept a connection
        self.down_since = None       # when the outage began; None while online
        self.failing_since = None    # when the current run of failed probes began
        self.failures = 0
        self.ended = []              # Outages that ended, for the desk to take
        self.last_outage = None      # the latest Outage that ended
        self.lock = threading.Lock()

    def online(self):
        return self.down_since is None

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="network").start()

    def loop(self):
        while True:
            try:
                self.probe(time.time())
            except Exception:
                log(f"network probe failed:\n{traceback.format_exc().rstrip()}")
            time.sleep(PROBE_EVERY if self.online() else PROBE_EVERY_DOWN)

    def probe(self, now):
        problems = self.problems()
        with self.lock:
            if problems:
                self.failed(now, problems)
            else:
                self.got_through(now)

    def problems(self):
        """Why each target that can't be reached can't be."""
        found = []
        for host, port in self.targets:
            try:
                telegram.connect_fast((host, port), PROBE_TIMEOUT).close()
            except OSError as e:
                found.append(f"{host}:{port}: {e!r}")
        return found

    def failed(self, now, problems):
        self.failures += 1
        self.failing_since = self.failing_since or now
        if self.online() and self.failures >= DOWN_AFTER:
            self.down_since = self.failing_since
            log(f"network: the VM is offline: {'; '.join(problems)}")

    def got_through(self, now):
        if not self.online():
            log(f"network: back after {fmt_duration(now - self.down_since)} offline")
            self.last_outage = Outage(self.down_since, now)
            self.ended.append(self.last_outage)
        self.down_since, self.failing_since, self.failures = None, None, 0

    def take_ended(self):
        """The Outages that ended since the last call."""
        with self.lock:
            ended, self.ended = self.ended, []
        return ended


def targets():
    """What the VM has to reach: Telegram's Bot API, and the Claude API the sessions use."""
    return [address(telegram.API_BASE), address(claude_api())]


def address(url):
    url = urllib.parse.urlsplit(url)
    return url.hostname, url.port or (443 if url.scheme == "https" else 80)


def claude_api():
    """The sessions' ANTHROPIC_BASE_URL: ~/assistant/.env's, which run.sh and run-user.sh load."""
    return env_file().get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or CLAUDE_API


def env_file():
    """The variables ~/assistant/.env sets, {} if there's no such file."""
    try:
        with open(paths.ENV_FILE) as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    pairs = (line.strip().removeprefix("export ").partition("=") for line in lines)
    return {key.strip(): value.strip().strip("'\"") for key, equals, value in pairs if equals}


@dataclass
class Resume:
    """A request an assistant's turn dropped on an API error, to pick up again."""
    error: str     # the first line of Claude Code's error
    failed: float  # when the turn ended
    wait: int      # how long after that it's picked up, at the earliest

    @property
    def due(self):
        return self.failed + self.wait

    def note(self, display, outage):
        """The line typed into the assistant to have it carry on. outage: the latest Outage, or None."""
        cause = (f" The VM was offline from {utc_time(outage.start)} to {utc_time(outage.end)} UTC."
                 if outage and outage.covers(self.failed) else "")
        return (f"[Router · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(self.failed))}] Your last turn "
                f"stopped on an API error before it was done ({self.error.rstrip('.') or 'no details'}).{cause} "
                f"It should work again now, so pick up where you left off; {display} was told you would carry "
                "on by yourself. If that work was already done, just stop.")


class Pickups:
    """One assistant's pick-ups of requests its turns dropped on an API error that may pass."""

    def __init__(self):
        self.waiting = None  # the Resume still to type in
        self.done = []       # when it last picked a request up

    def plan(self, failure, now):
        """Arrange to pick up the request a failed turn (a turn.Failure) dropped, if the failure
        may pass and this hour's pick-ups aren't used up; otherwise there's none to come."""
        self.done = [t for t in self.done if now - t < RESUME_WINDOW]
        self.waiting = None
        if failure.retryable and len(self.done) < len(RESUME_AFTER):
            self.waiting = Resume(failure.text, now, RESUME_AFTER[len(self.done)])

    def notice(self, failure):
        """What the person hears about the failure, once plan() has dealt with it."""
        detail = f": {failure.text}" if failure.text else "."
        if not self.waiting:
            return (f"⚠️ {bot.name()} hit an error and stopped before finishing{detail}\n\n"
                    "Send your message again in a minute. If it keeps happening, run `assistant status` on the VM.")
        wait = "a minute" if self.waiting.wait <= 60 else f"{self.waiting.wait // 60} minutes"
        return (f"⚠️ {bot.name()} hit an error before finishing{detail}\n\n"
                f"It will pick up where it left off by itself in about {wait}, once Claude can be reached.")

    def due(self, now):
        return self.waiting is not None and now >= self.waiting.due

    def take(self, now):
        """The Resume that's due, counted as done from now."""
        resume, self.waiting = self.waiting, None
        self.done.append(now)
        return resume

    def cancel(self):
        self.waiting = None


def utc_time(when):
    return time.strftime("%H:%M", time.gmtime(when))
