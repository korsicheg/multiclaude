"""Tests for the add-in mechanism (lib/multiclaude/addons/__init__.py): which add-ins are on, and
calling their hooks so that none can break the core."""
import os
import re
from types import SimpleNamespace

import pytest

from conftest import REPO, enable_addons


@pytest.fixture
def addons(lib):
    from multiclaude import addons
    return addons


@pytest.fixture
def turn_on(lib):
    """turn_on(name, …): those add-ins on in config.json, the rest off."""
    from multiclaude import paths
    return lambda *names: enable_addons(paths.ROOT, *names)


def test_nothing_is_on_without_config_json(addons):
    assert addons.names() == () and addons.active() == []
    assert addons.call("help_info", None, "telegram") == []
    assert addons.through("before_telegram_send", {"chat_id": "1"}, "1", "sendRichMessage") == {"chat_id": "1"}


def test_the_add_ins_config_json_lists_are_on_and_other_names_are_ignored(addons, turn_on):
    turn_on("quiet-hours", "no-such-thing", "voice-notes")
    assert addons.names() == ("voice-notes", "quiet-hours")  # in ALL's order
    assert addons.enabled("quiet-hours") and not addons.enabled("locations")


def test_config_json_is_read_again_once_it_changes(addons, turn_on):
    from multiclaude import paths
    turn_on("locations")
    assert addons.names() == ("locations",)
    turn_on("locations", "skill-notes")
    assert addons.names() == ("locations", "skill-notes")
    os.remove(paths.CONFIG)
    assert addons.names() == ()


def test_an_add_in_without_code_of_its_own_has_no_hooks_and_breaks_nothing(addons, turn_on, monkeypatch):
    monkeypatch.setattr(addons, "ALL", (*addons.ALL, "rules-only"))
    turn_on("rules-only", "locations")
    assert [m.__name__ for m in addons.active()] == ["multiclaude.addons.locations"]
    assert addons.call("help_info", None, "Telegram") == [{"can_send": ["your location, live too"]}]


def fake_modules(addons, turn_on, monkeypatch, **hooks_by_name):
    """Stand-ins for the add-ins' modules, each with the hooks given, and those add-ins on."""
    modules = {name.replace("_", "-"): SimpleNamespace(__name__=f"multiclaude.addons.{name}", **hooks)
               for name, hooks in hooks_by_name.items()}
    monkeypatch.setattr(addons, "_module", modules.get)
    turn_on(*modules)


def test_a_hook_that_fails_is_logged_and_skipped(addons, turn_on, monkeypatch, capsys):
    def broken(app):
        raise RuntimeError("boom")

    fake_modules(addons, turn_on, monkeypatch, locations={"help_info": broken},
                 skill_notes={"help_info": lambda app: app})

    assert addons.call("help_info", "telegram") == ["telegram"]
    assert "add-in locations: help_info failed" in capsys.readouterr().err


def test_through_passes_the_value_from_one_add_in_to_the_next(addons, turn_on, monkeypatch):
    def broken(chat, value):
        raise RuntimeError("boom")

    fake_modules(addons, turn_on, monkeypatch, voice_notes={"hook": lambda chat, value: value + ["a"]},
                 locations={"hook": broken}, quiet_hours={"hook": lambda chat, value: None},
                 skill_notes={"hook": lambda chat, value: value + [chat]})

    assert addons.through("hook", [], "1") == ["a", "1"]


def test_an_add_in_that_cant_load_is_logged_once_and_left_out(addons, turn_on, monkeypatch, capsys):
    import importlib
    real = importlib.import_module

    def failing(name):
        if name.endswith(".locations"):
            raise ImportError("broken")
        return real(name)

    monkeypatch.setattr(importlib, "import_module", failing)
    turn_on("locations", "skill-notes")

    assert [m.__name__ for m in addons.active()] == ["multiclaude.addons.skill_notes"]
    addons.active()
    assert capsys.readouterr().err.count("add-in locations can't load") == 1


def test_the_core_never_imports_an_add_in():
    """Only through addons.call, through and enabled: an add-in's module is loaded only when it's on."""
    importing = re.compile(r"(from multiclaude\.addons(\.\w+)? import|import multiclaude\.addons\.)")
    core = [p for p in (REPO / "lib" / "multiclaude").rglob("*.py") if "addons" not in p.parts]
    assert [str(p) for p in core if importing.search(p.read_text())] == []
