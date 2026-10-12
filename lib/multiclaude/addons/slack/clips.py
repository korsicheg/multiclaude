"""Voice clips recorded in Slack, transcribed like Telegram's voice notes while the voice-notes
add-in is on too: it asks for this source (voice_note_sources)."""
from pathlib import Path

from multiclaude.addons import voice_notes
from multiclaude.addons.slack import api
from multiclaude.log import log


def clip(inbound):
    """The id of the voice clip recorded in Slack that this message carries, or None."""
    found = next((f for f in inbound.attrs.get("slack_files") or [] if f.get("voice")), None)
    return found["id"] if found else None


def fetch(file_id, folder):
    try:
        slack = api.bot()
        info = slack.call("files.info", file=file_id)["file"]
        audio = folder / f"clip{Path(info.get('name') or 'clip.m4a').suffix or '.m4a'}"
        slack.download(info["url_private_download"], audio)
    except (api.SlackError, KeyError) as e:
        raise voice_notes.TranscriptionFailed(f"fetching it failed: {e}") from e
    return audio


def show(inbound, text):
    """An answer in the clip's thread."""
    try:
        api.post(inbound.chat, text, thread=inbound.msg_id)
    except api.SlackError as e:
        log(f"Slack: {e}")


SOURCE = voice_notes.Source("voice clips (I transcribe them)", clip, fetch, show)
