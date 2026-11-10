"""Telegram's updates as the router's Inbound messages, for when the router polls the Bot API
itself (telegram_entrance.py). Each comes out as the patched Telegram plugin passes it on through
the front desk (bin/patch-telegram-plugin, router/inbound.py): the same text and the same
attributes, so routing, the add-ins' hooks and what is typed into an assistant don't depend on
who polls."""
import re
import time

from multiclaude.router import rich
from multiclaude.router.inbound import Inbound

REPLY_TEXT_MAX = 3000  # characters of a replied-to message passed along
# File names and titles are the uploader's: nothing in them may pass for a line of the message.
UNSAFE_NAME_RE = re.compile(r"[<>\[\]\r\n;]")
ASK_RE = re.compile(r"ask:\d+")  # the callback data of `tg ask` and `tg checklist` buttons


def stamp(when):
    """A Unix time as the plugin writes it: 2026-10-08T18:53:12.000Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(when))


def safe_name(name):
    return UNSAFE_NAME_RE.sub("_", name) if name else None


def content(message):
    """(text, attributes, the photo to fetch or None) for a message of a kind the core passes on:
    text, a formatted message as Markdown, a photo, a file, a voice message, audio, a video, a
    video note or a sticker. None for any other (a contact, a poll, a location), which an add-in
    may still take (telegram_message)."""
    caption = message.get("caption")
    if message.get("text"):
        return message["text"], {}, None
    if message.get("rich_message"):
        return rich.markdown(message["rich_message"].get("blocks")), {}, None
    if message.get("photo"):
        return caption or "(photo)", {}, message["photo"][-1]  # the largest size is last
    if doc := message.get("document"):
        name = safe_name(doc.get("file_name"))
        return caption or f"(document: {name or 'file'})", attachment("document", doc, name), None
    if voice := message.get("voice"):
        return caption or "(voice message)", attachment("voice", voice), None
    if audio := message.get("audio"):
        name = safe_name(audio.get("file_name"))
        return caption or f"(audio: {safe_name(audio.get('title')) or name or 'audio'})", attachment("audio", audio, name), None
    if video := message.get("video"):
        return caption or "(video)", attachment("video", video, safe_name(video.get("file_name"))), None
    if note := message.get("video_note"):
        return "(video note)", attachment("video_note", note), None
    if sticker := message.get("sticker"):
        emoji = f" {sticker['emoji']}" if sticker.get("emoji") else ""
        return f"(sticker{emoji})", attachment("sticker", sticker), None
    return None


def attachment(kind, file, name=None):
    """A file's attributes: the assistant fetches it with `tg download <file_id>`."""
    attrs = {"attachment_kind": kind, "attachment_file_id": file["file_id"]}
    if file.get("file_size") is not None:
        attrs["attachment_size"] = str(file["file_size"])
    if file.get("mime_type"):
        attrs["attachment_mime"] = file["mime_type"]
    if name:
        attrs["attachment_name"] = name
    return attrs


def replied_to(message):
    """What a message replies to, and the part of it the person quoted."""
    attrs, reply = {}, message.get("reply_to_message")
    if reply:
        text = reply.get("text") or reply.get("caption") or reply.get("rich_message") or ""
        attrs = {"reply_to_message_id": str(reply["message_id"]), "reply_to_text": rich.plain_text(text)[:REPLY_TEXT_MAX]}
    if (message.get("quote") or {}).get("text"):
        attrs["reply_quote"] = message["quote"]["text"]
    return attrs


def sender_attrs(chat, sender, when, message_id=None):
    attrs = {"chat_id": str(chat["id"])}
    if message_id is not None:
        attrs["message_id"] = str(message_id)
    return {**attrs, "user": sender.get("username") or str(sender["id"]), "user_id": str(sender["id"]), "ts": stamp(when)}


def inbound(message, text, attrs):
    """The Inbound for a message, with the text and attributes found for it (content, or an add-in)."""
    return made({**sender_attrs(message["chat"], message["from"], message.get("date") or 0, message.get("message_id")),
                 **attrs, **replied_to(message)}, text)


def tap(query, now=None):
    """The Inbound for a tap on an `ask:<n>` button: the button's label as its text, and the
    question's message as its message."""
    message, data = query.get("message") or {}, query["data"]
    buttons = [button for row in (message.get("reply_markup") or {}).get("inline_keyboard") or [] for button in row]
    label = next((button.get("text") for button in buttons if button.get("callback_data") == data), None) or data
    when = time.time() if now is None else now
    return made({**sender_attrs(message["chat"], query["from"], when, message.get("message_id")),
                 "callback_id": str(query["id"]), "callback_data": data}, label)


def added_reactions(reaction):
    """(the emoji a reaction update added, the ids of the Premium custom emoji it added): what
    is in the new reaction and wasn't in the old one. Taking a reaction off adds nothing."""
    def of(kind, key, which):
        return [r[key] for r in reaction.get(which) or [] if r.get("type") == kind and r.get(key)]

    emoji = [e for e in of("emoji", "emoji", "new_reaction") if e not in of("emoji", "emoji", "old_reaction")]
    custom = [c for c in of("custom_emoji", "custom_emoji_id", "new_reaction")
              if c not in of("custom_emoji", "custom_emoji_id", "old_reaction")]
    return emoji, custom


def reacted(reaction, added):
    """The Inbound for a reaction: the emoji added as its text, and the message reacted to as its message."""
    return made({**sender_attrs(reaction["chat"], reaction["user"], reaction.get("date") or 0, reaction["message_id"]),
                 "reaction": added}, added)


def made(attrs, text):
    return Inbound(attrs["chat_id"], attrs["user_id"], attrs.get("message_id"), text, attrs)
