"""One run of `assistant health sample`, which cron starts every 5 minutes: read the machine, keep the
sample, and tell the owner what changed. It tells them through `assistant notify`, which sends
through the Bot API itself and holds the message while Telegram is out of reach, never through the
router, so the owner also hears when the router is what's down."""
import subprocess

from multiclaude import jsonfile, paths, users
from multiclaude.health import alerts, history, machine
from multiclaude.log import log

LOCK = paths.HEALTH / "sample.lock"
NOTIFY_TIMEOUT = 120


def run():
    """Take a sample, unless the one before is still being taken."""
    try:
        with jsonfile.locked(LOCK, wait=0):
            take()
    except TimeoutError:
        log("the sample before is still being taken")


def take():
    sample = machine.sample(assistants(), ticks_of(history.latest()))
    settings = alerts.settings()
    standings, changes = alerts.check(sample, alerts.load_standings(), settings)
    for change in changes:
        if not tell_owner(change.text):
            standings[change.rule] = change.before  # so the next sample tells them
    alerts.save_standings(standings)
    history.keep({**sample, "alerts": alerts.standing_alerts(sample, standings, settings)})


def assistants():
    """The people whose assistant should be running: everyone with a folder, as `assistant start-users` has it."""
    try:
        people = users.load_users().values()
    except (OSError, ValueError):
        return []
    return [person for person in people if person.workspace.is_dir()]


def ticks_of(sample):
    """The CPU ticks a sample kept, or None."""
    try:
        return machine.CpuTicks(*sample["ticks"])
    except (TypeError, KeyError, ValueError):
        return None


def tell_owner(text):
    """Send text to the owner as `assistant notify` does. Whether it went, or was held to go later."""
    try:
        done = subprocess.run([str(paths.BIN / "assistant"), "notify"], input=text, text=True, capture_output=True,
                              timeout=NOTIFY_TIMEOUT)
    except (subprocess.SubprocessError, OSError) as e:
        log(f"can't tell the owner: {e}")
        return False
    if done.returncode:
        log(f"can't tell the owner: {done.stderr.strip()}")
        return False
    log(f"told the owner: {text.splitlines()[0]}")
    return True
