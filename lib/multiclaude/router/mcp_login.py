"""/mcp login: run `claude mcp login --no-browser` on a pseudo-terminal (it won't take the
redirect URL otherwise), send the sign-in link, and pass on the redirect URL the person pastes
back. The tokens land where Claude Code keeps them; the assistant then reconnects the server."""
import fcntl
import os
import pty
import queue
import re
import select
import struct
import subprocess
import termios
import time

from multiclaude import bot, paths
from multiclaude.log import log
from multiclaude.router.inbound import REDIRECT_RE
from multiclaude.router.work import SessionCommands

WAIT_FOR_URL = 10 * 60    # how long it waits for the pasted redirect URL
PROMPT_TIMEOUT = 60       # for claude to show the link, or to answer a pasted URL
EXIT_TIMEOUT = 30
READ_POLL = 0.5
TERMINAL_SIZE = (50, 4000)  # rows, columns: wide enough that the URL isn't wrapped
URL_PROMPT = "paste the redirect URL"
LINK_RE = re.compile(r"\x1b\]8;;([^\x1b\x07]+)")
TERMINAL_CODES_RE = re.compile(r"\x1b\]8;;[^\x1b\x07]*(\x1b\\|\x07)|\x1b\[[0-9;?]*[A-Za-z]|\r")
SHOWN_LINES = 4


class McpLogin:
    def __init__(self, user, server):
        self.user, self.server = user, server
        self.inbox = queue.Queue()  # the pasted redirect URL, or None to cancel
        self.output = bytearray()
        self.proc = self.master = None

    def run(self):
        user = self.user
        if user.pending_login:
            user.pending_login["inbox"].put(None)  # one sign-in at a time
            time.sleep(1)
        slot = user.pending_login = {"server": self.server, "inbox": self.inbox}
        try:
            self.start()
        except OSError as e:
            user.pending_login = None
            self.say(f"⚠️ Couldn't start the sign-in: {e}")
            return
        try:
            self.converse()
        except (OSError, subprocess.SubprocessError) as e:
            log(f"{user.name}: /mcp login {self.server}: {e!r}")
            self.say(f"⚠️ /mcp login {self.server} failed: {e}")
        finally:
            if user.pending_login is slot:
                user.pending_login = None
            if self.proc.poll() is None:
                self.proc.kill()
            os.close(self.master)

    def start(self):
        self.master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *TERMINAL_SIZE, 0, 0))
            self.proc = subprocess.Popen([paths.CLAUDE, "mcp", "login", self.server, "--no-browser"],
                                         cwd=paths.workspace(self.user.name),
                                         stdin=slave, stdout=slave, stderr=slave,
                                         env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"}, start_new_session=True)
        except OSError:
            os.close(self.master)
            raise
        finally:
            os.close(slave)

    def converse(self):
        asked = self.read_until(lambda text: URL_PROMPT in text, PROMPT_TIMEOUT)
        link = LINK_RE.search(self.output.decode(errors="replace"))
        if not asked or not link:
            self.say(f"⚠️ /mcp login {self.server}: {self.last_lines(0) or 'no sign-in link came up'}")
            return
        self.say(f"🔑 Sign in to {self.server}:\n{link[1]}\n\n"
                 "Open it, sign in and approve. Your browser then lands on a localhost page "
                 "that won't load; that's expected. Copy that page's full address (tap the "
                 f"address bar) and send it here. I'll wait {WAIT_FOR_URL // 60} minutes; `/mcp login cancel` stops it.")
        deadline = time.time() + WAIT_FOR_URL
        while True:
            try:
                url = self.inbox.get(timeout=max(1, deadline - time.time()))
            except queue.Empty:
                url = None
                again = f"/mcp login {self.server}"
                self.say(f"⌛ The sign-in to {self.server} timed out. Send `{again}` again.", copy=[again])
            if url is None:
                if time.time() < deadline:
                    self.say(f"Cancelled the sign-in to {self.server}.")
                return
            if self.try_url(url):
                return

    def try_url(self, url):
        """Paste a redirect URL. True when the sign-in is over, either way."""
        start = len(self.output)
        os.write(self.master, url.encode() + b"\r")
        asked_again = self.read_until(lambda _: URL_PROMPT in self.output[start:].decode(errors="replace"), PROMPT_TIMEOUT)
        if self.proc.poll() is None and not asked_again:
            self.proc.wait(timeout=EXIT_TIMEOUT)
        if self.proc.poll() == 0:
            self.say(f"✅ Signed in to {self.server}. {bot.name()} reconnects it once it's idle.")
            self.user.enqueue(SessionCommands(time.time(), commands=[f"/mcp reconnect {self.server}"]))
            return True
        if self.proc.poll() is None:
            self.say(f"⚠️ That didn't work: {self.last_lines(start) or 'no reply'}\n"
                     "Send the full address again, or `/mcp login cancel`.")
            return False
        self.say(f"⚠️ Sign-in to {self.server} failed: {self.last_lines(start) or f'exit {self.proc.returncode}'}")
        return True

    def read_until(self, test, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline and self.proc.poll() is None:
            if select.select([self.master], [], [], READ_POLL)[0]:
                try:
                    self.output.extend(os.read(self.master, 65536))
                except OSError:
                    break
            if test(self.output.decode(errors="replace")):
                return True
        return False

    def last_lines(self, start):
        """What claude said since start, without terminal codes, prompts or the echoed URL."""
        text = TERMINAL_CODES_RE.sub("", self.output[start:].decode(errors="replace"))
        keep = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("[mcp-sdk]")
                and URL_PROMPT not in ln and "^C to cancel" not in ln and not REDIRECT_RE.match(ln.strip())]
        return "\n".join(keep[-SHOWN_LINES:])

    def say(self, text, copy=()):
        self.user.say(text, copy)
