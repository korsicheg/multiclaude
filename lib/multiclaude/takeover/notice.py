"""The take-over link in the person's Telegram chat, as a Mini App button: Telegram never fetches the
page for a preview, and the page can stop Telegram's sheet from closing when a drag scrolls the
browser. The message is edited once the take-over ends. --print-link prints the link
instead (PrintedLink), for trying it out on the machine or passing it on another way."""
from multiclaude.log import log
from multiclaude.telegram import BotApi, ChatHistory, TelegramError, without_highlights

API_TIMEOUT = 30
ENDINGS = {"done": "✅ **Done**, thank you. I'm carrying on.",
           "gave-up": "🚫 **Not done:** you tapped I can't do it.",
           "expired": "⌛ **The link expired** after {minutes} minutes.",
           "closed": "⚠️ **The link stopped working:** the browser closed.",
           "stopped": "⏹️ **I stopped the take-over**, so the link no longer works."}


class TelegramLink:
    def __init__(self, chat, task, minutes):
        self.chat, self.task, self.minutes = chat, without_highlights(task), minutes
        self.api = BotApi(timeout=API_TIMEOUT)
        self.message_id = None

    def send(self, link):
        """The message with the link. It isn't held when Telegram can't be reached, since the
        link would be dead by the time it went out: TelegramError."""
        text = (f"🖐️ **Your turn in the browser**\n\n{self.task}\n\n"
                "Tap **Open the browser**, do it there, then tap **Done** on that page. "
                f"The link works for {self.minutes} minutes and is yours alone: whoever has it sees and "
                "controls this browser, so don't forward it.")
        button = {"text": "🖐️ Open the browser", "web_app": {"url": link}}
        result = self.api.call("sendRichMessage", chat_id=self.chat, rich_message={"markdown": text},
                               reply_markup={"inline_keyboard": [[button]]})
        self.message_id = result["message_id"]
        ChatHistory(self.chat).remember(self.message_id)

    def close(self, outcome):
        """Say how it ended, in place of the link. The take-over is over either way, so a failure
        is only logged."""
        ending = ENDINGS[outcome].format(minutes=self.minutes)
        try:
            self.api.call("editMessageText", chat_id=self.chat, message_id=self.message_id,
                          rich_message={"markdown": f"{ending}\n\n{self.task}"})
        except TelegramError as e:
            log(f"takeover: couldn't say how it ended: {e}")


class PrintedLink:
    def send(self, link):
        print(f"link: {link}", flush=True)

    def close(self, outcome):
        pass
