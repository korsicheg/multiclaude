"""Tests for the quiet-hours add-in (lib/multiclaude/addons/quiet_hours.py)."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import CHAT, enable_addons, last_snapshot, read_json, tap, write_json


@pytest.fixture
def quiet(lib):
    from multiclaude.addons import quiet_hours
    return quiet_hours


@pytest.mark.parametrize("hours, utc, is_quiet", [
    ("00:00-09:00", "2026-10-08T21:00", True),   # 00:00 in Helsinki, on summer time (UTC+3)
    ("00:00-09:00", "2026-10-09T05:59", True),   # 08:59
    ("00:00-09:00", "2026-10-09T06:00", False),  # 09:00
    ("00:00-09:00", "2026-10-08T20:59", False),  # 23:59
    ("00:00-09:00", "2026-11-09T06:30", True),   # 08:30 on winter time (UTC+2)
    ("23:00-07:00", "2026-10-08T20:30", True),   # 23:30, in a window that passes midnight
    ("23:00-07:00", "2026-10-09T03:59", True),   # 06:59
    ("23:00-07:00", "2026-10-09T04:00", False),  # 07:00
    ("", "2026-10-08T21:00", False),
    ("all night", "2026-10-08T21:00", False),
])
def test_quiet_hours_are_kept_in_the_persons_own_timezone(lib, quiet, hours, utc, is_quiet):
    write_json(lib.paths.USERS, {"alice": {"telegram_id": "1", "timezone": "Europe/Helsinki", "quiet_hours": hours}})
    now = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc)
    assert quiet.is_quiet("1", now) is is_quiet
    assert quiet.is_quiet("2", now) is False


def test_quiet_hours_default_to_utc_and_ignore_an_unknown_timezone(lib, quiet):
    write_json(lib.paths.USERS, {"a": {"telegram_id": "1", "quiet_hours": "00:00-09:00"},
                                 "b": {"telegram_id": "2", "quiet_hours": "00:00-09:00", "timezone": "Mars/Olympus"}})
    now = datetime(2026, 10, 9, 8, 30, tzinfo=timezone.utc)
    assert quiet.is_quiet("1", now) is True
    assert quiet.is_quiet("2", now) is False


# --- every sender: tg here, the router and jobs alike, since all post through BotApi.call ---

def quiet_hours(tg, start, end):
    """Give the chat's person quiet hours from start to end, both UTC datetimes."""
    write_json(tg.root / "users.json", {"alice": {"telegram_id": CHAT, "timezone": "UTC",
                                                  "quiet_hours": f"{start:%H:%M}-{end:%H:%M}"}})


def test_messages_in_quiet_hours_are_sent_silently(tg):
    enable_addons(tg.root, "quiet-hours")
    now = datetime.now(timezone.utc)
    quiet_hours(tg, now - timedelta(hours=1), now + timedelta(hours=1))
    tg("send", "hello")
    tg("ask", "Which?", "A", "B")
    tg("typing")

    assert tg.api.methods() == ["sendRichMessage", "sendRichMessage", "sendChatAction"]
    assert [fields.get("disable_notification") for _, fields, _ in tg.api.calls] == ["True", "True", None]


def test_send_with_sound_notifies_even_in_quiet_hours(tg):
    enable_addons(tg.root, "quiet-hours")
    now = datetime.now(timezone.utc)
    quiet_hours(tg, now - timedelta(hours=1), now + timedelta(hours=1))
    tg("send", "--sound", "Good morning")
    tg("send", "hello")

    assert [fields.get("disable_notification") for _, fields, _ in tg.api.calls] == ["False", "True"]


def test_messages_outside_quiet_hours_make_a_sound(tg):
    enable_addons(tg.root, "quiet-hours")
    now = datetime.now(timezone.utc)
    quiet_hours(tg, now + timedelta(hours=1), now + timedelta(hours=2))
    tg("send", "hello")

    assert "disable_notification" not in tg.api.calls[0][1]


def test_quiet_hours_do_nothing_while_the_add_in_is_off(tg):
    enable_addons(tg.root, "voice-notes")
    now = datetime.now(timezone.utc)
    quiet_hours(tg, now - timedelta(hours=1), now + timedelta(hours=1))
    tg("send", "hello")

    assert "disable_notification" not in tg.api.calls[0][1]


def test_a_quiet_hours_check_that_fails_never_stops_a_message(lib, quiet, bot_api, monkeypatch):
    from multiclaude import telegram
    enable_addons(lib.paths.ROOT, "quiet-hours")
    monkeypatch.setattr(telegram, "bot_token", lambda: "t")
    monkeypatch.setattr(quiet, "is_quiet", lambda chat: 1 / 0)

    assert telegram.BotApi(base=bot_api.url).call("sendRichMessage", chat_id=CHAT, text="hi")["message_id"]
    assert "disable_notification" not in bot_api.calls[0][1]


# --- the control panel ---

def test_the_panel_shows_and_sets_quiet_hours(r, panel, addons_on):
    addons_on("quiet-hours")
    tap(panel, "setting", field="quiet_hours", value="23:00-07:30")

    assert read_json(r.paths.USERS)["alice"]["quiet_hours"] == "23:00-07:30"
    snapshot = last_snapshot(r)
    assert snapshot["addons"] == ["quiet-hours"] and snapshot["person"]["quiet_hours"] == "23:00-07:30"
    assert "restore_model" not in snapshot["person"]  # model-fallback is off


def test_the_panel_turns_quiet_hours_off_and_refuses_what_isnt_a_window(r, panel, addons_on):
    addons_on("quiet-hours")
    tap(panel, "setting", field="quiet_hours", value="00:00-09:00")
    tap(panel, "setting", field="quiet_hours", value="")
    assert "quiet_hours" not in read_json(r.paths.USERS)["alice"]

    tap(panel, "setting", field="quiet_hours", value="late")
    assert last_snapshot(r)["answer"]["text"] == "Quiet hours need a start and an end, like 00:00-09:00."
