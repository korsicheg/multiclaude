"""The bot's own name, in everything it says about itself (config.py)."""
from multiclaude import config

DEFAULT_NAME = "Claude"


def name():
    return config.read().get("name") or DEFAULT_NAME


def full_name():
    """"Ada · Ада" when the bot has a second name, else just its name."""
    aka = config.read().get("aka")
    return f"{name()} · {aka}" if aka else name()
