"""A person's DM with the Slack bot, where the router answers what they sent there (channels.Chat)."""
from multiclaude.addons.slack import api
from multiclaude.channels import Chat, keep_question
from multiclaude.log import log


class SlackChat(Chat):
    app = "Slack"
    cli = "slack"

    def __init__(self, channel, person):
        super().__init__(channel)
        self.person = person  # their Slack user id

    def send(self, text, copy=(), quiet=False):
        try:
            return api.post(self.id, api.with_code(text, copy))
        except api.SlackError as e:
            log(f"Slack: {e}")
            return None

    def ask(self, question, options, kind):
        try:
            ts = api.bot().call("chat.postMessage", channel=self.id, text=question,
                                blocks=api.question_blocks(question, options))["ts"]
        except api.SlackError as e:
            log(f"Slack: {e}")
            return None
        keep_question(api.asks_folder(self.id), ts, question, options, kind)
        return ts

    def edit(self, ts, text):
        try:
            api.bot().call("chat.update", channel=self.id, ts=ts, text=text, blocks=[{"type": "markdown", "text": text}])
        except api.SlackError as e:
            log(f"Slack: {e}")

    def cleared_note(self):
        """The bot can't delete the person's own messages here until they let it, so until they
        have, the note offers the sign-in that does."""
        note = super().cleared_note()
        if api.person_api(self.person):
            return note
        link = api.sign_in_link(self.person)
        return note + (f"\n\nYour own messages here stay: Slack lets me delete them only with your OK. "
                       f"[Sign in with Slack]({link}) once, and /clear deletes them too." if link else "")
