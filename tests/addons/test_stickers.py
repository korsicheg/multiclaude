"""Tests for the stickers add-in (lib/multiclaude/addons/stickers.py)."""
import os
import time

import pytest

from conftest import ALICE_CHAT, REPO, channel, sent

STICKER = {"attachment_kind": "sticker", "attachment_file_id": "STICKER1", "attachment_size": "206483"}
AS_BEFORE = "(sticker 🫤)\n\nAttachments:\nfile_id: STICKER1 (sticker, 206483 bytes)"
VIDEO = "a video sticker, 3.0 s: 9 numbered frames from start to end"


@pytest.fixture
def stickers(r, addons_on):
    addons_on("stickers")
    from multiclaude.addons import stickers
    return stickers


def typed_into(user, monkeypatch):
    """What gets typed into the user's terminal, as (header, body) pairs."""
    typed = []
    monkeypatch.setattr(user, "type_message", lambda header, body: typed.append((header, body)))
    return typed


def renderer(stickers, monkeypatch, tmp_path, script):
    """The add-in's venv Python, replaced by a shell script."""
    python = tmp_path / "stickers-python"
    python.write_text(f"#!/bin/sh\n{script}\n")
    python.chmod(0o755)
    monkeypatch.setattr(stickers, "PYTHON", python)


@pytest.fixture
def fake_telegram(r, bot_api, monkeypatch):
    monkeypatch.setattr(r.telegram, "bot", lambda: r.telegram.BotApi(base=bot_api.url))
    monkeypatch.setattr(r.telegram, "bot_token", lambda: "t")
    return bot_api


def test_a_sticker_waits_in_the_queue_undrawn(r, desk, stickers, monkeypatch):
    monkeypatch.setattr(stickers, "draw", lambda file_id, picture: pytest.fail("drawn too early"))
    desk.inbound(channel("(sticker 🫤)", **STICKER))

    (item,) = desk.users["alice"].queue
    assert isinstance(item, stickers.DeliverSticker)
    assert (item.header, item.msg_id) == ("", "5")


def test_a_sticker_is_typed_with_its_picture(r, desk, stickers, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(stickers, "draw", lambda file_id, picture: VIDEO if file_id == "STICKER1" else "")
    typed = typed_into(alice, monkeypatch)
    desk.inbound(channel("(sticker 🫤)", **STICKER))

    assert alice.queue[0].run(alice, time.time())

    ((header, body),) = typed
    assert header.startswith("[Telegram from Alice · msg 5 · 2026-10-08 18:53 UTC] My message is pasted below")
    assert body == f"{AS_BEFORE}\nsticker: {stickers.PICTURES / f'{ALICE_CHAT}-5.png'} ({VIDEO})"
    assert alice.queue == []


def test_a_sticker_that_cant_be_drawn_goes_in_as_before(r, desk, stickers, monkeypatch):
    alice = desk.users["alice"]

    def fail(file_id, picture):
        raise stickers.RenderFailed("drawing it failed: no frames")

    monkeypatch.setattr(stickers, "draw", fail)
    typed = typed_into(alice, monkeypatch)
    desk.inbound(channel("(sticker 🫤)", **STICKER))

    alice.queue[0].run(alice, time.time())

    ((header, body),) = typed
    assert body == AS_BEFORE
    assert sent(r, ALICE_CHAT) == []


def test_draw_fetches_the_sticker_and_runs_the_renderer(r, fake_telegram, stickers, monkeypatch, tmp_path):
    # The fake Bot API's file is documents/report.pdf: the renderer gets it under that extension.
    renderer(stickers, monkeypatch, tmp_path, 'case "$3" in *.pdf) ;; *) exit 1 ;; esac\n'
                                              '[ "$(cat "$3")" = "attachment bytes" ] && touch "$4" '
                                              '&& echo \'{"kind": "video", "seconds": 3.0, "frames": 9}\'')
    picture = stickers.PICTURES / "100-5.png"

    assert stickers.draw("STICKER1", picture) == VIDEO
    assert picture.is_file()
    assert fake_telegram.calls[0][:2] == ("getFile", {"file_id": "STICKER1"})


def test_draw_reports_the_renderer_failing(r, fake_telegram, stickers, monkeypatch, tmp_path):
    renderer(stickers, monkeypatch, tmp_path, 'echo "ValueError: not enough image data" >&2\nexit 1')

    with pytest.raises(stickers.RenderFailed, match="not enough image data"):
        stickers.draw("STICKER1", stickers.PICTURES / "100-5.png")


def test_draw_reports_a_renderer_that_isnt_there(r, fake_telegram, stickers, monkeypatch, tmp_path):
    monkeypatch.setattr(stickers, "PYTHON", tmp_path / "no-venv" / "python")

    with pytest.raises(stickers.RenderFailed, match="didn't run"):
        stickers.draw("STICKER1", stickers.PICTURES / "100-5.png")


def test_drawing_forgets_pictures_older_than_a_week(r, stickers):
    stickers.PICTURES.mkdir(parents=True)
    old, recent = stickers.PICTURES / "100-1.png", stickers.PICTURES / "100-2.png"
    for picture, age_days in ((old, 8), (recent, 6)):
        picture.write_bytes(b"png")
        stamp = time.time() - age_days * 24 * 60 * 60
        os.utime(picture, (stamp, stamp))

    stickers.forget_old_pictures(time.time())

    assert (old.exists(), recent.exists()) == (False, True)


@pytest.mark.parametrize("drawn, shows", [
    ({"kind": "still", "seconds": None, "frames": 1}, "a still sticker"),
    ({"kind": "animated", "seconds": 1.5, "frames": 9}, "an animated sticker, 1.5 s: 9 numbered frames from start to end"),
    ({"kind": "video", "seconds": None, "frames": 4}, "a video sticker: 4 numbered frames from start to end"),
])
def test_what_the_picture_shows(stickers, drawn, shows):
    assert stickers.described(drawn) == shows


def test_the_renderer_is_the_add_ins_own_script(r, stickers):
    assert stickers.RENDER == r.paths.ROOT / "addons" / "stickers" / "render.py"
    assert (REPO / stickers.RENDER.relative_to(r.paths.ROOT)).is_file()


def test_help_and_the_bots_description_say_stickers_are_looked_at(r, desk, stickers):
    desk.start()
    box = next(p["description"] for m, p in r.api_calls if m == "setMyDescription")
    assert "• stickers (I look at them, not just their emoji)" in box

    desk.inbound(channel("/help", msg_id="9"))
    assert "• stickers (I look at them, not just their emoji)" in sent(r, ALICE_CHAT)[-1]


def test_a_sticker_goes_in_as_before_while_it_is_off(r, desk, addons_on, monkeypatch):
    addons_on("voice-notes")
    from multiclaude.addons import stickers
    monkeypatch.setattr(stickers, "draw", lambda file_id, picture: pytest.fail("drawn while off"))
    desk.inbound(channel("(sticker 🫤)", **STICKER))

    (item,) = desk.users["alice"].queue
    assert type(item).__name__ == "Deliver"
    assert item.body == AS_BEFORE
