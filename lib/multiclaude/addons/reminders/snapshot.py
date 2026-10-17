"""A person's open reminders as their phone's Reminders shortcut sent them last, kept in
FOLDER/<telegram id>.json as {"updated": <Unix time of the sync>, "reminders": [...]}, each reminder
its "title" and whichever of "list", "notes", "due", "priority", "flagged" and "completed" it has.
What the last sync sent, as it came, stays next to it (<telegram id>.sent.txt) for checking a
shortcut that sends something unexpected.

Shortcuts can't be relied on for the shape of a list of reminders, so what it sends is read
leniently: JSON {"reminders": …} or a bare list, whose reminders are objects, objects written one
after another as text (how Shortcuts turns a list of dictionaries into text), or titles; plain text
is one title per line. Field names are matched whatever their case, and empty fields are left out."""
import json

from multiclaude import jsonfile, paths

FOLDER = paths.STATE / "reminders"
TEXT_FIELDS = ("list", "notes", "due", "priority")
FLAGS = ("flagged", "completed")
YES = {"true", "yes", "1"}
NO_PRIORITY = {"0", "none"}


def path(chat_id):
    return FOLDER / f"{chat_id}.json"


def sent_path(chat_id):
    return FOLDER / f"{chat_id}.sent.txt"


def latest(chat_id):
    """{"updated", "reminders"} from the last sync, or None before the first."""
    kept = jsonfile.read_json(path(chat_id))
    return kept if isinstance(kept, dict) and isinstance(kept.get("reminders"), list) else None


def keep(chat_id, sent, received):
    """Keep what a sync received at received (a Unix time) sent as the person's latest reminders,
    unless a later sync's are already kept: the reminders read from it, or None when it was older."""
    jsonfile.write_atomically(sent_path(chat_id), sent)
    reminders = reminders_in(sent)
    return reminders if jsonfile.write_newer(path(chat_id), {"updated": int(received), "reminders": reminders}) else None


def reminders_in(sent):
    """The reminders in what a shortcut sent, in the snapshot's form."""
    try:
        data = json.loads(sent)
    except ValueError:
        data = sent
    if isinstance(data, dict):
        data = lowercase_keys(data).get("reminders", data)
    return [found for found in map(reminder, items(data)) if found]


def items(data):
    """The reminders, not yet read, in a list, a text or a single object."""
    if isinstance(data, list):
        return [item for element in data for item in (items(element) if isinstance(element, (list, str)) else [element])]
    if isinstance(data, str):
        return values_or_lines(data)
    return [data]


def values_or_lines(text):
    """The JSON values written one after another in text, or, when it isn't that, its lines."""
    decoder, values, at = json.JSONDecoder(), [], 0
    text = text.strip()
    while at < len(text):
        try:
            value, at = decoder.raw_decode(text, at)
        except ValueError:
            return [line.strip() for line in text.splitlines() if line.strip()]
        values.append(value)
        while at < len(text) and text[at].isspace():
            at += 1
    return values


def reminder(item):
    """A reminder in the snapshot's form, or None without a title."""
    if isinstance(item, (str, int, float)) and not isinstance(item, bool):
        item = {"title": item}
    if not isinstance(item, dict):
        return None
    fields = lowercase_keys(item)
    title = text_of(fields.get("title"))
    if not title:
        return None
    found = {"title": title}
    for name in TEXT_FIELDS:
        value = text_of(fields.get(name))
        if value and not (name == "priority" and value.lower() in NO_PRIORITY):
            found[name] = value
    found.update({name: True for name in FLAGS if is_yes(fields.get(name))})
    return found


def lowercase_keys(fields):
    return {str(key).strip().lower(): value for key, value in fields.items()}


def text_of(value):
    return "" if value is None or isinstance(value, (bool, dict, list)) else str(value).strip()


def is_yes(value):
    return value is True or (isinstance(value, (str, int)) and str(value).strip().lower() in YES)
