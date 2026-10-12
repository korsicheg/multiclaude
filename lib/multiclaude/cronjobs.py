"""A person's scheduled jobs: their lines in the shared crontab, each group under a
`# <name>: <purpose>` comment (~/workspaces/.claude/CLAUDE.md, "Scheduled and recurring work").
The control panel (router/panel.py) lists them with their next run, and pauses and resumes them.
A paused job's lines stay in the crontab commented out behind PAUSED, so resuming puts them back
exactly as they were."""
import hashlib
import re
import subprocess
from dataclasses import dataclass
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from multiclaude import jsonfile, paths

PAUSED = "#⏸ "
CRONTAB_LOCK = paths.STATE / "crontab.lock"  # the router's own crontab edits, one at a time
LOOK_AHEAD_DAYS = 400  # a job that runs less often than this shows no next run
NICKNAMES = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
             "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
WEEKDAYS = "sun mon tue wed thu fri sat".split()
# The check that keeps a job at the same local hour across clock changes, as the shared CLAUDE.md
# has assistants write it: [ "$(TZ=Europe/Berlin date +\%H)" = 09 ] && …
HOUR_CHECK_RE = re.compile(r'\[ "\$\(TZ=(?P<zone>[\w/+-]+) date \+\\?%H\)" = (?P<hour>\d\d) \]')
TITLE_END_RE = re.compile(r"\. |: ")
SEE_ALSO_RE = re.compile(r"\s*\(see [^)]*\)\s*$")


@dataclass(frozen=True)
class Schedule:
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset  # 0 is Sunday
    any_day: bool
    any_weekday: bool
    hour_check: tuple = None  # (timezone, hour) from an hour check, or None

    @classmethod
    def of(cls, line):
        """The schedule of a crontab line, or None for @reboot and lines it can't read."""
        line = line.removeprefix(PAUSED).strip()
        nickname, _, rest = line.partition(" ")
        if nickname in NICKNAMES:
            line = f"{NICKNAMES[nickname]} {rest}"
        fields = line.split(None, 5)
        if len(fields) < 6:
            return None
        minute, hour, day, month, weekday, command = fields
        try:
            weekdays = {d % 7 for d in field_values(weekday, 0, 7, WEEKDAYS)}
            check = HOUR_CHECK_RE.match(command.strip())
            return cls(frozenset(field_values(minute, 0, 59)), frozenset(field_values(hour, 0, 23)),
                       frozenset(field_values(day, 1, 31)), frozenset(field_values(month, 1, 12, MONTHS, 1)),
                       frozenset(weekdays), day == "*", weekday == "*",
                       (check["zone"], int(check["hour"])) if check else None)
        except ValueError:
            return None

    def next_run(self, after):
        """The first time after `after` (an aware datetime) that it runs, or None within LOOK_AHEAD_DAYS."""
        start = after.astimezone(timezone.utc).replace(second=0, microsecond=0)
        for offset in range(LOOK_AHEAD_DAYS):
            day = start + timedelta(days=offset)
            if day.month not in self.months or not self.runs_on(day):
                continue
            for hour in sorted(self.hours):
                for minute in sorted(self.minutes):
                    when = day.replace(hour=hour, minute=minute)
                    if when > after and self.passes_hour_check(when):
                        return when
        return None

    def runs_on(self, day):
        """Cron's rule: when both the day of the month and the weekday are set, either one will do."""
        weekday = day.isoweekday() % 7
        if self.any_day or self.any_weekday:
            return day.day in self.days and weekday in self.weekdays
        return day.day in self.days or weekday in self.weekdays

    def passes_hour_check(self, when):
        if not self.hour_check:
            return True
        zone, hour = self.hour_check
        try:
            return when.astimezone(ZoneInfo(zone)).hour == hour
        except (ValueError, ZoneInfoNotFoundError):
            return True


def field_values(text, low, high, names=(), first_name=0):
    """The numbers a crontab time field matches: "*", "*/15", "7-59/15", "1,3", "mon-fri"."""
    values = set()
    for part in text.lower().split(","):
        spec, _, step = part.partition("/")
        if spec == "*":
            start, end = low, high
        else:
            first, _, last = spec.partition("-")
            start = number(first, names, first_name)
            end = number(last, names, first_name) if last else (high if step else start)
        if not low <= start <= end <= high:
            raise ValueError(f"{part} is out of range")
        values.update(range(start, end + 1, int(step or 1)))
    return values


def number(text, names, first_name):
    return names.index(text) + first_name if text in names else int(text)


@dataclass(frozen=True)
class Job:
    purpose: str  # its comment, after "# <name>: "
    lines: tuple  # its crontab lines, as they are written there

    @property
    def id(self):
        return job_id(self.purpose)

    @property
    def paused(self):
        return all(line.startswith(PAUSED) for line in self.lines)

    @property
    def title(self):
        """Its comment's first clause: "Morning brief at 09:00 Berlin"."""
        title = TITLE_END_RE.split(self.purpose, maxsplit=1)[0]
        return SEE_ALSO_RE.sub("", title[:1].upper() + title[1:])

    @property
    def detail(self):
        """The rest of its comment."""
        parts = TITLE_END_RE.split(self.purpose, maxsplit=1)
        return SEE_ALSO_RE.sub("", parts[1]) if len(parts) > 1 else ""

    def next_run(self, after):
        """When it runs next (an aware datetime in UTC), or None: paused, @reboot, or too far off."""
        if self.paused:
            return None
        runs = [s.next_run(after) for s in map(Schedule.of, self.lines) if s]
        return min((run for run in runs if run), default=None)


def job_id(purpose):
    """Stays the same while a job's comment does, so a tap still finds it after other jobs change."""
    return hashlib.sha1(purpose.encode()).hexdigest()[:12]


def groups(name, lines):
    """[(purpose, [indexes of its lines])] for name's jobs among the crontab's lines. A job's
    lines follow its comment, up to the next comment or blank line."""
    found, current = [], None
    marker = f"# {name}:"
    for index, line in enumerate(lines):
        if line.startswith(PAUSED):
            if current:
                current[1].append(index)
        elif line.startswith("#"):
            current = (line[len(marker):].strip(), []) if line.startswith(marker) else None
            if current:
                found.append(current)
        elif not line.strip():
            current = None
        elif current:
            current[1].append(index)
    return [(purpose, indexes) for purpose, indexes in found if indexes]


def jobs(name, crontab):
    """name's jobs in crontab (its text)."""
    lines = crontab.splitlines()
    return [Job(purpose, tuple(lines[i] for i in indexes)) for purpose, indexes in groups(name, lines)]


def set_paused(name, wanted, paused):
    """Pause or resume one of name's jobs. False if it isn't there any more."""
    with jsonfile.locked(CRONTAB_LOCK):
        lines = read_crontab().splitlines()
        found = next((indexes for purpose, indexes in groups(name, lines) if job_id(purpose) == wanted), None)
        if found is None:
            return False
        for i in found:
            line = lines[i].removeprefix(PAUSED)
            lines[i] = PAUSED + line if paused else line
        write_crontab("\n".join(lines) + "\n")
        return True


def read_crontab():
    """The crontab's text, "" when there is none."""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    return result.stdout if result.returncode == 0 else ""


def write_crontab(text):
    subprocess.run(["crontab", "-"], input=text, text=True, check=True, capture_output=True, timeout=10)
