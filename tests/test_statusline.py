"""Tests for the status line (system/statusline.sh), which every session on the VM runs."""
import json
import os
import subprocess

import pytest

from conftest import REPO

SCRIPT = REPO / "system" / "statusline.sh"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "work").mkdir(parents=True)
    return h


def payload(home, **overrides):
    data = {"session_id": "abc-123", "session_name": "alice", "cwd": str(home / "work"),
            "workspace": {"current_dir": str(home / "work"), "project_dir": str(home / "work")},
            "model": {"display_name": "Opus 5.5 (1M context)"}, "effort": {"level": "medium"},
            "context_window": {"total_input_tokens": 47300, "context_window_size": 1000000, "used_percentage": 4.7},
            "cost": {"total_cost_usd": 1.25}}
    data.update(overrides)
    return data


def render(home, data):
    r = subprocess.run(["bash", str(SCRIPT)], input=json.dumps(data), capture_output=True, text=True, timeout=30,
                       env={**os.environ, "HOME": str(home)}, cwd=home)
    assert r.returncode == 0, r.stderr
    return r.stdout


def strip_colours(text):
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_renders_model_effort_context_and_folder(home):
    out = render(home, payload(home))
    assert strip_colours(out) == "Opus 5.5 1M·med │ ctx 47.3k/1.0M 4% │ ~/work\n"


def test_renders_even_from_an_empty_payload(home):
    assert strip_colours(render(home, {})).startswith("?")


def test_saves_a_snapshot_per_named_session(home):
    data = payload(home)
    render(home, data)
    assert json.loads((home / ".claude/state/statusline-alice.json").read_text()) == data


def test_keeps_a_cost_ledger_per_session(home):
    render(home, payload(home))
    ledger = json.loads((home / ".claude/state/costs/abc-123.json").read_text())
    assert {k: ledger[k] for k in ("session_id", "name", "dir", "base", "last")} == {
        "session_id": "abc-123", "name": "alice", "dir": str(home / "work"), "base": 0, "last": 1.25}
    first_since = ledger["since"]

    render(home, payload(home, cost={"total_cost_usd": 2.0}))
    ledger = json.loads((home / ".claude/state/costs/abc-123.json").read_text())
    assert (ledger["base"], ledger["last"], ledger["since"]) == (0, 2.0, first_since)

    render(home, payload(home, cost={"total_cost_usd": 0.5}))  # a resume started the counter again
    ledger = json.loads((home / ".claude/state/costs/abc-123.json").read_text())
    assert (ledger["base"], ledger["last"]) == (2.0, 0.5)


def test_a_zero_cost_is_not_recorded(home):
    render(home, payload(home, cost={"total_cost_usd": 0}))
    assert not (home / ".claude/state/costs/abc-123.json").exists()


def test_shows_the_plan_usage_cache(home):
    usage = home / ".local/bin/claude-usage"
    usage.parent.mkdir(parents=True)
    usage.write_text("#!/bin/sh\nexit 0\n")
    usage.chmod(0o755)
    (home / ".config/claude-usage").mkdir(parents=True)
    (home / ".config/claude-usage/credentials.json").write_text("{}")
    (home / ".cache/claude-usage").mkdir(parents=True)
    (home / ".cache/claude-usage/usage.json").write_text(json.dumps(
        {"five_hour": {"utilization": 39.6, "resets_at": "2099-01-01T00:00:00+00:00"},
         "seven_day": {"utilization": 10, "resets_at": "2099-01-01T00:00:00+00:00"}}))

    out = strip_colours(render(home, payload(home)))

    assert " │ 5h 39% ↻" in out and " · 7d 10% ↻" in out
