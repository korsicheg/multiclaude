"""The reminders a person's assistant queued to add on their phone, kept in
snapshot.FOLDER/<telegram id>.outbox.json until a sync of their Reminders shortcut takes them:
{"queued": [...], "delivered": [...]}. Each is {"id", "list", "title", "notes", "due", "queued"}, and
once a sync's answer took it, it moves to the last DELIVERED_KEPT delivered with "delivered", the
Unix time of that sync. The assistant (through `assistant reminders`) and the router edit the file
at once, so every edit holds its lock."""
import secrets
import time

from multiclaude import jsonfile
from multiclaude.addons.reminders import snapshot

DELIVERED_KEPT = 50
SENT_FIELDS = ("id", "list", "title", "notes", "due")  # what the shortcut gets of each


def path(chat_id):
    return snapshot.FOLDER / f"{chat_id}.outbox.json"


def edit(chat_id, change):
    """Apply change({"queued", "delivered"}) to the person's outbox under its lock; what change returns."""
    outcome = []

    def apply(data):
        data.setdefault("queued", [])
        data.setdefault("delivered", [])
        outcome.append(change(data))

    with jsonfile.locked(path(chat_id).with_suffix(".lock")):
        jsonfile.edit_object(path(chat_id), apply, create=False)
    return outcome[0]


def read(chat_id):
    """{"queued", "delivered"}, empty before anything was queued."""
    data = jsonfile.read_json(path(chat_id), {})
    data = data if isinstance(data, dict) else {}
    return {"queued": data.get("queued") or [], "delivered": data.get("delivered") or []}


def add(chat_id, title, list_name, notes="", due=""):
    """Queue a reminder for the person's phone; its id."""
    queued = {"id": secrets.token_hex(3), "list": list_name, "title": title, "notes": notes, "due": due,
              "queued": int(time.time())}
    edit(chat_id, lambda data: data["queued"].append(queued))
    return queued["id"]


def cancel(chat_id, reminder_id):
    """Take a queued reminder out; the reminder, or None if none waits with that id."""
    def take_out(data):
        found = next((r for r in data["queued"] if r.get("id") == reminder_id), None)
        if found:
            data["queued"].remove(found)
        return found

    return edit(chat_id, take_out)


def take(chat_id):
    """Everything queued, taken out for a sync's answer."""
    def take_all(data):
        taken, data["queued"] = data["queued"], []
        return taken

    return edit(chat_id, take_all)


def put_back(chat_id, taken):
    """Reminders a sync's answer didn't deliver after all, ahead of any queued since."""
    edit(chat_id, lambda data: data.update(queued=taken + data["queued"]))


def delivered(chat_id, taken, when):
    """Log reminders a sync's answer at when (a Unix time) delivered."""
    def log_delivered(data):
        data["delivered"] = (data["delivered"] + [{**r, "delivered": int(when)} for r in taken])[-DELIVERED_KEPT:]

    edit(chat_id, log_delivered)


def for_shortcut(taken):
    """Taken reminders as the shortcut gets them."""
    return [{field: r.get(field, "") for field in SENT_FIELDS} for r in taken]
