"""The agent-browser session being taken over. Every session runs a WebSocket stream server on
127.0.0.1 that sends what its page shows and takes mouse and keyboard input; `agent-browser
stream status` names its port, and `stream enable` turns it back on if it was turned off."""
import json
import os
import subprocess

AGENT_BROWSER = "agent-browser"
CLI_TIMEOUT = 30  # seconds for one agent-browser command


class NoBrowser(Exception):
    """The session isn't running, has no browser open, or its stream can't be reached."""


def stream_port(session, home=None):
    """The port of session's stream server, turning streaming on if it's off. home: the HOME
    the session was started with, for one a plugin runs under its own (its sockets are there)."""
    if session not in call(["session", "list"], home).get("sessions", []):
        raise NoBrowser(f"no agent-browser session {session!r} is running here (`agent-browser session list`)")
    status = call(["--session", session, "stream", "status"], home)
    if not status.get("enabled") or not status.get("port"):
        status = call(["--session", session, "stream", "enable"], home)
    if not status.get("connected"):
        raise NoBrowser(f"agent-browser session {session!r} has no browser open")
    if not status.get("port"):
        raise NoBrowser(f"agent-browser session {session!r} has no stream server")
    return int(status["port"])


def call(args, home=None):
    """Run an agent-browser command with --json; the data of its reply. A command for a session
    that isn't running starts one, so ask `session list` first."""
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
