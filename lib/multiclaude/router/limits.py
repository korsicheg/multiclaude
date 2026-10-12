"""Warning each person once per limit window as the plan's usage passes each level: 80%, 90% and
99% of the 5-hour limit, and 25%, 50%, 75%, 90% and 99% of the weekly ones. The limits are
account-wide, so everyone sees the same numbers."""
import os
import subprocess
import threading
import time

from multiclaude import jsonfile, paths
from multiclaude.log import log
from multiclaude.router.usage import PLAN_FRESH, PLAN_STALE, PLAN_USAGE, PLAN_WINDOWS, fmt_duration, read_plan, resets_at

WEEKLY_LEVELS = [25, 50, 75, 90, 99]
LEVELS = {"five_hour": [80, 90, 99], "seven_day": WEEKLY_LEVELS,  # the keys of usage.PLAN_WINDOWS
          "seven_day_opus": WEEKLY_LEVELS, "seven_day_sonnet": WEEKLY_LEVELS}
WARY_FROM = 75  # a level below this gets a calm note rather than a warning
STOP_AT = 99    # from here, requests may start failing
ALERTS = paths.STATE / "limit-alerts.json"  # the warnings already sent, so restarts don't repeat them
NEW_WINDOW_AFTER = 600  # resets_at shifts by about a second between fetches; a jump bigger than this is a new window


def warning(label, used, level, resets):
    left = fmt_duration(resets - time.time())
    if level >= STOP_AT:
        return (f"🛑 The plan's {label} limit is {int(used)}% used. Requests may start failing until "
                f"it resets in {left}. The limit is shared by everyone on this bot.")
    lead = "⚠️ Be wary: the" if level >= WARY_FROM else "📊 The"
    return (f"{lead} plan's {label} limit is {int(used)}% used. It resets in {left}. "
            "The limit is shared by everyone on this bot; /usage shows the details.")


class LimitAlerts:
    def __init__(self):
        self.sent = jsonfile.read_json(ALERTS, {})  # name -> window -> {"resets": ts, "level": highest warned}
        self.fetch = None                          # a background claude-usage refresh
        self.lock = threading.Lock()               # messages are checked in background threads

    def warn(self, user):
        """Warn the person about every limit that passed a new level. Never waits for a fetch:
        a stale cache gets refreshed in the background for the next message."""
        with self.lock:
            self.check(user)

    def check(self, user):
        data, age = read_plan()
        if age is None or age >= PLAN_FRESH:
            self.refresh()
        if data is None or age > PLAN_STALE:
            return
        mine = self.sent.setdefault(user.name, {})
        changed = False
        for key, label in PLAN_WINDOWS:
            window = data.get(key) or {}
            used, resets = window.get("utilization"), resets_at(window)
            if used is None or resets is None or resets <= time.time():
                continue
            seen = mine.get(key) or {"resets": 0, "level": 0}
            if resets - seen["resets"] > NEW_WINDOW_AFTER:
                seen = {"resets": resets, "level": 0}
            level = max((lv for lv in LEVELS[key] if used >= lv), default=0)
            if level > seen["level"]:
                log(f"{user.name}: {label} plan limit at {used:.0f}%; warning them")
                user.say(warning(label, used, level, resets))
                seen = {**seen, "level": level}
            if mine.get(key) != seen:
                mine[key], changed = seen, True
        if changed:
            self.save()

    def refresh(self):
        if self.fetch and self.fetch.poll() is None:
            return  # one fetch at a time
        if os.access(PLAN_USAGE, os.X_OK):
            self.fetch = subprocess.Popen([PLAN_USAGE, "--cache"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def save(self):
        try:
            jsonfile.write_json(ALERTS, self.sent)
        except OSError as e:
            log(f"can't save {ALERTS}: {e}")
