#!/bin/bash
# Claude Code status line for every session on the VM:
#   model·effort │ context │ plan usage │ git │ folder
# It also keeps two files for the router's /usage (lib/multiclaude/router/usage.py):
# the latest payload of each named session, and each session's running cost.
set -uo pipefail

STATE="$HOME/.claude/state"
input=$(cat)

eval "$(printf '%s' "$input" | jq -r '
  def n($v): ($v // 0) | floor | tostring;
  def safe($v): ($v // "") | gsub("[^A-Za-z0-9_-]"; "");
  @sh "SESSION_NAME=\(safe(.session_name))",
  @sh "SESSION_ID=\(safe(.session_id))",
  @sh "MODEL=\(.model.display_name // "?")",
  @sh "CTX_USED=\(n(.context_window.total_input_tokens))",
  @sh "CTX_MAX=\(n(.context_window.context_window_size))",
  @sh "CTX_PCT=\(n(.context_window.used_percentage))",
  @sh "EFFORT=\(.effort.level // "")",
  @sh "CWD=\(.workspace.current_dir // "")",
  @sh "FAST=\(.fast_mode // false)"
' 2>/dev/null)"

# write_atomically FILE: stdin into FILE, so readers never see half of it.
write_atomically() {
  mkdir -p "${1%/*}" && cat > "$1.$$" && mv -f "$1.$$" "$1"
} 2>/dev/null

# The payload of each named session, so each person's /usage shows their own assistant.
save_snapshot() {
  [ -n "$SESSION_NAME" ] || return 0
  printf '%s' "$input" | write_atomically "$STATE/statusline-$SESSION_NAME.json"
}

# Running cost per session, for the "spent so far" total in /usage: one file per session id.
# A session's cost only grows. If a resume ever starts its counter again (well below what it
# had), what it had moves into "base". A 0 is a render before any cost is known: skip it.
update_cost_ledger() {
  [ -n "$SESSION_ID" ] || return 0
  local ledger="$STATE/costs/$SESSION_ID.json" old new
  old=$(cat "$ledger" 2>/dev/null); [ -n "$old" ] || old='{}'
  new=$(printf '%s' "$input" | jq -c --argjson old "$old" '
    (.cost.total_cost_usd // 0) as $c | ($old.last // 0) as $l
    | if $c == 0 or ($c == $l and $old.session_id) then empty else
        {session_id, name: (.session_name // ""), dir: (.workspace.project_dir // .cwd // ""),
         base: (($old.base // 0) + (if $c < $l / 2 then $l else 0 end)), last: $c,
         since: ($old.since // (now | floor)), updated: (now | floor)}
      end' 2>/dev/null)
  [ -z "$new" ] || printf '%s\n' "$new" | write_atomically "$ledger"
}

save_snapshot
update_cost_ledger

# --- palette -------------------------------------------------------------
R=$'\e[0m'; D=$'\e[2m'; B=$'\e[1m'
C_MODEL=$'\e[38;5;141m'; C_GIT=$'\e[38;5;79m'; C_DIR=$'\e[38;5;110m'
C_LBL=$'\e[38;5;244m'; C_DIRTY=$'\e[38;5;179m'
G=$'\e[38;5;114m'; Y=$'\e[38;5;179m'; O=$'\e[38;5;215m'; E=$'\e[38;5;203m'
SEP="${D}${C_LBL} │ ${R}"

# Color a 0-100 percentage by severity.
pct_color() {
  local p=${1%%.*}
  if   [ "$p" -ge 90 ]; then printf '%s' "$E"
  elif [ "$p" -ge 75 ]; then printf '%s' "$O"
  elif [ "$p" -ge 50 ]; then printf '%s' "$Y"
  else                       printf '%s' "$G"; fi
}

# 47300 -> 47k ; 1000000 -> 1.0M
fmt_tokens() {
  local n=$1
  if   [ "$n" -lt 1000 ];    then printf '%d' "$n"
  elif [ "$n" -lt 1000000 ]; then awk -v n="$n" 'BEGIN{printf (n<100000 ? "%.1fk" : "%.0fk"), n/1000}'
  else                            awk -v n="$n" 'BEGIN{printf "%.1fM", n/1000000}'; fi
}

MODEL=${MODEL:-?}; MODEL=${MODEL/ (1M context)/ 1M}; CTX_USED=${CTX_USED:-0}; CTX_MAX=${CTX_MAX:-0}; CTX_PCT=${CTX_PCT:-0}
EFFORT=${EFFORT:-}; EFFORT=${EFFORT/medium/med}   # low | med | high | xhigh
CWD=${CWD:-$PWD}

# --- git -----------------------------------------------------------------
GIT=""
if git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  branch=$(git -C "$CWD" symbolic-ref --quiet --short HEAD 2>/dev/null) \
    || branch=$(git -C "$CWD" rev-parse --short HEAD 2>/dev/null)
  [ -n "$branch" ] || branch="(no commits)"
  mark=""
  git -C "$CWD" diff --quiet --ignore-submodules HEAD 2>/dev/null || mark="*"
  if [ -z "$mark" ] && [ -n "$(git -C "$CWD" ls-files --others --exclude-standard 2>/dev/null | head -1)" ]; then
    mark="?"
  fi
  ab=$(git -C "$CWD" rev-list --left-right --count '@{upstream}...HEAD' 2>/dev/null)
  track=""
  if [ -n "$ab" ]; then
    behind=${ab%%[[:space:]]*}; ahead=${ab##*[[:space:]]}
    [ "${ahead:-0}" -gt 0 ] 2>/dev/null && track="${track}↑${ahead}"
    [ "${behind:-0}" -gt 0 ] 2>/dev/null && track="${track}↓${behind}"
  fi
  GIT="${C_GIT}⎇ ${branch}${R}${C_DIRTY}${mark}${R}${D}${track}${R}"
fi

# --- folder --------------------------------------------------------------
TILDE="~"; DIR="${CWD/#$HOME/$TILDE}"

# --- render --------------------------------------------------------------
ctx_c=$(pct_color "$CTX_PCT")
fast=""; [ "${FAST:-false}" = "true" ] && fast="${D} ⚡${R}"
# Effort sits with the model, without the dim attribute, which washes out on dark themes.
eff=""; [ -n "$EFFORT" ] && eff="${C_LBL}·${EFFORT}${R}"

# --- plan usage (dedicated token via ~/.local/bin/claude-usage) -------------
# Reads the cache only, so it never blocks: a cache older than PLAN_FRESH (the same as the
# router's in lib/multiclaude/router/usage.py) starts a detached refresh, backing off 5 min
# after a failure.
ucache="${XDG_CACHE_HOME:-$HOME/.cache}/claude-usage"
PLAN_FRESH=300
fmt_reset() {   # ISO timestamp -> 1h8m / 2d1h / 12m
  local s; s=$(( $(date -d "$1" +%s 2>/dev/null || echo 0) - $(date +%s) ))
  if   [ "$s" -le 0 ];     then printf 'now'
  elif [ "$s" -ge 86400 ]; then printf '%dd%dh' $((s/86400)) $((s%86400/3600))
  elif [ "$s" -ge 3600 ];  then printf '%dh%dm' $((s/3600)) $((s%3600/60))
  else                          printf '%dm' $((s/60)); fi
}
USAGE=""
if [ -x "$HOME/.local/bin/claude-usage" ] && [ -f "$HOME/.config/claude-usage/credentials.json" ]; then
  now=$(date +%s); mt=$(stat -c %Y "$ucache/usage.json" 2>/dev/null || echo 0)
  et=$(cat "$ucache/error" 2>/dev/null || echo 0)
  if [ $((now-mt)) -gt "$PLAN_FRESH" ] && [ $((now-et)) -gt 300 ]; then
    nohup "$HOME/.local/bin/claude-usage" --cache >/dev/null 2>&1 </dev/null &
  fi
  if [ -f "$ucache/usage.json" ]; then
    for k in five_hour:5h seven_day:7d; do
      read -r p r < <(jq -r --arg k "${k%%:*}" '.[$k] | select(.) | "\(.utilization|floor) \(.resets_at)"' "$ucache/usage.json" 2>/dev/null)
      [ -n "${p:-}" ] || continue
      seg="${C_LBL}${k##*:} ${R}$(pct_color "$p")${p}%${R} ${C_LBL}↻$(fmt_reset "$r")${R}"
      USAGE+="${USAGE:+${D}${C_LBL} · ${R}}${seg}"
    done
    # Stale beyond 15 min (fetch failing, e.g. token expired): mark it.
    [ $((now-mt)) -gt 900 ] && [ -n "$USAGE" ] && USAGE+=" ${E}!${R}"
  fi
fi

out="${B}${C_MODEL}${MODEL}${R}${eff}${fast}"
out+="${SEP}${C_LBL}ctx ${R}$(fmt_tokens "$CTX_USED")${D}/${R}$(fmt_tokens "$CTX_MAX") ${ctx_c}${CTX_PCT}%${R}"
[ -n "$USAGE" ] && out+="${SEP}${USAGE}"
[ -n "$GIT" ] && out+="${SEP}${GIT}"
out+="${SEP}${C_DIR}${DIR}${R}"

printf '%s\n' "$out"
