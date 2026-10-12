"""Claude Code's approval dialogs, relayed to Telegram. Even with bypassPermissions a few built-in
safety checks stop and ask in the terminal, which nobody watches: the session's worker sends the
person the question with Approve and Deny buttons (`ask:<n>`, so the patched plugin passes the
tap on) and presses their answer."""
from dataclasses import dataclass

from multiclaude import bot, jsonfile, paths, questions, telegram
from multiclaude.log import log
from multiclaude.router import claude_ui

APPROVE, DENY = "✅ Approve", "🚫 Deny"
WORDS = {APPROVE: "Yes", DENY: "No"}  # the dialog's options
CHECK_EVERY = 1.0   # how often a working assistant's screen is checked for a dialog
SHOWN_MAX = 2500    # characters of the dialog shown on Telegram


@dataclass
class Pending:
    """A dialog relayed to the person and not closed yet."""
    message_id: int       # the Telegram question, or None if sending it failed
    blocks: list          # the question, as rich message blocks
    dialog_text: str
    answer: str = None    # APPROVE or DENY once tapped
    pressed: bool = False


def check(session):
    """Relay a dialog that just opened, press an answer that came in, and close the question
    when the dialog goes away. Runs in the session's worker thread."""
    dialog = claude_ui.approval_dialog(session.pane.screen())
    pending = session.approval
    if pending and (dialog is None or dialog.text != pending.dialog_text):
        closed(session, pending)
        pending = None
    if dialog is None:
        return
    if pending is None:
        session.approval = relay(session, dialog)
    elif pending.answer and not pending.pressed:
        press(session, dialog, pending)


def relay(session, dialog):
    deadline = f" in {dialog.deny_in // 60}:{dialog.deny_in % 60:02d}" if dialog.deny_in else ""
    text = dialog.text if len(dialog.text) <= SHOWN_MAX else dialog.text[:SHOWN_MAX] + "\n…"
    blocks = (telegram.rich_blocks(f"🔐 {bot.name()} wants to do something that Claude Code's safety check "
                                   "stopped. Approve it?")
              + [{"type": "pre", "text": text}]
              + telegram.rich_blocks(f"If you don't answer, Claude Code denies it by itself{deadline}."))
    log(f"{session.name}: approval dialog open; asking on Telegram: {dialog.text.splitlines()[0]}")
    result = telegram.api("sendRichMessage", chat_id=session.chat_id, rich_message={"blocks": blocks},
                          reply_markup=questions.option_buttons([APPROVE, DENY]))
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if message_id:
        telegram.ChatHistory(session.chat_id).remember(message_id)
        try:
            jsonfile.write_json(paths.ASKS / str(session.chat_id) / f"{message_id}.json",
                                {"blocks": blocks, "options": [APPROVE, DENY], "kind": "approval"})
        except OSError as e:
            log(f"{session.name}: can't save approval question {message_id}: {e}")
    return Pending(message_id, blocks, dialog.text)


def tapped(user, msg_id, label):
    """A tap on an approval question's button. False if its dialog had already closed."""
    pending = user.approval
    if not pending or pending.message_id != int(msg_id) or pending.answer:
        return False
    pending.answer = label
    user.wake.set()  # the worker presses it
    log(f"{user.name}: approval answered on Telegram: {label}")
    return True


def press(session, dialog, pending):
    pending.pressed = True
    if problem := claude_ui.choose(session.pane, dialog, WORDS[pending.answer]):
        log(f"{session.name}: couldn't answer the approval dialog: {problem}")
        edit(session, pending, f"⚠️ I couldn't press it: {problem}.")


def closed(session, pending):
    """The dialog went away: answered from here, at the terminal, or denied by Claude Code's timer."""
    session.approval = None
    if pending.answer or not pending.message_id:
        return
    log(f"{session.name}: approval dialog closed without an answer from Telegram")
    edit(session, pending, "⌛ It closed before you answered, so Claude Code denied it.")
    try:
        path = paths.ASKS / str(session.chat_id) / f"{pending.message_id}.json"
        jsonfile.write_json(path, {**jsonfile.read_json(path, {}), "answer": "expired"})
    except OSError:
        pass


def edit(session, pending, note):
    if pending.message_id:
        show_note(session.chat_id, pending.message_id, pending.blocks, note)


def show_note(chat, message_id, blocks, note):
    """Edit an approval question to end with note; its buttons go."""
    telegram.later(telegram.api, "editMessageText", chat_id=chat, message_id=message_id,
                   rich_message={"blocks": blocks + telegram.rich_blocks(note)})
