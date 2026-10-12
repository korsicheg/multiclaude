#!/usr/bin/env bash
# Runs one user's assistant inside the "u-<name>" tmux session (socket -L
# assistant), started by `assistant start-users`, and by the router with "fresh" for
# /restart. Keeps a Claude Code session open in that user's workspace, restarting it
# if it exits. The router types the user's messages into it, and it replies with `tg`
# (or an add-in's command, for an app the add-in brings).
set -u

name=${1:?usage: run-user.sh NAME [fresh]}
# shellcheck source-path=SCRIPTDIR source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
WS="$HOME/workspaces/$name"
SESSIONS=$(project_dir "$WS")
QUICK_EXIT=30  # a session that dies sooner than this after starting isn't resumed next time

cd "$WS" || { echo "no workspace at $WS"; sleep 60; exit 1; }
load_env
TG_CHAT_ID=$(user_field "$name" telegram_id)
export TG_CHAT_ID ASSISTANT_USER="$name" CLAUDE_CODE_SANDBOXED=1
if addon_on locations; then
  # Tools and plugins find the latest location the person shared on Telegram here.
  export ASSISTANT_LOCATION="$ROOT/state/locations/$TG_CHAT_ID.json"
fi

resume=1
[ "${2:-}" = fresh ] && resume=0  # /restart: begin with a new conversation
while true; do
  args=(--permission-mode bypassPermissions --name "$name" --settings "$TELEGRAM_PLUGIN_OFF")
  # Pick up the previous conversation so context survives restarts. If that
  # session dies right away (e.g. it's corrupt), start fresh next time.
  if [ "$resume" = 1 ] && compgen -G "$SESSIONS/*.jsonl" >/dev/null; then
    args+=(--continue)
  fi
  started=$(date +%s)
  claude "${args[@]}"
  code=$?
  if [ $(( $(date +%s) - started )) -lt "$QUICK_EXIT" ]; then resume=0; else resume=1; fi
  echo "[$(date -Is)] claude exited with $code; restarting in 5s"
  sleep 5
done
