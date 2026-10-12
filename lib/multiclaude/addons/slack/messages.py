"""What arrives from Slack through the relay, as the router's Inbound messages: a DM to the bot, a
slash command, or a tap on a `slack ask` button. Their app is "Slack"; the chat is the DM's channel
id, the sender a Slack user id and the message id a Slack ts."""
import html
import re
import time
from datetime import datetime, timezone

from multiclaude.addons.slack.chat import SlackChat
from multiclaude.router.inbound import Inbound

LINK_RE = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]+))?>")
VOICE_SUBTYPE = "slack_audio"  # a voice clip recorded in Slack


def message(event):
    """A DM to the bot (an events_api envelope's event), or None for anything else: the bot's own
    messages, edits, other event kinds."""
    if event.get("type") != "message" or event.get("channel_type") != "im":
        return None
    if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
        return None
    files = [{"id": f["id"], "name": f.get("name") or f.get("title") or "", "mime": f.get("mimetype", ""),
              "size": f.get("size"), "voice": f.get("subtype") == VOICE_SUBTYPE}
             for f in event.get("files") or [] if f.get("id")]
    attrs = {"ts": iso(event["ts"]), **({"slack_files": files} if files else {})}
    return Inbound(event.get("channel"), event.get("user"), event["ts"], plain(event.get("text") or ""), attrs,
                   SlackChat.app)


def command(payload):
    """A slash command, as the text it would be on Telegram ("/model opus")."""
    text = f"{payload.get('command', '')} {payload.get('text', '')}".strip()
    attrs = {"ts": iso(time.time()), "slash": "1"}
    return Inbound(payload.get("channel_id"), payload.get("user_id"), f"cmd-{payload.get('trigger_id', '')}", text, attrs,
                   SlackChat.app)


def tap(payload):
    """A tap on a button (an interactive envelope's payload), or None for other interactions."""
    if payload.get("type") != "block_actions":
        return None
    action = (payload.get("actions") or [{}])[0]
    message_ts = (payload.get("message") or {}).get("ts") or (payload.get("container") or {}).get("message_ts")
    attrs = {"ts": iso(time.time()), "callback_id": action.get("action_ts", ""), "callback_data": action.get("action_id", "")}
    label = (action.get("text") or {}).get("text", "")
    return Inbound((payload.get("channel") or {}).get("id"), (payload.get("user") or {}).get("id"), message_ts, label,
                   attrs, SlackChat.app)


def plain(text):
    """Slack's mrkdwn as the person typed it: links as their URL, entities decoded."""
    return html.unescape(LINK_RE.sub(lambda m: m[1] if not m[2] or m[2] == m[1] else f"{m[2]} ({m[1]})", text))


def iso(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
