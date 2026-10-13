"""Running the `claude` command line (plugin, marketplace and mcp subcommands)."""
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from multiclaude import jsonfile, paths
from multiclaude.log import log

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TIMEOUT = 300


@dataclass
class CliResult:
    ok: bool
    message: str                               # what to tell the person: claude's own words, cleaned up
    lines: list = field(default_factory=list)  # every non-empty output line, without colours
    returncode: int = -1
    stdout: str = ""
    output: str = ""  # stdout and stderr together, without colours
    data: dict = field(default_factory=dict)  # claude's --json outcome, when it printed one


def env_for(cwd):
    """The environment for `claude` run in cwd: this process's, plus the `env` that cwd's own
    settings (project, then local) give the sessions started there, such as a person's own
    GitHub login for cloning a private marketplace. Claude Code's subcommands don't apply it."""
    env = dict(os.environ)
    for name in ("settings.json", "settings.local.json"):
        try:
            extra = jsonfile.read_object(Path(cwd) / ".claude" / name).get("env")
        except jsonfile.NotAnObject:
            continue
        if isinstance(extra, dict):
            env.update({str(k): str(v) for k, v in extra.items()})
    return env


def run(args, cwd, timeout=TIMEOUT):
    """Run `claude <args>` in cwd, with env_for(cwd). Never raises: a failure to run is a failed CliResult."""
    try:
        r = subprocess.run([paths.CLAUDE, *args], cwd=cwd, stdin=subprocess.DEVNULL, env=env_for(cwd),
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"{cwd}: claude {' '.join(args)}: {e}")
        return CliResult(False, str(e))
    lines = [ln.strip() for ln in ANSI_RE.sub("", r.stdout + "\n" + r.stderr).splitlines()
             if ln.strip() and not ln.startswith("Warning: no stdin")]
    log(f"{cwd}: claude {' '.join(args)}: exit {r.returncode}: {lines[-1] if lines else ''}")
    ok, message, data = outcome(lines, r.returncode)
    return CliResult(ok, message, lines, r.returncode, r.stdout, ANSI_RE.sub("", r.stdout + r.stderr), data)


def outcome(lines, returncode):
    """(worked, message, its --json outcome or {}) from claude's output: the outcome line when
    there is one, else the exit code, a ✘ anywhere, and the last lines. When it worked, the
    message is its last line; the progress before it (cloning, refreshing) is noise."""
    for line in reversed(lines):
        try:
            out = json.loads(line)
        except ValueError:
            continue
        if isinstance(out, dict) and "outcome" in out:
            ok = out["outcome"] == "ok"
            message = out.get("message") or out.get("error") or out["outcome"]
            return ok, message.strip().splitlines()[-1] if ok else message, out
    failed = returncode != 0 or any("✘" in ln for ln in lines)
    message = "\n".join(ln.rpartition("✘ ")[2].rpartition("✔ ")[2] for ln in lines[-3:])
    return not failed, message or f"exit {returncode}", {}
