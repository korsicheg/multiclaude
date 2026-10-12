"""Voice notes (add-in voice-notes): Telegram voice notes, and those of other apps add-ins bring
(voice_note_sources), are transcribed on the VM before their person's assistant gets them. A note waits its turn in the
person's queue untranscribed (DeliverVoiceNote), so their earlier messages aren't held up. When it
comes, the router fetches the audio, transcribes it with Whisper (addons/voice-notes/transcribe.py,
run with Whisper's own venv, so the router itself needs no packages), shows the words in the chat as
a quiet answer to the note, and types them in like a message. If that fails, the note goes in as an
attachment and the person is told why."""
import dataclasses
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from multiclaude import addons, paths, telegram
from multiclaude.channels import TelegramChat
from multiclaude.log import log
from multiclaude.router import inbound as messages
from multiclaude.router import work

# Whisper's own venv and its downloaded models, outside the repo.
WHISPER = Path(os.environ.get("ASSISTANT_WHISPER") or paths.HOME / ".local" / "share" / "whisper")
WHISPER_PYTHON = WHISPER / "venv" / "bin" / "python"
WHISPER_MODELS = WHISPER / "models"
TRANSCRIBE = addons.folder("voice-notes") / "transcribe.py"
TRANSCRIBE_TIMEOUT = 300  # a 10-minute note takes about 3 minutes
PLACEHOLDER = "(voice message)"  # the Telegram plugin's text for a note without a caption
INTRO = "I sent this as a voice note; its transcript is pasted below and it is my own request to you:"


class TranscriptionFailed(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Source:
    """An app voice notes come from: Telegram, or one an add-in brings (voice_note_sources)."""
    help: str        # what /help says that app's notes become
    note: Callable   # note(inbound): the id of the note a message carries, or None
    fetch: Callable  # fetch(note_id, folder): the path its audio is saved at; TranscriptionFailed if it can't be
    show: Callable   # show(inbound, text): a quiet answer to the note


def on_message(user, msg, now):
    if not note_in(msg):
        return False
    user.queue_message(DeliverVoiceNote(now, msg_id=msg.msg_id, inbound=msg), now)
    return True


def help_info(user, app):
    source = sources().get(app)
    return {"can_send": [source.help]} if source else {}


def sources():
    """{app: Source}: Telegram's, and those the add-ins that are on offer (voice_note_sources)."""
    found = {TelegramChat.app: TELEGRAM}
    for offered in addons.call("voice_note_sources"):
        found.update(offered or {})
    return found


@dataclasses.dataclass
class DeliverVoiceNote(work.Deliver):
    """A voice note, transcribed when its turn comes so the person's earlier messages aren't held
    up meanwhile; then typed in like a message."""
    inbound: object = None

    def run(self, user, now):
        if not self.header:
            self.header, self.body = message(user.display, self.inbound)
        return super().run(user, now)


def note_in(inbound):
    source = sources().get(inbound.app)
    return source.note(inbound) if source else None


def message(display, inbound):
    """The header and body to type for a voice note: its words, or the note as an attachment
    with the reason it couldn't be transcribed."""
    source = sources()[inbound.app]
    try:
        words = transcribe(source, source.note(inbound))
    except TranscriptionFailed as e:
        log(f"voice note {inbound.msg_id}: {e}")
        source.show(inbound, f"⚠️ I couldn't transcribe your voice note ({e}), so I'm working from the audio "
                             "file instead.")
        return messages.header(display, inbound), messages.body(inbound)
    source.show(inbound, f"🎙️ {words}")
    caption = "" if inbound.text in (PLACEHOLDER, "") else f"\n\n{inbound.text}"
    transcribed = dataclasses.replace(inbound, text=words + caption)
    return messages.header(display, inbound, "voice note · msg {id}", INTRO), messages.body(transcribed)


def transcribe(source, note_id):
    """The words in a note from source; TranscriptionFailed if it can't be fetched or heard."""
    with tempfile.TemporaryDirectory(prefix="voice-note-") as folder:
        return hear(source.fetch(note_id, Path(folder)))


def hear(audio):
    """Whisper's transcript of an audio file."""
    command = [str(WHISPER_PYTHON), "-I", str(TRANSCRIBE), str(audio), str(WHISPER_MODELS)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=TRANSCRIBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise TranscriptionFailed(f"Whisper didn't run: {e}") from e
    if done.returncode != 0:
        last_line = (done.stderr.strip().splitlines() or ["no output"])[-1]
        raise TranscriptionFailed(f"Whisper failed: {last_line}")
    if not done.stdout.strip():
        raise TranscriptionFailed("I heard no speech in it")
    return done.stdout.strip()


# --- Telegram ---

def telegram_note(inbound):
    attrs = inbound.attrs
    return attrs.get("attachment_file_id") if attrs.get("attachment_kind") == "voice" else None


def fetch_from_telegram(file_id, folder):
    audio = folder / "note.oga"
    try:
        bot = telegram.bot()
        bot.download(bot.call("getFile", file_id=file_id)["file_path"], audio)
    except telegram.TelegramError as e:
        raise TranscriptionFailed(f"fetching it failed: {e}") from e
    return audio


def show_on_telegram(inbound, text):
    """A reply to the note."""
    telegram.send(inbound.chat, text, disable_notification=True,
                  reply_parameters={"message_id": int(inbound.msg_id), "allow_sending_without_reply": True})


TELEGRAM = Source("voice notes (I transcribe them)", telegram_note, fetch_from_telegram, show_on_telegram)
