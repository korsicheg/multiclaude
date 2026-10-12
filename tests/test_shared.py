"""Characterization tests for bin/shared.py, run as cron and the router run it."""
import json

import pytest

from conftest import read_json, write_json

OFFICIAL = "claude-plugins-official"
FOO = f"foo@{OFFICIAL}"
MARKET = {"source": {"source": "github", "repo": "someone/market"}}


# --- sync: moving up ---------------------------------------------------------------------------

def test_sync_shares_a_plugin_everyone_has(box):
    box.users("alice", "bob")
    for name in ("alice", "bob"):
        box.settings(name, {"enabledPlugins": {FOO: True}})

    rc, _, _ = box.shared("sync")

    assert rc == 0
    assert box.fake.argvs() == [
        ["plugin", "install", FOO, "--scope", "user"],
        ["plugin", "uninstall", FOO, "--scope", "project", "--keep-data"],
        ["plugin", "uninstall", FOO, "--scope", "project", "--keep-data"],
    ]
    assert box.manifest()["plugins"] == [FOO]
    assert read_json(box.root / ".claude" / "settings.local.json") == {"enabledPlugins": {FOO: False}}


def test_sync_leaves_a_plugin_only_some_have(box):
    box.users("alice", "bob")
    box.settings("alice", {"enabledPlugins": {FOO: True}})

    box.shared("sync")

    assert box.fake.argvs() == []
    assert box.manifest()["plugins"] == []


def test_sync_waits_for_a_plugins_marketplace_to_be_shared(box):
    box.users("alice", "bob")
    for name in ("alice", "bob"):
        box.settings(name, {"enabledPlugins": {"bar@elsewhere": True}})

    _, _, err = box.shared("sync")

    assert box.fake.argvs() == []
    assert "marketplace elsewhere isn't shared yet" in err


def test_sync_removes_copies_of_a_plugin_already_on_in_user_settings(box):
    box.users("alice", "bob")
    for name in ("alice", "bob"):
        box.settings(name, {"enabledPlugins": {FOO: True}})
    write_json(box.config / "settings.json", {"enabledPlugins": {FOO: True}})

    box.shared("sync")

    assert box.fake.argvs() == [["plugin", "uninstall", FOO, "--scope", "project", "--keep-data"]] * 2
    assert box.manifest()["plugins"] == []


def test_sync_drops_the_entry_when_uninstalling_a_copy_fails(box):
    box.users("alice", "bob")
    for name in ("alice", "bob"):
        box.settings(name, {"enabledPlugins": {FOO: True}, "model": "opus"})
    box.fake.on(["plugin", "uninstall"], rc=1)

    box.shared("sync")

    assert read_json(box.ws_settings("alice")) == {"enabledPlugins": {}, "model": "opus"}


def test_sync_shares_a_marketplace_everyone_declares(box):
    box.users("alice", "bob")
    for name in ("alice", "bob"):
        box.settings(name, {"extraKnownMarketplaces": {"market": MARKET}})

    box.shared("sync")

    user = read_json(box.config / "settings.json")
    assert user["extraKnownMarketplaces"] == {"market": {**MARKET, "autoUpdate": True}}
    assert box.manifest()["marketplaces"] == ["market"]
    for name in ("alice", "bob"):
        assert read_json(box.ws_settings(name)) == {"extraKnownMarketplaces": {}}


def test_sync_keeps_marketplaces_with_different_sources_per_person(box):
    box.users("alice", "bob")
    box.settings("alice", {"extraKnownMarketplaces": {"market": MARKET}})
    box.settings("bob", {"extraKnownMarketplaces": {"market": {"source": {"source": "github", "repo": "x/y"}}}})

    box.shared("sync")

    assert box.manifest()["marketplaces"] == []
    assert not (box.config / "settings.json").exists()


def test_sync_shares_a_plugin_once_its_marketplace_moved_up_in_the_same_run(box):
    box.users("alice", "bob")
    for name in ("alice", "bob"):
        box.settings(name, {"extraKnownMarketplaces": {"market": MARKET},
                            "enabledPlugins": {"bar@market": True}})

    box.shared("sync")

    assert ["plugin", "install", "bar@market", "--scope", "user"] in box.fake.argvs()
    assert box.manifest()["plugins"] == ["bar@market"]
    assert box.manifest()["marketplaces"] == ["market"]


def test_sync_shares_a_skill_everyone_has_the_same_files_of(box):
    box.users("alice", "bob")
    for name in ("alice", "bob"):
        box.skill(box.person_skills(name), "notes")

    box.shared("sync")

    assert (box.config / "skills" / "notes" / "SKILL.md").is_file()
    assert box.manifest()["skills"] == ["notes"]
    for name in ("alice", "bob"):
        assert not (box.person_skills(name) / "notes").exists()


def test_sync_leaves_skills_whose_files_differ(box):
    box.users("alice", "bob")
    box.skill(box.person_skills("alice"), "notes", "one")
    box.skill(box.person_skills("bob"), "notes", "two")

    box.shared("sync")

    assert not (box.config / "skills" / "notes").exists()
    assert (box.person_skills("alice") / "notes").is_dir()


def test_sync_dry_run_changes_nothing(box):
    box.users("alice", "bob")
    for name in ("alice", "bob"):
        box.settings(name, {"enabledPlugins": {FOO: True}})
        box.skill(box.person_skills(name), "notes")

    _, _, err = box.shared("sync", "--dry-run")

    assert box.fake.argvs() == []
    assert box.manifest() == {}
    assert not (box.config / "skills").exists()
    assert "(dry run)" in err


def test_sync_with_nobody_registered_does_nothing(box):
    box.users()

    rc, _, _ = box.shared("sync")

    assert rc == 0
    assert box.manifest() == {}


# --- sync: moving back -------------------------------------------------------------------------

def test_sync_moves_a_plugin_back_when_someone_turns_it_off(box):
    box.users("alice", "bob")
    write_json(box.root / "state" / "shared.json", {"plugins": [FOO], "marketplaces": [], "skills": []})
    write_json(box.config / "settings.json", {"enabledPlugins": {FOO: True}})
    box.settings("alice", {"enabledPlugins": {FOO: False}})

    box.shared("sync")

    assert box.fake.argvs() == [
        ["plugin", "install", FOO, "--scope", "project"],
        ["plugin", "uninstall", FOO, "--scope", "user", "--keep-data"],
    ]
    assert box.fake.calls[0]["cwd"].endswith("/workspaces/bob")
    assert read_json(box.ws_settings("alice")) == {"enabledPlugins": {}}
    assert box.manifest()["plugins"] == []


def test_sync_moves_a_skill_back_when_someone_turns_it_off(box):
    box.users("alice", "bob")
    write_json(box.root / "state" / "shared.json", {"plugins": [], "marketplaces": [], "skills": ["notes"]})
    box.skill(box.config / "skills", "notes")
    box.settings("alice", {"skillOverrides": {"notes": "off"}})

    box.shared("sync")

    assert (box.person_skills("bob") / "notes" / "SKILL.md").is_file()
    assert not (box.person_skills("alice") / "notes").exists()
    assert not (box.config / "skills" / "notes").exists()
    assert len(list((box.root / "state" / "shared-trash").iterdir())) == 1
    assert read_json(box.ws_settings("alice")) == {"skillOverrides": {}}
    assert box.manifest()["skills"] == []


# --- sync: strays ------------------------------------------------------------------------------

def test_sync_reports_something_installed_straight_into_user_scope_once(box):
    box.users("alice")
    box.shared("sync")  # takes the baseline
    write_json(box.config / "settings.json", {"enabledPlugins": {"stray@x": True}})

    _, _, first = box.shared("sync")
    _, _, second = box.shared("sync")

    assert "Installed straight into ~/.claude" in first and "plugin stray@x" in first
    assert "Installed straight" not in second
    assert box.manifest()["reported"] == {"plugins": ["stray@x"]}


def test_sync_baseline_holds_what_was_in_user_scope_before_the_first_run(box):
    box.users("alice")
    write_json(box.config / "settings.json", {"enabledPlugins": {"old@x": True}})

    _, _, err = box.shared("sync")

    assert "Installed straight" not in err
    assert box.manifest()["baseline"]["plugins"] == ["old@x"]


# --- the router's command contract -------------------------------------------------------------

def test_command_for_an_unknown_person(box):
    box.users("alice")

    assert box.command("bob", "/plugin") == {"text": "No user bob.", "reload": False}


def test_plugin_overview(box):
    box.users("alice")
    write_json(box.config / "settings.json", {"enabledPlugins": {"shared@m": True}})
    box.settings("alice", {"enabledPlugins": {"mine@m": True, "off@m": False}})

    reply = box.command("alice", "/plugin")

    assert reply == {"reload": False, "text": "\n".join([
        "🔌 Plugins", "Shared with everyone: shared@m", "Yours: mine@m",
        "Turned off for you: off@m", "", "`/plugin help`: all commands"])}


def test_plugin_install_runs_claude_at_project_scope(box):
    box.users("alice")
    box.fake.on(["plugin", "install"], stdout=json.dumps({"outcome": "ok", "message": "Installed foo"}) + "\n")

    reply = box.command("alice", "/plugin install foo@m")

    assert box.fake.argvs()[0] == ["plugin", "install", "foo@m", "--scope", "project", "--json"]
    assert box.fake.calls[0]["cwd"].endswith("/workspaces/alice")
    assert reply == {"reload": True, "text": "✅ Installed foo\n\n"
                                             "Claude reloads its plugins and skills once it's idle."}


def test_plugin_install_reports_claudes_error(box):
    box.users("alice")
    box.fake.on(["plugin", "install"], rc=1,
                stdout=json.dumps({"outcome": "error", "error": "Plugin not found"}) + "\n")

    reply = box.command("alice", "/plugin install nope@m")

    assert reply == {"reload": False, "text": "⚠️ Plugin not found"}


def test_plugin_install_refuses_the_telegram_plugin(box):
    box.users("alice")

    reply = box.command("alice", "/plugin install telegram")

    assert "can't be installed here" in reply["text"]
    assert not [a for a in box.fake.argvs() if a[:2] == ["plugin", "install"]]


def test_plugin_disable_of_a_shared_plugin_turns_it_off_for_that_person(box):
    box.users("alice")
    write_json(box.config / "settings.json", {"enabledPlugins": {FOO: True}})

    reply = box.command("alice", "/plugin disable foo")

    assert box.fake.argvs()[0] == ["plugin", "disable", FOO, "--scope", "project"]
    assert reply["reload"] is True
    assert reply["text"].startswith(f"✅ Turned {FOO} off for you.")


def test_skills_off_writes_the_override(box):
    box.users("alice")
    box.settings("alice", {"model": "opus"})

    reply = box.command("alice", "/skills off notes")

    assert read_json(box.ws_settings("alice")) == {"model": "opus", "skillOverrides": {"notes": "off"}}
    assert reply == {"reload": True, "text": "✅ Skill notes: off.\n\n"
                                             "Claude reloads its plugins and skills once it's idle."}


def test_skills_on_removes_the_override(box):
    box.users("alice")
    box.settings("alice", {"skillOverrides": {"notes": "off"}})

    box.command("alice", "/skills on notes")

    assert read_json(box.ws_settings("alice")) == {"skillOverrides": {}}


def test_skills_usage(box):
    box.users("alice")

    assert box.command("alice", "/skills off")["text"] == "Usage: `/skills off <name>`, `/skills on <name>`"


def test_mcp_add_url_server_gets_http_and_the_per_person_header(box):
    box.users("alice")
    ws = str(box.workspaces / "alice")
    box.fake.on(["mcp", "add"], merge={str(box.claude_json): {
        "projects": {ws: {"mcpServers": {"srv": {"type": "http", "url": "https://x"}}}}}})

    reply = box.command("alice", "/mcp add srv https://x")

    assert box.fake.argvs()[0] == ["mcp", "add", "-H", "X-Assistant-User: alice", "--scope", "local",
                                   "--transport", "http", "srv", "https://x"]
    assert reply == {"reload": False, "session": ["/reload-plugins", "/mcp reconnect srv"],
                     "text": "✅ Added srv to your folder.\nIf it needs you to sign in: `/mcp login srv`",
                     "copy": ["/mcp login srv"]}


def test_mcp_add_drops_a_scope_the_person_typed(box):
    box.users("alice")
    ws = str(box.workspaces / "alice")
    box.fake.on(["mcp", "add"], merge={str(box.claude_json): {
        "projects": {ws: {"mcpServers": {"tool": {"type": "stdio"}}}}}})

    reply = box.command("alice", "/mcp add -s user tool -- npx tool")

    assert box.fake.argvs()[0] == ["mcp", "add", "--scope", "local", "tool", "--", "npx", "tool"]
    assert reply["text"] == "✅ Added tool to your folder."


def test_mcp_add_json_fixes_phone_quotes_and_adds_the_header(box):
    box.users("alice")
    ws = str(box.workspaces / "alice")
    box.fake.on(["mcp", "add-json"], merge={str(box.claude_json): {
        "projects": {ws: {"mcpServers": {"srv": {"type": "http"}}}}}})

    box.command("alice", '/mcp add-json srv {“type”:“http”,“url”:“https://x”}')

    args = box.fake.argvs()[0]
    assert args[:5] == ["mcp", "add-json", "--scope", "local", "srv"]
    assert json.loads(args[5]) == {"type": "http", "url": "https://x", "headers": {"X-Assistant-User": "alice"}}


def test_mcp_add_of_a_disabled_name_enables_it_first(box):
    box.users("alice")
    ws = str(box.workspaces / "alice")
    write_json(box.claude_json, {"projects": {ws: {"disabledMcpServers": ["srv"]}}})
    box.fake.on(["mcp", "add"], merge={str(box.claude_json): {
        "projects": {ws: {"mcpServers": {"srv": {"type": "stdio"}}}}}})

    reply = box.command("alice", "/mcp add srv -- run")

    assert reply["session"] == ["/reload-plugins", "/mcp enable srv", "/mcp reconnect srv"]


def test_mcp_session_commands(box):
    box.users("alice")

    assert box.command("alice", "/mcp enable srv") == {
        "text": "", "reload": False, "session": ["/reload-plugins", "/mcp enable srv"]}
    assert box.command("alice", "/mcp disable srv")["session"] == ["/mcp disable srv"]
    assert box.command("alice", "/mcp reconnect srv")["session"] == ["/reload-plugins", "/mcp reconnect srv"]


def test_mcp_remove_of_a_server_not_in_the_folder(box):
    box.users("alice")

    reply = box.command("alice", "/mcp remove srv")

    assert reply["text"].startswith("srv isn't in your folder")
    assert box.fake.argvs() == []


def test_mcp_get_hides_secrets(box):
    box.users("alice")
    box.fake.on(["mcp", "get"], stdout="\n".join([
        "srv:",
        "  Type: http",
        "  URL: https://api.example.com/mcp?key=secret",
        "  Headers:",
        "    Authorization: Bearer abc",
        "    X-Assistant-User: alice",
        "  Environment:",
        "    API_TOKEN=hunter2",
        "  Args: --key " + "k" * 40,
        "",
        "To remove this server, run: claude mcp remove srv",
    ]))

    text = box.command("alice", "/mcp get srv")["text"]

    assert text == "\n".join([
        "srv:",
        "  Type: http",
        "  URL: https://api.example.com/mcp?•••",
        "  Headers:",
        "    Authorization: •••",
        "    X-Assistant-User: alice",
        "  Environment:",
        "    API_TOKEN=•••",
        "  Args: --key •••",
    ])


def test_long_replies_are_cut_at_a_line_break(box):
    box.users("alice")
    box.fake.on(["mcp", "list"], stdout="".join(f"server{i:04}: cmd - ✔ Connected\n" for i in range(1500)))

    text = box.command("alice", "/mcp")["text"]

    assert len(text) <= 32000 + 2
    assert text.endswith("\n…")


# --- turning a shared item off -----------------------------------------------------------------

def test_a_failed_move_back_keeps_the_persons_off_choice(box):
    box.users("alice", "bob")
    write_json(box.root / "state" / "shared.json", {"plugins": [FOO], "marketplaces": [], "skills": []})
    write_json(box.config / "settings.json", {"enabledPlugins": {FOO: True}})
    box.settings("alice", {"enabledPlugins": {FOO: False}})
    box.fake.on(["plugin", "install"], rc=1)  # bob's copy can't be installed, so it stays shared

    box.shared("sync")

    assert read_json(box.ws_settings("alice")) == {"enabledPlugins": {FOO: False}}
    assert ["plugin", "uninstall", FOO, "--scope", "project", "--keep-data"] not in box.fake.argvs()
    assert box.manifest()["plugins"] == [FOO]


# --- a settings file that isn't valid JSON -----------------------------------------------------

def break_settings(box, name):
    path = box.ws_settings(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{oops")
    return path


def test_sync_survives_a_broken_settings_file_and_leaves_it_alone(box):
    box.users("alice", "bob")
    box.settings("alice", {"enabledPlugins": {FOO: True}})
    broken = break_settings(box, "bob")
    for name in ("alice", "bob"):
        box.skill(box.person_skills(name), "notes")

    rc, _, err = box.shared("sync")

    assert rc == 0
    assert broken.read_text() == "{oops"
    assert "isn't valid JSON" in err
    assert box.manifest()["skills"] == ["notes"]  # what doesn't depend on that file still moves
    assert box.manifest()["plugins"] == []


def test_a_command_does_not_overwrite_a_broken_settings_file(box):
    box.users("alice")
    broken = break_settings(box, "alice")

    reply = box.command("alice", "/skills off notes")

    assert broken.read_text() == "{oops"
    assert reply["reload"] is False
    assert reply["text"].startswith("⚠️ ") and "isn't valid JSON" in reply["text"]


def test_someone_elses_broken_settings_do_not_fail_my_command(box):
    box.users("alice", "bob")
    break_settings(box, "bob")
    box.fake.on(["plugin", "install"], stdout=json.dumps({"outcome": "ok", "message": "Installed foo"}) + "\n")

    reply = box.command("alice", "/plugin install foo@m")

    assert reply["text"].startswith("✅ Installed foo")


# --- the manifest ------------------------------------------------------------------------------

def test_a_move_is_recorded_even_if_the_run_dies_after_it(box):
    box.users("alice", "bob")
    for name in ("alice", "bob"):
        box.settings(name, {"extraKnownMarketplaces": {"market": MARKET}, "enabledPlugins": {"bar@market": True}})
    (box.root / ".claude" / "settings.local.json").mkdir(parents=True)  # the front desk edit after it will fail

    rc, _, err = box.shared("sync")

    assert rc != 0 and "IsADirectoryError" in err
    assert "market" in read_json(box.config / "settings.json")["extraKnownMarketplaces"]
    assert box.manifest()["marketplaces"] == ["market"]


# --- skill copies ------------------------------------------------------------------------------

def test_removed_skill_copies_go_to_the_trash(box):
    box.users("alice", "bob")
    for name in ("alice", "bob"):
        box.skill(box.person_skills(name), "notes")

    box.shared("sync")

    trashed = sorted(d.name.split("-", 2)[2] for d in (box.root / "state" / "shared-trash").iterdir())
    assert trashed == ["alice-notes", "bob-notes"]


def test_a_skill_copy_edited_since_it_was_compared_is_kept(box, shared):
    box.users("alice")
    copy = box.skill(box.person_skills("alice"), "notes", "old")
    person = shared.people.Person("alice", "Alice")
    (copy / "SKILL.md").write_text("edited meanwhile")

    shared.sync.Sync().remove_skill_copy(person, "notes", person.skills["notes"])

    assert (copy / "SKILL.md").read_text() == "edited meanwhile"


def test_a_skill_copy_that_fails_halfway_leaves_nothing_behind(box, shared, tmp_path):
    src = box.skill(tmp_path / "src", "notes")
    secret = src / "unreadable.md"
    secret.write_text("x")
    secret.chmod(0)
    dst = box.person_skills("alice") / "notes"

    with pytest.raises(OSError):
        shared.skillfiles.copy_skill(src, dst)

    assert not dst.exists()


# --- command edge cases ------------------------------------------------------------------------

def test_a_skill_name_cannot_be_a_path(box):
    box.users("alice", "bob")
    box.skill(box.workspaces / "bob" / ".claude" / "skills", "x")

    reply = box.command("alice", "/skills on ../../bob/.claude/skills/x")

    assert reply == {"text": "../../bob/.claude/skills/x isn't a skill name.", "reload": False}
    assert not box.ws_settings("alice").exists()


def test_browsing_plugins_does_not_run_a_sync(box):
    box.users("alice")

    box.command("alice", "/plugin install")

    assert box.manifest() == {}


def test_adding_a_marketplace_runs_a_sync_whatever_the_case(box):
    box.users("alice")

    box.command("alice", "/plugin Marketplace ADD someone/market")

    assert box.manifest() != {}


def test_old_trash_is_emptied(box):
    import os
    import time
    box.users("alice")
    old = box.root / "state" / "shared-trash" / "20260801-000000-notes"
    old.mkdir(parents=True)
    recent = box.root / "state" / "shared-trash" / "20261008-000000-tips"
    recent.mkdir()
    month_ago = time.time() - 31 * 86400
    os.utime(old, (month_ago, month_ago))

    box.shared("sync")

    assert not old.exists() and recent.exists()


def test_marketplace_add_replies_with_its_outcome_and_its_name(box):
    box.users("alice")
    box.fake.on(["plugin", "marketplace", "add"], stdout=json.dumps({
        "command": "marketplace-add", "outcome": "ok", "marketplace": "context7-marketplace",
        "message": "SSH not configured, cloning via HTTPS: https://github.com/upstash/context7.git\n"
                   "Cloning repository (timeout: 120s)…\nClone complete, validating marketplace…\n"
                   "Successfully added marketplace: context7-marketplace (declared in project settings)"}) + "\n")

    reply = box.command("alice", "/plugin marketplace add upstash/context7")

    assert reply["text"] == ("✅ Successfully added marketplace: context7-marketplace (declared in project settings)\n"
                             "Browse it: `/plugin install context7-marketplace`")


def test_a_failure_keeps_claudes_whole_message(box):
    box.users("alice")
    box.fake.on(["plugin", "install"], rc=1, stdout=json.dumps({
        "outcome": "error", "error": "Fetching…\nPlugin foo not found in marketplace m"}) + "\n")

    reply = box.command("alice", "/plugin install foo@m")

    assert reply["text"] == "⚠️ Fetching…\nPlugin foo not found in marketplace m"
