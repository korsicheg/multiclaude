"""A person's queue: what the router still has to do in their assistant's terminal, in order.
Messages are typed even while the assistant works (Claude Code queues them); everything else
waits until it's idle."""
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field

from multiclaude import addons, bot, telegram
from multiclaude.channels import TelegramChat
from multiclaude.log import log
from multiclaude.router import approvals, switching
from multiclaude.router.session import DialogOpen

DELIVER_TIMEOUT = 120  # how long work waits for a (re)starting assistant
POLL = 0.5
DELETE_BATCH = 100     # deleteMessages takes up to 100 ids
CLEAR_WAITS = ("🧹 I'll clear once I finish what I'm doing. Tap **Cancel** to keep everything, "
               "or send /stop to stop me and clear now.")
CANCEL = "Cancel"
CANCEL_CLEAR = "cancel-clear"  # the kind of question CLEAR_WAITS is (router/asks.py)
CLEARED = "🧹 Context cleared. Starting fresh."
RESTARTED = "🔄 Restarted with a new conversation. Starting fresh."
FORCE_RELOAD = "/reload-plugins --force"  # what Claude Code asks for when a reload changes MCP tools


@dataclass
class Work:
    since: float  # when it was queued, or last held back by a turn
    waits_for_idle = True
    needs_assistant = True

    def hold(self, user, now):
        """The assistant is busy; its delivery clock starts once the turn is over."""
        self.since = now

    def run(self, user, now):
        """Do it, leaving the queue when done; False to stop serving the queue for now."""
        raise NotImplementedError


@dataclass
class Deliver(Work):
    header: str = ""
    body: str = ""
    msg_id: str = ""
    waits_for_idle = False

    def run(self, user, now):
        try:
            user.type_message(self.header, self.body)
            log(f"{user.name}: delivered {self.header}")
        except DialogOpen:
            return False  # it waits; the worker relays the dialog
        except (subprocess.SubprocessError, OSError) as e:
            log(f"{user.name}: delivery failed: {e}")
            if now - self.since > DELIVER_TIMEOUT:
                user.queue.pop(0)
                user.turn.busy = False
                user.say(f"⚠️ Your message couldn't be typed into {bot.name()}'s terminal, so it "
                         "didn't get it. Send it again in a minute; if it keeps happening, "
                         "run `assistant status` on the VM.")
            return False
        user.queue.pop(0)
        user.turn.expect(now)
        return True


@dataclass
class Clear(Work):
    """/clear. While the assistant works it waits, and the person can cancel it meanwhile."""
    notice: object = None  # the id of the message offering to cancel it, once sent
    offered: bool = field(default=False, init=False)
    started: bool = field(default=False, init=False)
    cancelled: bool = field(default=False, init=False)
    decided: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    needs_assistant = False

    @property
    def waits_for_idle(self):
        return not self.cancelled  # a cancelled one only leaves the queue, holding up nothing behind it

    def hold(self, user, now):
        """Say once that it waits, with a Cancel button."""
        if not self.offered and user.reply_to:
            self.offered = True
            self.notice = user.reply_to.ask(CLEAR_WAITS, [CANCEL], CANCEL_CLEAR)

    def cancel(self):
        """Drop it unless it has started. Whether that was in time."""
        with self.decided:
            self.cancelled = not self.started
            return self.cancelled

    def start(self):
        """Start it unless it was cancelled. Whether it may go ahead."""
        with self.decided:
            self.started = not self.cancelled
            return self.started

    def run(self, user, now):
        if not self.start():
            user.queue.pop(0)
            log(f"{user.name}: dropped the cancelled /clear")
            return True
        if user.ready():
            # Plugins updated since the session started load first; forced, as /clear drops the prompt cache anyway.
            run_in_session(user, FORCE_RELOAD)
        ok = user.clear()
        user.queue.pop(0)
        if ok:
            wipe_chats(user, CLEARED)
        else:
            user.say("⚠️ I sent /clear but couldn't confirm it worked. Run `assistant status` on the VM to check.")
        return True


def cancel_clear(user, notice):
    """A tap on the Cancel under the waiting /clear whose notice is that message. Whether it was in time."""
    waiting = next((w for w in list(user.queue) if isinstance(w, Clear) and str(w.notice) == str(notice)), None)
    if not (waiting and waiting.cancel()):
        return False
    user.wake.set()  # its worker drops it from the queue, letting through what waited behind it
    return True


@dataclass
class Switch(Work):
    setting: str = ""
    target: str = ""
    keep: bool = False   # also save it as the person's default
    quiet: bool = False  # putting a session-only switch back after an update restart

    def run(self, user, now):
        switching.switch(user, self.setting, self.target, self.keep, self.quiet)
        user.queue.pop(0)
        return True


@dataclass
class Reload(Work):
    """/reload-plugins after a plugin or skill change, or when the person sends it. Claude Code holds back
    a reload that changes MCP tools, since it costs the prompt cache, until it comes with --force."""
    force: bool = False

    def run(self, user, now):
        user.queue.pop(0)  # first, so a change made meanwhile queues another reload
        result = run_in_session(user, FORCE_RELOAD if self.force else "/reload-plugins")
        held = bool(result) and FORCE_RELOAD in result
        user.say(f"🔌 {result}" if result else
                 f"⚠️ {bot.name()} didn't confirm that it reloaded its plugins. "
                 "The change applies when it next restarts.", copy=[FORCE_RELOAD] if held else ())
        return True


@dataclass
class SessionCommands(Work):
    """In-session commands for /mcp, such as `/mcp reconnect x`."""
    commands: list = field(default_factory=list)

    def run(self, user, now):
        user.queue.pop(0)
        results, sign_ins = [], []
        for command in self.commands:
            result = run_in_session(user, command)
            if result and len(command.split()) > 2 and "Use /mcp to authenticate." in result:
                sign_ins.append(f"/mcp login {command.split()[2]}")  # instead of Claude Code's own /mcp screen
                result = result.replace("Use /mcp to authenticate.", f"Sign in: `{sign_ins[-1]}`")
            if command != "/reload-plugins" or not result:
                results.append(result or f"⚠️ {command}: no answer from {bot.name()}; "
                                         "the change applies when it next restarts.")
        user.say("🔌 " + "\n".join(results), copy=sign_ins)
        return True


@dataclass
class Restart(Work):
    """Restart claude onto a new Claude Code version, then put its session-only switches back.
    Checked again first: the session may have got busy since upgrade.py found it idle."""
    pid: int = 0
    needs_assistant = False

    def run(self, session, now):
        session.queue.pop(0)
        proc = session.claude_proc()
        if not proc or proc[0] != self.pid or not session.can_upgrade(now, proc):
            log(f"{session.name}: busy again; the update restart waits for the next check")
            session.upgrade_tried = None
            return True
        session.restart(self.pid)
        put_switches_back(session, now)
        return True


@dataclass
class ResetContext(Work):
    """/clear for the front desk, which only needs to see messages arrive; done(ok) hears how it went."""
    done: object = None
    needs_assistant = False

    def hold(self, session, now):
        pass

    def run(self, session, now):
        ok = session.clear()
        session.queue.pop(0)
        self.done(ok)
        return True


def run_in_session(user, command):
    try:
        result = user.run_command(command)
    except (subprocess.SubprocessError, OSError) as e:
        result = None
        log(f"{user.name}: {command} keystrokes failed: {e}")
    log(f"{user.name}: {command}: {result}")
    return result


def serve(user, now):
    """Work through the person's queue in order, as far as their assistant allows."""
    while user.queue:
        work = user.queue[0]
        if work.waits_for_idle and user.turn.busy:
            work.hold(user, now)
            return
        if work.needs_assistant and not user.ready():
            if now - work.since > DELIVER_TIMEOUT:
                log(f"{user.name}: assistant not ready; dropping {type(work).__name__}")
                user.say(f"⚠️ {bot.name()} isn't running right now, so that didn't reach it. "
                         "Try again in a minute; if it keeps happening, run `assistant status` on the VM.")
                user.queue.pop(0)
                user.turn.busy = False  # nothing is working on it; don't also report it as stuck later
                continue
            user.ensure_started(now)
            return
        if not work.run(user, now):
            return


def stop(session):
    """/stop: Escape in the assistant's terminal. Messages sent meanwhile still come next, as they
    do after Escape at the terminal."""
    if session.stop():
        session.say("⏹️ Stopped.")
    elif not session.turn.busy:
        session.say("It had just finished, so there was nothing to stop.")
    else:
        session.say(f"⚠️ Escape was pressed, but {bot.name()} didn't show that it stopped. "
                    "Run `assistant status` on the VM to check.")


def restart_fresh(session, now):
    """/restart: start the session over with a new conversation, put its session-only switches
    back and empty the chats, as after /clear. A waiting /clear or update restart goes: the new
    conversation and the newest Claude Code make them pointless."""
    for superseded in [w for w in session.queue if isinstance(w, (Clear, Restart))]:
        session.queue.remove(superseded)
    if not session.restart_fresh():
        session.say(f"⚠️ I ended {bot.name()}'s session, but it hasn't come back yet. "
                    "Run `assistant status` on the VM to check.")
        return
    put_switches_back(session, now)
    wipe_chats(session, RESTARTED)


def put_switches_back(session, now):
    """A restart drops the session-only /model and /effort switches; type them in again."""
    for setting, target in session.session_switches.items():
        session.enqueue(Switch(now, setting, target, keep=False, quiet=True))


def wipe_chats(user, note):
    """Empty the person's chats with the bot, Telegram's and those of add-ins' apps (after_wipe),
    since all of them talk to the context /clear (or /restart) just reset, leaving only note.
    The note goes out before anything is deleted: a Telegram chat with a bot that stands empty,
    even for a moment, shows a big Start button, as if they had never talked. Messages sent
    after the /clear, still on their way to the fresh context, stay."""
    waiting = {str(w.msg_id) for w in user.queue if isinstance(w, Deliver)}
    keep = {int(m) for m in waiting if m.isdigit()}
    before, recorded = time.time(), telegram.ChatHistory(user.chat_id).take_deletable(keep)
    post_wipe_note(user, note)
    delete_history(user.chat_id, recorded, keep)
    addons.call("after_wipe", user, before, waiting)


def post_wipe_note(user, note):
    """Say note in the chat the person wrote from, and quietly in their Telegram chat too when
    that was another app's: the Telegram chat is emptied as well, and must not stand empty."""
    user.say(user.reply_to.wipe_note(note) if user.reply_to else note)
    if user.chat_id and not isinstance(user.reply_to, TelegramChat):
        TelegramChat(user.chat_id).send(note, quiet=True)


def delete_history(chat, recorded, keep):
    """Delete a chat's recorded messages in the background, and sweep the ids between them that
    were never recorded, except the ids in keep."""
    if recorded:
        unrecorded = unrecorded_between(recorded, keep)
        threading.Thread(target=lambda: (delete_messages(chat, recorded), sweep(chat, unrecorded)),
                         daemon=True).start()


def unrecorded_between(recorded, keep):
    """The ids from the oldest recorded message to the newest that are neither recorded nor
    kept: messages the plugin doesn't pass on, such as a contact or a poll, so the router never
    saw them."""
    known = set(recorded) | set(keep)
    return [m for m in range(min(recorded), max(recorded)) if m not in known]


def delete_messages(chat, ids):
    failed = 0
    for batch in batches(ids):
        if telegram.api("deleteMessages", chat_id=chat, message_ids=batch) is None:
            # Retry one by one, so one message that can't go doesn't keep the rest.
            failed += sum(telegram.api("deleteMessage", chat_id=chat, message_id=m) is None for m in batch)
    log(f"chat {chat}: deleted {len(ids) - failed} of {len(ids)} messages")


def sweep(chat, ids):
    """Delete whatever of ids is in the chat. deleteMessages skips the ids it can't find there,
    which are mostly the messages of the bot's other chats, since they share one id sequence."""
    for batch in batches(ids):
        telegram.api("deleteMessages", chat_id=chat, message_ids=batch)
    if ids:
        log(f"chat {chat}: swept {len(ids)} unrecorded ids")


def batches(ids):
    return [ids[i:i + DELETE_BATCH] for i in range(0, len(ids), DELETE_BATCH)]


class Worker:
    """Works through one session's queue in its own thread. A slow /clear, /model or restart in
    one terminal then never holds up routing or anyone else, and each terminal gets its
    keystrokes from this one thread only."""

    def __init__(self, session):
        self.session = session
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True, name=f"queue-{session.name}")
        self.watches_approvals = session.chat_id is not None  # the front desk has no one to ask
        self.last_check = 0.0

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.session.wake.set()

    def loop(self):
        while not self.stopped.is_set():
            self.session.wake.wait(POLL)
            self.session.wake.clear()
            if not self.stopped.is_set():
                try:
                    self.step(time.time())
                except Exception:
                    log(f"{self.session.name}'s queue failed:\n{traceback.format_exc().rstrip()}")

    def step(self, now):
        """Act on /stop and /restart, watch for an approval dialog while the assistant works, then
        work through the queue, which waits while a dialog is open."""
        session = self.session
        if session.stop_requested:
            session.stop_requested = False
            stop(session)
        if session.restart_requested:
            session.restart_requested = False
            restart_fresh(session, now)
        if (self.watches_approvals and (session.turn.busy or session.approval)
                and (session.approval and session.approval.answer or now - self.last_check >= approvals.CHECK_EVERY)):
            self.last_check = now
            approvals.check(session)
        if session.queue and not session.approval:
            serve(session, now)
