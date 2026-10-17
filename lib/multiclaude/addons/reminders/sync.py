"""A sync of a person's Reminders shortcut: the relay's "reminders" envelope, {id, key_hash, sent,
received}. The shortcut shows the person's reminders key, kept only as its SHA-256 (KEY_RING), and
an unknown one is turned away. What it sent becomes their latest snapshot, and the relay, holding
the shortcut's request, gets back {"add": [...]}, the reminders their assistant queued. Those count
as delivered only once the relay took the answer: when the shortcut had stopped waiting, or the
relay can't be reached, they wait for the next sync."""
import re
import time

from multiclaude import keyring, paths
from multiclaude.addons.reminders import outbox, snapshot
from multiclaude.log import log
from multiclaude.router import relay_entrance

KEYS = paths.STATE / "reminders-keys.json"
KEY_RING = keyring.KeyRing(KEYS)
REQUEST_ID_RE = re.compile(r"[0-9a-f]{8}")
UNKNOWN_KEY = {"error": "unknown reminders key"}


def handle(desk, payload):
    request_id = str(payload.get("id") or "")
    if not REQUEST_ID_RE.fullmatch(request_id):
        log(f"reminders sync with a bad id {request_id!r}: dropped")
        return
    name = KEY_RING.owner(payload.get("key_hash"))
    user = desk.users.get(name) if name else None
    if not user:
        log(f"reminders sync {request_id}: unknown key, turned away")
        desk.background("a reminders sync", answer, request_id, UNKNOWN_KEY)
        return
    kept = snapshot.keep(user.chat_id, str(payload.get("sent") or ""), received_at(payload))
    log(f"{name}: reminders sync {request_id}, " + (f"{len(kept)} open" if kept is not None else "older than the latest: not kept"))
    desk.background("a reminders sync", deliver, request_id, user)


def received_at(payload):
    """When the relay took the sync (a Unix time), or now if it doesn't say."""
    received = payload.get("received")
    return received if isinstance(received, (int, float)) and not isinstance(received, bool) else time.time()


def deliver(request_id, user):
    """Answer the sync with the reminders queued for the phone, and log them delivered once the relay
    took the answer; otherwise they go back in the queue."""
    taken = outbox.take(user.chat_id)
    delivered = False
    try:
        delivered = answer(request_id, {"add": outbox.for_shortcut(taken)})
    finally:
        if taken and not delivered:
            outbox.put_back(user.chat_id, taken)
    if taken and delivered:
        outbox.delivered(user.chat_id, taken, time.time())
        log(f"{user.name}: {len(taken)} reminder(s) went to the phone with sync {request_id}")


def answer(request_id, body):
    """Put body on the relay for the shortcut's request; whether the shortcut was still waiting for it."""
    try:
        if relay_entrance.answer(f"/reminders/{request_id}", body):
            return True
        log(f"reminders sync {request_id}: the shortcut had stopped waiting")
    except OSError as e:
        log(f"reminders sync {request_id}: can't put the answer on the relay ({e})")
    return False
