"""Tests for the voice-notes add-in (lib/multiclaude/addons/voice_notes.py)."""
import time

import pytest

from conftest import ALICE_CHAT, REPO, channel, sent, shown

VOICE_NOTE = {"attachment_kind": "voice", "attachment_file_id": "VOICE1", "attachment_mime": "audio/ogg"}


@pytest.fixture
def notes(r, addons_on):
    addons_on("voice-notes")
    from multiclaude.addons import voice_notes
    return voice_notes


def hears(notes, monkeypatch, words=lambda note_id: f"words of {note_id}"):
    """Have Whisper hear words(note_id) in every note."""
    monkeypatch.setattr(notes, "transcribe", lambda source, note_id: words(note_id))


def typed_into(user, monkeypatch):
    """What gets typed into the user's terminal, as (header, body) pairs."""
    typed = []
    monkeypatch.setattr(user, "type_message", lambda header, body: typed.append((header, body)))
    return typed


def test_a_voice_note_waits_in_the_queue_untranscribed(r, desk, notes, monkeypatch):
    monkeypatch.setattr(notes, "transcribe", lambda source, note_id: pytest.fail("transcribed too early"))
    desk.inbound(channel("(voice message)", **VOICE_NOTE))

    (item,) = desk.users["alice"].queue
    assert isinstance(item, notes.DeliverVoiceNote)
    assert (item.header, item.msg_id) == ("", "5")
    assert desk.users["alice"].turn.busy


def test_a_voice_note_is_typed_as_its_transcript_and_shown_in_the_chat(r, desk, notes, monkeypatch):
    alice = desk.users["alice"]
    hears(notes, monkeypatch)
    typed = typed_into(alice, monkeypatch)
    desk.inbound(channel("(voice message)", **VOICE_NOTE))

    assert alice.queue[0].run(alice, time.time())

    ((header, body),) = typed
    assert header == ("[Telegram from Alice · voice note · msg 5 · 2026-10-08 18:53 UTC] I sent this as a voice "
                      "note; its transcript is pasted below and it is my own request to you:")
    assert body.startswith("words of VOICE1\n\nAttachments:\nfile_id: VOICE1 (voice, audio/ogg)")
    method, params = r.api_calls[-1]
    assert (method, shown(params), params["disable_notification"]) == ("sendRichMessage", "🎙️ words of VOICE1", True)
    assert params["reply_parameters"]["message_id"] == 5
    assert alice.queue == []


def test_a_voice_note_that_cant_be_transcribed_goes_in_as_audio(r, desk, notes, monkeypatch):
    alice = desk.users["alice"]

    def fail(note_id):
        raise notes.TranscriptionFailed("I heard no speech in it")

    hears(notes, monkeypatch, fail)
    typed = typed_into(alice, monkeypatch)
    desk.inbound(channel("(voice message)", **VOICE_NOTE))

    alice.queue[0].run(alice, time.time())

    ((header, body),) = typed
    assert "· msg 5 ·" in header and "voice note" not in header
    assert body == "(voice message)\n\nAttachments:\nfile_id: VOICE1 (voice, audio/ogg)"
    assert "I heard no speech in it" in sent(r, ALICE_CHAT)[-1]


def test_transcribe_fetches_the_note_and_runs_whisper(r, bot_api, notes, monkeypatch, tmp_path):
    whisper = tmp_path / "whisper-python"
    whisper.write_text('#!/bin/sh\n[ "$(cat "$3")" = "attachment bytes" ] && echo "  hello there  "\n')
    whisper.chmod(0o755)
    monkeypatch.setattr(notes, "WHISPER_PYTHON", whisper)
    monkeypatch.setattr(r.telegram, "bot", lambda: r.telegram.BotApi(base=bot_api.url))
    monkeypatch.setattr(r.telegram, "bot_token", lambda: "t")

    assert notes.transcribe(notes.TELEGRAM, "VOICE1") == "hello there"
    assert bot_api.calls[0][:2] == ("getFile", {"file_id": "VOICE1"})


def test_transcribe_reports_whisper_failing(r, bot_api, notes, monkeypatch, tmp_path):
    whisper = tmp_path / "whisper-python"
    whisper.write_text('#!/bin/sh\necho "RuntimeError: model missing" >&2\nexit 1\n')
    whisper.chmod(0o755)
    monkeypatch.setattr(notes, "WHISPER_PYTHON", whisper)
    monkeypatch.setattr(r.telegram, "bot", lambda: r.telegram.BotApi(base=bot_api.url))
    monkeypatch.setattr(r.telegram, "bot_token", lambda: "t")

    with pytest.raises(notes.TranscriptionFailed, match="model missing"):
        notes.transcribe(notes.TELEGRAM, "VOICE1")


def test_the_transcriber_is_the_add_ins_own_script(r, notes):
    assert notes.TRANSCRIBE == r.paths.ROOT / "addons" / "voice-notes" / "transcribe.py"
    assert (REPO / notes.TRANSCRIBE.relative_to(r.paths.ROOT)).is_file()


def test_help_and_the_bots_description_say_voice_notes_are_transcribed(r, desk, notes):
    desk.start()
    box = next(p["description"] for m, p in r.api_calls if m == "setMyDescription")
    assert "• voice notes (I transcribe them)" in box

    desk.inbound(channel("/help", msg_id="9"))
    assert "• voice notes (I transcribe them)" in sent(r, ALICE_CHAT)[-1]


def test_a_voice_note_goes_in_as_audio_while_it_is_off(r, desk, addons_on, monkeypatch):
    addons_on("locations")
    from multiclaude.addons import voice_notes
    monkeypatch.setattr(voice_notes, "transcribe", lambda source, note_id: pytest.fail("transcribed while off"))
    desk.inbound(channel("(voice message)", **VOICE_NOTE))

    (item,) = desk.users["alice"].queue
    assert type(item).__name__ == "Deliver"
    assert item.body == "(voice message)\n\nAttachments:\nfile_id: VOICE1 (voice, audio/ogg)"
