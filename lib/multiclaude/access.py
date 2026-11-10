"""Who may reach the bot on Telegram: ~/.claude/channels/telegram/access.json, kept in the Telegram
plugin's own form, so the plugin (in the front desk) and the router (when it polls Telegram
itself, router/telegram_entrance.py) share one file, and `assistant invite` and `assistant pair`
work with either:
{"dmPolicy": "pairing" | "allowlist" | "disabled", "allowFrom": [sender ids], "pending": {code:
{"senderId", "chatId", "createdAt", "expiresAt", "replies"}}, "groups": {chat id:
{"requireMention", "allowFrom"}}, "mentionPatterns": [regex], "ackReaction": "👀"}.
Nothing here changes who is allowed: the owner does that on the machine. gate() only hands out
and counts pairing codes, as the plugin does."""
import json
import os
import re
import secrets
import time
from typing import NamedTuple

from multiclaude import paths
from multiclaude.log import log

FILE = paths.TELEGRAM_DIR / "access.json"
# `assistant pair` leaves <sender id> here for each person it approved, to be told they're paired.
APPROVED = paths.TELEGRAM_DIR / "approved"
CODE_VALID_FOR = 3600  # seconds a pairing code can be approved
PENDING_MAX = 3        # codes waiting at once; more strangers get no answer
REPLIES_MAX = 2        # times a stranger is told their code: at first, and one reminder


class Verdict(NamedTuple):
    """What to do with a message: "deliver" it, "drop" it, or answer it with a pairing code ("pair")."""
    action: str
    access: dict
    code: str = ""
    resend: bool = False  # the code was handed out before


def read():
    """The access file with its defaults filled in: pairing, nobody allowed. One that can't be
    parsed is moved aside and counts as none, as the plugin has it."""
    try:
        with open(FILE) as f:
            kept = json.load(f)
        if not isinstance(kept, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        kept = {}
    except OSError as e:
        log(f"can't read {FILE}: {e}")
        kept = {}
    except ValueError:
        aside = f"{FILE}.corrupt-{int(time.time() * 1000)}"
        try:
            os.replace(FILE, aside)
        except OSError:
            pass
        log(f"{FILE} couldn't be read as JSON, so it was moved to {aside}; starting with nobody allowed")
        kept = {}
    return {**kept, "dmPolicy": kept.get("dmPolicy") or "pairing", "allowFrom": [str(i) for i in kept.get("allowFrom") or []],
            "groups": kept.get("groups") or {}, "pending": kept.get("pending") or {}}


def save(access):
    """Replace the file at once, readable by the bot's user only."""
    paths.TELEGRAM_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = f"{FILE}.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(json.dumps(access, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, FILE)


def allowed(access, sender):
    """Whether sender (a Telegram user id) is paired."""
    return str(sender) in access["allowFrom"]


def prune(access, now):
    """Drop the pairing codes that have expired. Whether there were any."""
    expired = [code for code, entry in access["pending"].items() if entry.get("expiresAt", 0) < now * 1000]
    for code in expired:
        del access["pending"][code]
    return bool(expired)


def pending_code(access, sender):
    """The pairing code waiting for sender, or None."""
    return next((code for code, entry in access["pending"].items() if entry.get("senderId") == str(sender)), None)


def current(now=None):
    """The access file as it stands now, expired pairing codes dropped from it."""
    access = read()
    if prune(access, time.time() if now is None else now):
        save(access)
    return access


def gate(message, username, now=None):
    """The Verdict on a message (a Bot API Message), username being the bot's own. A private
    chat: delivered from a paired sender; from anyone else dropped, unless the bot is open to
    pairing, when they get a code (PENDING_MAX at once, each told REPLIES_MAX times). A group:
    delivered when the file lists it, from the senders it allows, and when it asks for a mention
    only with one."""
    now = time.time() if now is None else now
    access = read()
    pruned = prune(access, now)
    verdict = decide(access, message, username, now)
    if pruned or verdict.action == "pair":
        save(access)
    return verdict


def decide(access, message, username, now):
    sender, chat = message.get("from"), message.get("chat") or {}
    if access["dmPolicy"] == "disabled" or not sender:
        return Verdict("drop", access)
    sender_id = str(sender["id"])
    if chat.get("type") == "private":
        if allowed(access, sender_id):
            return Verdict("deliver", access)
        if access["dmPolicy"] == "allowlist":
            return Verdict("drop", access)
        return pair(access, sender_id, str(chat["id"]), now)
    if chat.get("type") in ("group", "supergroup"):
        policy = access["groups"].get(str(chat["id"]))
        if not isinstance(policy, dict):
            return Verdict("drop", access)
        only = [str(i) for i in policy.get("allowFrom") or []]
        if only and sender_id not in only:
            return Verdict("drop", access)
        if policy.get("requireMention", True) and not mentioned(message, username, access.get("mentionPatterns")):
            return Verdict("drop", access)
        return Verdict("deliver", access)
    return Verdict("drop", access)


def pair(access, sender_id, chat_id, now):
    """A stranger while the bot is open to pairing: their code again, once, or a new one."""
    if code := pending_code(access, sender_id):
        entry = access["pending"][code]
        if (entry.get("replies") or 1) >= REPLIES_MAX:
            return Verdict("drop", access)
        entry["replies"] = (entry.get("replies") or 1) + 1
        return Verdict("pair", access, code, resend=True)
    if len(access["pending"]) >= PENDING_MAX:
        return Verdict("drop", access)
    code = secrets.token_hex(3)
    access["pending"][code] = {"senderId": sender_id, "chatId": chat_id, "createdAt": int(now * 1000),
                               "expiresAt": int((now + CODE_VALID_FOR) * 1000), "replies": 1}
    return Verdict("pair", access, code)


def mentioned(message, username, patterns=None):
    """Whether a group message is meant for the bot: it names it, replies to it, or matches one
    of the owner's patterns."""
    text = message.get("text") or message.get("caption") or ""
    name = (username or "").lower()
    for entity in message.get("entities") or message.get("caption_entities") or []:
        if entity.get("type") == "mention" and name:
            if utf16_slice(text, entity["offset"], entity["length"]).lower() == f"@{name}":
                return True
        user = entity.get("user") or {}
        if entity.get("type") == "text_mention" and user.get("is_bot") and (user.get("username") or "").lower() == name:
            return True
    replied = ((message.get("reply_to_message") or {}).get("from") or {}).get("username") or ""
    if name and replied.lower() == name:
        return True
    for pattern in patterns or []:
        try:
            if re.search(pattern, text, re.I):
                return True
        except re.error:
            pass  # the owner's pattern isn't a regex; skip it
    return False


def utf16_slice(text, offset, length):
    """The part of text an entity covers: Telegram counts in UTF-16 code units."""
    units = text.encode("utf-16-le")
    return units[2 * offset:2 * (offset + length)].decode("utf-16-le", errors="ignore")


def take_approved():
    """The sender ids `assistant pair` approved since the last call (APPROVED), each to be told once."""
    try:
        names = sorted(os.listdir(APPROVED))
    except OSError:
        return []
    for name in names:
        try:
            os.remove(APPROVED / name)
        except OSError:
            pass
    return names
