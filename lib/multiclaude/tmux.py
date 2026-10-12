"""Driving the assistants' terminals: every session runs in tmux on its own socket."""
import subprocess
import time

SOCKET = "assistant"
# The router reads Claude Code's pickers off the screen, so every session has this size.
WIDTH, HEIGHT = 160, 50
KEY_GAP = 0.15  # between repeated key presses, so Claude Code's UI keeps up


def run(*args, stdin=None):
    """Run a tmux command on the assistants' socket; CalledProcessError if it fails."""
    return subprocess.run(["tmux", "-L", SOCKET, *args], input=stdin, capture_output=True, timeout=10, check=True)


class Pane:
    """The terminal of one tmux session."""

    def __init__(self, session):
        self.session = session
        self.target = f"={session}:"

    def exists(self):
        try:
            run("has-session", "-t", f"={self.session}")
            return True
        except subprocess.CalledProcessError:
            return False

    def type(self, text):
        """Type text literally, without pressing Enter."""
        run("send-keys", "-t", self.target, "-l", text)

    def press(self, key, times=1):
        for _ in range(times):
            run("send-keys", "-t", self.target, key)
            time.sleep(KEY_GAP)

    def load(self, buffer, text):
        """Put text into a tmux buffer, ready for paste()."""
        run("load-buffer", "-b", buffer, "-", stdin=text.encode())

    def paste(self, buffer):
        """Bracketed paste of a loaded buffer, so its newlines don't submit it; the buffer goes."""
        run("paste-buffer", "-p", "-d", "-b", buffer, "-t", self.target)

    def screen(self):
        return run("capture-pane", "-p", "-t", self.target).stdout.decode(errors="replace")

    def respawn(self, *command):
        """Kill whatever runs in the pane and run command there instead."""
        run("respawn-pane", "-k", "-t", self.target, *command)

    def shell_pid(self):
        return run("display-message", "-p", "-t", self.target, "#{pane_pid}").stdout.decode().strip()
