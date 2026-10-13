"""Where everything lives. Each base location can be moved with an environment variable; the
tests use that to run in a temporary folder. On the VM none of them is set."""
import os
import re
import shutil
from pathlib import Path

HOME = Path(os.environ.get("HOME") or Path.home())
ROOT = Path(os.environ.get("ASSISTANT_ROOT") or Path(__file__).resolve().parents[2])  # ~/assistant
WORKSPACES = Path(os.environ.get("ASSISTANT_WORKSPACES") or HOME / "workspaces")
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
CLAUDE_JSON = Path(os.environ.get("CLAUDE_JSON") or HOME / ".claude.json")
TELEGRAM_DIR = Path(os.environ.get("TELEGRAM_STATE_DIR") or CLAUDE_DIR / "channels" / "telegram")

# The native installer points this link at the newest Claude Code version.
CLAUDE_LINK = HOME / ".local" / "bin" / "claude"
CLAUDE = str(CLAUDE_LINK) if CLAUDE_LINK.exists() else shutil.which("claude") or str(CLAUDE_LINK)

USERS = ROOT / "users.json"
CONFIG = ROOT / "config.json"  # the owner's settings: the bot's name, the add-ins that are on (config.py)
ENV_FILE = ROOT / ".env"  # the API key and proxy settings the sessions run with (common.sh's load_env)
BIN = ROOT / "bin"
STATE = ROOT / "state"
LOGS = ROOT / "logs"
# `tg ask` questions and their options, <chat id>/<message id>.json, for the router to read taps.
ASKS = STATE / "asks"
# An assistant asking for /reload-plugins after it installed or updated something itself: one
# empty file per person, written by `assistant reload-plugins` and taken by the router.
RELOAD_REQUESTS = STATE / "reload-requests"
# shared.py holds this lock while it edits workspace settings; the router takes it too.
SETTINGS_LOCK = STATE / "shared.lock"
# What each scheduled job run did, <name>.jsonl, written by `assistant job` (jobruns.py).
JOB_RUNS = STATE / "job-runs"

PROJECTS = CLAUDE_DIR / "projects"
USER_SETTINGS = CLAUDE_DIR / "settings.json"


def workspace(name):
    return WORKSPACES / name


def project_dir(folder):
    """The folder where Claude Code keeps the transcripts of sessions started in folder."""
    return PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(folder))


def workspace_settings(name, scope="project"):
    return workspace(name) / ".claude" / ("settings.json" if scope == "project" else "settings.local.json")
