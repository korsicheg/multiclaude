"""Quiet hours (add-in quiet-hours): while a person is in the hours set as "quiet_hours" in their
users.json entry, everything the bot sends them on Telegram arrives silently, with no sound or
vibration. The hours are in their own timezone, and may pass midnight ("23:00-07:00"). Every sender
posts through telegram.BotApi.call, which runs before_telegram_send: the router, tg, jobs and
`assistant notify`. A message whose sender set disable_notification itself keeps it: `tg send
--sound` notifies even in quiet hours, for what the person wants to hear then, such as a morning
brief they wake up to. users.json is read on every send, so an edit takes effect at once."""
import re
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from multiclaude import users
from multiclaude.log import log

FIELD = "quiet_hours"  # in a person's users.json entry: "HH:MM-HH:MM", or missing for none
WINDOW_RE = re.compile(r"(?P<start>\d\d:\d\d)-(?P<end>\d\d:\d\d)")


def before_telegram_send(chat_id, method, params):
    if "disable_notification" in params or not is_quiet(chat_id):
        return params
    return {**params, "disable_notification": True}


def panel_fields():
    return {FIELD: valid_hours}


def is_quiet(chat, now=None):
    """Whether the person whose private chat this is is in their quiet hours at now (an aware
    datetime; default the current time)."""
    person = users.by_chat(chat)
    hours = str(person.get(FIELD) or "") if person else ""
    if not hours:
        return False
    try:
        start, end = window(hours)
        local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(person.timezone)).time()
    except (ValueError, ZoneInfoNotFoundError) as e:
        log(f"{person.name}: ignoring quiet hours {hours!r} in {person.timezone!r}: {e}")
        return False
    return start <= local < end if start <= end else local >= start or local < end


def window(text):
    """(start, end) times of an "HH:MM-HH:MM" window; ValueError if it isn't one."""
    match = WINDOW_RE.fullmatch(text.strip())
    if not match:
        raise ValueError("not HH:MM-HH:MM")
    return time.fromisoformat(match["start"]), time.fromisoformat(match["end"])


def valid_hours(value):
    """"HH:MM-HH:MM", or "" for none."""
    hours = str(value or "").strip()
    if hours:
        try:
            window(hours)
        except ValueError:
            raise ValueError("Quiet hours need a start and an end, like 00:00-09:00.") from None
    return hours
