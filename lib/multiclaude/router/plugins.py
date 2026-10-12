"""/plugin, /skills and /mcp: bin/shared.py changes the person's own workspace and replies; the
change then reaches their assistant through /reload-plugins (or, for /mcp, the matching
in-session command) once it's idle."""
import json
import subprocess
import time

from multiclaude import paths, telegram
from multiclaude.channels import TelegramChat
from multiclaude.log import log
from multiclaude.router.work import Reload, SessionCommands

SHARED = paths.BIN / "shared.py"
SHARED_LOG = paths.LOGS / "shared.log"
SHARED_TIMEOUT = 900


def run_all(user, commands):
    """Run several commands one after another, each with its own reply, so a marketplace added
    by one is there for the install in the next."""
    for text in commands:
        run(user, text)


def run(user, text):
    """Run the command through shared.py (in a background thread: it can take minutes)."""
    command = text.split()[0]
    if isinstance(user.reply_to, TelegramChat):
        telegram.api("sendChatAction", chat_id=user.chat_id, action="typing")
    try:
        SHARED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SHARED_LOG, "a") as errors:
            r = subprocess.run(["python3", "-I", str(SHARED), "command", user.name, text], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=errors, timeout=SHARED_TIMEOUT)
        reply = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log(f"{user.name}: {command} failed: {e!r}")
        user.say(f"⚠️ {command} failed. Details are in `~/assistant/logs/shared.log` on the VM.")
        return
    in_session = reply.get("session") or []
    if reply.get("text"):
        user.say(reply["text"], reply.get("copy") or ())
    elif in_session and not user.idle():
        user.say(f"⏳ I'll run {in_session[-1]} once I finish what I'm doing.")
    elif not in_session:
        user.say("Done.")
    now = time.time()
    if in_session:
        user.enqueue(SessionCommands(now, commands=in_session))
    if reply.get("reload"):
        queue_reload(user, now)


def take_reload_requests(users, now):
    """Queue /reload-plugins for each assistant that asked for one with `assistant reload-plugins`
    (it installed or updated a plugin or skill itself), and forget the request."""
    for request in sorted(paths.RELOAD_REQUESTS.glob("*")):
        request.unlink(missing_ok=True)
        if user := users.get(request.name):
            log(f"{user.name}: its assistant asked for /reload-plugins")
            queue_reload(user, now)


def queue_reload(user, now, force=False):
    """One /reload-plugins waiting is enough: it picks up every change made before it runs."""
    waiting = next((w for w in user.queue if isinstance(w, Reload)), None)
    if waiting:
        waiting.force = waiting.force or force
    else:
        user.enqueue(Reload(now, force=force))
