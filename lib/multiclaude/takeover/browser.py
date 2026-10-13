"""The agent-browser session being taken over. Every session runs a WebSocket stream server on
127.0.0.1 that sends what its page shows and takes mouse and keyboard input; `agent-browser
stream status` names its port, and `stream enable` turns it back on if it was turned off. Its
viewport is read with `eval` and set with `set viewport W H SCALE`."""
import json
import os
import subprocess
from dataclasses import dataclass

AGENT_BROWSER = "agent-browser"
CLI_TIMEOUT = 30  # seconds for one agent-browser command
VIEWPORT_SCRIPT = "[innerWidth, innerHeight, devicePixelRatio]"


class NoBrowser(Exception):
    """The session isn't running, has no browser open, or its stream can't be reached."""


@dataclass(frozen=True)
class Viewport:
    width: int    # CSS pixels
    height: int
    scale: float  # device pixels per CSS pixel


class Session:
    def __init__(self, name, home=None):
        """name: the session (agent-browser --session NAME); home: the HOME it was started with,
        for one a plugin runs under its own (its sockets are there)."""
        self.name, self.home = name, home

    def stream_port(self):
        """The port of the session's stream server, turning streaming on if it's off."""
        if self.name not in call(["session", "list"], self.home).get("sessions", []):
            raise NoBrowser(f"no agent-browser session {self.name!r} is running here (`agent-browser session list`)")
        status = self.call("stream", "status")
        if not status.get("enabled") or not status.get("port"):
            status = self.call("stream", "enable")
        if not status.get("connected"):
            raise NoBrowser(f"agent-browser session {self.name!r} has no browser open")
        if not status.get("port"):
            raise NoBrowser(f"agent-browser session {self.name!r} has no stream server")
        return int(status["port"])

    def viewport(self):
        """The viewport as the page sees it."""
        result = self.call("eval", VIEWPORT_SCRIPT).get("result")
        try:
            width, height, scale = result
            return Viewport(int(width), int(height), float(scale))
        except (TypeError, ValueError):
            raise NoBrowser(f"agent-browser session {self.name!r}: no viewport in {result!r}") from None

    def set_viewport(self, viewport):
        self.call("set", "viewport", str(viewport.width), str(viewport.height), f"{viewport.scale:g}")

    def call(self, *args):
        """A command for this session. One for a session that isn't running starts it."""
        return call(["--session", self.name, *args], self.home)


def call(args, home=None):
    """Run an agent-browser command with --json; the data of its reply."""
    env = {**os.environ, "HOME": home} if home else None
    command = " ".join(args)
    try:
        done = subprocess.run([AGENT_BROWSER, "--json", *args], capture_output=True, text=True,
                              timeout=CLI_TIMEOUT, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NoBrowser(f"agent-browser {command}: {e}") from e
    return reply_data(done.stdout, command)


def reply_data(output, command):
    """The data of agent-browser's JSON reply ({"success", "data", "error"}); NoBrowser if it failed."""
    try:
        reply = json.loads(output)
    except ValueError:
        reply = {}
    if not isinstance(reply, dict) or not reply.get("success"):
        error = reply.get("error") if isinstance(reply, dict) else None
        raise NoBrowser(f"agent-browser {command}: {error or output.strip() or 'no answer'}")
    return reply.get("data") or {}
