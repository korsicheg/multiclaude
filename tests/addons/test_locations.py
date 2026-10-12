"""Tests for the locations add-in (lib/multiclaude/addons/locations.py, its plugin patch and
bin/run-user.sh's ASSISTANT_LOCATION)."""
import os
import shutil
import subprocess

import pytest

from conftest import ALICE_CHAT, BIN, channel, enable_addons, patch, plugin_version, sent, write_json


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
