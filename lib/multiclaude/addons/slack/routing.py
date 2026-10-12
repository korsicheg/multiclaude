"""What the relay brings from Slack, routed like a Telegram message: a DM, a slash command or a tap
goes to the person whose "slack_id" in users.json sent it, and the router's answers for it go back to
their DM with the bot. Someone without a linked account is told so, and the owner gets their id."""
import time

from multiclaude import bot, jsonfile, telegram
from multiclaude.addons.slack import api, messages
from multiclaude.addons.slack.chat import SlackChat
from multiclaude.log import log
from multiclaude.router import asks, work

strangers = set()  # Slack ids the owner was told about, unlinked


def event(desk, payload):
    """An events_api envelope: a DM to the bot, or something else, which is dropped."""
    route(desk, messages.message(payload.get("event") or {}))


def command(desk, payload):
    route(desk, messages.command(payload))


def interaction(desk, payload):
    route(desk, messages.tap(payload))


def route(desk, msg):
    """Hand a Slack message to its person's assistant, as Desk.inbound does a Telegram one."""
    if not msg or not desk.first_time(msg):
        return
    user, now = person(desk, msg.sender), time.time()
    if not user:
        turn_away(desk, msg)
        return
    dm = msg.chat if str(msg.chat).startswith("D") else api.dm_channel(msg.sender)
    user.reply_to = SlackChat(dm, msg.sender)
    if msg.tap:
        answer_tap(user, msg, now)
        return
    if not msg.attrs.get("slash"):
        telegram.later(api.react, msg.chat, msg.msg_id, "eyes")  # it arrived; Slack has no "typing…" for bots
    desk.dispatch(user, msg, now)


def person(desk, sender):
    """The Session of whoever has this Slack id, or None."""
    return next((user for user in desk.users.values() if sender and api.slack_id(user) == str(sender)), None)


def turn_away(desk, msg):
    """Someone whose Slack account isn't linked: tell them, and the owner once."""
    log(f"Slack message from {msg.sender}, who has no {api.ID_FIELD} in users.json; not routed")
    telegram.later(SlackChat(msg.chat, msg.sender).send, f"You can reach {bot.name()}, but it isn't set up for your "
                   "Slack account yet. The owner has been told and links it with your Slack id.")
    if msg.sender not in strangers:
        strangers.add(msg.sender)
        desk.background("telling the owner about a new Slack user", tell_owner_about, desk, msg.sender)


def tell_owner_about(desk, stranger):
    """Someone without a linked account wrote to the Slack bot: the owner links them on the VM, as
    with Telegram's pairing. One ready command for each person not linked yet."""
    owner = desk.users.get(desk.owner)
    if not owner:
        return
    try:
        who = api.bot().call("users.info", user=stranger)["user"].get("real_name") or stranger
    except api.SlackError:
        who = stranger
    unlinked = sorted(name for name, user in desk.users.items() if not api.slack_id(user))
    commands = [f"assistant slack-link {name} {stranger}" for name in unlinked]
    text = (f"🆕 **{who}** wrote to the Slack bot, but their Slack account (`{stranger}`) isn't linked to "
            f"anyone here, so nothing reached {bot.name()}. To link them to their user here, run on the VM: "
            + ("\n" + "\n".join(f"- `{c}`" for c in commands) if commands else f"`assistant slack-link <name> {stranger}`"))
    telegram.send(owner.chat_id, text, commands)


def answer_tap(user, tap, now):
    """A tap on a `slack ask` button, or on one of the router's own questions asked in Slack: as
    router/asks.py answers a Telegram one, with the question kept by `slack ask`."""
    channel, ts = tap.chat, tap.msg_id
    path = api.asks_folder(channel) / f"{ts}.json"
    ask = jsonfile.read_json(path, {})
    if not isinstance(ask, dict) or not ask.get("question"):
        log(f"{user.name}: tap on Slack message {ts}, which isn't a known question; ignored")
        return
    if ask.get("answer") is not None:
        log(f"{user.name}: question {ts} was already answered; ignoring a tap on {tap.text!r}")
        return
    if ask.get("kind") == work.CANCEL_CLEAR:
        asks.clear_cancel_tapped(user, user.reply_to, path, ask, ts)
        return
    options = ask.get("options") or []
    label = asks.full_label(options, tap.attrs.get("callback_data", ""), tap.text)
    telegram.later(show_choice, channel, ts, ask["question"], options, label)
    asks.save_answer(user, path, ask, ts, label)
    asks.deliver_choice(user, tap, ask["question"], label, SlackChat.cli, now)


def show_choice(channel, ts, question, options, label):
    try:
        api.bot().call("chat.update", channel=channel, ts=ts, text=question,
                       blocks=api.question_blocks(question, options, choice=label))
    except api.SlackError as e:
        log(f"Slack: showing the choice on question {ts} failed: {e}")


def sign_in(desk, payload):
    """Someone came back from "Sign in with Slack" (api.sign_in_link): keep their token, so /clear
    deletes their own Slack messages too, and tell them. Slack's token exchange takes a moment."""
    desk.background("a Slack sign-in", signed_in, payload)


def signed_in(payload):
    person = api.sign_in(payload.get("code", ""), payload.get("state", ""))
    log(f"Slack: {person} signed in; /clear deletes their own messages now")
    SlackChat(api.dm_channel(person), person).send("✅ Signed in. From now on /clear deletes your own messages here too.")
