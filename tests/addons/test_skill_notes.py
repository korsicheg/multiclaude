"""Tests for the skill-notes add-in (lib/multiclaude/addons/skill_notes.py)."""
import time

from conftest import ALICE_CHAT, append, sent, transcript

LOADS_A_SKILL = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Skill", "input": {"skill": "clean-code:clean-code-python"}},
    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}


def test_a_skill_the_assistant_loads_is_shown_quietly_in_its_chat(r, desk, addons_on):
    addons_on("skill-notes")
    alice = desk.users["alice"]
    append(transcript(alice), LOADS_A_SKILL)

    desk.tend(alice, time.time())

    assert sent(r, ALICE_CHAT) == ["🧩 Loaded the `clean-code:clean-code-python` skill"]
    assert [p.get("disable_notification") for m, p in r.api_calls if m == "sendRichMessage"] == [True]


def test_skills_loaded_before_the_router_started_are_not_shown(r, desk, addons_on):
    addons_on("skill-notes")
    append(transcript(desk.users["alice"]), LOADS_A_SKILL)
    restarted = r.desk.Desk()
    restarted.load_users()

    restarted.tend(restarted.users["alice"], time.time())

    assert sent(r) == []


def test_nothing_is_shown_while_it_is_off(r, desk, addons_on):
    addons_on("voice-notes")
    alice = desk.users["alice"]
    append(transcript(alice), LOADS_A_SKILL)

    desk.tend(alice, time.time())

    assert sent(r) == []
