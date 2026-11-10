"""The way in from Telegram when the router polls the Bot API itself: "frontdesk": "router" in
config.json, in place of the front desk, a Claude Code session that hosts the Telegram plugin
(one claude and two Bun processes, 300 to 400 MB, and a model turn for every message). A thread
long-polls getUpdates and does what the patched plugin does: it keeps strangers out and hands out
pairing codes (access.py), answers /start and /status and a stranger's /help, fetches photos,
and queues everything else for the loop as the Inbound the plugin would have passed on
(updates.py). Telegram keeps what was sent while nothing polled, so a restart loses nothing.

state/telegram-updates.json holds {"offset": the next update to ask for, "saved": when}, saved
after each batch and at least once a minute, so a restart can tell how long the router was down.
Only one program may poll a bot: the thread holds a lock (state/telegram-updates.lock) against a
second router, `assistant install` turns the plugin off for the repo's folder, and when Telegram
says another poller has the token anyway (409), the owner is told."""
import fcntl
import queue
import re
import threading
import time
import traceback

from multiclaude import access, addons, bot, jsonfile, paths, telegram
from multiclaude.log import log
from multiclaude.router import updates

STATE = paths.STATE / "telegram-updates.json"
LOCK = paths.STATE / "telegram-updates.lock"
INBOX = paths.TELEGRAM_DIR / "inbox"  # where photos are kept for the assistants, as the plugin keeps them
LONG_POLL = 25            # seconds Telegram holds a getUpdates with nothing to give
HEARTBEAT = 60            # the state is saved at least this often
RESUME_WITHIN = 60 * 60   # after a longer outage, what was sent meanwhile is skipped rather than delivered late
RETRY_FIRST, RETRY_MAX = 1, 15  # seconds between attempts while Telegram can't be polled, growing
LOCK_RETRY = 30
APPROVALS_EVERY = 2       # how often to look for people `assistant pair` approved
DOWNLOAD_TIMEOUT = 60
# What the thread asks Telegram for. Reactions come only to a poller that asks for them, and
# edited messages carry a live location's updates (the locations add-in).
ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "message_reaction"]
COMMAND_RE = re.compile(r"/(start|help|status)(?:@(\w+))?(?:\s|$)")
# The menu of a private chat nobody is paired in; a paired person's chat has the router's (about.COMMANDS).
STRANGER_COMMANDS = [{"command": "start", "description": "Welcome and setup guide"},
                     {"command": "help", "description": "What this bot can do"},
                     {"command": "status", "description": "Check your pairing status"}]
START = ("This bot gives each person their own Claude Code assistant.\n\n"
         "To pair:\n1. Send me any message: you'll get a 6-character code\n"
         "2. The owner of the machine runs: assistant pair <code>\n\n"
         "After that, what you send here reaches your assistant.")
STRANGER_HELP = ("What you send here goes to your own assistant once you're paired.\n\n"
                 "/start: how to pair\n/status: check your pairing")
PAIR = "{lead}. The owner of the machine runs:\n\nassistant pair {code}"
NOT_PAIRED = "Not paired. Send me a message to get a pairing code."
CONFLICT = ("📡 {bot} can't hear Telegram reliably: another program is polling with its bot token, and each "
            "message reaches only one of them. With `\"frontdesk\": \"router\"` in `config.json` only the router "
            "may. If the front desk's Telegram plugin still runs, `assistant install` and then `assistant restart` "
            "turn it off.")


def saved_state():
    """(the next update to ask for or None, when the state was last saved or None)."""
    kept = jsonfile.read_json(STATE, {})
    kept = kept if isinstance(kept, dict) else {}
    offset, saved = kept.get("offset"), kept.get("saved")
    return (offset if isinstance(offset, int) else None), (saved if isinstance(saved, (int, float)) else None)


class TelegramEntrance(threading.Thread):
    def __init__(self, tell_owner=lambda text: None):
        super().__init__(name="telegram-entrance", daemon=True)
        self.inbox = queue.Queue()  # router.inbound.Inbound, for the loop to route
        self.tell_owner = tell_owner
        self.api = telegram.BotApi(timeout=LONG_POLL + 15)  # its own client: a long poll outlasts the router's timeouts
        self.username = None        # the bot's, to tell its own commands and mentions
        self.offset, saved = saved_state()
        # After a long outage what Telegram kept meanwhile is skipped. A first start has nothing saved and takes it all.
        self.skip_kept = saved is not None and time.time() - saved > RESUME_WITHIN
        self.saved = (self.offset, 0.0)  # what was last written to STATE, and when
        self.conflict_told = False
        self.next_approvals = 0.0
        self.lock = None

    # --- the thread ---

    def run(self):
        self.hold_lock()
        wait = RETRY_FIRST
        while True:
            try:
                self.poll()
                wait = RETRY_FIRST
                continue
            except telegram.TelegramError as e:
                self.failed(e, wait)
            except Exception:
                log(f"polling Telegram failed:\n{traceback.format_exc().rstrip()}")
            time.sleep(wait)
            wait = min(wait * 2, RETRY_MAX)

    def hold_lock(self):
        """One router polls at a time: wait for the lock a router still running holds."""
        paths.STATE.mkdir(parents=True, exist_ok=True)
        self.lock = open(LOCK, "w")
        while True:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                log("another router is polling Telegram; waiting for it to stop")
                time.sleep(LOCK_RETRY)

    def failed(self, error, wait):
        if "Conflict" in str(error):
            log(f"{error}; trying again in {wait}s")
            if not self.conflict_told:
                self.conflict_told = True
                self.tell_owner(CONFLICT.format(bot=bot.name()))
        elif not isinstance(error, telegram.Unreachable) or wait == RETRY_FIRST:
            log(f"{error}; trying again in {wait}s")  # an outage is said once: the network check reports it

    def poll(self):
        """One getUpdates: take what came, then save how far that was."""
        if self.username is None:
            self.introduce()
        if self.skip_kept:
            self.skip()
        found = self.api.call("getUpdates", offset=self.offset, timeout=LONG_POLL, allowed_updates=ALLOWED_UPDATES)
        for update in found or []:
            try:
                self.take(update)
            except Exception:
                log(f"taking update {update.get('update_id')} failed:\n{traceback.format_exc().rstrip()}")
            self.offset = update["update_id"] + 1
        self.save(time.time())

    def introduce(self):
        """Learn the bot's own username, and give the chats of strangers their short menu."""
        self.username = (self.api.call("getMe") or {}).get("username") or ""
        log(f"polling Telegram as @{self.username}")
        telegram.later(telegram.api, "setMyCommands", commands=STRANGER_COMMANDS, scope={"type": "all_private_chats"})

    def skip(self):
        """Let go of everything Telegram kept: asking for the last update alone confirms the rest."""
        last = self.api.call("getUpdates", offset=-1, timeout=0, allowed_updates=ALLOWED_UPDATES)
        if last:
            self.offset = last[-1]["update_id"] + 1
        self.skip_kept = False
        log("the router was down too long; messages sent meanwhile are skipped")

    def save(self, now):
        offset, at = self.saved
        if self.offset == offset and now - at < HEARTBEAT:
            return
        try:
            jsonfile.write_json(STATE, {"offset": self.offset, "saved": now})
        except OSError as e:
            log(f"can't save {STATE}: {e}")
            return
        self.saved = (self.offset, now)

    # --- what came ---

    def take(self, update):
        if message := update.get("message"):
            self.message(message)
        elif edited := update.get("edited_message"):
            if self.paired_in_private(edited.get("chat"), edited.get("from")):
                addons.call("telegram_edited_message", edited)
        elif query := update.get("callback_query"):
            self.tapped(query)
        elif reaction := update.get("message_reaction"):
            self.reacted(reaction)

    def paired_in_private(self, chat, sender):
        return bool(sender) and (chat or {}).get("type") == "private" and access.allowed(access.read(), sender["id"])

    def message(self, message):
        chat, sender = message.get("chat") or {}, message.get("from")
        if not sender:
            return
        paired = self.paired_in_private(chat, sender)
        if self.command(message, paired):
            return
        found = updates.content(message) or self.from_addons(message, paired)
        if not found:
            return  # a kind nothing passes on (a contact, a poll, a dice): no code for it either, as with the plugin
        verdict = access.gate(message, self.username)
        if verdict.action == "pair":
            self.reply(chat, PAIR.format(lead="Still pending" if verdict.resend else "Pairing required", code=verdict.code))
        if verdict.action != "deliver":
            return
        text, attrs, photo = found
        if photo and (path := self.fetch(photo)):
            attrs = {**attrs, "image_path": path}
        self.acknowledge(message, verdict.access)
        self.inbox.put(updates.inbound(message, text, attrs))

    def from_addons(self, message, paired):
        """(text, attributes, None) from the first add-in that takes the message (telegram_message), or None."""
        taken = next((found for found in addons.call("telegram_message", message, paired) if found), None)
        return (*taken, None) if taken else None

    def command(self, message, paired):
        """Answer /start and /status, and a stranger's /help, as the plugin does. Whether the
        message was dealt with: a paired person's /help goes on to the router's own."""
        asked = COMMAND_RE.match(message.get("text") or "")
        if not asked or (asked[2] and self.username and asked[2].lower() != self.username.lower()):
            return False
        now_allowed = access.current()
        chat, sender = message["chat"], str(message["from"]["id"])
        if (chat.get("type") != "private" or now_allowed["dmPolicy"] == "disabled"
                or (now_allowed["dmPolicy"] == "allowlist" and not paired)):
            return True  # no answer in a group, where a code would show to everyone, and none to strangers
        if asked[1] == "start":
            self.reply(chat, START)
        elif asked[1] == "help":
            if paired:
                return False
            self.reply(chat, STRANGER_HELP)
        elif paired:
            username = message["from"].get("username")
            self.reply(chat, f"Paired as {'@' + username if username else sender}.")
        elif code := access.pending_code(now_allowed, sender):
            self.reply(chat, PAIR.format(lead="Pending pairing", code=code))
        else:
            self.reply(chat, NOT_PAIRED)
        return True

    def reply(self, chat, text):
        telegram.later(telegram.api, "sendMessage", chat_id=chat["id"], text=text)

    def acknowledge(self, message, allowed_now):
        """React to a message that is going on, when the owner set an ackReaction (`assistant pair` sets 👀)."""
        emoji = allowed_now.get("ackReaction")
        if emoji and message.get("message_id") is not None:
            telegram.later(telegram.api, "setMessageReaction", chat_id=message["chat"]["id"],
                           message_id=message["message_id"], reaction=[{"type": "emoji", "emoji": emoji}])

    def fetch(self, photo):
        """Save a photo for the assistant to look at; its path, or None if it couldn't be fetched."""
        try:
            file_path = (self.api.call("getFile", file_id=photo["file_id"]) or {}).get("file_path")
            if not file_path:
                return None
            INBOX.mkdir(parents=True, exist_ok=True)
            extension = file_path.rsplit(".", 1)[1] if "." in file_path else "jpg"
            path = INBOX / f"{int(time.time() * 1000)}-{photo.get('file_unique_id', 'photo')}.{extension}"
            self.api.download(file_path, path, timeout=DOWNLOAD_TIMEOUT)
        except (telegram.TelegramError, OSError) as e:
            log(f"photo download failed: {e}")
            return None
        return str(path)

    def tapped(self, query):
        """A tap on a button. Those of `tg ask` and `tg checklist` (ask:<n>) go on, from a paired
        person in their private chat; every tap is answered, so the button stops spinning."""
        telegram.later(telegram.api, "answerCallbackQuery", callback_query_id=query["id"])
        message = query.get("message") or {}
        if updates.ASK_RE.fullmatch(query.get("data") or "") and self.paired_in_private(message.get("chat"), query.get("from")):
            self.inbox.put(updates.tap(query))

    def reacted(self, reaction):
        """An emoji a paired person added to a message in their private chat."""
        if not self.paired_in_private(reaction.get("chat"), reaction.get("user")):
            return
        emoji, custom = updates.added_reactions(reaction)
        added = " ".join(emoji + self.custom_emoji(custom))
        if added:
            self.inbox.put(updates.reacted(reaction, added))

    def custom_emoji(self, ids):
        """The emoji each Premium custom emoji stands for, as its sticker says."""
        if not ids:
            return []
        try:
            stickers = self.api.call("getCustomEmojiStickers", custom_emoji_ids=ids) or []
        except telegram.TelegramError:
            stickers = []
        stands_for = {sticker.get("custom_emoji_id"): sticker.get("emoji") for sticker in stickers}
        return [stands_for.get(i) or "(custom emoji)" for i in ids]

    # --- for the router's loop ---

    def waiting(self):
        """The messages that arrived since the last call."""
        found = []
        while True:
            try:
                found.append(self.inbox.get_nowait())
            except queue.Empty:
                return found

    def tick(self, now):
        """Tell the people `assistant pair` approved since the last look that they're paired."""
        if now < self.next_approvals:
            return
        self.next_approvals = now + APPROVALS_EVERY
        for sender in access.take_approved():
            telegram.later(telegram.api, "sendMessage", chat_id=sender, text=f"Paired! Say hi to {bot.name()}.")
