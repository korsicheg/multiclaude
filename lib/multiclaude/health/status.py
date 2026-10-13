"""`assistant health`: the machine now, for the terminal, with the alerts standing at the last sample
and the highs of the last day."""
import time

from multiclaude.health import history, machine
from multiclaude.health.sample import assistants
from multiclaude.health.words import duration, load, program, share, size

CPU_WINDOW = 1  # seconds the CPU is watched, for how busy it is now
LABEL_WIDTH = 24
CHARTED_AS = {"cpu": "CPU", "ram": "memory", "disk": "disk"}


def report():
    """The machine now, as lines of text."""
    before = machine.cpu_ticks(machine.read("stat"))
    time.sleep(CPU_WINDOW)
    now = machine.sample(assistants(), before)
    return "\n".join([*machine_lines(now), "", *top_lines(now["top"]), "", *kept_lines(history.latest(), now["time"])])


def machine_lines(sample):
    cpu, memory, swap = sample["cpu"], sample["memory"], sample["swap"]
    used = memory["total_mb"] - memory["available_mb"]
    rows = [("CPU", f"{share(cpu['percent']):>4}  load {load(cpu)}"),
            ("Memory", f"{share(memory['percent']):>4}  {size(used)} of {size(memory['total_mb'])} used, "
                       f"{size(memory['available_mb'])} available"),
            ("Swap", f"{share(swap['percent']):>4}  {size(swap['used_mb'])} of {size(swap['total_mb'])} used"),
            *((f"Disk {disk['mount']}", f"{share(disk['percent']):>4}  {size(disk['used_mb'])} of "
                                        f"{size(disk['total_mb'])} used, {size(disk['free_mb'])} free")
              for disk in sample["disks"]),
            ("Router", running(sample["router"])),
            *((f"{session['display']}'s assistant", running(session["running"])) for session in sample["sessions"])]
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(sample["time"]))
    return [f"The machine at {when}, up {duration(sample['uptime'])}",
            *(f"  {label:<{LABEL_WIDTH}}{value}" for label, value in rows)]


def running(on):
    return "running" if on else "NOT running"


def top_lines(top):
    return ["Using the most memory:", *(f"  {size(row['mb']):>8}  {program(row)}" for row in top)]


def kept_lines(latest, now):
    """The alerts standing at the last sample cron took, and the highs of the day before it."""
    if not latest:
        return ["No samples kept yet: cron takes one every 5 minutes, once `assistant install` has added its line."]
    standing = latest.get("alerts") or []
    lines = [f"Alerts at the last sample, {duration(now - latest['time'])} ago: {'none' if not standing else ''}".rstrip()]
    lines += [f"  {alert['level']}: {alert['text']}, since {time.strftime('%H:%M UTC', time.gmtime(alert['since']))}"
              for alert in standing]
    day = history.last_day(latest["time"])
    highs = (f"{label} {high(day[name])}" for name, label in CHARTED_AS.items())
    return [*lines, f"The last day's highs: {', '.join(highs)}"]


def high(values):
    found = history.highest(*values)
    return "unknown" if found is None else f"{found}%"
