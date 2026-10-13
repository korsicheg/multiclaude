"""When the owner hears about the machine. Each rule stands at a level after every sample (fine,
high, or for a disk critical), kept in alerts.json, and the owner gets a message when a rule goes up
a level or back to fine, never while it stays put. A share goes up after `samples` samples in a row
at or over its `at` (or `critical`), and goes down at once, but only under its `clear` (or
`critical_clear`), so a value hovering at a line doesn't flap. The router and each person's
assistant are high while they aren't running. The owner may change any of DEFAULTS under "health"
in config.json: {"health": {"ram": {"at": 95, "samples": 2}}}."""
from dataclasses import asdict, dataclass, replace
from typing import NamedTuple

from multiclaude import config, jsonfile, paths
from multiclaude.health.words import duration, load, share, size, top_list
from multiclaude.telegram import markdown_literal

STATE = paths.HEALTH / "alerts.json"  # {rule: {"level", "streak", "since"}}
SAMPLE_EVERY = 5 * 60                 # seconds between samples: the cron line `assistant install` adds
DEFAULTS = {
    "cpu": {"at": 90, "clear": 70, "samples": 3},
    "ram": {"at": 90, "clear": 80, "samples": 3},
    "swap": {"at": 50, "clear": 40, "samples": 3},
    "disk": {"at": 85, "clear": 80, "critical": 95, "critical_clear": 90, "samples": 1},
    "router": {"samples": 1},
    "assistant": {"samples": 2},
}
LEVELS = ("fine", "high", "critical")


def settings():
    """DEFAULTS, with the numbers the owner set under "health" in config.json over them."""
    own = config.read().get("health")
    own = own if isinstance(own, dict) else {}
    return {rule: {**defaults, **numbers_for(defaults, own.get(rule))} for rule, defaults in DEFAULTS.items()}


def numbers_for(defaults, given):
    """The numbers in given for settings the rule has."""
    if not isinstance(given, dict):
        return {}
    return {key: value for key, value in given.items()
            if key in defaults and isinstance(value, (int, float)) and not isinstance(value, bool)}


def limits(settings):
    """Where CPU, RAM and the disks start counting as high, for the panel's sparklines."""
    return {"cpu": settings["cpu"]["at"], "ram": settings["ram"]["at"], "disk": settings["disk"]["at"]}


@dataclass(frozen=True)
class Standing:
    """Where one rule stands."""
    level: int = 0            # an index into LEVELS
    streak: int = 0           # samples in a row that asked for a higher level, short of the rule's count
    since: int | None = None  # when it went up from fine

    @classmethod
    def kept(cls, data):
        """A Standing as alerts.json keeps it; fine when it isn't one."""
        try:
            return cls(int(data["level"]), int(data["streak"]), data["since"])
        except (TypeError, KeyError, ValueError):
            return cls()

    def after(self, wanted, samples, now):
        """Where it stands after a sample that asks for the level wanted: up once `samples` samples in
        a row asked for more, down at once."""
        if wanted <= self.level:
            return Standing(wanted, 0, self.since if wanted else None)
        if self.streak + 1 < samples:
            return replace(self, streak=self.streak + 1)
        return Standing(wanted, 0, self.since or now)


class Change(NamedTuple):
    rule: str
    before: Standing  # where it stood: kept when the owner couldn't be told, so it's told next time
    text: str         # what the owner is told


def load_standings():
    kept = jsonfile.read_json(STATE, {})
    return {rule: Standing.kept(data) for rule, data in kept.items()} if isinstance(kept, dict) else {}


def save_standings(standings):
    jsonfile.write_json(STATE, {rule: asdict(standing) for rule, standing in standings.items()})


def check(sample, standings, settings):
    """Where each rule stands after sample, and what to tell the owner about each one that went up a
    level or back to fine: (standings, [Change])."""
    after, changes = {}, []
    for rule in rules(sample, settings):
        before = standings.get(rule.key, Standing())
        wanted = rule.wanted(sample, before.level)
        now = after[rule.key] = before if wanted is None else before.after(wanted, rule.samples, sample["time"])
        if now.level > before.level:
            changes.append(Change(rule.key, before, rule.alert(sample, now.level)))
        elif now.level == 0 < before.level:
            changes.append(Change(rule.key, before, rule.all_clear(sample)))
    return after, changes


def standing_alerts(sample, standings, settings):
    """The alerts standing after sample, for the panel and the terminal: [{"rule", "level", "since", "text"}]."""
    return [{"rule": rule.key, "level": LEVELS[standings[rule.key].level], "since": standings[rule.key].since,
             "text": rule.headline(sample)}
            for rule in rules(sample, settings) if rule.key in standings and standings[rule.key].level]


def rules(sample, settings):
    """The rules for a sample: CPU, RAM, swap, each of its disks, the router and each person's assistant."""
    return [Cpu(settings["cpu"]), Ram(settings["ram"]), Swap(settings["swap"]),
            *(Disk(disk["mount"], settings["disk"]) for disk in sample["disks"]),
            Router(settings["router"]),
            *(Assistant(session["name"], session["display"], settings["assistant"]) for session in sample["sessions"])]


# --- the rules: each says what level a sample asks for, and what the owner is told ---

def lasting(samples):
    """How long a rule has been high when it alerts after this many samples: 15 min."""
    return duration(samples * SAMPLE_EVERY)


class Share:
    """A percentage that's high from `at` and, when it has `critical`, critical from there."""
    key = ""

    def __init__(self, setting):
        self.samples = setting["samples"]
        self.clear = setting["clear"]
        self.levels = [(setting["at"], setting["clear"])]
        if "critical" in setting:
            self.levels.append((setting["critical"], setting["critical_clear"]))

    def value(self, sample):
        raise NotImplementedError

    def wanted(self, sample, level):
        """The levels it has reached, or the level it's at while it isn't under that level's clear; None
        when the sample doesn't know it (the CPU, at a first sample), which leaves the rule as it was."""
        value = self.value(sample)
        if value is None:
            return None
        reached = sum(value >= at for at, _ in self.levels)
        while level > reached and value < self.levels[level - 1][1]:
            level -= 1
        return max(level, reached)


class Cpu(Share):
    key = "cpu"

    def value(self, sample):
        return sample["cpu"]["percent"]

    def alert(self, sample, level):
        return (f"🔥 **The CPU has been busy for {lasting(self.samples)}**: {share(self.value(sample))} in the last "
                f"{duration(SAMPLE_EVERY)}, load {load(sample['cpu'])}. I'll say when it's under {self.clear}% again."
                f"\n\nUsing the most memory:\n{top_list(sample['top'])}")

    def all_clear(self, sample):
        return f"✅ **The CPU is calm again**: {share(self.value(sample))} busy in the last {duration(SAMPLE_EVERY)}."

    def headline(self, sample):
        return f"CPU {share(self.value(sample))} busy"


class Ram(Share):
    key = "ram"

    def value(self, sample):
        return sample["memory"]["percent"]

    def alert(self, sample, level):
        memory = sample["memory"]
        return (f"⚠️ **Memory has been running low for {lasting(self.samples)}**: {share(memory['percent'])} used, "
                f"{size(memory['available_mb'])} of {size(memory['total_mb'])} available. I'll say when it's under "
                f"{self.clear}% again.\n\nUsing the most memory:\n{top_list(sample['top'])}")

    def all_clear(self, sample):
        memory = sample["memory"]
        return f"✅ **Memory is fine again**: {share(memory['percent'])} used, {size(memory['available_mb'])} available."

    def headline(self, sample):
        return f"Memory {share(self.value(sample))} used"


class Swap(Share):
    key = "swap"

    def value(self, sample):
        return sample["swap"]["percent"]

    def alert(self, sample, level):
        swap = sample["swap"]
        return (f"⚠️ **Swap has been filling up for {lasting(self.samples)}**: {share(swap['percent'])} used, "
                f"{size(swap['used_mb'])} of {size(swap['total_mb'])}, with memory {share(sample['memory']['percent'])} "
                f"used. Swapping slows everything down.\n\nUsing the most memory:\n{top_list(sample['top'])}")

    def all_clear(self, sample):
        return f"✅ **Swap is fine again**: {share(self.value(sample))} used."

    def headline(self, sample):
        return f"Swap {share(self.value(sample))} used"


class Disk(Share):
    def __init__(self, mount, setting):
        super().__init__(setting)
        self.mount = mount
        self.key = f"disk:{mount}"

    def disk(self, sample):
        return next(disk for disk in sample["disks"] if disk["mount"] == self.mount)

    def value(self, sample):
        return self.disk(sample)["percent"]

    def alert(self, sample, level):
        disk = self.disk(sample)
        full, free, total = share(disk["percent"]), size(disk["free_mb"]), size(disk["total_mb"])
        if LEVELS[level] == "critical":
            return (f"🚨 **The disk is nearly full**: `{self.mount}` is {full} full, only {free} of {total} free. "
                    "Once it's full, assistants and jobs start failing.")
        return (f"💽 **The disk is filling up**: `{self.mount}` is {full} full, {free} of {total} free. "
                f"I'll say when it's under {self.clear}% again.")

    def all_clear(self, sample):
        disk = self.disk(sample)
        return f"✅ **Disk space is fine again**: `{self.mount}` is {share(disk['percent'])} full, {size(disk['free_mb'])} free."

    def headline(self, sample):
        return f"Disk {self.mount} {share(self.value(sample))} full"


class Outage:
    """Something of the bot's that should be running: high while it isn't, for `samples` samples in a row."""
    key = ""

    def __init__(self, setting):
        self.samples = setting["samples"]

    def down(self, sample):
        raise NotImplementedError

    def wanted(self, sample, level):
        return 1 if self.down(sample) else 0


class Router(Outage):
    key = "router"

    def down(self, sample):
        return not sample["router"]

    def alert(self, sample, level):
        return ("🛑 **The router is down**: `assistant-watcher` isn't running, so messages don't reach anyone's "
                "assistant. See why with `journalctl -u assistant-watcher -n 50`, and start it with "
                "`sudo systemctl restart assistant-watcher`.")

    def all_clear(self, sample):
        return "✅ **The router is running again.** It routes what was sent meanwhile, unless it was down for over an hour."

    def headline(self, sample):
        return "The router is down"


class Assistant(Outage):
    def __init__(self, name, display, setting):
        super().__init__(setting)
        self.name, self.display = name, display
        self.key = f"assistant:{name}"

    def down(self, sample):
        return not next(session["running"] for session in sample["sessions"] if session["name"] == self.name)

    def alert(self, sample, level):
        whose = markdown_literal(self.display)
        return (f"⚠️ **{whose}'s assistant isn't running**: no Claude Code in its tmux session `u-{self.name}` "
                f"for {lasting(self.samples)}, so {whose}'s messages wait. `assistant start-users` starts it, and "
                f"`assistant attach {self.name}` shows its terminal.")

    def all_clear(self, sample):
        return f"✅ **{markdown_literal(self.display)}'s assistant is running again.**"

    def headline(self, sample):
        return f"{self.display}'s assistant isn't running"
