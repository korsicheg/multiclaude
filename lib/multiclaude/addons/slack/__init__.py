"""Slack (add-in slack): people talk to their assistant from a DM with a Slack app too: the same
assistant, answering where they wrote. Slack sends everything to the relay (relay/src/slack.js),
which holds it for the router; people are matched by "slack_id" in their users.json entry
(`assistant slack-link`). The assistants answer with addons/slack/slack, the way they use tg.

- api: Slack's Web API, the bot's DMs, wiping a DM, and "Sign in with Slack", which lets /clear
  delete a person's own messages too.
- messages: what arrives, as the router's Inbound messages; routing: where it goes.
- chat: SlackChat, where the router answers a Slack message.
- clips: voice clips recorded in Slack, for the voice-notes add-in."""
import threading

from multiclaude.addons.slack import api, routing
from multiclaude.addons.slack.chat import SlackChat

CAN_SEND = ["text, in any language", "files and images"]


def relay_envelopes():
    return {"events_api": routing.event, "slash_commands": routing.command, "interactive": routing.interaction,
            "sign_in": routing.sign_in}


def help_info(user, app):
    if app == SlackChat.app:
        return {"can_send": CAN_SEND}
    return {"ways": [SlackChat.app]} if user is None or api.slack_id(user) else {}


def attachment_lines(attrs):
    return [file_line(f) for f in attrs.get("slack_files") or []]


def after_wipe(user, before, keep):
    """Their DM with the Slack bot goes too, in the background: Slack deletes one message a call."""
    person = api.slack_id(user)
    if person and api.configured():
        threading.Thread(target=api.wipe_dm, args=(person, before, keep), daemon=True).start()


def voice_note_sources():
    from multiclaude.addons.slack import clips  # only with voice-notes on, which asks for it
    return {SlackChat.app: clips.SOURCE}


def file_line(f):
    """A file sent on Slack: `slack download` fetches it."""
    details = ", ".join(str(x) for x in (f.get("name"), f.get("mime")) if x)
    if f.get("size"):
        details += f", {f['size']} bytes"
    return f"slack file: {f['id']}" + (f" ({details})" if details else "")
