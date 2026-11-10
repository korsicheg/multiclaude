"""Assistants asleep: with "hibernate_after_minutes" in config.json, the router ends the whole tmux
session of an assistant that has been idle that long (router/hibernate.py) and starts it again
for its person's next message, so a machine short of memory holds only the assistants in use.
state/asleep/<name> marks one put to sleep on purpose: `assistant start-users` leaves it be, the
health alerts don't count it as down, and `assistant wake <name>` (or the router) takes the mark
off as it starts it."""
import time

from multiclaude import paths


def marker(name):
    return paths.ASLEEP / name


def marked(name):
    """Whether name's assistant was put to sleep and hasn't been woken since."""
    return marker(name).exists()


def mark(name):
    paths.ASLEEP.mkdir(parents=True, exist_ok=True)
    marker(name).write_text(f"{int(time.time())}\n")


def unmark(name):
    marker(name).unlink(missing_ok=True)
