"""Keeping every session on the newest Claude Code: install updates in the background, and
restart each session still on an older version once it's idle. The owner hears about it once
every session has moved."""
import os
import subprocess
import threading

from multiclaude import paths, telegram
from multiclaude.log import log
from multiclaude.router.session import version_of
from multiclaude.router.usage import fmt_duration
from multiclaude.router.work import Restart

UPDATE_EVERY = 30 * 60    # how often to run `claude update` (the sessions' own auto-updater may be slower)
UPDATE_TIMEOUT = 600
CHECK_EVERY = 60          # how often to compare each session's version with the installed one
REPORT_AFTER = 6 * 3600   # tell the owner by then, even if a session is still too busy to move


def installed_version():
    """The Claude Code version new sessions start on, or None."""
    return version_of(os.path.realpath(paths.CLAUDE_LINK))


def update_claude():
    """Install a newer Claude Code if there is one. Running sessions keep their version."""
    try:
        out = subprocess.run([str(paths.CLAUDE_LINK), "update"], capture_output=True, text=True,
                             timeout=UPDATE_TIMEOUT, cwd=paths.HOME)
    except (subprocess.SubprocessError, OSError) as e:
        log(f"claude update failed: {e!r}")
        return
    if out.returncode or "up to date" not in out.stdout:
        log(f"claude update (exit {out.returncode}): " + " | ".join((out.stdout + out.stderr).strip().splitlines()[-3:]))


class Upgrader:
    def __init__(self):
        self.updater = None    # the background `claude update`
        self.last_update = 0.0
        self.last_check = 0.0
        self.rollout = None    # {"to", "from", "restarted", "stuck", "started"} while sessions move to a new version

    def tick(self, now, sessions, owner):
        """sessions: the front desk and every assistant; owner: who hears about it, or None."""
        if now - self.last_update >= UPDATE_EVERY and not (self.updater and self.updater.is_alive()):
            self.updater = threading.Thread(target=update_claude, daemon=True)
            self.updater.start()
            self.last_update = now
        if now - self.last_check < CHECK_EVERY:
            return
        self.last_check = now
        latest = installed_version()
        if not latest:
            return
        restarted, busy = False, []
        for session in sessions:
            proc = session.claude_proc()
            running = proc and session.running_version(proc)
            if not running or running == latest:
                continue  # down or starting (it starts on the newest), or up to date
            if session.upgrade_tried == latest:
                if self.rollout:
                    self.rollout["stuck"].add(session.display)
                continue
            if not session.can_upgrade(now, proc):
                busy.append(session.display)
                continue
            self.restart(session, proc, running, latest, now)
            restarted = True  # judge it once it's back
        if self.rollout and not restarted and (not busy or now - self.rollout["started"] >= REPORT_AFTER):
            self.report(owner, busy, now)

    def restart(self, session, proc, running, latest, now):
        """Have the session's worker restart it; a restart drops session-only /model and /effort
        switches, so the worker puts them back once it's up."""
        log(f"{session.name}: restarting to move from Claude Code {running} to {latest}")
        session.upgrade_tried = latest
        session.enqueue(Restart(now, pid=proc[0]))
        if not self.rollout or self.rollout["to"] != latest:
            self.rollout = {"to": latest, "from": set(), "restarted": [], "stuck": set(), "started": now}
        self.rollout["from"].add(running)
        if session.display not in self.rollout["restarted"]:
            self.rollout["restarted"].append(session.display)

    def report(self, owner, busy, now):
        rollout, self.rollout = self.rollout, None
        log(f"sessions now on Claude Code {rollout['to']}; restarted {', '.join(rollout['restarted'])}")
        if not owner:
            return
        text = (f"🔄 Claude Code updated to {rollout['to']} (from {', '.join(sorted(rollout['from']))}). "
                f"Restarted once idle: {', '.join(rollout['restarted'])}. Conversations carried over.")
        if rollout["stuck"]:
            text += (f"\n\n⚠️ Still on the old version after the restart: {', '.join(sorted(rollout['stuck']))}. "
                     "Run `assistant status` on the VM to check.")
        if busy:
            text += (f"\n\n⏳ Not moved yet, busy ever since the update {fmt_duration(now - rollout['started'])} "
                     f"ago: {', '.join(busy)}. They move once idle.")
        telegram.later(telegram.send, owner.chat_id, text)
