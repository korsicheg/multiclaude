"""The router's loop: each message goes to its person's assistant or is one of the bot's commands,
and every session is tended. It never waits on a terminal or the network: each session's queue
has its own thread (work.Worker), and replies go out in the background. It only sends to the Bot
API, so it never competes with the Telegram plugin for getUpdates."""
import os
import threading
import time
import traceback

from multiclaude import addons, bot, jsonfile, paths, telegram
from multiclaude import users as registry
from multiclaude.channels import TelegramChat
from multiclaude.log import log
from multiclaude.router import (about, asks, limits, mcp_login, panel, plugins, poller, recovery, relay_entrance, switching,
                                usage, work)
from multiclaude.router import inbound as messages
from multiclaude.router.session import Session
from multiclaude.router.upgrade import Upgrader

POLL = 0.5
TYPING_EVERY = 4.0        # Telegram shows "typing" for ~5s per call
AFTER_REPLY_GRACE = 5.0   # a sent message hides typing; don't show it again right after the final answer
DESK_PANE = "assistant"
DESK_RESET_EVERY = 40     # /clear the front desk after this many routed messages
DESK_RESET_RETRY = 10 * 60  # after a front desk /clear that didn't work, wait this long before the next try
# How far the router has read the front desk's transcripts, so messages that arrive while it
# restarts (a deploy, a crash) are still routed afterwards.
DESK_OFFSETS = paths.STATE / "router-offsets.json"
OFFSETS_HEARTBEAT = 60    # saved at least this often, so a restart can tell how long the router was down
RESUME_WITHIN = 60 * 60   # after a longer outage, messages from the gap are skipped rather than delivered late
SEEN_KEPT = 200           # recently routed messages remembered, to route each one once
SEND_HELD_EVERY = 30      # how often to look for held messages (telegram.OUTBOX) while the network is up


def survive(what, fn, *args):
    """Run fn(*args); log an exception with its traceback instead of raising it, so one odd
    message or one person's session never holds up the others."""
    try:
        fn(*args)
    except Exception:
        log(f"{what} failed:\n{traceback.format_exc().rstrip()}")


def in_background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def allowed_chats():
    access = jsonfile.read_json(paths.TELEGRAM_DIR / "access.json", {})
    return [str(x) for x in (access.get("allowFrom", []) if isinstance(access, dict) else [])]


class Desk:
    def __init__(self):
        self.users = {}                  # name -> their assistant's Session
        self.by_chat = {}                # Telegram id -> name
        self.relay = None                # the relay entrance (the control panel, add-ins' apps), when there is a relay
        self.users_mtime = None
        self.owner = None                # the owner's name: told about updates and outages
        self.seen = []                   # recently routed message keys, oldest first
        self.routed = 0                  # messages routed since the front desk was last reset
        self.next_desk_reset = 0.0       # no front desk /clear before this time
        self.offsets_saved = ({}, 0.0)   # the front desk offsets last written to DESK_OFFSETS, and when
        self.desk = Session("front desk", DESK_PANE, paths.project_dir(paths.ROOT))
        self.workers = {}                # session name -> the Worker for its queue
        self.limits = limits.LimitAlerts()
        self.upgrader = Upgrader()
        self.poller = poller.PollerWatch(self.desk)
        self.network = recovery.Network(recovery.targets())
        self.next_send_held = 0.0        # no look for held messages before this time
        self.panel = panel.Panel(self, lambda fn, *args: self.background("the control panel", fn, *args))

    def start(self):
        self.network.start()
        self.start_worker(self.desk)
        self.load_users()
        self.resume_desk()
        self.open_relay()
        self.describe_bot()
        panel.set_menu_button()
        for chat in allowed_chats():
            if not chat.lstrip("-").isdigit():
                log(f"allowFrom has {chat!r}, which isn't a chat id; no command menu for it")
                continue
            telegram.api("setMyCommands", commands=about.COMMANDS, scope={"type": "chat", "chat_id": int(chat)})
        log(f"routing for {', '.join(self.users) or 'nobody yet'}")

    def describe_bot(self):
        """The "What can this bot do?" box of an empty chat, and the line on the bot's profile."""
        owner = self.users.get(self.owner)
        display = owner.display if owner else None
        telegram.api("setMyDescription", description=about.description(display))
        telegram.api("setMyShortDescription", short_description=about.short_description(display))

    def load_users(self):
        """Follow users.json: a session for each person, by their Telegram id."""
        try:
            mtime = os.path.getmtime(paths.USERS)
            if mtime == self.users_mtime:
                return
            found = registry.load_users()
        except (OSError, ValueError) as e:
            log(f"can't read {paths.USERS}: {e}")
            return
        self.users_mtime = mtime
        self.by_chat = {u.chat_id: name for name, u in found.items()}
        for name, u in found.items():
            if name not in self.users or self.users[name].chat_id != u.chat_id:
                self.users[name] = Session(name, u.pane, paths.project_dir(u.workspace), u.chat_id, u.display)
                self.start_worker(self.users[name])
                log(f"user {name} (Telegram {u.chat_id}): {self.users[name].tail.dir}")
            session = self.users[name]
            session.display, session.entry = u.display, u.entry
        for name in set(self.users) - set(found):
            self.workers.pop(name).stop()
            del self.users[name]
        owner = registry.owner(found)
        self.owner = owner.name if owner else None

    def start_worker(self, session):
        if session.name in self.workers:
            self.workers[session.name].stop()
        self.workers[session.name] = work.Worker(session)
        self.workers[session.name].start()

    def displays(self):
        return {name: s.display for name, s in self.users.items()}

    @staticmethod
    def background(what, fn, *args):
        """Run fn(*args) on a thread of its own; a failure is logged as what failed."""
        in_background(survive, what, fn, *args)

    # --- the loop ---

    def poll(self):
        now = time.time()
        self.load_users()
        if not self.network.online():
            self.postpone_stuck(now)
        for entry in self.desk.read(now):
            survive("routing a message", self.inbound, entry)
        for envelope in self.relay.waiting() if self.relay else []:
            survive("routing from the relay", self.relay_inbound, envelope)
        survive("taking reload requests", plugins.take_reload_requests, self.users, now)
        self.save_desk_offsets(now)
        for user in list(self.users.values()):
            survive(f"{user.name}'s assistant", self.tend, user, now)
        for outage in self.network.take_ended():
            in_background(survive, "getting going after the outage", self.after_outage, outage)
        self.send_held(now)
        self.panel.tick(now)
        self.reset_desk(now)
        self.upgrader.tick(now, [self.desk, *self.users.values()], self.users.get(self.owner))
        self.poller.tick(now, self.users.get(self.owner), self.background)

    def tend(self, user, now):
        """One poll's work for one person: follow their assistant (add-ins see each new transcript
        entry first), pass on its problems, have it pick up a request it dropped, and keep "typing…"
        showing while it works. Their worker meanwhile works through their queue."""
        user.read(now, lambda entry: addons.call("on_transcript_entry", user, entry, now))
        user.check_restart(now)
        if user.turn.notice:
            telegram.later(user.say, user.turn.notice)
            user.turn.notice = None
        if failure := user.turn.failed:
            user.turn.failed = None
            user.pickups.plan(failure, now)
            telegram.later(user.say, user.pickups.notice(failure))
        if user.pickups.due(now) and self.network.online() and user.idle():
            self.pick_up(user, now)
        if self.network.online():
            show_typing(user, now)

    def pick_up(self, user, now):
        """Have the assistant carry on with the request its last turn dropped (recovery.Pickups)."""
        log(f"{user.name}: having it pick up the request its last turn dropped")
        resume = user.pickups.take(now)
        user.queue_message(work.Deliver(now, resume.note(user.display, self.network.last_outage)), now)

    def reset_desk(self, now):
        """The front desk only needs to see messages arrive; keep its context small."""
        if self.routed < DESK_RESET_EVERY or self.desk.queue or self.desk.turn.busy or now < self.next_desk_reset:
            return
        log("resetting the front desk context")
        self.desk.enqueue(work.ResetContext(now, done=self.desk_reset_done))

    def desk_reset_done(self, ok):
        if ok:
            self.routed = 0
        else:
            self.next_desk_reset = time.time() + DESK_RESET_RETRY

    # --- network outages ---

    def postpone_stuck(self, now):
        """While the VM is offline, no turn is judged stuck (turn.Turn.postpone_stuck)."""
        for session in [self.desk, *self.users.values()]:
            session.turn.postpone_stuck(now)

    def after_outage(self, outage):
        """The network is back: send what was held meanwhile, then tell the owner about a long outage."""
        telegram.flush_held()
        owner = self.users.get(self.owner)
        if owner and outage.duration >= recovery.REPORT_AFTER:
            telegram.send(owner.chat_id, outage.report(owner.chat_id), disable_notification=True)

    def send_held(self, now):
        """Now and then, send messages still held: one tg held while the outage was too short for
        the router to notice, or one held before the router restarted."""
        if now < self.next_send_held or not self.network.online():
            return
        self.next_send_held = now + SEND_HELD_EVERY
        if telegram.Outbox.chats():
            in_background(survive, "sending held messages", telegram.flush_held)

    # --- messages ---

    def first_time(self, msg):
        """Whether msg is new: the front desk's transcripts and the relay may bring a message twice."""
        if msg.key in self.seen:
            return False
        self.seen = (self.seen + [msg.key])[-SEEN_KEPT:]
        return True

    def inbound(self, entry):
        msg = messages.parse(entry)
        if not msg or not self.first_time(msg):
            return
        self.routed += 1
        name = self.by_chat.get(str(msg.sender))
        if not name:
            log(f"message from unregistered user {msg.sender} ({msg.attrs.get('user')}); not routed")
            text = (f"You can reach {bot.name()}, but it isn't set up for you yet. "
                    f"Ask the VM owner to run: `assistant add-user <name> {msg.sender}`")
            telegram.later(telegram.api, "sendRichMessage", chat_id=msg.chat,
                           rich_message={"blocks": telegram.rich_blocks(text)})
            return
        user, now = self.users[name], time.time()
        user.reply_to = TelegramChat(user.chat_id)
        if msg.tap:
            asks.answer(user, msg, now)
            return
        if str(msg.msg_id).isdigit():
            telegram.ChatHistory(msg.chat).remember(msg.msg_id)
        self.dispatch(user, msg, now)

    def dispatch(self, user, msg, now):
        text = messages.in_latin_layout(msg.text.strip())
        waiting = user.pending_login  # read once: the login thread clears it when it ends
        redirect = messages.REDIRECT_RE.search(text) if waiting else None
        if redirect:
            log(f"{user.name}: redirect URL for /mcp login {waiting['server']}")
            waiting["inbox"].put(redirect[0])
        elif login := messages.MCP_LOGIN_RE.fullmatch(text):
            self.mcp_login(user, login[1], waiting)
        elif messages.CLEAR_RE.fullmatch(text):
            log(f"{user.name}: /clear requested")
            user.enqueue(work.Clear(now))
        elif messages.STOP_RE.fullmatch(text):
            self.stop(user)
        elif messages.RESTART_RE.fullmatch(text):
            self.restart(user)
        elif messages.USAGE_RE.fullmatch(text):
            in_background(self.send_usage, user)  # it may fetch the plan's limits first
        elif messages.HELP_RE.fullmatch(text):
            self.help(user, now)
        elif messages.CONFIG_RE.fullmatch(text):
            log(f"{user.name}: /config requested")
            self.panel.offer(user)
        elif messages.PLUGIN_RE.fullmatch(text):
            commands = messages.plugin_commands(text)
            log(f"{user.name}: {', '.join(c.split()[0] for c in commands)} requested")
            in_background(plugins.run_all, user, commands)
        elif reload := messages.RELOAD_RE.fullmatch(text):
            log(f"{user.name}: /reload-plugins{reload[1] or ''} requested")
            plugins.queue_reload(user, now, force=bool(reload[1]))
        elif switch := messages.parse_switch(text):
            self.switch(user, *switch, now)
        else:
            self.deliver(user, msg, now)

    def stop(self, user):
        """/stop: its worker presses Escape (work.stop), and a request dropped on an API error
        isn't picked up again."""
        user.pickups.cancel()
        if not user.turn.busy:
            telegram.later(user.say, f"{bot.name()} isn't working on anything, so there's nothing to stop.")
            return
        log(f"{user.name}: /stop requested")
        user.stop_requested = True
        user.wake.set()

    def help(self, user, now):
        """/help: what they can send and the commands at once, then their assistant adds what it can
        do with their MCP servers (about.mcp_note)."""
        log(f"{user.name}: /help requested")
        telegram.later(user.say, about.help_text(user))
        user.queue_message(work.Deliver(now, about.mcp_note(user, now)), now)

    def restart(self, user):
        """/restart: its worker starts the session over at once, without waiting for the turn
        (work.restart_fresh). The note goes with the rest of the chat once it's back."""
        log(f"{user.name}: /restart requested")
        user.pickups.cancel()
        telegram.later(user.say, f"🔄 Restarting {bot.name()} with a new conversation…")
        user.restart_requested = True
        user.wake.set()

    def send_usage(self, user):
        user.say(usage.usage_text(user, self.displays()))

    def mcp_login(self, user, server, waiting):
        if not server:
            telegram.later(user.say, "Usage: `/mcp login <name>`. `/mcp` lists your servers.")
        elif server.lower() == "cancel":
            if waiting:
                waiting["inbox"].put(None)
            else:
                telegram.later(user.say, "No sign-in is waiting.")
        else:
            log(f"{user.name}: /mcp login {server} requested")
            in_background(mcp_login.McpLogin(user, server).run)

    def switch(self, user, setting, target, keep, now):
        if not target:
            telegram.later(user.say, switching.current(user, setting))
            return
        log(f"{user.name}: /{setting} {target}{' default' if keep else ''} requested")
        if not user.idle():
            telegram.later(user.say, f"⏳ I'll switch your {setting} to {target} once I finish what I'm doing.")
        user.enqueue(work.Switch(now, setting, target, keep))

    def deliver(self, user, msg, now):
        """Queue a message for the assistant, unless an add-in queues it its own way (on_message)."""
        user.pickups.cancel()  # with a new message the assistant carries on anyway, its last turn in view
        if not any(addons.call("on_message", user, msg, now)):
            user.queue_message(work.Deliver(now, messages.header(user.display, msg), messages.body(msg), msg.msg_id), now)
        log(f"{user.name}: message {msg.msg_id} queued for delivery")
        # In the background, and so that a failing check can never cost the message itself.
        in_background(survive, f"{user.name}'s plan limit check", self.limits.warn, user)

    # --- the relay: the control panel, and add-ins' apps ---

    def open_relay(self):
        found = relay_entrance.settings()
        if not found:
            return
        self.relay = relay_entrance.RelayEntrance(found["relay_url"], found["relay_token"])
        self.relay.start()
        log("relay: listening")

    def relay_inbound(self, envelope):
        """A tap in the control panel, or an envelope of a type an add-in takes (relay_envelopes)."""
        kind, payload = envelope.get("type"), envelope.get("payload") or {}
        if kind == "panel":
            self.panel.handle(payload)
        elif handler := relay_handlers().get(kind):
            handler(self, payload)
        else:
            log(f"relay: a {kind!r} envelope, which nothing that's on takes; dropped")

    # --- surviving restarts ---

    def resume_desk(self):
        """Route the messages that reached the front desk while the router was down, reading its
        transcripts from where the last run stopped (a transcript started meanwhile, by a /clear,
        from the top). Only routed: starting up already replayed the newest transcript for the
        front desk's busy state, so they mustn't count twice."""
        saved = jsonfile.read_json(DESK_OFFSETS, {})
        try:
            offsets, saved_at = dict(saved["offsets"]), float(saved["saved"])
        except (KeyError, TypeError, ValueError):
            return
        if time.time() - saved_at > RESUME_WITHIN:
            log("the router was down too long; messages sent meanwhile are skipped")
            return
        tail = self.desk.tail
        for path in tail.files():
            if path in offsets and offsets[path] <= os.path.getsize(path):
                tail.offsets[path] = offsets[path]
            elif path not in offsets and os.path.getmtime(path) > saved_at:
                tail.offsets[path] = 0
            else:
                continue
            for entry in tail.read(path):
                survive("routing a message from while the router was down", self.inbound, entry)

    def save_desk_offsets(self, now):
        offsets, saved_at = self.offsets_saved
        if self.desk.tail.offsets == offsets and now - saved_at < OFFSETS_HEARTBEAT:
            return
        try:
            jsonfile.write_json(DESK_OFFSETS, {"saved": now, "offsets": self.desk.tail.offsets})
        except OSError as e:
            log(f"can't save {DESK_OFFSETS}: {e}")
            return
        self.offsets_saved = (dict(self.desk.tail.offsets), now)


def relay_handlers():
    """{envelope type: handler(desk, payload)} of the add-ins that are on."""
    return {kind: handler for handlers in addons.call("relay_envelopes") for kind, handler in (handlers or {}).items()}


def show_typing(user, now):
    """Keep "typing…" showing in Telegram while the assistant works, though not right after its
    answer went out (a sent message hides it). In the background, so a slow Bot API call never
    holds up routing."""
    if (isinstance(user.reply_to, TelegramChat) and not user.idle()
            and now - user.last_typing >= TYPING_EVERY and now - user.turn.last_reply >= AFTER_REPLY_GRACE
            and not (user.typing and user.typing.is_alive())):
        user.typing = threading.Thread(target=telegram.api, args=("sendChatAction",), daemon=True,
                                       kwargs={"chat_id": user.chat_id, "action": "typing"})
        user.typing.start()
        user.last_typing = now


def main():
    desk = Desk()
    desk.start()
    while True:
        survive("poll", desk.poll)  # keep routing alive through odd transcript states
        time.sleep(POLL)
