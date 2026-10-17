"""OwnTracks (part of the locations add-in): the OwnTracks app on a person's phone posts where it is
to the relay (relay/src/owntracks.js), with their OwnTracks key as its HTTP Basic password. The
relay can't check a key, so its "owntracks" envelope carries the key's SHA-256. A location, or a
region's enter or leave, from a known key becomes that person's latest position, in the same file a
Telegram live location keeps, the newer of the two winning. The relay holds the app's request for the
answer: {"ok": true}, or {"error"} for a key nobody has, which the app gets as a 401. `assistant
location` (addons/locations/assistant-location) makes and revokes keys."""
import re

from multiclaude import jsonfile, keyring, paths
from multiclaude.log import log
from multiclaude.router import relay_entrance

KEYS = paths.STATE / "owntracks-keys.json"
KEY_RING = keyring.KeyRing(KEYS)
REQUEST_ID_RE = re.compile(r"[0-9a-f]{8}")
ACCEPTED = {"ok": True}
UNKNOWN_KEY = {"error": "unknown OwnTracks key"}
SOURCE = "owntracks"
# What else a message tells, by its _type: (OwnTracks' field, the position file's)
EXTRAS = {
    "location": (("vel", "velocity_kmh"), ("batt", "battery"), ("inregions", "regions"), ("t", "trigger")),
    "transition": (("event", "event"), ("desc", "region")),
}


def handle(desk, payload, locations):
    """An "owntracks" envelope's payload: {id, key_hash, message, received}. The position goes to
    locations/<telegram id>.json."""
    request_id = str(payload.get("id") or "")
    if not REQUEST_ID_RE.fullmatch(request_id):
        log(f"OwnTracks update with a bad id {request_id!r}: dropped")
        return
    name = KEY_RING.owner(payload.get("key_hash"))
    user = desk.users.get(name) if name else None
    if not user:
        log(f"OwnTracks update {request_id}: unknown key, turned away")
        desk.background("an OwnTracks update", answer, request_id, UNKNOWN_KEY)
        return
    found = position(payload.get("message"))
    if found:
        kept = jsonfile.write_newer(locations / f"{user.chat_id}.json", found)
        accuracy = f" (±{found['accuracy_m']} m)" if "accuracy_m" in found else ""
        late = "" if kept else ", older than the latest: not kept"
        log(f"{name}: OwnTracks {payload['message']['_type']}{accuracy}{late}")
    desk.background("an OwnTracks update", answer, request_id, ACCEPTED)


def position(message):
    """Where an OwnTracks location or transition says the phone was, in the position file's form
    (as the Telegram plugin writes it, with source "owntracks"); None for any other message."""
    if not isinstance(message, dict) or message.get("_type") not in EXTRAS:
        return None
    latitude, longitude, when = message.get("lat"), message.get("lon"), message.get("tst")
    if not all(is_number(value) for value in (latitude, longitude, when)):
        return None
    found = {"latitude": latitude, "longitude": longitude}
    if is_number(message.get("acc")):
        found["accuracy_m"] = message["acc"]
    if is_number(message.get("cog")):
        found["heading"] = message["cog"]
    found.update(updated=int(when), source=SOURCE)
    found.update({ours: message[theirs] for theirs, ours in EXTRAS[message["_type"]] if theirs in message})
    return found


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def answer(request_id, body):
    """Put body ({ok} or {error}) on the relay for the app's request request_id. When the app has
    stopped waiting, its update is taken all the same."""
    try:
        relay_entrance.answer(f"/owntracks/{request_id}", body)
    except OSError as e:
        log(f"OwnTracks update {request_id}: can't put the answer on the relay ({e})")
