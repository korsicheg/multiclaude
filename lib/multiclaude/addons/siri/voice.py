"""The siri add-in's voice keys and reply mailbox. An Apple Shortcut posts the spoken words, with the
person's voice key, to the relay, which passes them to the router with the key's hash. The router
types them into that person's assistant, which answers with `tg voice ID TEXT`, and the router
puts the answer back on the relay for Siri to speak.

Keys are stored only as SHA-256 hashes, in state/voice-keys.json ({hash: name}). Each request
leaves state/voice/<id>.json (who asked, and the Telegram record of the question) and, once
answered, <id>.reply."""
import hashlib
import re
import secrets
import time

from multiclaude import jsonfile, paths

KEYS = paths.STATE / "voice-keys.json"
MAILBOX = paths.STATE / "voice"
REQUEST_ID_RE = re.compile(r"[0-9a-f]{8}")
REQUEST_KEPT_FOR = 60 * 60
POLL = 0.2


def digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(name):
    """A fresh voice key for name, replacing the one they had; only its hash is kept."""
    key = secrets.token_urlsafe(32)

    def replace(keys):
        drop_keys_of(keys, name)
        keys[digest(key)] = name

    jsonfile.edit_object(KEYS, replace)
    return key


def revoke(name):
    """Forget name's voice key; False if they had none."""
    return jsonfile.edit_object(KEYS, lambda keys: drop_keys_of(keys, name), create=False)


def drop_keys_of(keys, name):
    for hashed in [h for h, owner in keys.items() if owner == name]:
        del keys[hashed]


def has_key(name):
    return name in jsonfile.read_object(KEYS).values()


def owner(key_hash):
    """Whose voice key has this SHA-256 hash, or None."""
    return jsonfile.read_object(KEYS).get(key_hash) if key_hash else None


def open_request(request_id, name, msg_id):
    """Start the spoken request request_id (the relay's) from name, recorded in Telegram as msg_id."""
    forget_old_requests()
    jsonfile.write_json(MAILBOX / f"{request_id}.json", {"user": name, "msg_id": msg_id, "since": time.time()})


def post_reply(request_id, text):
    """Leave the assistant's answer for the router, which waits for it (await_reply). The request's record,
    or {} when it is unknown or already forgotten."""
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError(f"not a voice request id: {request_id}")
    request = jsonfile.read_object(MAILBOX / f"{request_id}.json")
    if request:
        reply = MAILBOX / f"{request_id}.reply"
        reply.with_suffix(".tmp").write_text(text)
        reply.with_suffix(".tmp").replace(reply)
    return request


def await_reply(request_id, timeout):
    """The answer to a request once it arrives, or None after timeout seconds."""
    reply = MAILBOX / f"{request_id}.reply"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reply.exists():
            return reply.read_text()
        time.sleep(POLL)
    return None


def forget_old_requests():
    if not MAILBOX.is_dir():
        return
    for path in MAILBOX.iterdir():
        if time.time() - path.stat().st_mtime > REQUEST_KEPT_FOR:
            path.unlink(missing_ok=True)
