"""The samples kept: the latest in full, with its CPU ticks for the next one and the alerts standing
then, and a week of their numbers for looking back."""
import json

from multiclaude import jsonfile, paths

LATEST = paths.HEALTH / "latest.json"
HISTORY = paths.HEALTH / "history.jsonl"  # {"time", "cpu", "ram", "swap", "disk"} a line, oldest first
KEPT_FOR = 7 * 24 * 3600
DAY = 24 * 3600
STEPS = 96                                # the panel's day, in steps of 15 minutes
CHARTED = ("cpu", "ram", "disk")


def latest():
    """The newest sample, or None before the first."""
    sample = jsonfile.read_json(LATEST)
    return sample if isinstance(sample, dict) else None


def keep(sample):
    """Keep sample as the newest, and its numbers in the history, which drops what's over a week older."""
    jsonfile.write_json(LATEST, sample)
    kept = points(since=sample["time"] - KEPT_FOR)
    jsonfile.write_lines(HISTORY, [*kept, numbers(sample)])


def numbers(sample):
    """A sample's percentages: CPU busy, RAM and swap used, and how full the fullest disk is."""
    return {"time": sample["time"], "cpu": sample["cpu"]["percent"], "ram": sample["memory"]["percent"],
            "swap": sample["swap"]["percent"], "disk": max((disk["percent"] for disk in sample["disks"]), default=None)}


def points(since=0):
    """The numbers kept from since (a Unix time) on, oldest first; a line that isn't one is left out."""
    try:
        lines = HISTORY.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [point for point in map(parsed, lines) if point and point["time"] > since]


def parsed(line):
    try:
        point = json.loads(line)
    except ValueError:
        return None
    return point if isinstance(point, dict) and is_number(point.get("time")) else None


def last_day(until):
    """The day of samples up to until (a Unix time), for the panel's sparklines: CPU, RAM and the
    fullest disk in STEPS steps of equal time, each the highest sampled in it, None where nothing
    was. Anchored on a sample's time, so it only changes with a new sample."""
    step = DAY / STEPS
    start = until - DAY
    charts = {name: [None] * STEPS for name in CHARTED}
    for point in points(since=start):
        index = min(STEPS - 1, int((point["time"] - start) // step))
        for name, values in charts.items():
            values[index] = highest(values[index], point.get(name))
    return {"until": until, "step": step, **charts}


def highest(*values):
    """The highest of these percentages, whole, ignoring any that are missing; None if all are."""
    present = [value for value in values if is_number(value)]
    return round(max(present)) if present else None


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)
