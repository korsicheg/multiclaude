# Voice notes

Telegram voice notes are transcribed on the machine before the person's assistant gets them, and so are voice clips recorded in Slack while the slack add-in is on too. The words are shown in the chat as a quiet reply to the note (in the clip's thread on Slack) and typed in like a message, with the audio still listed as an attachment. A note waits its turn in the person's queue, so messages sent before it aren't held up. If fetching or transcribing fails, the assistant gets the note as an audio file and the person is told why.

It uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with the multilingual `small` model on the CPU, in a venv of its own outside the repo, so the bot itself stays standard-library only. A note takes about a third of its length to transcribe, on half the cores.

## What it needs

- [uv](https://docs.astral.sh/uv/), which installs Python 3.12 and Whisper into the venv.
- About 1 GB of disk for the venv and the model.

## Setup

`assistant addon enable voice-notes`. Its `setup` script makes Whisper's venv in `~/.local/share/whisper` (`ASSISTANT_WHISPER` moves it); by hand, that is:

```sh
uv venv --python 3.12 ~/.local/share/whisper/venv
uv pip install --python ~/.local/share/whisper/venv/bin/python faster-whisper==1.2.1 "av<16"
```

The model (about 460 MB) downloads into `~/.local/share/whisper/models` with the first voice note.

## Files

- `lib/multiclaude/addons/voice_notes.py`: queueing a note, fetching it, transcribing it and answering it, per app (`sources()`: Telegram's, and those other add-ins offer with `voice_note_sources`).
- `addons/voice-notes/setup`: run by `assistant addon enable voice-notes`; makes Whisper's venv.
- `addons/voice-notes/transcribe.py`: run with Whisper's venv; prints the words in an audio file.
