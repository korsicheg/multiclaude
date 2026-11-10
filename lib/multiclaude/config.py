"""The owner's settings for the bot, in ~/assistant/config.json (not in git):
{"name": "Ada", "aka": "Ада", "username": "ada_bot", "addons": ["voice-notes", "quiet-hours"],
"health": {"ram": {"at": 95}}, "hibernate_after_minutes": 30, "frontdesk": "router"}. `assistant
setup` writes the name and username from Telegram, the owner may change the name and add "aka", a
second name, `assistant addon enable|disable` keeps the list of add-ins, and the owner may change
when the machine's health alerts (health/alerts.py), have idle assistants put to sleep (asleep.py)
and have the router poll Telegram itself (frontdesk)."""
from multiclaude import jsonfile, paths


def read():
    """The settings, {} if there are none."""
    config = jsonfile.read_json(paths.CONFIG, {})
    return config if isinstance(config, dict) else {}


def frontdesk():
    """Who hears Telegram ("frontdesk"): "plugin", the front desk, a Claude Code session hosting
    the Telegram plugin, unless the owner set "router", the router polling the Bot API itself
    (router/telegram_entrance.py). bin/common.sh reads it the same way."""
    return "router" if read().get("frontdesk") == "router" else "plugin"


def hibernate_after():
    """How many seconds an assistant must have been idle before it's put to sleep
    ("hibernate_after_minutes"); None, which is never, unless the owner set it."""
    minutes = read().get("hibernate_after_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
        return None
    return minutes * 60
