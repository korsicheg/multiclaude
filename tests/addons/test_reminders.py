"""Tests for the reminders add-in (lib/multiclaude/addons/reminders: a Reminders shortcut's syncs from
the relay, the snapshot it leaves, the reminders queued for the phone; addons/reminders/assistant-reminders)."""
import json
import os
import subprocess
import sys
import urllib.error

import pytest

from conftest import ALICE_CHAT, RELAY, REPO, write_json

CLI = REPO / "addons" / "reminders" / "assistant-reminders"
SHORTCUT_TEXT = ('{"list":"Groceries","title":"Milk","notes":"","due":"","flagged":"No"}\n'
                 '{"list":"Общее","title":"Call the bank","notes":"about the card","due":"2026-10-18T09:00:00+03:00",'
                 '"flagged":"Yes"}')


@pytest.fixture
def rem(r, addons_on, monkeypatch):
    """The add-in, with a relay set up and every answer put on it recorded in rem.answered as (path,
    body); rem.gone makes the relay answer that nothing waits any more."""
    addons_on("reminders")
    from multiclaude.addons import reminders
    from multiclaude.addons.reminders import outbox, snapshot, sync
    from multiclaude.router import relay_entrance
    monkeypatch.setattr(relay_entrance, "settings", lambda: RELAY)
    rem = type("Rem", (), {})()
    rem.module, rem.outbox, rem.snapshot, rem.sync = reminders, outbox, snapshot, sync
    rem.answered, rem.gone = [], False

    def put(url, token, body):
        if rem.gone:
            raise urllib.error.HTTPError(url, relay_entrance.GONE, "Gone", {}, None)
        rem.answered.append((url.removeprefix("https://relay.test"), json.loads(body)))

    monkeypatch.setattr(relay_entrance, "put", put)
    rem.key_hash = sync.keyring.digest(sync.KEY_RING.new_key("alice"))
    return rem


def sync_from_phone(desk, key_hash, sent, request_id="0badcafe", received=1791530000):
    desk.relay_inbound({"type": "reminders", "payload": {
        "id": request_id, "key_hash": key_hash, "sent": sent, "received": received}})


def alices_snapshot(rem):
    return json.loads(rem.snapshot.path(ALICE_CHAT).read_text())


def test_a_sync_becomes_the_latest_snapshot_and_hears_theres_nothing_to_add(r, desk, rem):
    sync_from_phone(desk, rem.key_hash, json.dumps({"reminders": SHORTCUT_TEXT}))

    assert alices_snapshot(rem) == {"updated": 1791530000, "reminders": [
        {"title": "Milk", "list": "Groceries"},
        {"title": "Call the bank", "list": "Общее", "notes": "about the card", "due": "2026-10-18T09:00:00+03:00",
         "flagged": True}]}
    assert rem.answered == [("/reminders/0badcafe", {"add": []})]
    assert rem.snapshot.sent_path(ALICE_CHAT).read_text() == json.dumps({"reminders": SHORTCUT_TEXT})
    assert desk.users["alice"].queue == []  # it never costs a turn


@pytest.mark.parametrize("sent", [
    json.dumps({"reminders": [{"List": "Groceries", "Title": " Milk "}, {"title": "Bread", "priority": "None"}]}),
    json.dumps([{"list": "Groceries", "title": "Milk"}, "Bread"]),
    json.dumps({"Reminders": '{"list": "Groceries",\n "title": "Milk"}\n{"title": "Bread"}'}),
])
def test_what_shortcuts_may_send_is_read_leniently(r, desk, rem, sent):
    sync_from_phone(desk, rem.key_hash, sent)

    assert alices_snapshot(rem)["reminders"] == [{"title": "Milk", "list": "Groceries"}, {"title": "Bread"}]


def test_plain_text_is_a_title_per_line_and_untitled_reminders_are_left_out(r, desk, rem):
    sync_from_phone(desk, rem.key_hash, "Milk\n\n2 eggs\n")
    assert [x["title"] for x in alices_snapshot(rem)["reminders"]] == ["Milk", "2 eggs"]

    sync_from_phone(desk, rem.key_hash, json.dumps({"reminders": [{"title": ""}, {"notes": "x"}, None]}), "0badcaff",
                    received=1791530100)
    assert alices_snapshot(rem)["reminders"] == []


def test_an_older_sync_never_replaces_a_newer_snapshot(r, desk, rem):
    sync_from_phone(desk, rem.key_hash, "Milk", received=1791530500)
    sync_from_phone(desk, rem.key_hash, "Bread", "0badcaff", received=1791530000)

    assert alices_snapshot(rem) == {"updated": 1791530500, "reminders": [{"title": "Milk"}]}
    assert rem.answered[-1] == ("/reminders/0badcaff", {"add": []})


def test_an_unknown_key_is_turned_away(r, desk, rem):
    sync_from_phone(desk, "0" * 64, "Milk")

    assert rem.answered == [("/reminders/0badcafe", {"error": "unknown reminders key"})]
    assert not rem.snapshot.path(ALICE_CHAT).exists()


def test_a_sync_with_a_bad_id_is_dropped(r, desk, rem):
    sync_from_phone(desk, rem.key_hash, "Milk", "../panel")
    assert rem.answered == [] and not rem.snapshot.path(ALICE_CHAT).exists()


def test_queued_reminders_go_with_the_next_sync_and_are_logged_delivered(r, desk, rem):
    first = rem.outbox.add(ALICE_CHAT, "Bread", "Groceries")
    second = rem.outbox.add(ALICE_CHAT, "Call mum", "Общее", notes="Sunday", due="2026-10-18T18:00:00+03:00")
    sync_from_phone(desk, rem.key_hash, "Milk")

    assert rem.answered == [("/reminders/0badcafe", {"add": [
        {"id": first, "list": "Groceries", "title": "Bread", "notes": "", "due": ""},
        {"id": second, "list": "Общее", "title": "Call mum", "notes": "Sunday", "due": "2026-10-18T18:00:00+03:00"}]})]
    kept = rem.outbox.read(ALICE_CHAT)
    assert kept["queued"] == [] and [x["id"] for x in kept["delivered"]] == [first, second]

    sync_from_phone(desk, rem.key_hash, "Milk\nBread", "0badcaff", received=1791530100)
    assert rem.answered[-1] == ("/reminders/0badcaff", {"add": []})  # each goes once


def test_reminders_wait_for_the_next_sync_when_the_shortcut_had_stopped_waiting(r, desk, rem):
    reminder_id = rem.outbox.add(ALICE_CHAT, "Bread", "Groceries")
    rem.gone = True
    sync_from_phone(desk, rem.key_hash, "Milk")

    assert [x["id"] for x in rem.outbox.read(ALICE_CHAT)["queued"]] == [reminder_id]
    rem.gone = False
    sync_from_phone(desk, rem.key_hash, "Milk", "0badcaff", received=1791530100)
    assert [x["id"] for x in rem.answered[-1][1]["add"]] == [reminder_id]


def test_reminders_wait_when_the_relay_cant_be_reached(r, desk, rem, monkeypatch):
    from multiclaude.router import relay_entrance
    monkeypatch.setattr(relay_entrance, "settings", lambda: {})
    rem.outbox.add(ALICE_CHAT, "Bread", "Groceries")
    sync_from_phone(desk, rem.key_hash, "Milk")

    assert [x["title"] for x in rem.outbox.read(ALICE_CHAT)["queued"]] == ["Bread"]


def test_a_cancelled_reminder_never_goes(r, desk, rem):
    kept = rem.outbox.add(ALICE_CHAT, "Bread", "Groceries")
    dropped = rem.outbox.add(ALICE_CHAT, "Cake", "Groceries")
    assert rem.outbox.cancel(ALICE_CHAT, dropped)["title"] == "Cake"
    assert rem.outbox.cancel(ALICE_CHAT, dropped) is None
    sync_from_phone(desk, rem.key_hash, "Milk")

    assert [x["id"] for x in rem.answered[-1][1]["add"]] == [kept]


def test_while_it_is_off_syncs_go_nowhere(r, desk, addons_on):
    addons_on("reminders")
    from multiclaude.addons.reminders import snapshot, sync
    key_hash = sync.keyring.digest(sync.KEY_RING.new_key("alice"))
    addons_on("locations")
    sync_from_phone(desk, key_hash, "Milk")

    assert not snapshot.path(ALICE_CHAT).exists()


# --- assistant reminders ---

@pytest.fixture
def cli(tmp_path):
    """Run `assistant reminders` with tmp_path as the bot's folder, as alice's assistant would."""
    write_json(tmp_path / "users.json", {"alice": {"telegram_id": ALICE_CHAT, "display": "Alice"}})
    env = {**os.environ, "ASSISTANT_ROOT": str(tmp_path), "ASSISTANT_USER": "alice"}

    def run(*args):
        done = subprocess.run([sys.executable, "-I", str(CLI), *args], env=env, capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr

    run.folder = tmp_path / "state" / "reminders"
    return run


def test_the_cli_makes_a_key_once_and_keeps_only_its_hash(cli):
    status, key = cli("key", "alice")
    assert status == 0
    kept = json.loads((cli.folder.parent / "reminders-keys.json").read_text())
    assert key.strip() not in json.dumps(kept) and list(kept.values()) == ["alice"]
    assert cli("revoke", "alice") == (0, "alice's reminders key is revoked\n")
    assert cli("key", "nobody")[0] == 1


def test_the_cli_queues_lists_and_cancels_reminders(cli):
    status, said = cli("add", "--list", "Groceries", "--notes", "2 litres", "Milk")
    assert status == 0 and "next sync" in said
    reminder_id = said.split()[1].rstrip(":")

    assert "Milk · Groceries, notes: 2 litres" in cli("queue")[1]
    assert "hasn't synced yet" in cli("list")[1]
    assert cli("cancel", reminder_id) == (0, f"Cancelled {reminder_id}: Milk\n")
    assert cli("queue")[1] == "Nothing waits for the next sync.\n"


def test_the_cli_shows_the_snapshot_by_list(cli):
    write_json(cli.folder / f"{ALICE_CHAT}.json", {"updated": 1791530000, "reminders": [
        {"title": "Milk", "list": "Groceries"}, {"title": "Call the bank", "list": "Общее", "flagged": True}]})
    status, shown = cli("list")

    assert status == 0
    assert "2 open" in shown and "\nGroceries\n  - Milk\n" in shown and "\nОбщее\n  - Call the bank · flagged\n" in shown


def test_the_cli_refuses_a_due_time_that_isnt_iso_8601_and_an_empty_title(cli):
    assert cli("add", "--list", "Groceries", "--due", "tomorrow", "Milk")[0] == 1
    assert cli("add", "--list", "Groceries", " ")[0] == 1
    assert not (cli.folder / f"{ALICE_CHAT}.outbox.json").exists()
