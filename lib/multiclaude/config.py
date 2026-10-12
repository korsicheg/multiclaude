"""The owner's settings for the bot, in ~/assistant/config.json (not in git):
{"name": "Ada", "aka": "Ада", "username": "ada_bot", "addons": ["voice-notes", "quiet-hours"]}.
`assistant setup` writes the name and username from Telegram, the owner may change the name and add
"aka", a second name, and `assistant addon enable|disable` keeps the list of add-ins."""
from multiclaude import jsonfile, paths


def read():
    """The settings, {} if there are none."""
    config = jsonfile.read_json(paths.CONFIG, {})
    return config if isinstance(config, dict) else {}
