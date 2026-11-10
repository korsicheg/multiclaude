"""Locations (add-in locations): a location or place a person shares on Telegram reaches their
assistant, and their latest position, a live location's updates included, is kept for tools to
read. The Telegram plugin drops locations, so bin/patch-telegram-plugin adds handlers while this is
on: the message comes with location_lat, location_lon, location_accuracy_m and location_live_period
in its meta, and the plugin keeps the position in LOCATIONS/<telegram id>.json. A live location's
updates only change that file and never reach the assistant. When the router polls Telegram itself
(router/telegram_entrance.py) the same is done here (telegram_message, telegram_edited_message).
The OwnTracks app can keep the file up to date too, through the relay (owntracks.py).
bin/run-user.sh gives each session its file as ASSISTANT_LOCATION."""
from multiclaude import jsonfile, paths
from multiclaude.addons.locations import owntracks
from multiclaude.channels import TelegramChat

LOCATIONS = paths.STATE / "locations"  # where the patched plugin keeps them, by Telegram id
LIVE_UNTIL_STOPPED = 0x7FFFFFFF  # Telegram's live_period for a live location shared "until I stop it"


def relay_envelopes():
    return {"owntracks": from_owntracks}


def from_owntracks(desk, payload):
    owntracks.handle(desk, payload, LOCATIONS)


def telegram_message(message, paired):
    """A location or place shared on Telegram, for the router polling itself: (text, attributes)
    as the patched plugin passes it on, with a paired sender's position kept; None for anything else."""
    shared = message.get("location")
    if not shared:
        return None
    if paired:
        keep_position(message)
    venue = message.get("venue")
    if venue:
        text = f"(place: {', '.join(filter(None, [venue.get('title'), venue.get('address')]))})"
    else:
        text = "(live location)" if shared.get("live_period") else "(location)"
    attrs = {"location_lat": str(shared["latitude"]), "location_lon": str(shared["longitude"])}
    if shared.get("horizontal_accuracy") is not None:
        attrs["location_accuracy_m"] = str(round(shared["horizontal_accuracy"]))
    if shared.get("live_period"):
        attrs["location_live_period"] = str(shared["live_period"])
    return text, attrs


def telegram_edited_message(message):
    """A live location's later position, which comes as an edit: it only updates the file."""
    if message.get("location"):
        keep_position(message)


def keep_position(message):
    """Keep a Telegram message's location as its sender's latest position, in the plugin patch's form."""
    shared = message["location"]
    position = {"latitude": shared["latitude"], "longitude": shared["longitude"]}
    if shared.get("horizontal_accuracy") is not None:
        position["accuracy_m"] = shared["horizontal_accuracy"]
    if shared.get("heading") is not None:
        position["heading"] = shared["heading"]
    position["message_id"] = message["message_id"]
    if shared.get("live_period"):
        position["live_until"] = message["date"] + shared["live_period"]
    position["updated"] = message.get("edit_date") or message["date"]
    jsonfile.write_json(LOCATIONS / f"{message['from']['id']}.json", position)


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
