"""The control panel, /config: a Telegram Mini App, served by the relay, where a person sees their
usage and changes their settings, scheduled jobs and MCP servers with a tap. Taps come in as
"panel" envelopes, checked against Telegram's signature; the router carries them out and puts
each person's snapshot on the relay whenever it changes (fingerprint)."""
import hashlib
import hmac
import json
import re
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from multiclaude import addons, bot, cronjobs, jsonfile, paths, telegram
from multiclaude import users as registry
from multiclaude.channels import TelegramChat
from multiclaude.log import log
from multiclaude.router import claude_ui, relay_entrance, switching, usage, work
from multiclaude.router.inbound import Inbound
from multiclaude.sharing import mcp
from multiclaude.sharing.people import load_person

SIGNED_FOR = 24 * 3600  # Telegram's signature on a panel request is trusted this long
PANELS = paths.STATE / "panel"  # <name>.json: the snapshot last put on the relay for each person, and its fingerprint
WATCH_EVERY = 30        # seconds between looks for changes
USAGE_STEP = 5          # percent: context and plan limits count as changed in steps this big
PUSH_TRIES, PUSH_PAUSE = 3, 5
DISPLAY_MAX = 40
MCP_COMMANDS = ("reconnect", "enable", "disable", "login")
SERVER_NAME_RE = re.compile(r"[\w.:@-]+")
OFFER = "⚙️ **Control panel**: your settings, scheduled jobs, MCP servers and usage."
SWITCH_SCOPES = {False: " for this session", True: ", also after restarts"}
MENU_BUTTON = "Panel"  # in place of the ☰ Menu button; the commands still show when you type /


class Refused(Exception):
    """A request the panel can't carry out; its text says why, for the page to show."""


def signed_sender(init_data, token, now):
    """The Telegram id of whoever opened the panel, from its init data, when Telegram signed that
    with the bot's token less than SIGNED_FOR ago; None otherwise."""
    fields = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    given = fields.pop("hash", "")
    checked = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    if not token or not hmac.compare_digest(given, hmac.new(secret, checked.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        if now - int(fields["auth_date"]) > SIGNED_FOR:
            return None
        return str(json.loads(fields["user"])["id"])
    except (KeyError, ValueError, TypeError):
        return None


def address():
    """The page's address on the relay, or None when there's no relay."""
    return relay_entrance.address("/app/")


def set_menu_button():
    """Have the button by every private chat's message box open the panel."""
    url = address()
    if url:
        telegram.api("setChatMenuButton", menu_button={"type": "web_app", "text": MENU_BUTTON, "web_app": {"url": url}})


# --- what a setting may be set to: the value to save, or ValueError saying why not ---

def valid_display(value):
    name = str(value or "").strip()
    if not 0 < len(name) <= DISPLAY_MAX:
        raise ValueError(f"A name needs 1 to {DISPLAY_MAX} characters.")
    return name


def valid_timezone(value):
    try:
        ZoneInfo(str(value))
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError(f"{value} isn't a timezone.") from None
    return str(value)


SETTINGS = {"display": valid_display, "timezone": valid_timezone}


def addon_settings():
    """The fields of a person's users.json entry that the add-ins that are on let the panel set (panel_fields)."""
    return {field: validate for fields in addons.call("panel_fields") for field, validate in (fields or {}).items()}


# --- what the page shows ---

def person_part(person):
    """Their settings, the add-ins' as they are in their users.json entry (None when not set)."""
    return {"display": person.display, "timezone": person.timezone,
            **{field: person.get(field) for field in addon_settings()}}


def alias(model):
    """The model's name in /model: opus for "Opus 5.5", "opus[1m]" or "claude-opus-5-5"; None for none."""
    return next((m for m in switching.MODELS if m in (model or "").lower()), None)


def claude_part(user, status):
    """The model and effort running now, and what they start with after a restart: their own
    saved ones (saved_*), else the VM's (shared_*), else Claude Code's own default (None)."""
    model, effort = usage.model_and_effort(status)
    saved, shared = switching.workspace_settings(user) or {}, jsonfile.read_json(paths.USER_SETTINGS, {})
    return {"model": model, "effort": effort, "current_model": alias(model),
            "saved_model": alias(saved.get("model")) or saved.get("model"), "saved_effort": saved.get("effortLevel"),
            "shared_model": alias(shared.get("model")) or shared.get("model"), "shared_effort": shared.get("effortLevel"),
            "models": switching.MODELS, "efforts": claude_ui.EFFORTS}


def usage_part(status):
    used, size, percent = usage.context(status)
    limits, _ = usage.plan_limits()
    return {"context": {"used": used, "size": size, "percent": percent} if size else None,
            "cost": (status.get("cost") or {}).get("total_cost_usd"),
            "plan": [{"label": label, "used": used, "resets": resets} for label, used, resets in limits]}


def jobs_part(user, now):
    after = datetime.fromtimestamp(now, timezone.utc)
    rows = []
    for job in cronjobs.jobs(user.name, cronjobs.read_crontab()):
        run = job.next_run(after)
        rows.append({"id": job.id, "title": job.title, "detail": job.detail, "paused": job.paused,
                     "next": run.timestamp() if run else None})
    return rows


def fingerprint(snapshot):
    """What has to change for a snapshot to go to the relay again: anything, but usage only in steps
    of USAGE_STEP percent and whole dollars, and reset times only to the minute."""
    used = snapshot["usage"]
    stepped = {"context": step((used["context"] or {}).get("percent")), "cost": int(used["cost"] or 0),
               "plan": [(p["label"], step(p["used"]), int((p["resets"] or 0) // 60)) for p in used["plan"]]}
    return hashlib.sha256(json.dumps({**snapshot, "usage": stepped}, sort_keys=True).encode()).hexdigest()


def step(percent):
    return None if percent is None else int(percent // USAGE_STEP)


def server_row(server):
    return {"name": server.name, "own": server.own, "health": server.health, "state": server.state}


class Panel:
    def __init__(self, desk, later):
        self.desk = desk        # the router's Desk: its people, and dispatch for the bot's commands
        self.later = later      # runs fn(*args) in the background
        self.servers = {}       # name -> (when checked, [their MCP servers as the page shows them])
        self.checking = set()   # names whose servers are being checked
        self.answers = {}       # name -> the answer to their latest request, {"id", "ok", "text"}
        self.put_last = {}      # name -> the fingerprint of the snapshot last put on the relay (PANELS)
        self.putting = threading.Lock()  # one snapshot built and put at a time
        self.next_watch = 0.0
        self.watching = threading.Event()  # set while a look for changes runs

    def offer(self, user):
        """/config: a message with a button that opens the panel."""
        url = address()
        if not isinstance(user.reply_to, TelegramChat):
            telegram.later(user.say, f"The control panel opens in Telegram: send /config to {bot.name()} there.")
        elif not url:
            telegram.later(user.say, "The control panel needs the relay, which isn't set up: run "
                                     "`assistant relay deploy` on the VM.")
        else:
            button = {"inline_keyboard": [[{"text": "⚙️ Open the control panel", "web_app": {"url": url}}]]}
            telegram.later(telegram.send, user.chat_id, OFFER, reply_markup=button)

    # --- requests ---

    def handle(self, payload):
        """A request from the page (a "panel" envelope): carry it out, then put a new snapshot on the relay."""
        user = self.sender(str(payload.get("init_data") or ""))
        if not user:
            log("control panel: a request Telegram didn't sign for anyone here; ignored")
            return
        action, args = payload.get("action"), payload.get("args") or {}
        try:
            text, ok = self.act(user, action, args), True
        except Refused as e:
            text, ok = str(e), False
        log(f"{user.name}: control panel {action} {json.dumps(args, ensure_ascii=False)}: {text or 'done'}")
        self.answers[user.name] = {"id": str(payload.get("id") or ""), "ok": ok, "text": text}
        self.later(self.refresh, user, action in ("refresh", "mcp"))

    def sender(self, init_data):
        """The Session of the person whose signed init data this is, or None."""
        chat = signed_sender(init_data, telegram.bot_token() or "", time.time())
        return self.desk.users.get(self.desk.by_chat.get(chat)) if chat else None

    def act(self, user, action, args):
        """Carry out one request; what to tell the person ("" for nothing). Refused when it can't be."""
        handlers = {"refresh": lambda user, args: "", "setting": self.change_setting, "model": self.switch_model,
                    "effort": self.switch_effort, "mcp": self.mcp_command, "job": self.job_command}
        if action not in handlers:
            raise Refused(f"The panel asked for {action!r}, which the router doesn't know.")
        return handlers[action](user, args)

    def change_setting(self, user, args):
        field = args.get("field")
        validate = {**addon_settings(), **SETTINGS}.get(field)  # an add-in never takes over a core setting
        if not validate:
            raise Refused(f"{field} isn't a setting here.")
        try:
            value = validate(args.get("value"))
        except ValueError as e:
            raise Refused(str(e)) from None
        try:
            registry.update(user.name, field, value)
        except (KeyError, OSError, ValueError) as e:
            raise Refused(f"Couldn't save it: {e}") from None
        return "Saved."

    def switch_model(self, user, args):
        """Like /model <target>, or with keep like /model <target> default."""
        target, keep = args.get("target"), args.get("keep") is True
        if target not in switching.MODELS:
            raise Refused(f"{target} isn't a model here.")
        self.command(user, f"/model {target}" + (" default" if keep else ""))
        return f"Switching to {target}{SWITCH_SCOPES[keep]}. {bot.name()} confirms in the chat."

    def switch_effort(self, user, args):
        """Like /effort <target>, or with keep like /effort <target> default; max can't be kept."""
        target = args.get("target")
        if target not in claude_ui.EFFORTS:
            raise Refused(f"{target} isn't an effort level.")
        keep = args.get("keep") is True and target in switching.SAVABLE_EFFORTS
        self.command(user, f"/effort {target}" + (" default" if keep else ""))
        unkept = " (Claude Code can't keep max)" if args.get("keep") is True and not keep else ""
        return f"Switching to {target} effort{SWITCH_SCOPES[keep]}{unkept}. {bot.name()} confirms in the chat."

    def mcp_command(self, user, args):
        command, server = args.get("command"), str(args.get("server") or "")
        if command not in MCP_COMMANDS or not SERVER_NAME_RE.fullmatch(server):
            raise Refused("The panel asked for an MCP command the router doesn't know.")
        self.command(user, f"/mcp {command} {server}")
        return "The sign-in link is coming in the chat." if command == "login" else "Done. The answer comes in the chat."

    def job_command(self, user, args):
        command = args.get("command")
        try:
            job = next((j for j in cronjobs.jobs(user.name, cronjobs.read_crontab()) if j.id == args.get("id")), None)
            if not job:
                raise Refused("That job isn't in the crontab any more.")
            if command == "delete":
                self.ask_to_delete(user, job)
                return f"{bot.name()} deletes it and confirms in the chat."
            if command not in ("pause", "resume") or not cronjobs.set_paused(user.name, job.id, command == "pause"):
                raise Refused(f"Couldn't {command} it.")
        except (subprocess.SubprocessError, OSError) as e:
            raise Refused(f"Couldn't change the crontab: {e}") from None
        return "Paused." if command == "pause" else "Resumed."

    def command(self, user, text):
        """Run one of the bot's commands for them, as if they had sent it on Telegram."""
        user.reply_to = TelegramChat(user.chat_id)
        self.desk.dispatch(user, Inbound(user.chat_id, user.chat_id, "", text), time.time())

    def ask_to_delete(self, user, job):
        """Deleting a job is the assistant's work: its jobs.md entry goes too."""
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))
        header = (f"[Control panel from {user.display} · {stamp}] I tapped Delete on a scheduled job in my control "
                  "panel; it is my own request to you:")
        body = ("Delete this scheduled job of mine: its lines and their comment in the crontab, and its entry in "
                "jobs.md. Then tell me it's gone.\n\n" + "\n".join([f"# {user.name}: {job.purpose}", *job.lines]))
        user.pickups.cancel()
        user.queue_message(work.Deliver(now, header, body), now)

    # --- snapshots ---

    def tick(self, now):
        """From the router's loop: every WATCH_EVERY seconds, in the background, put each person's
        panel on the relay if it changed."""
        if now < self.next_watch or self.watching.is_set():
            return
        self.next_watch = now + WATCH_EVERY
        self.watching.set()
        self.later(self.watch)

    def watch(self):
        try:
            for user in list(self.desk.users.values()):
                self.update(user)
        finally:
            self.watching.clear()

    def refresh(self, user, check_servers=False):
        """After a request: put their panel on the relay. With check_servers (the page asks when its
        last check is old), check their MCP servers and the plan's limits too, about 5 seconds, and
        put it again."""
        if check_servers and user.name not in self.checking:
            self.checking.add(user.name)
            try:
                self.update(user)
                usage.refresh_plan()
                found = mcp.servers(load_person(user.name))
                self.servers[user.name] = (time.time(), [server_row(s) for s in found])
            finally:
                self.checking.discard(user.name)
        self.update(user)

    def update(self, user):
        """Put their panel on the relay, unless it's the same as the one last put there."""
        with self.putting:
            self.restore(user.name)
            snapshot = self.snapshot(user)
            mark = fingerprint(snapshot)
            if mark == self.put_last.get(user.name) or not self.push(user, snapshot):
                return
            self.put_last[user.name] = mark
            try:
                jsonfile.write_json(PANELS / f"{user.name}.json", {"fingerprint": mark, "snapshot": snapshot})
            except OSError as e:
                log(f"{user.name}: can't keep the control panel's snapshot: {e}")

    def restore(self, name):
        """After a restart, what was last put on the relay for them: its fingerprint, their servers'
        last check and the answer to their latest request, so an unchanged panel isn't put again."""
        if name in self.put_last:
            return
        kept = jsonfile.read_json(PANELS / f"{name}.json", {})
        snapshot = kept.get("snapshot") if isinstance(kept, dict) else None
        self.put_last[name] = kept.get("fingerprint") if snapshot else None
        if snapshot:
            mcp_part = snapshot.get("mcp") or {}
            self.servers.setdefault(name, (mcp_part.get("checked"), mcp_part.get("servers") or []))
            self.answers.setdefault(name, snapshot.get("answer"))

    def snapshot(self, user):
        """What the page shows them."""
        now = time.time()
        status = usage.snapshot(user) or {}
        checked, servers = self.servers.get(user.name, (None, []))
        return {"answer": self.answers.get(user.name), "bot": bot.name(), "addons": list(addons.names()),
                "person": person_part(registry.load_users()[user.name]), "claude": claude_part(user, status),
                "usage": usage_part(status), "jobs": jobs_part(user, now),
                "mcp": {"checked": checked, "checking": user.name in self.checking, "servers": servers}}

    def push(self, user, snapshot):
        """Put a snapshot on the relay, for the page to read. Whether it got there."""
        url, token = relay_entrance.address(f"/panel/{user.chat_id}"), relay_entrance.token()
        if not url or not token:
            return False
        body = json.dumps(snapshot, ensure_ascii=False).encode()
        for attempt in range(1, PUSH_TRIES + 1):
            try:
                relay_entrance.put(url, token, body)
                return True
            except OSError as e:
                log(f"{user.name}: can't put the control panel on the relay (try {attempt}): {e}")
                if attempt < PUSH_TRIES:
                    time.sleep(PUSH_PAUSE)
        return False
