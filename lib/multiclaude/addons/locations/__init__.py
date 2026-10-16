"""Locations (add-in locations): a location or place a person shares on Telegram reaches their
assistant, and their latest position, a live location's updates included, is kept for tools to
read. The Telegram plugin drops locations, so bin/patch-telegram-plugin adds handlers while this is
on: the message comes with location_lat, location_lon, location_accuracy_m and location_live_period
in its meta, and the plugin keeps the position in LOCATIONS/<telegram id>.json. A live location's
updates only change that file and never reach the assistant. The OwnTracks app can keep it up to
date too, through the relay (owntracks.py). bin/run-user.sh gives each session its file as
ASSISTANT_LOCATION."""
from multiclaude import paths
from multiclaude.addons.locations import owntracks
from multiclaude.channels import TelegramChat

LOCATIONS = paths.STATE / "locations"  # where the patched plugin keeps them, by Telegram id
LIVE_UNTIL_STOPPED = 0x7FFFFFFF  # Telegram's live_period for a live location shared "until I stop it"


def relay_envelopes():
    return {"owntracks": from_owntracks}


def from_owntracks(desk, payload):
    owntracks.handle(desk, payload, LOCATIONS)


def attachment_lines(attrs):
    return [location(attrs)] if attrs.get("location_lat") else []


def help_info(user, app):
    return {"can_send": ["your location, live too"]} if app == TelegramChat.app else {}


def location(attrs):
    """A shared location: its coordinates, accuracy, how long it stays live, and the file that
    keeps its latest position."""
    details = []
    if attrs.get("location_accuracy_m"):
        details.append(f"±{attrs['location_accuracy_m']} m")
    period = int(attrs.get("location_live_period") or 0)
    if period:
        live = "until stopped" if period == LIVE_UNTIL_STOPPED else f"for {period // 60} min"
        details.append(f"live {live}; latest position in {LOCATIONS / (attrs.get('user_id', '') + '.json')}")
    where = f"location: {attrs['location_lat']}, {attrs['location_lon']}"
    return where + (f" ({', '.join(details)})" if details else "")
