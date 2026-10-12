"""The people the bot serves, from ~/assistant/users.json, keyed by name:
{"ada": {"telegram_id": "…", "display": "Ada", "role": "owner", …}}. User has the fields the core
uses; add-ins read their own from the entry as it is (User.get), and other fields are ignored."""
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from multiclaude import jsonfile, paths


@dataclass(frozen=True)
class User:
    name: str
    chat_id: str  # their Telegram id, which is also the id of their private chat with the bot
    display: str
    role: str = "member"  # or "owner", the one who runs the VM
    timezone: str = "UTC"  # an IANA name; the VM itself runs on UTC
    entry: dict = field(default_factory=dict, compare=False, repr=False)  # their users.json entry as it is

    def get(self, key, default=None):
        """A field of their users.json entry, such as an add-in's own ("quiet_hours")."""
        return self.entry.get(key, default)

    @property
    def workspace(self):
        return paths.workspace(self.name)

    @property
    def pane(self):
        """Their assistant's tmux session."""
        return f"u-{self.name}"


def load_users():
    """{name: User}. OSError or ValueError if users.json can't be read."""
    with open(paths.USERS) as f:
        raw = json.load(f)
    return {name: User(name, str(info.get("telegram_id", "")), info.get("display") or name, info.get("role") or "member",
                       info.get("timezone") or "UTC", dict(info))
            for name, info in sorted(raw.items())}


def update(name, field, value):
    """Set one field of name's own entry in users.json; an empty value or False removes it.
    KeyError if they have no entry."""
    def change(data):
        entry = data.get(name)
        if not isinstance(entry, dict):
            raise KeyError(name)
        if value in ("", None, False):
            entry.pop(field, None)
        else:
            entry[field] = value

    jsonfile.edit_object(paths.USERS, change, create=False)


def owner(users):
    return next((u for u in users.values() if u.role == "owner"), None)


def by_chat(chat):
    """The person whose private chat with the bot this is, or None (also if users.json can't be read)."""
    try:
        return next((u for u in load_users().values() if u.chat_id == str(chat)), None)
    except (OSError, ValueError):
        return None


def local_time(chat, when):
    """The time when (a Unix time) as "09:18" in the timezone of the person whose chat this is,
    or as "06:18 UTC" when they have none."""
    person = by_chat(chat)
    try:
        zone = ZoneInfo(person.timezone if person else "UTC")
    except (ValueError, ZoneInfoNotFoundError):
        zone = ZoneInfo("UTC")
    local = datetime.fromtimestamp(when, timezone.utc).astimezone(zone)
    return local.strftime("%H:%M") + (" UTC" if str(zone) == "UTC" else "")
