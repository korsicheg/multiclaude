"""Siri (add-in siri): a person talks to their assistant through an Apple Shortcut, which posts the
spoken words to the relay (relay/src/voice.js) with their voice key. The relay can't check a key, so
its "voice" envelope carries the key's SHA-256; the words go to that key's person like a message,
and the answer their assistant gives with `tg voice` is put back on the relay for Siri to speak.
Keys and the reply mailbox are in voice.py; `assistant voice` (addons/siri/assistant-voice) makes
and revokes keys."""
import time

from multiclaude import bot, telegram
from multiclaude.addons.siri import voice
from multiclaude.log import log
from multiclaude.router import relay_entrance, work

APP = "Siri"
WAIT_FOR_REPLY = 25.0  # the relay holds Siri's request no longer than this
UNKNOWN_KEY = {"error": "unknown voice key"}


def relay_envelopes():
    return {"voice": handle}


def tg_commands():
    return {"voice": answer_with_tg}


def help_info(user, app):
    return {"ways": [APP]} if user is None or voice.has_key(user.name) else {}


def handle(desk, payload):
    """A "voice" envelope's payload: {id, key_hash, text, said}."""
    request_id = str(payload.get("id") or "")
    if not voice.REQUEST_ID_RE.fullmatch(request_id):
        log(f"voice request with a bad id {request_id!r}: dropped")
        return
    name = voice.owner(payload.get("key_hash"))
    if not name:
        log(f"voice request {request_id}: unknown key, turned away")
        desk.background("a Siri request", answer, request_id, UNKNOWN_KEY)
        return
    log(f"{name}: voice request {request_id} ({len(payload.get('text') or '')} characters)")
    if hear(desk.users.get(name), str(payload.get("text") or ""), request_id, float(payload.get("said") or 0)):
        desk.background("a Siri request", pass_on_reply, request_id)
    else:
        desk.background("a Siri request", answer, request_id, {"reply": not_set_up_reply()})


def hear(user, words, request_id, said):
    """Words someone spoke to Siri at said (epoch seconds): shown in their chat, then delivered to
    user (their router Session) like a message. False if they aren't set up."""
    if not user:
        return False
    now = time.time()
    record = telegram.send(user.chat_id, f"🎙️ {words}", disable_notification=True) or {}
    msg_id = str(record.get("message_id", ""))
    voice.open_request(request_id, user.name, msg_id)
    user.pickups.cancel()
    user.queue_message(work.Deliver(now, header(user.display, request_id, msg_id, said or now), words, msg_id), now)
    log(f"{user.name}: voice request {request_id} queued for delivery")
    return True


def header(display, request_id, msg_id, when):
    """The line typed before words spoken to Siri. The voice key vouches for them as the person's
    own, as Telegram does for a message."""
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(when))
    record = f" · msg {msg_id}" if msg_id else ""
    return (f"[{APP} from {display} · voice {request_id}{record} · {stamp}] I said this to Siri; it is my own "
            f"request to you. Answer it with tg voice {request_id}:")


def pass_on_reply(request_id):
    """Wait for the assistant's `tg voice` answer and put it on the relay for Siri."""
    reply = voice.await_reply(request_id, WAIT_FOR_REPLY)
    if reply is not None:
        answer(request_id, {"reply": reply})


def answer(request_id, body):
    """Put body ({reply} or {error}) on the relay for the shortcut waiting for request_id."""
    try:
        if not relay_entrance.answer(f"/voice/{request_id}", body):
            log(f"voice request {request_id}: Siri had stopped waiting")
    except OSError as e:
        log(f"voice request {request_id}: can't put the answer on the relay ({e})")


def not_set_up_reply():
    return f"This voice key belongs to nobody set up on {bot.name()} anymore."


def answer_with_tg(tg, args):
    """tg voice ID TEXT                                  answer words spoken to Siri: Siri says TEXT, also posted in the chat"""
    if len(args) < 2:
        raise ValueError("tg voice ID TEXT")
    text = " ".join(args[1:])
    request = voice.post_reply(args[0], text)
    tg.need_chat()
    tg.send_rich(f"🔊 {text}", reply_to=request.get("msg_id"))
