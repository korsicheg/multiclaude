"""Whether an assistant is working, read from the entries Claude Code writes to its transcript."""
import json
import re
import time
from dataclasses import dataclass

from multiclaude import bot
from multiclaude.log import log
from multiclaude.router.transcript import INTERRUPTED_MARKER

STALE_AFTER = 15 * 60  # a turn that hasn't written anything this long has died (crash, interrupt)
REPLY_RE = re.compile(r"\btg\s+send\b|\bassistant\s+notify\b")
ERROR_TEXT_MAX = 300
# Claude Code's kinds of API error that trying again unchanged won't fix: a login, billing or
# plan limit problem, or a request the API refuses.
LASTING_ERRORS = {"authentication_failed", "billing_error", "invalid_request", "rate_limit"}


@dataclass
class Failure:
    """A turn that ended on an API error, after Claude Code's own retries."""
    text: str        # the first line of Claude Code's error message
    retryable: bool  # whether the same request may work later: the API couldn't be reached or failed on its side


def retryable(entry):
    """Whether an API error entry is one that may pass: no answer from the API at all (the
    network), or a failure on its side (5xx, overloaded) rather than a refusal."""
    status = entry.get("apiErrorStatus")
    return entry.get("error") not in LASTING_ERRORS and (status is None or status >= 500 or status == 408)


def interrupted(entry):
    """Whether a user entry is Claude Code's note that Escape stopped the turn."""
    content = (entry.get("message") or {}).get("content")
    blocks = content if isinstance(content, list) else [{"text": content}]
    return any(isinstance(b, dict) and str(b.get("text") or "").startswith(INTERRUPTED_MARKER) for b in blocks)


class Turn:
    """One session's work: busy from a queued message until the turn that handles it ends."""

    def __init__(self, name):
        self.name = name
        self.busy = False
        self.pending = 0         # queued messages no turn has picked up yet
        self.in_turn = False     # it started working on something and hasn't finished
        self.last_write = 0.0    # when its transcript last grew
        self.last_reply = 0.0    # when it last ran `tg send` or `assistant notify`
        self.notice = None       # a problem to tell the person about; the desk sends it
        self.failed = None       # a Failure the desk hasn't dealt with yet (recovery.py)
        self.model = None        # the model of its latest reply (the model-fallback add-in reads it)
        self.stalled = False     # judged stuck, and nothing written since: a dialog may still open

    def end(self):
        self.busy = self.in_turn = False

    def reset(self):
        """End the turn and forget the messages queued in it: claude restarted or its context was cleared."""
        self.end()
        self.pending = 0

    def expect(self, now):
        """A message is on its way: show "typing…" right away, and start the stuck-turn clock
        now rather than at the last transcript write, which may be hours old."""
        self.busy, self.last_write = True, now

    def handle(self, entry):
        self.stalled = False
        kind = entry.get("type")
        if kind == "queue-operation":
            self.busy = True
            self.pending = self.pending + 1 if entry.get("operation") == "enqueue" else max(0, self.pending - 1)
        elif kind == "assistant":
            self.handle_reply(entry)
        elif kind == "user" and interrupted(entry) and self.pending == 0:
            self.end()  # Escape (or /stop) ends a turn without the usual turn_duration entry
        elif kind == "user" and entry.get("queueTranscriptOnly") and self.pending == 0 and not self.in_turn:
            self.end()  # a queued item Claude Code only wrote down, starting no turn
        elif kind == "system" and entry.get("subtype") == "turn_duration" and self.pending == 0:
            self.end()

    def handle_reply(self, entry):
        content = (entry.get("message") or {}).get("content") or []
        if entry.get("isApiErrorMessage"):
            # Claude Code gave up on the API (after its own retries), which ends the turn.
            text = content if isinstance(content, str) else "".join(
                b.get("text", "") for b in content if isinstance(b, dict))
            text = (text.strip().splitlines() or [""])[0][:ERROR_TEXT_MAX]
            log(f"{self.name}: turn ended with an API error: {text}")
            if self.pending == 0:
                self.end()
            self.failed = Failure(text, retryable(entry))
            return
        self.busy = self.in_turn = True
        model = (entry.get("message") or {}).get("model") or ""
        if model.startswith("claude-"):  # not Claude Code's own "<synthetic>" notes
            self.model = model
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and REPLY_RE.search(json.dumps(block.get("input") or {}))):
                self.last_reply = time.time()

    def postpone_stuck(self, now):
        """Judge it stuck no sooner than STALE_AFTER from now. While the VM is offline a turn is
        held up through no fault of its own: it may be waiting out Claude Code's retries."""
        self.last_write = max(self.last_write, now)

    def stuck(self, now, latest_subagent_write):
        """End a turn that has written nothing for STALE_AFTER, subagents included. Whether it did."""
        if not self.busy or now - self.last_write <= STALE_AFTER:
            return False
        self.last_write = max(self.last_write, latest_subagent_write())
        if now - self.last_write <= STALE_AFTER:
            return False
        log(f"{self.name}: turn looks stuck; stopping typing")
        self.end()
        self.stalled = True
        self.notice = (f"⚠️ {bot.name()} has shown no progress on your request for {STALE_AFTER // 60} minutes, "
                       "so it may be stuck. If you don't hear back soon, send `/clear` or run "
                       "`assistant status` on the VM.")
        return True
