"""Keeping the bot listening. Only the front desk's Telegram plugin polls Telegram (getUpdates),
and Claude Code never starts a plugin's server again once it has exited. If the poller goes
(killed, crashed, or taken over by another session's copy) while the front desk's claude stays
up, nothing reaches the bot and nothing says so. PollerWatch notices and restarts the front
desk; its run.sh loop starts claude again, and with it a new poller."""
import subprocess
from typing import NamedTuple

from multiclaude import bot, paths, telegram
from multiclaude.log import log
from multiclaude.router.session import end_process

CHECK_EVERY = 15
DOWN_AFTER = 90          # seconds without a poller before the front desk restarts: it starts one within seconds
RESTART_GAP = 5 * 60     # at least this long between restarts, in case one doesn't bring the poller back
MAX_HOPS = 6             # from the poller up to the front desk's claude: bun server.ts, bun run, claude
PS_TIMEOUT = 5
RESTARTED = ("📡 {bot} stopped hearing Telegram messages: the front desk's Telegram plugin wasn't running. "
             "I restarted the front desk, so messages sent meanwhile arrive now.")


def pid_file():
    """Where the plugin keeps the pid of the server.ts that polls."""
    return paths.TELEGRAM_DIR / "bot.pid"


class PollerWatch:
    def __init__(self, desk_session):
        self.desk = desk_session
        self.last_check = 0.0
        self.missing_since = None  # when the front desk was first seen up without a poller
        self.last_restart = 0.0

    def tick(self, now, owner, background):
        """owner: who hears about a restart, or None. background(what, fn, *args) runs the
        restart off the router's loop."""
        if now - self.last_check < CHECK_EVERY:
            return
        self.last_check = now
        proc = self.desk.claude_proc()
        if proc is None or polling(proc[0]):
            self.missing_since = None  # down or between restarts, or fine
            return
        self.missing_since = self.missing_since or now
        if now - self.missing_since < DOWN_AFTER or now - self.last_restart < RESTART_GAP:
            return
        log(f"the front desk has had no Telegram poller for {int(now - self.missing_since)}s; restarting it")
        self.last_restart, self.missing_since = now, None
        background("restarting the front desk", end_process, proc[0])
        if owner:
            telegram.later(telegram.send, owner.chat_id, RESTARTED.format(bot=bot.name()), disable_notification=True)


def polling(desk_pid):
    """Whether the server.ts the plugin's pid file names runs under the front desk's claude.
    True when ps can't be run: a restart is only for a poller known to be gone."""
    try:
        poller = int(pid_file().read_text().strip())
    except (OSError, ValueError):
        return False  # the poller removes it as it exits
    try:
        ps = subprocess.run(["ps", "-e", "-ww", "-o", "pid=,ppid=,args="], capture_output=True, text=True,
                            timeout=PS_TIMEOUT).stdout
    except (subprocess.SubprocessError, OSError):
        return True
    return poller_under(ps, poller, desk_pid)


class Proc(NamedTuple):
    ppid: int
    args: str


GONE = Proc(0, "")


def processes(ps):
    """{pid: Proc} from `ps -o pid=,ppid=,args=` output."""
    procs = {}
    for line in ps.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            procs[int(parts[0])] = Proc(int(parts[1]), parts[2])
    return procs


def poller_under(ps, poller, desk_pid):
    """Whether `ps -o pid=,ppid=,args=` output shows poller as a server.ts under desk_pid."""
    procs = processes(ps)
    if "server.ts" not in procs.get(poller, GONE).args:
        return False
    pid = poller
    for _ in range(MAX_HOPS):
        pid = procs.get(pid, GONE).ppid
        if pid == desk_pid:
            return True
    return False
