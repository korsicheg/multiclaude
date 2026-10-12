"""Where the router answers a person: the chat their latest message came from. Telegram is the
default, and the only place for what nobody asked for just then (jobs, alerts, update notes). An
add-in that brings another app gives its messages a Chat of its own, so the router's replies and
notices for that request go back there."""
from abc import ABC, abstractmethod

from multiclaude import paths, questions, telegram
from multiclaude.log import log

CLEARED = "🧹 Context cleared. Starting fresh."


class Chat(ABC):
    """A person's chat with the bot in one app."""
    app = ""  # the app's name: in message headers and /help, and how add-ins tell apps apart
    cli = ""  # the command the assistant answers there with

    def __init__(self, chat_id):
        self.id = chat_id

    @abstractmethod
    def send(self, text, copy=(), quiet=False):
        """Post text (the router's plain text, with `code`); copy: what the person may paste next."""

    @abstractmethod
    def ask(self, question, options, kind):
        """A question of the router's own, with one button per option; router/asks.py hears the
        tap. Its message id, or None if it couldn't be sent."""

    @abstractmethod
    def edit(self, message_id, text):
        """Replace a message's text; its buttons go."""

    def cleared_note(self):
        """What the person hears once /clear has worked."""
        return CLEARED


class TelegramChat(Chat):
    app = "Telegram"
    cli = "tg"

    def send(self, text, copy=(), quiet=False):
        return telegram.send(self.id, text, copy, **({"disable_notification": True} if quiet else {}))

    def ask(self, question, options, kind):
        result = telegram.api("sendRichMessage", chat_id=self.id, rich_message={"markdown": question},
                              reply_markup=questions.option_buttons(options))
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id:
            telegram.ChatHistory(self.id).remember(message_id)
            keep_question(paths.ASKS / str(self.id), message_id, question, options, kind)
        return message_id

    def edit(self, message_id, text):
        telegram.api("editMessageText", chat_id=self.id, message_id=message_id, rich_message={"markdown": text})


def keep_question(folder, message_id, question, options, kind):
    """Keep a question of the router's own, so a tap on it is understood (questions.save)."""
    try:
        questions.save(folder, message_id, question, options, kind)
    except OSError as e:
        log(f"can't save question {message_id}, so a tap on it won't be understood: {e}")
