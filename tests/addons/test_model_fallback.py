"""Tests for the model-fallback add-in (lib/multiclaude/addons/model_fallback.py)."""
import os
import time

import pytest

from conftest import ALICE_CHAT, BOB_CHAT, append, last_snapshot, read_json, sent, tap, transcript, write_json

FALLBACK_NOTE = ("Opus 5.5's safeguards flagged this session. You may be seeing this for the first time: Opus 5.5 "
                 "is more capable and has stronger safeguards as a result. Opus 4.8 is answering instead, or you can "
                 "edit and retry with Opus 5.5.\n\nDetails: `[cyber]`")
MOVED = ("⚠️ Opus 5.5's safety filter stopped one of my replies (`[cyber]`), so Claude Code moved this conversation "
         "to Opus 4.8.")


@pytest.fixture
def fallbacks(r, addons_on):
    addons_on("model-fallback")
    from multiclaude.addons import model_fallback
    return model_fallback


def fallback_in(user, stopped="claude-opus-5-5"):
    """Claude Code's entries when a safety classifier stops a reply of stopped and Opus 4.8 answers instead."""
    append(transcript(user),
           {"type": "assistant", "message": {"model": stopped, "stop_reason": "refusal", "content": []}},
           {"type": "assistant", "message": {"model": "<synthetic>", "content": []}},
           {"type": "system", "subtype": "model_refusal_fallback", "content": FALLBACK_NOTE},
           {"type": "assistant", "message": {"model": "claude-opus-4-8", "content": [{"type": "text", "text": "Hi"}]}})


def restore_model(r, desk, name="alice"):
    """Turn restore_model on in their users.json entry."""
    people = read_json(r.paths.USERS)
    people[name]["restore_model"] = True
    write_json(r.paths.USERS, people)
    os.utime(r.paths.USERS, (time.time() + 5, time.time() + 5))
    desk.load_users()


def kinds(user):
    return [type(w).__name__ for w in user.queue]


def test_a_fallback_names_the_stopped_model_and_the_one_answering_instead(fallbacks):
    entry = {"type": "system", "subtype": "model_refusal_fallback", "content": FALLBACK_NOTE}
    assert fallbacks.read(entry, "claude-opus-5-5") == fallbacks.Fallback("claude-opus-5-5", "Opus 4.8", "[cyber]")


def test_after_a_fallback_the_model_is_switched_back_for_those_who_want_it(r, desk, fallbacks):
    restore_model(r, desk)
    alice = desk.users["alice"]
    fallback_in(alice)

    desk.tend(alice, time.time())

    (item,) = alice.queue
    assert (type(item).__name__, item.setting, item.target, item.keep) == ("Switch", "model", "opus", False)
    assert sent(r, ALICE_CHAT) == [f"{MOVED} Switching back to Opus 5.5 once I'm done with what I'm doing."]


def test_a_fallback_is_only_reported_without_restore_model(r, desk, fallbacks):
    bob = desk.users["bob"]
    fallback_in(bob)

    desk.tend(bob, time.time())

    assert bob.queue == []
    assert sent(r, BOB_CHAT) == [f"{MOVED} Send `/model opus` to switch back."]


def test_a_model_that_is_stopped_again_soon_after_switching_back_is_left_alone(r, desk, fallbacks):
    restore_model(r, desk)
    alice, now = desk.users["alice"], time.time()
    fallback_in(alice)
    desk.tend(alice, now)
    alice.queue.clear()

    fallback_in(alice)
    desk.tend(alice, now + fallbacks.RETRY_AFTER - 60)
    assert alice.queue == []
    assert "so I'm staying on Opus 4.8 for now" in sent(r, ALICE_CHAT)[-1]

    fallback_in(alice)
    desk.tend(alice, now + fallbacks.RETRY_AFTER + 60)
    assert kinds(alice) == ["Switch"]


def test_fallbacks_from_before_the_router_started_are_ignored(r, desk, fallbacks):
    fallback_in(desk.users["alice"])
    restarted = r.desk.Desk()
    restarted.load_users()

    restarted.tend(restarted.users["alice"], time.time())

    assert sent(r) == [] and restarted.users["alice"].queue == []


def test_a_fallback_is_neither_reported_nor_undone_while_it_is_off(r, desk, addons_on):
    addons_on("skill-notes")
    restore_model(r, desk)
    alice = desk.users["alice"]
    fallback_in(alice)

    desk.tend(alice, time.time())

    assert sent(r) == [] and alice.queue == []
    assert alice.turn.model == "claude-opus-4-8"  # the core still follows the model


@pytest.mark.parametrize("model, name, family", [
    ("claude-opus-5-5", "Opus 5.5", "opus"),
    ("claude-sonnet-4-5-20250929", "Sonnet 4.5", "sonnet"),
    ("claude-mystery-1", "Mystery 1", None),
    ("", "My usual model", None),
])
def test_model_names(fallbacks, model, name, family):
    assert (fallbacks.name(model), fallbacks.family(model)) == (name, family)


# --- the control panel ---

def test_the_panel_shows_and_sets_restore_model(r, panel, fallbacks):
    tap(panel, "setting", field="restore_model", value=True)

    assert read_json(r.paths.USERS)["alice"]["restore_model"] is True
    snapshot = last_snapshot(r)
    assert snapshot["addons"] == ["model-fallback"] and snapshot["person"]["restore_model"] is True
    assert snapshot["answer"]["ok"]


def test_the_panel_turns_restore_model_off_and_refuses_what_isnt_on_or_off(r, panel, fallbacks):
    tap(panel, "setting", field="restore_model", value=True)
    tap(panel, "setting", field="restore_model", value=False)
    assert "restore_model" not in read_json(r.paths.USERS)["alice"]

    tap(panel, "setting", field="restore_model", value="yes")
    assert last_snapshot(r)["answer"] == {"id": "t-setting", "ok": False, "text": "That one is on or off."}
