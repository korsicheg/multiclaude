"""Stickers (add-in stickers): a Telegram sticker reaches its person's assistant as a picture it can
look at, since a sticker says more than its emoji. It waits its turn in the person's queue
(DeliverSticker), so their earlier messages aren't held up. When it comes, the router fetches the
sticker and draws it as one PNG (addons/stickers/render.py, run with the add-in's own venv: a still
sticker as it is, an animated or video one as a grid of frames), which the message lists under its
Attachments. If that fails, the sticker goes in as before: its emoji and file_id."""
import contextlib
import dataclasses
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from multiclaude import addons, paths, telegram
from multiclaude.channels import TelegramChat
from multiclaude.log import log
from multiclaude.router import inbound as messages
from multiclaude.router import work

# The renderer's own venv, and the pictures it draws, outside the repo and the backup.
STICKERS = Path(os.environ.get("ASSISTANT_STICKERS") or paths.HOME / ".local" / "share" / "stickers")
PYTHON = STICKERS / "venv" / "bin" / "python"
PICTURES = STICKERS / "pictures"
RENDER = addons.folder("stickers") / "render.py"
RENDER_TIMEOUT = 60  # a 3-second video sticker takes about a second
KEEP_PICTURES = 7 * 24 * 60 * 60
KINDS = {"still": "a still sticker", "animated": "an animated sticker", "video": "a video sticker"}
# The attrs a drawn sticker adds to its message, for attachment_lines.
PICTURE = "sticker_picture"
SHOWS = "sticker_shows"


class RenderFailed(Exception):
    pass


def on_message(user, msg, now):
    if not sticker_in(msg):
        return False
    user.queue_message(DeliverSticker(now, msg_id=msg.msg_id, inbound=msg), now)
    return True


def help_info(user, app):
    return {"can_send": ["stickers (I look at them, not just their emoji)"]} if app == TelegramChat.app else {}


def attachment_lines(attrs):
    return [f"sticker: {attrs[PICTURE]} ({attrs[SHOWS]})"] if attrs.get(PICTURE) else []


def sticker_in(inbound):
    """The file_id of the sticker a message is, or None."""
    attrs = inbound.attrs
    if inbound.app == TelegramChat.app and attrs.get("attachment_kind") == "sticker":
        return attrs.get("attachment_file_id")
    return None


@dataclasses.dataclass
class DeliverSticker(work.Deliver):
    """A sticker, drawn when its turn comes so the person's earlier messages aren't held up
    meanwhile; then typed in like a message."""
    inbound: object = None

    def run(self, user, now):
        if not self.header:
            self.header = messages.header(user.display, self.inbound)
            self.body = messages.body(with_picture(self.inbound))
        return super().run(user, now)


def with_picture(inbound):
    """The message with its sticker drawn (in its attrs, for attachment_lines), or as it was if the
    sticker can't be drawn."""
    picture = PICTURES / f"{inbound.chat}-{inbound.msg_id}.png"
    try:
        shows = draw(sticker_in(inbound), picture)
    except RenderFailed as e:
        log(f"sticker {inbound.msg_id}: {e}")
        return inbound
    return dataclasses.replace(inbound, attrs={**inbound.attrs, PICTURE: str(picture), SHOWS: shows})


def draw(file_id, picture):
    """Fetch the sticker and draw it at picture; what the picture shows. RenderFailed if it can't."""
    forget_old_pictures(time.time())
    with tempfile.TemporaryDirectory(prefix="sticker-") as folder:
        drawn = render(fetch(file_id, Path(folder)), picture)
    return described(drawn)


def forget_old_pictures(now):
    for old in PICTURES.glob("*.png"):
        with contextlib.suppress(OSError):
            if now - old.stat().st_mtime > KEEP_PICTURES:
                old.unlink()


def fetch(file_id, folder):
    """The sticker saved in folder, named by its format (.webp, .tgs or .webm)."""
    try:
        bot = telegram.bot()
        file_path = bot.call("getFile", file_id=file_id)["file_path"]
        sticker = folder / f"sticker{Path(file_path).suffix}"
        bot.download(file_path, sticker)
    except telegram.TelegramError as e:
        raise RenderFailed(f"fetching it failed: {e}") from e
    return sticker


def render(sticker, picture):
    """What render.py drew: {"kind", "seconds", "frames"}."""
    picture.parent.mkdir(parents=True, exist_ok=True)
    command = [str(PYTHON), "-I", str(RENDER), str(sticker), str(picture)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=RENDER_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise RenderFailed(f"the renderer didn't run: {e}") from e
    if done.returncode != 0:
        last_line = (done.stderr.strip().splitlines() or ["no output"])[-1]
        raise RenderFailed(f"drawing it failed: {last_line}")
    try:
        return json.loads(done.stdout)
    except ValueError as e:
        raise RenderFailed(f"the renderer said {done.stdout.strip()!r}") from e


def described(drawn):
    """What the picture shows, for the assistant: "a video sticker, 3.0 s: 9 frames from start to end…"."""
    kind = KINDS.get(drawn.get("kind"), "a sticker")
    if drawn.get("frames", 1) == 1:
        return kind
    length = f", {drawn['seconds']} s" if drawn.get("seconds") else ""
    return f"{kind}{length}: {drawn['frames']} numbered frames from start to end"
