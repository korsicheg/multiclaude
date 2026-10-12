"""The Telegram Bot API, for the router and tg. Only the Telegram plugin in the front desk
polls for updates; everything here only sends, so it never competes with it."""
import errno
import functools
import http.client
import itertools
import json
import os
import re
import select
import shutil
import socket
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from multiclaude import addons, jsonfile, paths, users
from multiclaude.log import log

API_BASE = os.environ.get("TG_API_BASE") or "https://api.telegram.org"  # the tests point this at a fake Bot API
RICH_LIMIT = 32000        # Telegram allows 32768 characters per rich message
# On some networks new IPv6 connections to the Bot API get no answer for seconds while IPv4
# connects at once, and Python waits on one address at a time. So connections are kept open
# between calls, and a new one tries IPv4 first and races a fresh attempt every CONNECT_STAGGER
# seconds.
CONNECT_STAGGER = 0.4     # a normal connect takes about 0.15s
# Give up connecting after this many seconds, even when the call itself may take longer
# (uploads); the tests shorten it.
CONNECT_WITHIN = float(os.environ.get("TG_CONNECT_WITHIN") or 10)
REUSE_WITHIN = 30         # don't reuse a connection idle longer than this; the server or a NAT may have dropped it
# The ids of every message in each person's chat, one "<message_id> <unix time>" line each,
# so /clear can delete them. Bot message ids are shared by all of the bot's chats, so a
# chat's history can't be found by id ranges alone; /clear also sweeps the gaps between
# recorded ids, for messages the router never saw.
MESSAGES = paths.STATE / "messages"
DELETABLE_FOR = 48 * 3600 - 600  # bots can only delete messages less than 48 hours old
# The router's own texts are plain text that put commands, paths and names in backticks;
# rich_blocks() shows those as code.
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
# Telegram doesn't copy code in a rich message on tap, so what the reader should paste somewhere
# gets a 📋 button under the message that copies it. Only what the sender names gets one: code
# formatting alone never adds a button.
COPY_BUTTONS_MAX = 8
COPY_TEXT_MAX = 256       # what a copy button can hold
COPY_LABEL_MAX = 40       # longer texts (a key, a long URL) are shortened on the button
PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")
MARKDOWN_SPECIAL_RE = re.compile(r"([\\`*_~=|\[\]$])")
# Telegram shows ==text== as a highlight, white on pale yellow, which can't be read in its dark
# theme; tg sends it as bold instead. Code is matched too, only so that it stays as written.
HIGHLIGHT_OR_CODE_RE = re.compile(r"(?P<code>^ {0,3}```.*?^ {0,3}```|`[^`\n]+`)|==(?=\S)(?P<text>[^\n]+?)(?<=\S)==",
                                  re.S | re.M)
# Messages that couldn't go out because Telegram was out of reach (the machine lost its network),
# each kept as <chat id>/<time>-<pid>.json with copies of its files in a folder of
# the same name. A chat's held messages go out before anything new to it, so it keeps its
# order, and the router sends them once its network check sees the network back.
OUTBOX = paths.STATE / "outbox"
HELD_FOR = 24 * 3600      # a message held longer than this is dropped rather than sent
LATE_AFTER = 120          # a message that goes out later than this says when it was written


class TelegramError(Exception):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after  # seconds Telegram asked to wait (429 Too Many Requests), or None

    @property
    def passing(self):
        """Whether the same call can go through later: Telegram only asked to wait."""
        return self.retry_after is not None


class Unreachable(TelegramError):
    """Telegram couldn't be reached, so the request never got there and can safely go again later."""

    @property
    def passing(self):
        return True


def bot_token():
    try:
        with open(paths.TELEGRAM_DIR / ".env") as f:
            for line in f:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def connect_fast(address, timeout, source_address=None):
    """socket.create_connection, but starting another attempt (new port, IPv4 and IPv6 in turn)
    every CONNECT_STAGGER seconds and keeping whichever connects first. It gives up after
    CONNECT_WITHIN seconds at most; timeout then applies to the socket."""
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    by_family = [[i for i in infos if i[0] == socket.AF_INET], [i for i in infos if i[0] == socket.AF_INET6]]
    order = [i for pair in itertools.zip_longest(*by_family) for i in pair if i] or infos
    within = min(timeout, CONNECT_WITHIN)
    deadline = time.time() + within
    trying, error = [], None
    try:
        for family, kind, proto, _, addr in itertools.cycle(order):
            if time.time() >= deadline:
                break
            sock = socket.socket(family, kind, proto)
            sock.setblocking(False)
            err = sock.connect_ex(addr)
            if err in (0, errno.EINPROGRESS):
                trying.append(sock)
            else:
                error = OSError(err, os.strerror(err))
                sock.close()
            _, done, _ = select.select([], trying, [], max(0, min(CONNECT_STAGGER, deadline - time.time())))
            for sock in done:
                trying.remove(sock)
                if err := sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                    error = OSError(err, os.strerror(err))
                    sock.close()
                    continue
                sock.setblocking(True)
                sock.settimeout(timeout)
                return sock
        raise error or TimeoutError(f"couldn't connect to {host} in {within}s")
    finally:
        for sock in trying:
            sock.close()


class BotApi:
    """A Bot API client that keeps its connections open between calls. Thread-safe."""

    def __init__(self, timeout=10, base=API_BASE):
        self.timeout = timeout
        url = urllib.parse.urlsplit(base)
        self.connection_class = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        self.host, self.port = url.hostname, url.port
        self.base = base
        self._idle = []  # (connection, last used)
        self._lock = threading.Lock()

    def call(self, method, files=None, **params):
        """Call a Bot API method and return its result; TelegramError if it fails, Unreachable if
        it never got to Telegram. files maps a field to a file path to upload; None params are
        left out. Add-ins may change a message before it's posted (before_telegram_send)."""
        token = bot_token()
        if not token:
            raise TelegramError(f"no bot token in {paths.TELEGRAM_DIR / '.env'}")
        if posts_message(method):
            params = addons.through("before_telegram_send", params, params.get("chat_id"), method)
        body, content_type = encode(params, files)
        for attempt in range(2):
            conn, reused = self._connection(method)
            try:
                conn.request("POST", f"/bot{token}/{method}", body, {"Content-Type": content_type})
                response = conn.getresponse()
                reply = json.loads(response.read() or b"{}")
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError) as e:
                conn.close()
                if reused and attempt == 0:
                    continue  # the server closed it while it sat idle; the request never got there
                raise TelegramError(f"{method}: {e!r}") from e
            except (OSError, http.client.HTTPException, ValueError) as e:
                conn.close()
                raise TelegramError(f"{method}: {e!r}") from e
            self._release(conn)
            if response.status != 200 or not reply.get("ok"):
                raise TelegramError(f"{method}: {reply.get('description') or f'HTTP {response.status}'}",
                                    (reply.get("parameters") or {}).get("retry_after"))
            return reply.get("result")

    def download(self, file_path, dest, timeout=300):
        """Save a file from getFile's file_path to dest."""
        url = f"{self.base}/file/bot{bot_token()}/{file_path}"
        conn = self.connection_class(self.host, self.port, timeout=timeout)
        conn._create_connection = connect_fast
        try:
            conn.request("GET", urllib.parse.urlsplit(url).path)
            response = conn.getresponse()
            if response.status != 200:
                raise TelegramError(f"download: HTTP {response.status}")
            Path(dest).write_bytes(response.read())
        except (OSError, http.client.HTTPException) as e:
            raise TelegramError(f"download: {e!r}") from e
        finally:
            conn.close()

    def _connection(self, method):
        """(an open connection for calling method, whether it was used before). Unreachable if a
        new one can't be opened."""
        with self._lock:
            while self._idle:
                conn, used = self._idle.pop()
                if time.time() - used <= REUSE_WITHIN:
                    return conn, True
                conn.close()
        conn = self.connection_class(self.host, self.port, timeout=self.timeout)
        conn._create_connection = connect_fast
        try:
            conn.connect()  # before the request, so a failure here is known to have sent nothing
        except OSError as e:
            conn.close()
            raise Unreachable(f"{method}: {e!r}") from e
        return conn, False

    def _release(self, conn):
        with self._lock:
            self._idle.append((conn, time.time()))


def posts_message(method):
    """Whether a Bot API method posts a message, which can make the person's phone sound.
    sendChatAction only shows "typing…"."""
    return method.startswith("send") and method != "sendChatAction"


def encode(params, files=None):
    """(body, content type) for a Bot API call: form fields, with dicts and lists as JSON, and
    multipart when there are files to upload."""
    fields = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in params.items() if v is not None}
    if not files:
        return urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded"
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                     + value.encode() + b"\r\n")
    for name, path in files.items():
        filename = Path(path).name.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + Path(path).read_bytes() + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def rich_blocks(text):
    """Rich message blocks for plain text with `commands` in backticks: a paragraph for each part
    between blank lines, its line breaks kept, and each backticked span as code. Nothing in it
    is read as Markdown, so it needs no escaping."""
    return [{"type": "paragraph", "text": rich_text(part)} for part in PARAGRAPH_BREAK_RE.split(text.strip()) if part]


def rich_text(text):
    pieces = CODE_SPAN_RE.split(text)  # text, code, text, code, …
    return [{"type": "code", "text": piece} if i % 2 else piece for i, piece in enumerate(pieces) if piece]


def copy_buttons(texts):
    """An inline keyboard with a 📋 button that copies each text, or None if there are none.
    Texts too long for a button, and any past the first COPY_BUTTONS_MAX, are left out."""
    copyable = [t for t in dict.fromkeys(texts) if t.strip() and len(t) <= COPY_TEXT_MAX]
    rows = [[{"text": f"📋 {copy_label(t)}", "copy_text": {"text": t}}] for t in copyable[:COPY_BUTTONS_MAX]]
    return {"inline_keyboard": rows} if rows else None


def copy_label(text):
    text = " ".join(text.split())
    return text if len(text) <= COPY_LABEL_MAX else text[:COPY_LABEL_MAX - 1] + "…"


def without_highlights(markdown):
    """markdown with every highlight made bold, outside code."""
    return HIGHLIGHT_OR_CODE_RE.sub(lambda m: m["code"] or f"**{m['text']}**", markdown)


def markdown_literal(text):
    """text to show as written inside a rich Markdown message."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return MARKDOWN_SPECIAL_RE.sub(r"\\\1", text)


def split_text(text, limit=RICH_LIMIT):
    """Cut text into messages of at most limit characters, at a line break where there is one, so
    a line's formatting stays in one message."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        cut = cut + 1 if cut > 0 else limit
        chunks.append(text[:cut])
        text = text[cut:]
    return chunks + ([text] if text else [])


class ChatHistory:
    """The recorded messages of one chat (see MESSAGES). tg, the router and `assistant notify`
    record what they send; the router records inbound messages and wipes them on /clear."""

    def __init__(self, chat):
        self.path = MESSAGES / str(chat)
        self.lock = MESSAGES / f"{chat}.lock"

    def remember(self, message_id):
        try:
            with jsonfile.locked(self.lock):
                with open(self.path, "a") as f:
                    f.write(f"{message_id} {int(time.time())}\n")
        except OSError as e:
            log(f"can't record message {message_id} in {self.path}: {e}")

    def take_deletable(self, keep=()):
        """Forget the recorded messages and return the ids still young enough to delete. The ids
        in keep stay recorded, as they were."""
        try:
            with jsonfile.locked(self.lock):
                with open(self.path) as f:
                    lines = f.read().splitlines()
                kept, ids = [], []
                for line in lines:
                    try:
                        message_id, sent = (int(x) for x in line.split())
                    except ValueError:
                        continue
                    if message_id in keep:
                        kept.append(f"{line}\n")
                    elif time.time() - sent < DELETABLE_FOR and message_id not in ids:
                        ids.append(message_id)
                with open(self.path, "w") as f:
                    f.writelines(kept)
        except OSError:
            return []
        return ids


class Outbox:
    """The held messages of one chat (see OUTBOX). tg and the router hold a message here when
    Telegram can't be reached, and send what's held before anything new to the chat."""

    def __init__(self, chat):
        self.chat = str(chat)
        self.dir = OUTBOX / self.chat
        self.lock = OUTBOX / f"{self.chat}.lock"

    @staticmethod
    def chats():
        """The chats with messages waiting."""
        return sorted({p.parent.name for p in OUTBOX.glob("*/*.json")})

    def waiting(self):
        return any(self.dir.glob("*.json"))

    def hold(self, method, params, files=None):
        """Keep a message to send later. Its files are copied, since the sender may delete them."""
        name = f"{time.time_ns()}-{os.getpid()}"
        kept = {}
        with jsonfile.locked(self.lock):
            for field, path in (files or {}).items():
                copy = self.dir / name / field / Path(path).name  # the upload keeps its file name
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, copy)
                kept[field] = str(copy)
            jsonfile.write_json(self.dir / f"{name}.json",
                                {"method": method, "params": params, "files": kept, "held": time.time()})

    def flush(self, api):
        """Send the held messages in order. True once none is left; False if the rest has to
        wait, because Telegram still can't be reached or asks to slow down."""
        with jsonfile.locked(self.lock):
            for path in sorted(self.dir.glob("*.json")):
                if not self.settle(api, path):
                    return False
        return True

    def settle(self, api, path):
        """Send the held message at path and let it go. One held too long, or that Telegram
        refuses, is let go unsent, since it would never go. False if it has to wait instead."""
        try:
            self.send_held(api, jsonfile.read_json(path, {}))
        except TelegramError as e:
            if e.passing:
                return False
            log(f"chat {self.chat}: dropping a held message Telegram refused: {e}")
        path.unlink(missing_ok=True)
        shutil.rmtree(path.with_suffix(""), ignore_errors=True)
        return True

    def send_held(self, api, held):
        """Send a held message, recording it for /clear, unless it was held too long."""
        if time.time() - held.get("held", 0) > HELD_FOR:
            log(f"chat {self.chat}: dropping a message held for over {HELD_FOR // 3600} hours")
            return
        result = api.call(held["method"], files=held["files"] or None, **with_held_note(held))
        if isinstance(result, dict) and result.get("message_id"):
            ChatHistory(self.chat).remember(result["message_id"])


def with_held_note(held):
    """A held message's parameters, saying when it was written if it goes out late."""
    params = held["params"]
    if time.time() - held["held"] < LATE_AFTER or "rich_message" not in params:
        return params  # a file sent on its own (tg send --separate) follows its text, which has the note
    note = (f"⏳ Written at {users.local_time(params.get('chat_id'), held['held'])} and held until now, "
            "while Telegram was out of reach.")
    rich = dict(params.get("rich_message") or {})
    if "blocks" in rich:
        rich["blocks"] = [*rich["blocks"], {"type": "paragraph", "text": [note]}]
    else:
        rich["markdown"] = "\n\n".join(filter(None, [rich.get("markdown"), f"*{note}*"]))
    return {**params, "rich_message": rich}


def send_or_hold(api, method, files=None, **params):
    """BotApi.call with api for a method that posts a message, after anything held for its chat.
    If Telegram can't be reached, the message is held instead (see OUTBOX). The sent message, or
    None if it was held; TelegramError if Telegram refused it."""
    box = Outbox(params["chat_id"])
    if box.waiting() and not box.flush(api):
        box.hold(method, params, files)
        return None
    try:
        return api.call(method, files=files, **params)
    except Unreachable:
        box.hold(method, params, files)
        return None


def flush_held():
    """Send every chat's held messages, for the router once Telegram can be reached again."""
    for chat in Outbox.chats():
        if not Outbox(chat).flush(bot()):
            return  # still out of reach


@functools.cache
def bot():
    """This process's BotApi for the router: short timeouts, so a slow call never stalls it."""
    return BotApi(timeout=10)


def api(method, **params):
    """Call the Bot API; its result, or None if the call failed (logged)."""
    try:
        return bot().call(method, **params)
    except TelegramError as e:
        log(f"{e}")
        return None


def later(fn, *args, **kwargs):
    """Call fn in the background, so a slow Bot API call never holds up the caller."""
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


def send(chat, text, copy=(), **options):
    """Send plain text with `commands` in backticks (see rich_blocks), recording the message so
    the person's next /clear deletes it. copy is what the reader should paste somewhere, each
    shown as a 📋 button; options are more sendRichMessage parameters, such as
    disable_notification, or reply_markup for other buttons instead. When Telegram can't be
    reached the message is held, to go out once it can (see OUTBOX). The sent message, or None
    if it was held or sending failed."""
    buttons = options.pop("reply_markup", None) or copy_buttons(copy)
    try:
        result = send_or_hold(bot(), "sendRichMessage", chat_id=chat, rich_message={"blocks": rich_blocks(text)},
                              reply_markup=buttons, **options)
    except TelegramError as e:
        log(f"{e}")
        return None
    if result is None:
        log(f"chat {chat}: Telegram is out of reach; the message is held to send later")
    elif isinstance(result, dict) and result.get("message_id"):
        ChatHistory(chat).remember(result["message_id"])
    return result
