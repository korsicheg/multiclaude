# Sourced by the assistant's bash scripts (assistant, run.sh, run-user.sh):
# where things are, and what they share.
# shellcheck shell=bash disable=SC2034  # the variables are for the scripts that source this

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"   # ~/assistant
USERS="$ROOT/users.json"
CONFIG="$ROOT/config.json"  # the bot's name and add-ins (lib/multiclaude/config.py)
TELEGRAM_DIR="${TELEGRAM_STATE_DIR:-$HOME/.claude/channels/telegram}"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:/usr/local/bin:/usr/bin:/bin"

TMUX_SOCKET=assistant
# Every session's terminal has this size: the router reads Claude Code's pickers off the screen.
TMUX_SIZE=(-x 160 -y 50)
# Only the front desk hosts the Telegram plugin; a second copy anywhere would take over the bot.
TELEGRAM_PLUGIN_OFF='{"enabledPlugins":{"telegram@claude-plugins-official":false}}'

tm() { TMUX='' tmux -L "$TMUX_SOCKET" "$@"; }

# The API key and proxy settings the sessions run with.
load_env() {
  if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi
}

# user_field NAME FIELD: a field of a person in users.json, empty if there's none.
user_field() { jq -r --arg n "$1" --arg f "$2" '.[$n][$f] // empty' "$USERS" 2>/dev/null; }
bot_name() { jq -r '.name // empty' "$CONFIG" 2>/dev/null | grep . || echo Claude; }
# addon_on NAME: whether config.json turns the add-in NAME on (lib/multiclaude/addons).
addon_on() { jq -e --arg name "$1" '.addons // [] | any(. == $name)' "$CONFIG" >/dev/null 2>&1; }
owner_name() { jq -r 'to_entries[] | select(.value.role == "owner") | .key' "$USERS" 2>/dev/null | head -1; }

# The folder where Claude Code keeps the transcripts of sessions started in a folder.
project_dir() { printf '%s/.claude/projects/%s' "$HOME" "$(printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g')"; }
