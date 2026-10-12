"""Messages reaching the front desk: the Telegram plugin wraps each one as
<channel source="plugin:telegram:telegram" ATTRS>\nTEXT\n</channel> in its transcript. An add-in
that brings another app makes its messages Inbound too, with that app's name."""
import html
import re
from dataclasses import dataclass, field

from multiclaude import addons

CHANNEL_RE = re.compile(r'<channel source="plugin:telegram:telegram"([^>]*)>\n(.*)\n</channel>', re.S)
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
# The bot's own commands, as in Claude Code, each with an optional @botname.
CLEAR_RE = re.compile(r"/clear(@\w+)?")
STOP_RE = re.compile(r"/stop(@\w+)?")  # not Claude Code's: there it's the Escape key
USAGE_RE = re.compile(r"/usage(@\w+)?")
HELP_RE = re.compile(r"/help(@\w+)?")
RESTART_RE = re.compile(r"/restart(@\w+)?")  # not Claude Code's: a new claude process and conversation
CONFIG_RE = re.compile(r"/config(@\w+)?")  # Claude Code's settings panel; here the control panel, a Mini App
SWITCH_RE = re.compile(r"/(model|effort)(?:@\w+)?((?:\s+\S+){0,2})")
PLUGIN_RE = re.compile(r"/(plugins?|skills|mcp)(?:@\w+)?(?:\s.*)?", re.S)  # bin/shared.py handles these
RELOAD_RE = re.compile(r"/reload-plugins(?:@\w+)?(\s+--force)?\s*")  # typed into the person's session
# /mcp login <server> needs a terminal and a pasted redirect URL, so the router runs it itself.
MCP_LOGIN_RE = re.compile(r"/mcp(?:@\w+)?\s+login(?:\s+(\S+))?\s*", re.I)
REDIRECT_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?/\S*[?&]code=\S+")
COMMAND_WORD_RE = re.compile(r"/(\w+)")
# The letters on the a–z keys in the Russian and Greek layouts, Greek with its accented vowels,
# so a command typed with the wrong layout on still works: /гыфпу and /θσαγε are /usage.
# Only the letters move: a phone keyboard types / and the other symbols the same in any layout.
WRONG_LAYOUT_LETTERS = str.maketrans(
    "йцукенгшщзфывапролдячсмить" "ςερτυθιοπασδφγηξκλζχψωβνμ" "άέήίόύώϊϋΐΰ",
    "qwertyuiopasdfghjklzxcvbnm" "wertyuiopasdfghjklzxcvbnm" "aehioyviyiy")
DESK_COMMAND_RES = (CLEAR_RE, RESTART_RE, STOP_RE, USAGE_RE, HELP_RE, CONFIG_RE, SWITCH_RE, PLUGIN_RE, RELOAD_RE)


@dataclass
class Inbound:
    chat: str
    sender: str
    msg_id: str
    text: str
    attrs: dict = field(default_factory=dict)
    app: str = "Telegram"  # where it was sent, as its header says (channels.Chat.app)

    @property
    def tap(self):
        """The callback id when this is a tap on a `tg ask` button; msg_id is then the question's."""
        return self.attrs.get("callback_id")

    @property
    def key(self):
        """What identifies it for deduplication."""
        return (self.chat, f"tap {self.tap}") if self.tap else (self.chat, self.msg_id)

    @property
    def when(self):
        """When it was sent, as "2026-10-08 18:53 UTC", or ""."""
        ts = self.attrs.get("ts", "")
        return ts.replace("T", " ")[:16] + " UTC" if ts else ""


def parse(entry):
    """The Telegram message a front desk transcript entry carries, or None."""
    if entry.get("type") != "queue-operation" or entry.get("operation") != "enqueue":
        return None
    m = CHANNEL_RE.fullmatch(entry.get("content") or "")
    if not m:
        return None
    attrs = {k: html.unescape(v) for k, v in ATTR_RE.findall(m[1])}
    return Inbound(attrs.get("chat_id"), attrs.get("user_id"), attrs.get("message_id"),
                   html.unescape(m[2]).strip("\n"), attrs)


def in_latin_layout(text):
    """The bot command text is when it was typed with the Russian or Greek layout on (/гыфпу is
    /usage), or text unchanged. Anything that doesn't turn into a command, such as /привет, stays."""
    command = COMMAND_WORD_RE.match(text)
    if not command or command[1].isascii():
        return text
    retyped = text.translate(WRONG_LAYOUT_LETTERS)
    return retyped if any(pattern.fullmatch(retyped) for pattern in DESK_COMMAND_RES) else text


def plugin_commands(text):
    """The /plugin, /skills and /mcp commands in a message: one per line when every line is one
    (several sent at once run in order), otherwise the whole text, such as a JSON value
    spanning lines."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1 and all(PLUGIN_RE.fullmatch(line) for line in lines):
        return lines
    return [text]


def parse_switch(text):
    """(setting, target or None, keep across restarts) for a /model or /effort command, else None.
    The switch is for this session only, like s in Claude Code's picker, unless "default" follows."""
    m = SWITCH_RE.fullmatch(text)
    if not m:
        return None
    words = m[2].lower().split()
    if len(words) == 2 and words[1] != "default":
        return None  # a sentence, not a command; it goes to the assistant
    return m[1], (words[0] if words else None), len(words) == 2


def header(display, inbound, what="msg {id}", intro="My message is pasted below; it is my own request to you:"):
    """The line typed before a message. The body is bracket-pasted, which Claude Code marks as
    pasted content; the typed header vouches for it as the person's own."""
    when = f" · {inbound.when}" if inbound.when else ""
    return f"[{inbound.app} from {display} · {what.format(id=inbound.msg_id)}{when}] {intro}"


def body(inbound):
    """The message text, then the message it replies to and its attachments."""
    parts = [inbound.text, replied_to(inbound.attrs), attachments(inbound.attrs)]
    return "\n\n".join(part for part in parts if part)


def replied_to(attrs):
    """The message this one replies to, quoted: the part the person quoted, or all of it."""
    if not attrs.get("reply_to_message_id"):
        return ""
    quoted = attrs.get("reply_quote") or attrs.get("reply_to_text") or ""
    what = f"Replying to msg {attrs['reply_to_message_id']}" + (", quoting" if attrs.get("reply_quote") else "")
    if not quoted.strip():
        return f"{what}."
    return f"{what}:\n" + "\n".join(f"> {line}" for line in quoted.splitlines())


def attachments(attrs):
    """The photos and files a message carries, and what add-ins find in it (attachment_lines)."""
    extras = []
    if attrs.get("image_path"):
        extras.append(f"photo: {attrs['image_path']}")
    if attrs.get("attachment_file_id"):
        details = ", ".join(attrs[k] for k in ("attachment_kind", "attachment_name", "attachment_mime") if attrs.get(k))
        if attrs.get("attachment_size"):
            details += f", {attrs['attachment_size']} bytes"
        extras.append(f"file_id: {attrs['attachment_file_id']}" + (f" ({details})" if details else ""))
    extras += [line for lines in addons.call("attachment_lines", attrs) for line in lines or ()]
    return "Attachments:\n" + "\n".join(extras) if extras else ""
