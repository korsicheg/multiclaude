"""Model fallback (add-in model-fallback): when a model's safety classifier stops one of an assistant's
replies, Claude Code moves the session to another model and logs a system entry of subtype
model_refusal_fallback ("Opus 5.5's safeguards flagged this session. … Opus 4.8 is answering
instead, … Details: `[cyber]`"). The person is told, and with "restore_model": true in their
users.json entry the router switches back to that model's family once the assistant is idle, unless
the model was stopped again within RETRY_AFTER of the last switch back, when the conversation itself
keeps setting it off. Fallbacks logged before the router started are never seen: hooks only get new
entries."""
import re
from dataclasses import dataclass

from multiclaude import telegram
from multiclaude.log import log
from multiclaude.router import switching, work

NAME = "model-fallback"
FIELD = "restore_model"  # in a person's users.json entry: true to switch back by itself
SUBTYPE = "model_refusal_fallback"
REPLACEMENT_RE = re.compile(r"(?P<model>[A-Z]\w* \d+(?:\.\d+)*) is answering instead")
DETAILS_RE = re.compile(r"Details:\s*`?(?P<details>[^`\n]+)")
RETRY_AFTER = 30 * 60


@dataclass
class Fallback:
    """Claude Code moved the session to another model because a safety classifier stopped a reply."""
    stopped: str      # the model whose reply was stopped, e.g. claude-opus-5-5 ("" if not known)
    replacement: str  # the model answering instead, as Claude Code names it ("Opus 4.8"), "" if not given
    details: str      # the classifier's category, e.g. "[cyber]", "" if not given


def on_transcript_entry(user, entry, now):
    """The turn hasn't seen the entry yet, so user.turn.model is still the model whose reply was stopped."""
    if entry.get("type") != "system" or entry.get("subtype") != SUBTYPE:
        return
    fallback = read(entry, user.turn.model)
    log(f"{user.name}: {fallback.stopped}'s reply was stopped; Claude Code fell back to "
        f"{fallback.replacement or 'another model'} {fallback.details}".rstrip())
    telegram.later(user.say, handle(user, fallback, now))


def panel_fields():
    return {FIELD: valid_switch}


def valid_switch(value):
    if not isinstance(value, bool):
        raise ValueError("That one is on or off.")
    return value


def read(entry, stopped):
    content = str(entry.get("content") or "")
    replacement, details = REPLACEMENT_RE.search(content), DETAILS_RE.search(content)
    return Fallback(stopped or "", replacement["model"] if replacement else "",
                    details["details"].strip() if details else "")


def handle(user, fallback, now):
    """Queue the switch back if the person wants it and it's due. Returns what to tell them."""
    target = family(fallback.stopped)
    moved = moved_text(fallback)
    if not (user.entry.get(FIELD) is True and target):
        return f"{moved} Send `/model {target or 'opus'}` to switch back."
    state = user.addon_state.setdefault(NAME, {})
    if now - state.get("restored_at", 0.0) < RETRY_AFTER:
        return (f"{moved} It happened again within {RETRY_AFTER // 60} minutes of switching back, so I'm staying "
                f"on {fallback.replacement or 'it'} for now: this conversation itself keeps setting it off. "
                f"`/clear` starts a new one, and `/model {target}` switches back by hand.")
    state["restored_at"] = now
    user.enqueue(work.Switch(now, "model", target))
    return f"{moved} Switching back to {name(fallback.stopped)} once I'm done with what I'm doing."


def moved_text(fallback):
    details = f" (`{fallback.details}`)" if fallback.details else ""
    return (f"⚠️ {name(fallback.stopped)}'s safety filter stopped one of my replies{details}, so Claude Code "
            f"moved this conversation to {fallback.replacement or 'another model'}.")


def family(model):
    """The /model picker's name for a model id (claude-opus-5-5 → opus), or None if it has none."""
    first = model.removeprefix("claude-").split("-")[0]
    return first if model.startswith("claude-") and first in switching.MODELS else None


def name(model):
    """A model id as people say it: claude-opus-5-5 → Opus 5.5."""
    if not model.startswith("claude-"):
        return "My usual model"
    word, *rest = model.removeprefix("claude-").split("-")
    version = ".".join(part for part in rest if len(part) <= 2)  # not a date suffix
    return f"{word.title()} {version}".strip()
