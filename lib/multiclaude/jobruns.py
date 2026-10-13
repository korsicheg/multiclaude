"""What a person's scheduled jobs did, for the control panel's Activity (router/panel.py).
`assistant job` adds a line for each run to state/job-runs/<name>.jsonl and keeps their last 200
(record_job_run in bin/assistant): {"started", "finished" (Unix seconds), "title" (the first line of
its instructions, cut as job_title cuts it), "outcome" (sent; nothing, when its answer was NOTHING;
failed; skipped, when the previous run was still going), "cost_usd" when Claude Code's JSON result
gave one, and "preview": the start of its answer, or why it failed}."""
import json
import re

from multiclaude import paths

OUTCOMES = ("sent", "nothing", "failed", "skipped")
TITLE_MAX = 100  # characters, as job_title in bin/assistant cuts a title
WEEK = 7 * 24 * 3600
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
MARKUP_RE = re.compile(r"<[^>]*(>|$)|\*\*|__|~~|\|\||`|^#+\s*|^>\s?", re.MULTILINE)
BLANK_LINES_RE = re.compile(r"\n\s*\n")


def title(prompt):
    """What a run of these instructions is called: their first line, cut short."""
    first = prompt.partition("\n")[0]
    return first if len(first) <= TITLE_MAX else first[:TITLE_MAX - 1] + "…"


def runs(name):
    """name's recorded runs, newest first. A line that isn't a run is left out, and a record that
    can't be read has none."""
    try:
        lines = (paths.JOB_RUNS / f"{name}.jsonl").read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [run for run in map(parsed, reversed(lines)) if run]


def parsed(line):
    """The run on one line of a record, or None."""
    try:
        run = json.loads(line)
    except ValueError:
        return None
    if not isinstance(run, dict) or run.get("outcome") not in OUTCOMES or not isinstance(run.get("title"), str):
        return None
    return run if is_number(run.get("started")) else None


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def cost(run):
    """What the run cost in USD, or None when that isn't known."""
    value = run.get("cost_usd")
    return value if is_number(value) else None


def tally(runs, since):
    """How many of the runs started since then (a Unix time) ran, how many of those failed, and
    what they cost. A skipped run didn't run."""
    ran = [run for run in runs if run["started"] >= since and run["outcome"] != "skipped"]
    return {"runs": len(ran), "failed": sum(run["outcome"] == "failed" for run in ran),
            "cost": round(sum(cost(run) or 0 for run in ran), 4)}


def plain(markdown):
    """An answer's Markdown as plain text, near enough for a preview: links as their words, without
    tags, emphasis, code marks, heading and quote marks or blank lines."""
    text = MARKUP_RE.sub("", LINK_RE.sub(r"\1", markdown))
    return BLANK_LINES_RE.sub("\n", text).strip()
