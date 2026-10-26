"""What the bot tells people about itself: the command menu, the "What can this bot do?" box
Telegram shows in an empty chat (the bot's description), and /help. The last part of /help,
what the person's assistant can do with their MCP servers, is their own and changes as they add
servers, so the router has the assistant write it from the tools it has right then (mcp_note)."""
import re
import time

from multiclaude import addons, bot
from multiclaude.channels import TelegramChat

COMMANDS = [
    {"command": "clear", "description": "Clear my conversation context and our chat history"},
    {"command": "restart", "description": "Restart my session with a new conversation, a harder /clear"},
    {"command": "stop", "description": "Stop what I'm doing, like Esc in the terminal"},
    {"command": "usage", "description": "Show context, session cost and plan limits"},
    {"command": "model", "description": "Show or switch model: /model opus [default]"},
    {"command": "effort", "description": "Show or switch effort: /effort high [default]"},
    {"command": "plugin", "description": "Plugins and marketplaces: /plugin install name@marketplace"},
    {"command": "skills", "description": "List skills, or /skills off name"},
    {"command": "mcp", "description": "Your MCP servers: /mcp add, /mcp login name"},
    {"command": "config", "description": "Control panel: settings, scheduled jobs, MCP servers, plugins, skills, usage"},
    {"command": "help", "description": "What you can send me, the commands, what I can do with your MCPs"},
]
# What reaches the assistant from Telegram (router/inbound.py); add-ins add what they bring, and
# what their own apps take (help_info).
CAN_SEND = {
    TelegramChat.app: ["text, in any language", "photos and files, audio and video", "a reply to any earlier message",
                       "a reaction to any message"],
}
DESCRIPTION_MAX = 512        # Telegram's limits for setMyDescription
SHORT_DESCRIPTION_MAX = 120  # and setMyShortDescription
# A command with its arguments in a menu entry, such as "/model opus [default]": code in /help,
# because a bare /command there is a link that sends it at once.
EXAMPLE_RE = re.compile(r"/\w+(?: [\w@\[\]]+)*")


def description(owner):
    """The "What can this bot do?" box. owner: the VM owner's display name, or None."""
    return (f"{bot.full_name()}: Claude on {whose_vm(owner)}. I take on tasks, keep working on them "
            "and report back.\n\n" + can_send(TelegramChat.app) + "\n\n"
            "Send /help for the commands and what I can do with your MCP servers.")


def short_description(owner):
    """Shown on the bot's profile and when it's shared: the ways to reach it, Telegram's and the add-ins'."""
    ways = [TelegramChat.app, *addon_help(None, TelegramChat.app, "ways")]
    return f"{bot.full_name()}. Claude on {whose_vm(owner)}, on {listed(ways)}."


def whose_vm(owner):
    return f"{owner}'s VM" if owner else "this VM"


def can_send(app, user=None):
    kinds = CAN_SEND.get(app, []) + addon_help(user, app, "can_send")
    return "You can send me:\n" + "\n".join(f"• {kind}" for kind in kinds)


def addon_help(user, app, part):
    """What the add-ins that are on add to a part of /help ("can_send" or "ways")."""
    return [item for info in addons.call("help_info", user, app) for item in (info or {}).get(part, ())]


def help_text(user):
    """/help for user (a router Session), in the app they asked from: what they can send, the other
    ways to reach the bot they have, and the commands. Their MCP servers come next."""
    app = user.reply_to.app
    commands = "\n".join(f"`/{c['command']}` — {EXAMPLE_RE.sub(lambda m: f'`{m[0]}`', c['description'])}"
                         for c in COMMANDS)
    parts = [can_send(app, user), other_ways(user, app), "Commands:\n" + commands, "What I can do with your MCP servers "
             + ("comes next." if user.idle() else "comes once I finish what I'm doing.")]
    return "\n\n".join(part for part in parts if part)


def other_ways(user, app):
    ways = ([TelegramChat.app] if app != TelegramChat.app else []) + addon_help(user, app, "ways")
    return f"You can also reach me through {listed(ways)}." if ways else ""


def listed(items):
    """Items in a sentence: "A", "A and B", "A, B and C"."""
    return ", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else "".join(items)


def mcp_note(user, now):
    """The line typed into the assistant to have it send the MCP part of /help."""
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))
    return (f"[Router · {stamp}] {user.display} sent /help and already has what they can send me and the "
            f"commands. Now send them one more message with `{user.reply_to.cli} send`, headed **What I can do "
            "with your MCP servers**: one line for each MCP server you have (your own, your plugins' and claude.ai connectors "
            "alike) saying in plain words what it lets you do for them, with the accounts it reaches, the most "
            "useful first. Mark any server that isn't connected. Nothing else in the message.")
