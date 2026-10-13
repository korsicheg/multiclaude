"""Add-ins: features that are in the repo but not in the core, each on only while the owner lists it
in config.json, {"addons": ["voice-notes", "quiet-hours"]}. The core works fully with all of them off.

An add-in's code is the module or package named after it in this package (voice-notes:
voice_notes.py, slack: slack/); its README, its rules for the assistants and its scripts are in the
repo's addons/<name>/. An add-in may have no code at all. The core never imports an add-in: it asks enabled(name), or call()s a hook,
which runs that function of every add-in that is on and defines it. The hooks:

- on_transcript_entry(user, entry, now): a new entry in a person's transcript, before the router
  follows the turn with it, so user.turn still shows the state before it (router/desk.py).
- before_telegram_send(chat_id, method, params) -> params: a message about to be posted, by any
  sender: the router, tg, jobs (telegram.BotApi.call, through through()).
- on_message(user, msg, now) -> bool: a message for a person's assistant; True when the add-in
  queued it itself, so the core doesn't (router/desk.py).
- attachment_lines(attrs) -> [str]: lines for a message's Attachments (router/inbound.py).
- help_info(user, app) -> {"can_send": [...], "ways": [...]}: what /help and the bot's description
  say about it: what the person can send in app (a channels.Chat.app, "Telegram" or an add-in's),
  and the other ways they can reach the bot. user is None for the bot's description, whose "ways"
  are every way in (router/about.py).
- panel_fields() -> {field: validate}: fields of a person's users.json entry the control panel shows
  and sets; validate(value) gives the value to save, or ValueError saying why not (router/panel.py).
- relay_envelopes() -> {type: handler(desk, payload)}: the relay envelopes the add-in takes in, by
  type, such as the messages of its own app (router/desk.py, Desk.relay_inbound).
- after_wipe(user, before, keep): /clear or /restart emptied the person's Telegram chat; empty the
  add-in's own chats too, up to before (a Unix time), but for the message ids in keep, still on
  their way to the assistant (router/work.py).
- tg_commands() -> {name: fn(tg, args)}: commands the add-in adds to `tg` (bin/tg). fn's docstring
  is its usage line; ValueError says what's wrong with args.

Add-ins may offer hooks to each other the same way: voice-notes asks voice_note_sources() for the
apps other than Telegram that send voice notes.

A hook that fails is logged and skipped: an add-in never breaks the core."""
import functools
import importlib
import os
import traceback

from multiclaude import config, paths
from multiclaude.log import log

ALL = ("slack", "siri", "voice-notes", "locations", "quiet-hours", "model-fallback", "skill-notes", "backup")


class _Listing:
    """The add-ins config.json turns on. They're asked for on every message sent and every
    transcript entry, so the file is read again only when it changes."""

    def __init__(self):
        self.seen = (None, ())  # (the file's stamp, the names): one tuple, so a thread sees both or neither

    def names(self):
        stamp = file_stamp(paths.CONFIG)
        seen_stamp, names = self.seen
        if stamp != seen_stamp:
            listed = config.read().get("addons")
            names = tuple(name for name in ALL if isinstance(listed, list) and name in listed)
            self.seen = (stamp, names)
        return names


_listing = _Listing()


def file_stamp(path):
    """What changes whenever the file does, or None if there's no file."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def names():
    """The add-ins that are on, in ALL's order. Names config.json lists that aren't add-ins are ignored."""
    return _listing.names()


def enabled(name):
    return name in names()


def folder(name):
    """An add-in's own folder in the repo: its README, its rules for the assistants and its scripts."""
    return paths.ROOT / "addons" / name


def active():
    """The modules of the add-ins that are on and have code."""
    return [module for name in names() if (module := _module(name))]


@functools.cache
def _module(name):
    """The add-in's module, or None when it has no code (or its code doesn't load, which is logged once)."""
    module_name = f"{__name__}.{name.replace('-', '_')}"
    try:
        return importlib.import_module(module_name)
    except Exception as e:
        if not (isinstance(e, ModuleNotFoundError) and e.name == module_name):
            log(f"add-in {name} can't load:\n{traceback.format_exc().rstrip()}")
        return None


def _hooks(hook):
    """(module, function) for each active add-in that defines hook."""
    return [(module, fn) for module in active() if (fn := getattr(module, hook, None))]


def _failed(module, hook):
    log(f"add-in {module.__name__.rsplit('.', 1)[-1]}: {hook} failed:\n{traceback.format_exc().rstrip()}")


def call(hook, *args):
    """hook(*args) of every add-in that's on and has it: their results, in ALL's order."""
    results = []
    for module, fn in _hooks(hook):
        try:
            results.append(fn(*args))
        except Exception:
            _failed(module, hook)
    return results


def through(hook, value, *args):
    """value passed through hook(*args, value) of every add-in that's on and has it, each getting what
    the one before returned. One that fails, or returns None, leaves it as it was."""
    for module, fn in _hooks(hook):
        try:
            changed = fn(*args, value)
        except Exception:
            _failed(module, hook)
            continue
        if changed is not None:
            value = changed
    return value
