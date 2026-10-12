"""/usage: the assistant's context and cost from its status line, the plan's limits, and what
every session has cost so far."""
import datetime
import glob
import os
import re
import subprocess
import time

from multiclaude import jsonfile, paths
from multiclaude.log import log

# The status line saves each session's latest payload here (system/statusline.sh).
STATUSLINE_SNAPSHOT = str(paths.CLAUDE_DIR / "state" / "statusline-{name}.json")
# What every session has cost so far, one file per session id: the status line keeps live
# sessions' running cost there and `assistant job` adds each job's ({"name", "dir", "base",
# "last", "since", "updated"}, the cost being base + last).
COSTS = paths.CLAUDE_DIR / "state" / "costs"
# Plan limits come from a separate usage-only login (scope user:profile), never from the
# assistants' own credentials. The status line keeps the cache fresh too.
PLAN_USAGE = str(paths.HOME / ".local" / "bin" / "claude-usage")
PLAN_CACHE = paths.HOME / ".cache" / "claude-usage" / "usage.json"
PLAN_FRESH = 5 * 60  # as system/statusline.sh: limits are shown in whole percent and warned about at 80/90/99%
PLAN_FETCH_TIMEOUT = 20
PLAN_STALE = 15 * 60      # older than this (fetching keeps failing): say how old
PLAN_WINDOWS = [("five_hour", "5-hour"), ("seven_day", "Weekly"),
                ("seven_day_opus", "Weekly Opus"), ("seven_day_sonnet", "Weekly Sonnet")]
CENT = 0.01


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def fmt_duration(s):
    s = max(0, int(s))
    if s >= 86400:
        return f"{s // 86400}d {s % 86400 // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 60}m"


def resets_at(window):
    """When a plan window resets, as a timestamp, or None."""
    try:
        return datetime.datetime.fromisoformat(window["resets_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def snapshot(session):
    """The status line's latest payload for a session, or None."""
    snap = jsonfile.read_json(STATUSLINE_SNAPSHOT.format(name=session.name))
    if not isinstance(snap, dict):
        return None
    return snap if str(snap.get("transcript_path", "")).startswith(session.tail.dir + "/") else None


def model_and_effort(snap):
    return (snap.get("model") or {}).get("display_name"), (snap.get("effort") or {}).get("level")


def read_plan():
    """(the cached plan usage, its age in seconds), or (None, None)."""
    data = jsonfile.read_json(PLAN_CACHE)
    if not isinstance(data, dict):
        return None, None
    try:
        return data, time.time() - os.path.getmtime(PLAN_CACHE)
    except OSError:
        return None, None


def refresh_plan():
    """Fetch the plan's usage limits into the cache when it isn't fresh."""
    _, age = read_plan()
    if (age is None or age >= PLAN_FRESH) and os.access(PLAN_USAGE, os.X_OK):
        try:
            subprocess.run([PLAN_USAGE, "--cache"], capture_output=True, timeout=PLAN_FETCH_TIMEOUT)
        except subprocess.SubprocessError:
            pass


def plan_limits():
    """The plan's usage limits as cached, [(label, percent used, reset time or None)], and how old
    they are in seconds; ([], None) when they're unavailable."""
    data, age = read_plan()
    if data is None:
        return [], None
    windows = []
    for key, label in PLAN_WINDOWS:
        window = data.get(key) or {}
        if window.get("utilization") is not None:
            windows.append((label, window["utilization"], resets_at(window)))
    return windows, age


def plan_usage():
    """Lines describing the plan's usage limits, [] if they're unavailable."""
    refresh_plan()
    windows, age = plan_limits()
    if not windows:
        return []
    lines = [f"  {label}: {used:.0f}% used" + ("" if resets is None else f", resets in {fmt_duration(resets - time.time())}")
             for label, used, resets in windows]
    return ["Plan limits" + (f" (as of {fmt_duration(age)} ago)" if age > PLAN_STALE else "") + ":"] + lines


def spending(displays):
    """What all sessions have cost so far, in total and per person (displays: {name: display})."""
    per, total, since = {}, 0.0, None
    for path in glob.glob(f"{COSTS}/*.json"):
        entry = jsonfile.read_json(path)
        try:
            amount = float(entry.get("base") or 0) + float(entry.get("last") or 0)
        except (ValueError, TypeError, AttributeError):
            continue
        label = cost_label(str(entry.get("dir") or ""), displays)
        per[label] = per.get(label, 0.0) + amount
        total += amount
        when = entry.get("since") or entry.get("updated")
        if isinstance(when, (int, float)) and (since is None or when < since):
            since = when
    if not total:
        return []
    rank = {"front desk": 1, "other": 2}
    people = sorted((k for k in per if per[k] >= CENT), key=lambda k: (rank.get(k, 0), -per[k]))
    day = time.strftime("%b %-d", time.gmtime(since)) if since else "?"
    return ["", f"💸 Spent so far, all sessions: ${total:,.2f}",
            " · ".join(f"{k} ${per[k]:,.2f}" for k in people),
            f"Claude Code's own estimates, tracked since {day}."]


def cost_label(folder, displays):
    """Whose a session's cost is, by the folder it ran in."""
    owner = re.match(rf"{re.escape(str(paths.WORKSPACES))}/([^/]+)", folder)
    if owner and owner[1] in displays:
        return displays[owner[1]]
    if folder == str(paths.ROOT) or folder.startswith(f"{paths.ROOT}/"):
        return "front desk"
    return "other"  # setup work and tests outside anyone's folder


def context(snap):
    """(tokens in the context, the context window's size, percent used or None) from a status line payload."""
    ctx = snap.get("context_window") or {}
    used, size = ctx.get("total_input_tokens") or 0, ctx.get("context_window_size") or 0
    pct = ctx.get("used_percentage")
    if pct is None and size:
        pct = round(100 * used / size)
    return used, size, pct


def usage_text(session, displays):
    snap = snapshot(session)
    plan = plan_usage()
    try:
        spent = spending(displays)
    except Exception as e:  # a fun extra must never cost the rest of /usage
        log(f"spending total failed: {e!r}")
        spent = []
    if not snap:
        return "\n".join(["📊 No context data yet. It appears after my next reply."] + plan + spent)
    used, size, pct = context(snap)
    lines = [f"📊 Context: {fmt_tokens(used)} of {fmt_tokens(size)} tokens ({pct}%)"]
    cost = (snap.get("cost") or {}).get("total_cost_usd")
    if cost is not None:
        lines.append(f"Session cost: ${cost:.2f}")
    model, effort = model_and_effort(snap)
    if model:
        lines.append(f"Model: {model}" + (f", {effort} effort" if effort else ""))
    return "\n".join(lines + plan + spent)
