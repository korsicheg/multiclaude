#!/usr/bin/env bash
# Runs inside the "assistant" tmux session started by assistant.service.
# Keeps the front desk connected to Telegram, restarting it if it exits. The
# front desk only hosts the Telegram plugin; the router (lib/multiclaude/router) routes
# each message to the sender's own assistant session (see ARCHITECTURE.md).
set -u
# shellcheck source-path=SCRIPTDIR source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"

cd "$ROOT" || exit 1
load_env

while true; do
  # Claude Code remembers MCP servers that failed to connect and skips them at
  # startup, so one bad start (no network at boot, say) would keep the bot
  # offline. Forget any such failure before each launch.
  cache="$HOME/.claude/mcp-needs-auth-cache.json"
  if [ -f "$cache" ] && jq -e 'has("plugin:telegram:telegram")' "$cache" >/dev/null 2>&1; then
    jq 'del(.["plugin:telegram:telegram"])' "$cache" > "$cache.tmp" && mv "$cache.tmp" "$cache"
  fi

  # A plugin update brings a fresh copy without the router's patches (tg ask
  # buttons, replies, and the add-ins' that are on); patch it again (a no-op when
  # nothing changed).
  "$ROOT/bin/patch-telegram-plugin" || echo "[$(date -Is)] patching the Telegram plugin failed"

  # Every user's assistant runs in its own tmux session next to this one.
  "$ROOT/bin/assistant" start-users || echo "[$(date -Is)] starting user assistants failed"

  # The front desk keeps no history and needs no tools, a big model or much
  # effort. Its plugin tools are blocked so it can't reply in anyone's place.
  args=(--channels plugin:telegram@claude-plugins-official --permission-mode bypassPermissions
        --name assistant --model haiku --effort low --tools ""
        --disallowedTools mcp__plugin_telegram_telegram__reply mcp__plugin_telegram_telegram__react
          mcp__plugin_telegram_telegram__edit_message mcp__plugin_telegram_telegram__download_attachment)
  claude "${args[@]}"
  code=$?
  echo "[$(date -Is)] claude exited with $code; restarting in 5s"
  sleep 5
done
