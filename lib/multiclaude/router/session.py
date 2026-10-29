"""One Claude Code session in tmux: its transcript, its terminal, and its claude process."""
import os
import signal
import subprocess
import threading
import time

from multiclaude import bot, paths, tmux
from multiclaude.channels import TelegramChat
from multiclaude.log import log
from multiclaude.router import claude_ui, recovery
from multiclaude.router.transcript import CLEAR_MARKER, INTERRUPTED_MARKER, Tail
from multiclaude.router.turn import STALE_AFTER, Turn

POLL = 0.5
READY_AFTER = 8           # seconds a freshly started claude gets before anything is typed into it
START_EVERY = 60          # at most one `assistant start-users` per session per minute
START_TIMEOUT = 30
PID_CHECK_EVERY = 5       # how often to look for the claude process restarting
PROMPT_SETTLE = 1.5       # for the REPL to get back to its prompt after a turn
TYPE_SETTLE = 0.5         # between typing a command and pressing Enter
PASTE_SETTLE = 0.3        # between pasting a message and pressing Enter
SCREEN_POLL = 0.3
CLEAR_TIMEOUT = 30        # for /clear to start a new transcript
STOP_TIMEOUT = 10         # for Escape to stop the turn
COMMAND_TIMEOUT = 30      # for a slash command's result to show up in the transcript
PICKER_TIMEOUT = 5.0      # for the /model picker or /effort slider to open
SWITCH_TIMEOUT = 15       # for Claude Code to confirm a switch
EXIT_TIMEOUT = 20         # for /exit to end claude before it's sent SIGTERM
KILL_TIMEOUT = 10         # for SIGTERM to end claude before it's sent SIGKILL (/restart)
COME_BACK_TIMEOUT = 60    # for run-user.sh to start claude again after /restart ended it
UPGRADE_QUIET = 5 * 60    # a session must have been quiet this long before it's restarted onto a new version
# run-user.sh drops the conversation when claude exits within 30s of starting, so a younger
# process is never restarted.
YOUNGEST_RESTART = 60
SHELL_SNAPSHOT = "/.claude/shell-snapshots/"  # in the command line of every Bash tool command
SCREENS_KEPT = 20         # saved screens per session; older ones are deleted


class Session:
    """A Claude Code session: the front desk, or a person's assistant with its queue (router/work.py)."""

    def __init__(self, name, pane, project_dir, chat_id=None, display=None):
        self.name, self.chat_id, self.display = name, chat_id, display or name
        self.reply_to = TelegramChat(chat_id) if chat_id else None  # the chat of their latest message (channels.py)
        self.entry = {}                # their users.json entry as it is, where add-ins find their own fields
        self.addon_state = {}          # what add-ins remember about this session, by add-in name
        self.pane = tmux.Pane(pane)
        self.tail = Tail(project_dir)
        self.turn = Turn(name)
        self.queue = []                # work for this session, in arrival order (router/work.py)
        self.wake = threading.Event()  # set when work arrives, so its worker starts on it right away
        self.typing = None             # the background sendChatAction call, if one is still running
        self.last_typing = 0.0
        self.last_start = 0.0
        self.pid = None                # its claude process, to notice a restart mid-turn
        self.last_pid_check = 0.0
        self.upgrade_tried = None      # the Claude Code version the last update restart was meant to bring
        self.session_switches = {}     # /model and /effort set for this session only, put back after a restart
        self.box_hidden_since = None   # when queued work first found the message box hidden (work.serve)
        self.box_hidden_told = False   # whether the person has heard why it waits
        self.pending_login = None      # {"server", "inbox"} while /mcp login waits for the redirect URL
        self.approval = None           # an approval dialog relayed to the person (router/approvals.py)
        self.stop_requested = False    # /stop came; the worker presses Escape (work.stop)
        self.restart_requested = False  # /restart came; the worker starts it over (work.restart_fresh)
        self.pickups = recovery.Pickups()  # requests its turns dropped on an API error, to pick up again
        self.recover()

    def recover(self):
        """Pick up the live session's state from its newest transcript, without acting on old entries:
        their notices are dropped, and add-ins never see them (on_transcript_entry)."""
        newest = self.tail.newest()
        if newest:
            for entry in self.tail.read(newest):
                self.turn.handle(entry)
            self.turn.last_write = os.path.getmtime(newest)
            if time.time() - self.turn.last_write > STALE_AFTER:
                self.turn.end()  # a turn that ended badly before the router started
        self.turn.notice = self.turn.failed = None  # handled (or not) back then
        self.tail.skip_existing()

    def read(self, now, on_entry=None):
        """Follow the transcripts; the new entries. on_entry(entry), if given, sees each one before
        the turn does."""
        entries = self.tail.read_new()
        if entries:
            self.turn.last_write = now
        for entry in entries:
            if on_entry:
                on_entry(entry)
            self.turn.handle(entry)
        if self.approval:
            self.turn.postpone_stuck(now)  # it waits for the person's tap, not stuck
        if self.turn.stuck(now, self.tail.latest_subagent_write):
            self.save_screen("stuck")
        return entries

    def idle(self):
        return not (self.turn.busy or self.queue)

    def say(self, text, copy=(), quiet=False):
        """Tell the person something, in the chat their latest message came from."""
        return self.reply_to.send(text, copy, quiet) if self.reply_to else None

    def enqueue(self, work):
        self.queue.append(work)
        self.wake.set()

    def queue_message(self, deliver, now):
        """Queue a message to type in (a work.Deliver), and expect the turn that answers it (Turn.expect)."""
        self.enqueue(deliver)
        self.turn.expect(now)

    # --- the claude process ---

    def claude_proc(self):
        """(pid, seconds running) of the claude process in this session's pane, or None."""
        try:
            ps = subprocess.run(["ps", "-o", "pid=,comm=,etimes=", "--ppid", self.pane.shell_pid()],
                                capture_output=True, text=True, timeout=5).stdout
        except (subprocess.SubprocessError, OSError):
            return None
        for line in ps.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] == "claude":
                return int(parts[0]), int(parts[2])
        return None

    def running_version(self, proc):
        """The Claude Code version the claude process proc runs, or None."""
        try:
            return version_of(os.readlink(f"/proc/{proc[0]}/exe"))
        except OSError:
            return None

    def ready(self):
        """True once a claude process has been running in the pane for a few seconds."""
        proc = self.claude_proc()
        return proc is not None and proc[1] >= READY_AFTER

    def check_restart(self, now):
        """Tell the person when their assistant's claude process restarts in the middle of a turn."""
        if now - self.last_pid_check < PID_CHECK_EVERY:
            return
        self.last_pid_check = now
        proc = self.claude_proc()
        if proc is None:
            return  # down or between restarts; judge once a process is back
        if self.turn.in_turn and self.pid is not None and proc[0] != self.pid:
            log(f"{self.name}: claude restarted mid-turn (pid {self.pid} -> {proc[0]})")
            self.turn.reset()
            self.turn.notice = (f"⚠️ {bot.name()}'s session restarted in the middle of your request, "
                                "so that task stopped. Send it again in a minute.")
        self.pid = proc[0]

    def ensure_started(self, now):
        if now - self.last_start < START_EVERY:
            return
        self.last_start = now
        try:
            if self.pane.exists():
                return
            log(f"{self.name}: assistant not running; starting it")
            subprocess.run([str(paths.BIN / "assistant"), "start-users"], capture_output=True, timeout=START_TIMEOUT)
        except (subprocess.SubprocessError, OSError) as e:
            log(f"{self.name}: can't start it: {e}")

    def can_upgrade(self, now, proc):
        """True when restarting would interrupt nothing: no turn, nothing queued, no Bash command
        running, and no transcript (its subagents' included) written for a while."""
        pid, running_for = proc
        if (self.turn.busy or self.turn.in_turn or self.turn.pending or self.queue
                or running_for < YOUNGEST_RESTART or runs_commands(pid)):
            return False
        return now - max(self.turn.last_write, self.tail.latest_subagent_write()) >= UPGRADE_QUIET

    def restart(self, pid):
        """Exit claude with /exit (SIGTERM if that doesn't work); its run loop starts it again,
        and a person's assistant resumes its conversation with --continue."""
        try:
            self.enter("/exit", settle=False)
        except (subprocess.SubprocessError, OSError) as e:
            log(f"{self.name}: /exit keystrokes failed: {e}")
        deadline = time.time() + EXIT_TIMEOUT
        while os.path.exists(f"/proc/{pid}"):
            if time.time() > deadline:
                log(f"{self.name}: /exit didn't end claude; sending SIGTERM")
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
                break
            time.sleep(POLL)
        self.pid = None
        self.turn.end()
        self.turn.last_write = time.time()

    def restart_fresh(self):
        """/restart: end claude at once, mid-turn or stuck alike, then replace its run loop with
        a new one (the run-user.sh on disk now) that starts claude with a new conversation.
        Whether a new claude came up."""
        old = self.claude_proc()
        self.pid, self.approval = None, None  # its dialog goes with it
        self.turn.reset()
        if old:
            log(f"{self.name}: /restart; ending claude {old[0]}")
            end_process(old[0])
        try:
            self.pane.respawn(str(paths.BIN / "run-user.sh"), self.name, "fresh")
        except (subprocess.SubprocessError, OSError) as e:
            log(f"{self.name}: can't start a new run loop: {e}")
            return False
        self.turn.last_write = time.time()
        return self.wait_until_up(old[0] if old else None)

    def wait_until_up(self, old_pid):
        deadline = time.time() + COME_BACK_TIMEOUT
        while time.time() < deadline:
            proc = self.claude_proc()
            if proc and proc[0] != old_pid and proc[1] >= READY_AFTER:
                return True
            time.sleep(POLL)
        log(f"{self.name}: claude didn't come back after /restart")
        return False

    def save_screen(self, why):
        """Keep what the terminal shows now in paths.SCREENS, for the owner to see what held it up."""
        folder = paths.SCREENS / self.name
        path = folder / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{why}.txt"
        try:
            text = self.pane.screen()
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            for old in sorted(folder.glob("*.txt"))[:-SCREENS_KEPT]:
                old.unlink()
        except (subprocess.SubprocessError, OSError) as e:
            log(f"{self.name}: can't save its screen ({why}): {e}")
            return
        log(f"{self.name}: saved its screen ({why}) to {path}")

    def message_box_shown(self):
        try:
            return claude_ui.message_box_shown(self.pane.screen())
        except (subprocess.SubprocessError, OSError):
            return False

    # --- typing into it ---

    def enter(self, command, settle=True):
        """Type a command and press Enter, as a person would."""
        if settle:
            time.sleep(PROMPT_SETTLE)
        self.pane.type(command)
        time.sleep(TYPE_SETTLE)
        self.pane.press("Enter")

    def type_message(self, header, body):
        """Type the header, paste the body (bracketed, so newlines don't submit), press Enter.
        The body is loaded first, so if that fails nothing is half typed. DialogOpen if an
        approval dialog is open: the digits in the header would answer it."""
        if claude_ui.approval_dialog(self.pane.screen()):
            raise DialogOpen(self.name)
        buffer = f"tg-{self.name}"
        if body:
            self.pane.load(buffer, "\n" + body.replace("\x1b", ""))
        self.pane.type(header)
        if body:
            self.pane.paste(buffer)
        time.sleep(PASTE_SETTLE)
        self.pane.press("Enter")

    def clear(self):
        """/clear, confirmed by the new transcript it starts. Returns whether it worked."""
        before = set(self.tail.files())
        try:
            self.enter("/clear")
        except (subprocess.SubprocessError, OSError) as e:
            log(f"{self.name}: /clear keystrokes failed: {e}")
            return False
        deadline = time.time() + CLEAR_TIMEOUT
        while time.time() < deadline:
            if path := self.tail.new_file_with(before, CLEAR_MARKER):
                # Read from the top, unless the router's loop has started on it meanwhile.
                self.tail.offsets.setdefault(path, 0)
                self.turn.reset()
                log(f"{self.name}: context cleared, new transcript {os.path.basename(path)}")
                return True
            time.sleep(POLL)
        log(f"{self.name}: no new transcript after /clear")
        return False

    def stop(self):
        """Press Escape, as a person would, which stops the turn. Confirmed by the note Claude
        Code writes for it or, before any reply, by the message it puts back in the message box.
        Returns whether it worked."""
        sizes = self.tail.sizes()
        try:
            self.pane.press("Escape")
        except (subprocess.SubprocessError, OSError) as e:
            log(f"{self.name}: Escape keystroke failed: {e}")
            return False
        deadline = time.time() + STOP_TIMEOUT
        while time.time() < deadline:
            if self.tail.written_since(sizes, INTERRUPTED_MARKER):
                log(f"{self.name}: stopped with Escape")
                return True
            if self.message_put_back():
                self.empty_message_box()
                return True
            time.sleep(POLL)
        log(f"{self.name}: no sign that Escape stopped the turn")
        return False

    def message_put_back(self):
        """Whether the message is back in the message box: Escape before any reply writes no
        note, and Claude Code takes the message out of the conversation instead."""
        try:
            return claude_ui.holds_typed_message(self.pane.screen())
        except (subprocess.SubprocessError, OSError):
            return False

    def empty_message_box(self):
        """End the turn, which nothing in the transcript will, and empty the box with Escape
        twice, so the stopped message doesn't go out again with the next one."""
        self.turn.end()
        try:
            self.pane.press("Escape", times=2)
            emptied = self.wait_screen(lambda screen: not claude_ui.holds_typed_message(screen), STOP_TIMEOUT)
        except (subprocess.SubprocessError, OSError):
            emptied = None
        outcome = "emptied the message box" if emptied is not None else "its message is still in the box"
        log(f"{self.name}: stopped before any reply; {outcome}")

    def run_command(self, command, timeout=COMMAND_TIMEOUT):
        """Type a slash command such as `/mcp reconnect x` and return the output Claude Code
        logs for it, or None if none showed up in time."""
        sizes = self.tail.sizes()
        self.enter(command)
        return self.wait_for_result(command.split()[0], sizes, timeout)

    def wait_for_result(self, command, sizes, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if (result := self.tail.command_result(command, sizes)) is not None:
                return result
            time.sleep(POLL)
        return None

    def wait_screen(self, test, timeout=PICKER_TIMEOUT):
        """The pane's text once test(text) holds, or None after the timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if test(text := self.pane.screen()):
                return text
            time.sleep(SCREEN_POLL)
        return None

    def switch(self, setting, target):
        """Switch the model or effort for this session only, the way a person would: open
        Claude Code's /model picker or /effort slider, move to target, check the screen, press
        s. Returns (Claude Code's result line or the problem, whether it warned that the
        conversation gets re-read)."""
        command = f"/{setting}"
        sizes = self.tail.sizes()
        self.enter(command)
        screen = self.wait_screen(lambda text: claude_ui.OPEN_HINT[setting] in text)
        if screen is None:
            result = self.tail.command_result(command, sizes)
            if result is None:
                self.pane.press("Escape")  # should it open late, the next message mustn't land in it
            return result or f"the {command} picker didn't open", False
        if problem := claude_ui.PICKERS[setting](self.pane, screen, target):
            self.pane.press("Escape")
            return problem, False
        self.pane.press("s")
        warned = False
        deadline = time.time() + SWITCH_TIMEOUT
        while time.time() < deadline:
            if (result := self.tail.command_result(command, sizes)) is not None:
                return result, warned
            if not warned and claude_ui.asks_to_confirm(self.pane.screen()):
                self.pane.press("Enter")
                warned = True
            time.sleep(POLL)
        self.pane.press("Escape")
        return "Claude Code didn't confirm the switch", warned


class DialogOpen(Exception):
    """An approval dialog is open in the terminal, so nothing may be typed into it."""


def end_process(pid):
    """SIGTERM, then SIGKILL if pid is still there KILL_TIMEOUT later."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        deadline = time.time() + KILL_TIMEOUT
        while os.path.exists(f"/proc/{pid}") and time.time() < deadline:
            time.sleep(POLL)
        if not os.path.exists(f"/proc/{pid}"):
            return


def version_of(exe):
    """The Claude Code version of a file in the native installer's versions folder, else None."""
    exe = exe.removesuffix(" (deleted)")  # /proc's name for a running file the installer has removed
    return os.path.basename(exe) if os.path.dirname(exe).endswith("/claude/versions") else None


def runs_commands(pid):
    """Whether a Bash tool command (a background task or a monitor, say) runs under this claude process."""
    try:
        ps = subprocess.run(["ps", "-e", "-ww", "-o", "pid=,ppid=,args="], capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return True  # can't tell, so don't risk killing it
    return has_tool_descendant(ps, pid)


def has_tool_descendant(ps, pid):
    """Whether `ps -o pid=,ppid=,args=` output shows a Bash tool command anywhere under pid."""
    children = {}
    for line in ps.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            children.setdefault(parts[1], []).append((parts[0], parts[2]))
    todo = [str(pid)]
    while todo:
        for child, args in children.get(todo.pop(), []):
            if SHELL_SNAPSHOT in args:
                return True
            todo.append(child)
    return False
