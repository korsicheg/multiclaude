"""Tests for the locations add-in (lib/multiclaude/addons/locations: shared locations, OwnTracks'
updates from the relay; its plugin patch and bin/run-user.sh's ASSISTANT_LOCATION)."""
import json
import os
import shutil
import subprocess

import pytest

from conftest import ALICE_CHAT, BIN, RELAY, channel, enable_addons, patch, plugin_version, sent, write_json


@pytest.fixture
def places(r, addons_on):
    addons_on("locations")
    from multiclaude.addons import locations
    return locations


def test_a_shared_location_is_listed_with_its_coordinates(r, desk, places):
    desk.inbound(channel("(location)", msg_id="6", location_lat="52.52001", location_lon="13.404954",
                         location_accuracy_m="12"))
    desk.inbound(channel("(live location)", msg_id="7", location_lat="52.5200", location_lon="13.4050",
                         location_live_period="900"))
    desk.inbound(channel("(live location)", msg_id="8", location_lat="52.5200", location_lon="13.4050",
                         location_live_period=str(0x7FFFFFFF)))

    still, live, endless = (work.body for work in desk.users["alice"].queue)
    assert still == "(location)\n\nAttachments:\nlocation: 52.52001, 13.404954 (±12 m)"
    latest = places.LOCATIONS / f"{ALICE_CHAT}.json"
    assert latest == r.paths.STATE / "locations" / f"{ALICE_CHAT}.json"  # where the plugin patch keeps it
    assert live.endswith(f"location: 52.5200, 13.4050 (live for 15 min; latest position in {latest})")
    assert endless.endswith(f"(live until stopped; latest position in {latest})")


def test_help_says_locations_can_be_sent_on_telegram_only(r, desk, places):
    desk.inbound(channel("/help"))
    assert "• your location, live too" in sent(r, ALICE_CHAT)[-1]
    assert places.help_info(None, "slack") == {}


def test_a_location_is_not_listed_while_it_is_off(r, desk, addons_on):
    addons_on("skill-notes")
    desk.inbound(channel("(location)", location_lat="52.52001", location_lon="13.404954"))
    desk.inbound(channel("/help", msg_id="6"))

    assert desk.users["alice"].queue[0].body == "(location)"
    assert "location" not in sent(r, ALICE_CHAT)[-1]


# --- OwnTracks ---

LOCATION = {"_type": "location", "lat": 37.98, "lon": 23.73, "acc": 12, "cog": 90, "vel": 40, "batt": 80,
            "inregions": ["Home"], "t": "u", "tst": 1791530000, "tid": "al"}


@pytest.fixture
def tracks(places, monkeypatch):
    """The add-in's OwnTracks part, with a relay set up and every answer put on it recorded in
    tracks.answered as (path, body)."""
    from multiclaude.addons.locations import owntracks
    from multiclaude.router import relay_entrance
    monkeypatch.setattr(relay_entrance, "settings", lambda: RELAY)
    owntracks.answered = []
    monkeypatch.setattr(relay_entrance, "put", lambda url, token, body: owntracks.answered.append(
        (url.removeprefix("https://relay.test"), json.loads(body))))
    return owntracks


def from_owntracks(desk, key_hash, message, request_id="0badcafe"):
    desk.relay_inbound({"type": "owntracks", "payload": {
        "id": request_id, "key_hash": key_hash, "message": message, "received": 1791530001}})


def alices_position(places):
    return json.loads((places.LOCATIONS / f"{ALICE_CHAT}.json").read_text())


def test_an_owntracks_location_becomes_the_latest_position_as_the_plugin_keeps_it(r, desk, places, tracks):
    from_owntracks(desk, tracks.keyring.digest(tracks.KEY_RING.new_key("alice")), LOCATION)

    assert alices_position(places) == {"latitude": 37.98, "longitude": 23.73, "accuracy_m": 12, "heading": 90,
                                       "updated": 1791530000, "source": "owntracks", "velocity_kmh": 40,
                                       "battery": 80, "regions": ["Home"], "trigger": "u"}
    assert tracks.answered == [("/owntracks/0badcafe", {"ok": True})]
    assert desk.users["alice"].queue == []  # it never costs a turn


def test_a_region_left_is_kept_with_its_event(r, desk, places, tracks):
    transition = {"_type": "transition", "lat": 37.98, "lon": 23.73, "acc": 30, "event": "leave", "desc": "Home",
                  "t": "c", "tst": 1791530100, "wtst": 1791000000}
    from_owntracks(desk, tracks.keyring.digest(tracks.KEY_RING.new_key("alice")), transition)

    position = alices_position(places)
    assert (position["event"], position["region"], position["updated"]) == ("leave", "Home", 1791530100)
    assert "trigger" not in position


def test_an_older_update_never_replaces_a_newer_position(r, desk, places, tracks):
    write_json(places.LOCATIONS / f"{ALICE_CHAT}.json", {"latitude": 1, "longitude": 2, "message_id": 9,
                                                          "updated": 1791530500})
    from_owntracks(desk, tracks.keyring.digest(tracks.KEY_RING.new_key("alice")), LOCATION)

    assert alices_position(places)["latitude"] == 1
    assert tracks.answered == [("/owntracks/0badcafe", {"ok": True})]


def test_an_unknown_owntracks_key_is_turned_away(r, desk, places, tracks):
    from_owntracks(desk, "0" * 64, LOCATION)

    assert tracks.answered == [("/owntracks/0badcafe", {"error": "unknown OwnTracks key"})]
    assert not (places.LOCATIONS / f"{ALICE_CHAT}.json").exists()


def test_owntracks_messages_without_a_position_are_taken_and_dropped(r, desk, places, tracks):
    key_hash = tracks.keyring.digest(tracks.KEY_RING.new_key("alice"))
    from_owntracks(desk, key_hash, {"_type": "lwt", "tst": 1791530000})
    from_owntracks(desk, key_hash, {"_type": "location", "lat": "37.9", "lon": 23.7, "tst": 1791530000}, "0badcaff")
    from_owntracks(desk, key_hash, LOCATION, "not-an-id")

    assert tracks.answered == [("/owntracks/0badcafe", {"ok": True}), ("/owntracks/0badcaff", {"ok": True})]
    assert not (places.LOCATIONS / f"{ALICE_CHAT}.json").exists()


def test_while_it_is_off_owntracks_updates_go_nowhere(r, desk, addons_on):
    addons_on("locations")
    from multiclaude.addons.locations import owntracks
    key_hash = owntracks.keyring.digest(owntracks.KEY_RING.new_key("alice"))
    addons_on("skill-notes")
    from_owntracks(desk, key_hash, LOCATION)

    assert not (r.paths.STATE / "locations" / f"{ALICE_CHAT}.json").exists()


# --- the plugin patch ---

def test_the_plugin_passes_locations_on_and_keeps_the_latest_position_only_while_it_is_on(tmp_path):
    server = plugin_version(tmp_path, "0.0.7")
    patch(tmp_path)
    assert "multiclaude: locations" not in server.read_text()

    enable_addons(tmp_path / "assistant", "locations")
    assert patch(tmp_path)[0] == 0

    patched = server.read_text()
    assert patched.count("multiclaude: locations") == 2  # the handlers and the meta
    assert patched.index("bot.on('message:location'") < patched.index("type AttachmentMeta = {")
    assert "bot.on('edited_message:location'" in patched
    meta = patched[patched.index("ts: new Date((ctx.message"):]
    assert meta.index("multiclaudeLocationMeta(ctx.message)") < meta.index("image_path: imagePath")


# --- the sessions' ASSISTANT_LOCATION ---

STOP_AND_TELL = """#!/bin/sh
# claude, for run-user.sh: write what it got in its environment, then end the run loop.
env > "$HOME/claude-env"
kill -TERM "$PPID"
"""


def session_env(tmp_path, *addons):
    """The environment bin/run-user.sh starts alice's claude with, with those add-ins on."""
    home = tmp_path / "home"
    root = home / "assistant"
    (root / "bin").mkdir(parents=True)
    for script in ("run-user.sh", "common.sh"):
        shutil.copy2(BIN / script, root / "bin" / script)
    write_json(root / "users.json", {"alice": {"telegram_id": ALICE_CHAT, "display": "Alice"}})
    enable_addons(root, *addons)
    (home / "workspaces" / "alice").mkdir(parents=True)
    claude = home / ".local" / "bin" / "claude"
    claude.parent.mkdir(parents=True)
    claude.write_text(STOP_AND_TELL)
    claude.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if key != "ASSISTANT_LOCATION"}
    subprocess.run([str(root / "bin" / "run-user.sh"), "alice"], env={**env, "HOME": str(home)},
                   capture_output=True, timeout=30)
    return dict(line.split("=", 1) for line in (home / "claude-env").read_text().splitlines() if "=" in line)


def test_sessions_find_the_latest_location_only_while_it_is_on(tmp_path):
    on = session_env(tmp_path / "on", "locations")
    off = session_env(tmp_path / "off")

    assert on["ASSISTANT_LOCATION"] == f"{tmp_path}/on/home/assistant/state/locations/{ALICE_CHAT}.json"
    assert "ASSISTANT_LOCATION" not in off and off["TG_CHAT_ID"] == ALICE_CHAT
