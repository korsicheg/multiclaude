"""What the machine looks like now: CPU, memory, swap, disks and uptime, the programs using the most
memory, whether the router and each person's assistant are running, and the apps hosted in Podman. The parsers take a /proc
file's text, so the tests give them their own; the rest is read under paths.PROC."""
import json
import os
import shutil
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from multiclaude import paths, tmux

BYTES_PER_MB = 1024 * 1024
KB_PER_MB = 1024
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
NAME_CUT = 15                                  # characters the kernel keeps of a process's name
TOP_SHOWN = 5                                  # programs listed as using the most memory
DISK_FOLDERS = (Path("/"), paths.ROOT, paths.WORKSPACES)  # their file systems are watched, each once
ROUTER_UNIT = "assistant-watcher"
ROUTER_SCRIPT = paths.BIN / "watcher.py"
SYSTEMCTL_TIMEOUT = 10
PODMAN_TIMEOUT = 15
BROWSERS = ("chrome", "chromium", "headless_shell")  # what a browser's processes are called: one program, Chrome
INTERPRETERS = ("python", "node", "bun", "deno")     # called by the script or module they run instead
FRONT_DESK = "front desk"


class CpuTicks(NamedTuple):
    """CPU time spent since the machine started, all cores together, in ticks."""
    busy: int
    total: int


class Process(NamedTuple):
    pid: int
    ppid: int
    name: str         # the program's file name
    memory_mb: float  # what it holds in RAM, sharing split among those sharing it (PSS), else all it holds
    args: tuple
    cwd: str | None   # read for claude only, to tell whose assistant it is


def sample(people, since=None):
    """The machine now. people: those whose assistant should be running (users.User); since: the CPU
    ticks of the sample before, for how busy the CPU was in between."""
    ticks = cpu_ticks(read("stat"))
    fields = meminfo(read("meminfo"))
    running = processes()
    return {"time": int(time.time()), "uptime": int(float(read("uptime").split()[0])),
            "cpu": {"percent": cpu_percent(since, ticks) if since else None, "load": load(read("loadavg")),
                    "cores": os.cpu_count() or 1},
            "memory": memory(fields), "swap": swap(fields), "disks": disks(DISK_FOLDERS),
            "top": top_memory(running), "hosted": hosted(running), "router": router_running(),
            "sessions": [{"name": person.name, "display": person.display, "running": assistant_running(person, running)}
                         for person in people],
            "ticks": list(ticks)}


def read(name):
    return (paths.PROC / name).read_text()


# --- CPU, memory and swap ---

def cpu_ticks(stat):
    """From /proc/stat. Busy is everything but idle and waiting for the disk; time spent running
    guests is already counted in user."""
    user, nice, system, idle, iowait, irq, softirq, steal = (int(v) for v in stat.split("\n", 1)[0].split()[1:9])
    total = user + nice + system + idle + iowait + irq + softirq + steal
    return CpuTicks(total - idle - iowait, total)


def cpu_percent(before, after):
    """How busy the CPU was between two readings, in percent; None when they can't be compared: the
    machine restarted in between, or no time passed."""
    spent = after.total - before.total
    if spent <= 0 or after.busy < before.busy:
        return None
    return round(100 * (after.busy - before.busy) / spent, 1)


def load(loadavg):
    """The load averages over 1, 5 and 15 minutes, from /proc/loadavg."""
    return [float(value) for value in loadavg.split()[:3]]


def meminfo(text):
    """{field: kB} from /proc/meminfo."""
    fields = {}
    for line in text.splitlines():
        name, _, value = line.partition(":")
        amount = value.split()[:1]
        if amount and amount[0].isdigit():
            fields[name] = int(amount[0])
    return fields


def memory(fields):
    """RAM: what's used is what isn't available (MemAvailable: what's free, and the caches the
    kernel would drop for a program that asked)."""
    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable", fields.get("MemFree", 0))
    return {"total_mb": round(total / KB_PER_MB), "available_mb": round(available / KB_PER_MB),
            "percent": percent(total - available, total)}


def swap(fields):
    total = fields.get("SwapTotal", 0)
    used = total - fields.get("SwapFree", total)
    return {"total_mb": round(total / KB_PER_MB), "used_mb": round(used / KB_PER_MB), "percent": percent(used, total)}


def percent(part, whole):
    return round(100 * part / whole, 1) if whole > 0 else 0.0


# --- disks ---

def disks(folders):
    """The file systems holding these folders, each once, as df shows them: the share used is of
    what's used and what's still free to use, without the part kept back for root."""
    found, seen = [], set()
    for folder in folders:
        try:
            device, usage = os.stat(folder).st_dev, shutil.disk_usage(folder)
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        found.append({"mount": mount_point(folder), "total_mb": megabytes(usage.total), "used_mb": megabytes(usage.used),
                      "free_mb": megabytes(usage.free), "percent": percent(usage.used, usage.used + usage.free)})
    return found


def megabytes(size):
    return round(size / BYTES_PER_MB)


def mount_point(folder):
    folder = Path(os.path.realpath(folder))
    while not os.path.ismount(folder):
        folder = folder.parent
    return str(folder)


# --- processes ---

def processes():
    """The processes running now that hold memory, {pid: Process}; the kernel's own threads hold
    none, and one that ends while it's read is left out."""
    found = {}
    for folder in paths.PROC.iterdir():
        if folder.name.isdigit():
            process = read_process(folder)
            if process and process.memory_mb:
                found[process.pid] = process
    return found


def read_process(folder):
    """The process whose /proc folder this is, or None once it has ended."""
    try:
        stat = (folder / "stat").read_text()
        resident = int((folder / "statm").read_text().split()[1]) * PAGE_SIZE / BYTES_PER_MB
        args = tuple(arg for arg in (folder / "cmdline").read_bytes().decode(errors="replace").split("\0") if arg)
        name = stat[stat.index("(") + 1:stat.rindex(")")]
        ppid = int(stat[stat.rindex(")") + 1:].split()[1])
    except (OSError, ValueError, IndexError):
        return None
    name = uncut(name, args)
    cwd = working_folder(folder) if name == "claude" else None
    shared_fairly = fair_share(folder)
    return Process(int(folder.name), ppid, name, resident if shared_fairly is None else shared_fairly, args, cwd)


def uncut(name, args):
    """The kernel cuts a process's name at 15 characters: agent-browser-l is agent-browser-linux-x64,
    as its command line says."""
    command = Path(args[0]).name if args else ""
    return command if len(name) == NAME_CUT and command.startswith(name) else name


def fair_share(folder):
    """The process's proportional set size in MB: its own memory, and its share of what it shares with
    others, so a browser's dozens of processes add up to what they really take, where their resident
    sizes would count the same shared pages again for each. None when the kernel won't say: only
    root may read a root process's."""
    try:
        rollup = (folder / "smaps_rollup").read_text()
    except OSError:
        return None
    kb = meminfo(rollup).get("Pss")
    return None if kb is None else kb / KB_PER_MB


def working_folder(folder):
    try:
        return os.readlink(folder / "cwd")
    except OSError:
        return None


def top_memory(running):
    """The programs using the most memory, biggest first, with each program's processes together:
    [{"name", "mb", "count"}]."""
    used, count = defaultdict(float), defaultdict(int)
    for process in running.values():
        name = program(process)
        used[name] += process.memory_mb
        count[name] += 1
    return [{"name": name, "mb": round(used[name]), "count": count[name]}
            for name in sorted(used, key=used.get, reverse=True)[:TOP_SHOWN]]


def program(process):
    """What a process counts as among the top memory users: Chrome for any of a browser's
    processes, claude with whose it is, an interpreter by the script or module it runs, anything
    else by its name."""
    if process.name.startswith(BROWSERS):
        return "Chrome"
    if process.name == "claude":
        return claude_program(process)
    if process.name.startswith(INTERPRETERS):
        return interpreted(process.args) or process.name
    return process.name


def claude_program(process):
    """claude (ada) for Ada's assistant, claude job (ada) for one of her scheduled jobs (claude -p),
    claude (front desk) for the bot's own."""
    kind = "claude job" if {"-p", "--print"} & set(process.args) else "claude"
    whose = whose_folder(process.cwd)
    return f"{kind} ({whose})" if whose else kind


def whose_folder(folder):
    """Whose assistant works in folder: a person's name, the front desk, or None."""
    if not folder:
        return None
    path = Path(folder)
    if path == paths.ROOT:
        return FRONT_DESK
    if path.is_relative_to(paths.WORKSPACES) and path != paths.WORKSPACES:
        return path.relative_to(paths.WORKSPACES).parts[0]
    return None


def interpreted(args):
    """The script or module an interpreter runs, by its name (the router by its role), or None when
    it runs neither, such as python -c."""
    options = iter(args[1:])
    for arg in options:
        if arg == "-m":
            return next(options, None)
        if arg == "-c":
            return None
        if not arg.startswith("-"):
            return "router" if Path(arg) == ROUTER_SCRIPT else Path(arg).name
    return None


# --- apps hosted in Podman ---

class ProcessTree:
    """The processes running, by the process that started them."""

    def __init__(self, running):
        self.running = running
        self.children = defaultdict(list)
        for process in running.values():
            self.children[process.ppid].append(process.pid)

    def memory_mb(self, pid):
        """What a process holds together with everything it started, and they started, and so on."""
        total, waiting = 0.0, [pid]
        while waiting:
            current = waiting.pop()
            total += self.running[current].memory_mb if current in self.running else 0
            waiting.extend(self.children.get(current, ()))
        return total


def hosted(running):
    """The apps hosted in this user's Podman, each pod one app and a container outside any pod one of
    its own, with how many of its containers run and the memory they hold: [{"name", "containers",
    "up", "mb"}]; none when Podman isn't installed or can't say."""
    apps = defaultdict(list)
    for container in podman_containers():
        if not container.get("IsInfra"):  # a pod's own placeholder, which runs nothing
            apps[container.get("PodName") or container["Names"][0]].append(container)
    tree = ProcessTree(running)
    return [hosted_app(name, containers, tree) for name, containers in sorted(apps.items())]


def hosted_app(name, containers, tree):
    up = [container for container in containers if container.get("State") == "running"]
    return {"name": name, "containers": len(containers), "up": len(up),
            "mb": round(sum(tree.memory_mb(container["Pid"]) for container in up))}


def podman_containers():
    """`podman ps --all --pod`, or [] when Podman isn't installed or fails. Cron leaves out the user's
    runtime folder, without which rootless Podman may look in the wrong place."""
    env = {"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}", **os.environ}
    try:
        listed = subprocess.run(["podman", "ps", "--all", "--pod", "--format", "json"], capture_output=True, text=True,
                                env=env, timeout=PODMAN_TIMEOUT, check=True)
        return json.loads(listed.stdout or "[]")
    except (subprocess.SubprocessError, OSError, ValueError):
        return []


# --- the bot's own services ---

def router_running():
    """Whether systemd has the router running; not being able to ask counts as no."""
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", ROUTER_UNIT], timeout=SYSTEMCTL_TIMEOUT).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def assistant_running(person, running):
    """Whether claude runs in the person's tmux session, where `assistant start-users` starts it."""
    try:
        shell = int(tmux.Pane(person.pane).shell_pid())
    except (subprocess.SubprocessError, OSError, ValueError):
        return False
    return any(process.ppid == shell and process.name == "claude" for process in running.values())
