"""Tests for the control panel's VM side: router/panel.py, cronjobs.py and jobruns.py. The relay's
side is tested in relay/test/panel.test.js, how the page words things in relay/test/format.test.js and
its sparklines in relay/test/sparkline.test.js."""
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import ALICE_CHAT, BOB_CHAT, CRONTAB, TOKEN, last_snapshot, read_json, signed, tap, write_json

PAUSED = "#⏸ "  # cronjobs.PAUSED

# --- who opened it ---

def test_init_data_telegram_signed_names_its_sender(r, panel):
    assert r.panel_module.signed_sender(signed(ALICE_CHAT), TOKEN, time.time()) == ALICE_CHAT


@pytest.mark.parametrize("init_data", [
    signed(ALICE_CHAT, token="999:another-bot"),
    signed(ALICE_CHAT, auth_date=int(time.time()) - 2 * 24 * 3600),
    signed(ALICE_CHAT).replace("first_name", "last_name"),
    "user=%7B%22id%22%3A100%7D",
    "",
])
def test_init_data_not_signed_by_telegram_for_this_bot_names_no_one(r, panel, init_data):
    assert r.panel_module.signed_sender(init_data, TOKEN, time.time()) is None


def test_a_request_nobody_here_signed_changes_nothing(r, panel):
    panel.handle({"init_data": signed(ALICE_CHAT, token="999:another-bot"), "id": "x", "action": "setting",
                  "args": {"field": "timezone", "value": "Asia/Tokyo"}})
    assert read_json(r.paths.USERS)["alice"].get("timezone") is None
    assert r.pushed == []


# --- /config ---

def test_config_sends_a_button_that_opens_the_panel(r, desk, panel):
    desk.inbound({"type": "queue-operation", "operation": "enqueue", "content":
                  f'<channel source="plugin:telegram:telegram" chat_id="{ALICE_CHAT}" message_id="7" user="a" '
                  f'user_id="{ALICE_CHAT}" ts="2026-10-11T21:00:00.000Z">\n/config\n</channel>'})
    method, params = r.api_calls[-1]
    assert method == "sendRichMessage" and params["chat_id"] == ALICE_CHAT
    assert params["reply_markup"] == {"inline_keyboard": [[
        {"text": "⚙️ Open the control panel", "web_app": {"url": "https://relay.test/app/"}}]]}


def test_the_button_by_the_message_box_opens_the_panel_for_everyone(r, panel):
    r.panel_module.set_menu_button()
    assert r.api_calls[-1] == ("setChatMenuButton", {"menu_button": {
        "type": "web_app", "text": "Panel", "web_app": {"url": "https://relay.test/app/"}}})


# --- what it shows ---

def test_opening_the_panel_puts_a_snapshot_then_another_with_the_servers_checked(r, panel):
    tap(panel, "refresh")

    assert [url for url, _, _ in r.pushed] == ["https://relay.test/panel/100"] * 2
    assert r.pushed[0][1] == "vm-token"
    first, second = r.pushed[0][2], r.pushed[1][2]
    assert first["mcp"]["checking"] and not second["mcp"]["checking"]
    assert [(s["name"], s["health"]) for s in second["mcp"]["servers"]] == [("google", "ok"), ("maps", "auth")]
    assert second["answer"] == {"id": "t-refresh", "ok": True, "text": ""}
    assert second["person"] == {"display": "Alice", "timezone": "UTC"}
    assert (second["bot"], second["addons"]) == ("Claude", [])
    assert second["usage"]["plan"] == [{"label": "5-hour", "used": 12.0, "resets": 1_791_540_000.0}]
    assert second["claude"]["models"] == ["opus", "sonnet", "haiku", "fable"]
    assert (second["claude"]["saved_model"], second["claude"]["saved_effort"]) == (None, None)


def test_the_panel_lists_only_the_persons_own_jobs_with_their_next_run(r, panel):
    tap(panel, "refresh")
    jobs = last_snapshot(r)["jobs"]
    assert [(j["title"], j["paused"]) for j in jobs] == [("Morning brief at 09:00 Helsinki", False),
                                                         ("Invoice sync, Sundays 10:00 Helsinki", False)]
    assert jobs[0]["detail"] == "Runs at 06 and 07 UTC"
    assert jobs[1]["detail"] == "file new invoices"
    assert datetime.fromtimestamp(jobs[0]["next"], ZoneInfo("Europe/Helsinki")).hour == 9


def test_an_unchanged_panel_isnt_put_on_the_relay_again(r, desk, panel):
    alice = desk.users["alice"]
    panel.update(alice)
    panel.update(alice)
    assert len(r.pushed) == 1


def test_a_change_is_put_on_the_relay_by_itself(r, desk, panel):
    alice = desk.users["alice"]
    panel.update(alice)
    r.users_module.update("alice", "timezone", "Europe/Helsinki")
    panel.update(alice)
    assert [p["person"]["timezone"] for _, _, p in r.pushed] == ["UTC", "Europe/Helsinki"]


def test_usage_counts_as_changed_only_in_steps(r, desk, panel, monkeypatch):
    alice = desk.users["alice"]
    context = {"used": 0}
    monkeypatch.setattr(r.usage, "snapshot", lambda user: {"context_window": {
        "total_input_tokens": context["used"], "context_window_size": 100_000}})
    for used in (10_000, 12_000, 14_000, 16_000):
        context["used"] = used
        panel.update(alice)
    assert [p["usage"]["context"]["percent"] for _, _, p in r.pushed] == [10, 16]


def test_after_a_restart_the_router_remembers_what_it_put_on_the_relay(r, desk, panel):
    alice = desk.users["alice"]
    panel.refresh(alice, check_servers=True)
    pushed = len(r.pushed)

    restarted = r.panel_module.Panel(desk, desk.panel.later)
    restarted.update(alice)
    assert len(r.pushed) == pushed
    assert restarted.snapshot(alice)["mcp"]["servers"] == last_snapshot(r)["mcp"]["servers"]


def test_the_router_looks_for_changes_every_half_minute(r, desk, panel):
    panel.tick(1000.0)
    panel.tick(1010.0)
    panel.tick(1031.0)
    assert len(r.pushed) == 2  # alice and bob, once: the second look found nothing new


# --- the machine, for the owner ---

def keep_a_sample(when, cpu=12.0):
    """A sample of the machine, kept as `assistant health sample` keeps it."""
    from multiclaude.health import history
    history.keep({"time": when, "uptime": 3600, "cpu": {"percent": cpu, "load": [0.5, 0.4, 0.3], "cores": 4},
                  "memory": {"total_mb": 8192, "available_mb": 4096, "percent": 50.0},
                  "swap": {"total_mb": 0, "used_mb": 0, "percent": 0.0},
                  "disks": [{"mount": "/", "total_mb": 102_400, "used_mb": 40_960, "free_mb": 61_440, "percent": 40.0}],
                  "top": [{"name": "Chrome", "mb": 1500, "count": 34}], "router": True,
                  "sessions": [{"name": "alice", "display": "Alice", "running": True}], "ticks": [600, 2000],
                  "alerts": [{"rule": "router", "level": "high", "since": when, "text": "The router is down"}]})


def test_the_owner_sees_the_machine_and_no_one_else_does(r, panel):
    keep_a_sample(time.time() - 60)

    tap(panel, "refresh")
    machine = last_snapshot(r)["machine"]
    tap(panel, "refresh", chat=BOB_CHAT)

    assert "ticks" not in machine["sample"]
    assert (machine["sample"]["cpu"]["percent"], machine["sample"]["alerts"][0]["text"]) == (12.0, "The router is down")
    assert len(machine["day"]["cpu"]) == 96 and machine["day"]["cpu"][-1] == 12 and machine["day"]["disk"][-1] == 40
    assert machine["limits"] == {"cpu": 90, "ram": 90, "disk": 85}
    assert "machine" not in last_snapshot(r)


def test_before_the_first_sample_the_owner_sees_there_is_none(r, panel):
    tap(panel, "refresh")
    assert last_snapshot(r)["machine"] == {"sample": None}


def test_the_machine_puts_the_owners_panel_again_only_with_a_new_sample(r, desk, panel):
    alice, when = desk.users["alice"], time.time() - 60
    keep_a_sample(when)
    panel.update(alice)
    panel.update(alice)
    keep_a_sample(when + 300, cpu=40.0)
    panel.update(alice)
    assert [p["machine"]["sample"]["cpu"]["percent"] for _, _, p in r.pushed] == [12.0, 40.0]


# --- what the jobs did ---

def record_runs(r, name, *runs):
    """name's record of job runs, as `assistant job` writes it."""
    path = r.paths.JOB_RUNS / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(run) + "\n" for run in runs))


def a_run(started, title="brief", outcome="sent", **extra):
    return {"started": started, "finished": started + 240, "title": title, "outcome": outcome, **extra}


def test_the_activity_lists_the_persons_own_runs_newest_first_under_their_jobs_names(r, panel):
    record_runs(r, "alice", a_run(1000, cost_usd=0.12, preview="**Rain** at 5pm"),
                a_run(2000, title="invoices", outcome="failed", preview="API Error: 529"),
                a_run(3000, title="a one-off look around", outcome="nothing"))
    record_runs(r, "bob", a_run(2500, title="hers"))

    tap(panel, "refresh")

    runs = last_snapshot(r)["activity"]["runs"]
    assert [(run["title"], run["outcome"]) for run in runs] == [
        ("a one-off look around", "nothing"), ("Invoice sync, Sundays 10:00 Helsinki", "failed"),
        ("Morning brief at 09:00 Helsinki", "sent")]
    assert runs[-1] == {"started": 1000, "finished": 1240, "title": "Morning brief at 09:00 Helsinki",
                        "outcome": "sent", "cost": 0.12, "preview": "Rain at 5pm"}
    assert (runs[0]["cost"], runs[0]["preview"]) == (None, "")


def test_the_activity_lists_the_newest_runs_and_counts_every_run_kept(r, panel, monkeypatch):
    monkeypatch.setattr(r.panel_module, "RUNS_SHOWN", 2)
    now = time.time()
    record_runs(r, "alice", *(a_run(now - ago) for ago in (30, 20, 10)))
    tap(panel, "refresh")
    activity = last_snapshot(r)["activity"]
    assert [run["started"] for run in activity["runs"]] == [now - 10, now - 20]
    assert activity["week"]["runs"] == 3


def test_the_activity_counts_runs_failures_and_cost_today_in_the_persons_timezone_and_over_7_days(r, panel):
    r.users_module.update("alice", "timezone", "Pacific/Kiritimati")  # UTC+14: its day isn't UTC's
    midnight = r.panel_module.day_start(time.time(), "Pacific/Kiritimati")
    record_runs(r, "alice",
                a_run(midnight - 8 * 24 * 3600, cost_usd=5.0),  # over a week ago
                a_run(midnight - 60, outcome="failed", cost_usd=0.25),  # yesterday there
                a_run(midnight + 1, cost_usd=0.1), a_run(midnight + 2, outcome="failed"),
                a_run(midnight + 3, outcome="skipped"))  # didn't run

    tap(panel, "refresh")

    activity = last_snapshot(r)["activity"]
    assert activity["today"] == {"runs": 2, "failed": 1, "cost": 0.1}
    assert activity["week"] == {"runs": 3, "failed": 2, "cost": 0.35}


def test_a_day_starts_at_midnight_in_the_persons_timezone_or_utc(r, panel):
    now = datetime(2026, 10, 13, 22, 30, tzinfo=timezone.utc).timestamp()  # already the 14th in Helsinki
    helsinki = datetime(2026, 10, 14, tzinfo=ZoneInfo("Europe/Helsinki")).timestamp()
    assert r.panel_module.day_start(now, "Europe/Helsinki") == helsinki
    assert r.panel_module.day_start(now, "Mars/Olympus") == datetime(2026, 10, 13, tzinfo=timezone.utc).timestamp()


def test_lines_of_a_record_that_arent_runs_are_left_out(r, panel):
    from multiclaude import jobruns
    record_runs(r, "alice")
    with open(r.paths.JOB_RUNS / "alice.jsonl", "w") as record:
        record.write('not json\n[1]\n{"started": "soon", "title": "x", "outcome": "sent"}\n'
                     '{"started": 1, "title": "x", "outcome": "exploded"}\n' + json.dumps(a_run(1000)) + "\n{broken")
    assert [run["started"] for run in jobruns.runs("alice")] == [1000]


def test_a_record_that_cant_be_read_shows_no_runs(r, panel):
    (r.paths.JOB_RUNS / "alice.jsonl").mkdir(parents=True)
    tap(panel, "refresh")
    assert last_snapshot(r)["activity"]["runs"] == []
    assert last_snapshot(r)["activity"]["today"] == {"runs": 0, "failed": 0, "cost": 0}


@pytest.mark.parametrize("markdown, shown", [
    ("### 🌅 Morning\n\n**Rain** at `5pm`, see [the forecast](https://x.test).",
     "🌅 Morning\nRain at 5pm, see the forecast."),
    ("> quoted ~~old~~ ||secret|| __bold__", "quoted old secret bold"),
    ('<tg-time unix="1" format="t">Fri 21:00</tg-time>, <details><summary>More', "Fri 21:00, More"),
    ('cut off in a tag <tg-time unix="17', "cut off in a tag"),
])
def test_a_preview_shows_the_words_of_the_answer_without_its_markdown(r, panel, markdown, shown):
    from multiclaude import jobruns
    assert jobruns.plain(markdown) == shown


def test_a_runs_title_is_its_first_line_cut_as_assistant_job_cuts_it(r, panel):
    from multiclaude import jobruns
    assert jobruns.title("Brief: follow brief.md\n\nThe data…") == "Brief: follow brief.md"
    assert jobruns.title("é" * 150) == "é" * 99 + "…"  # as in test_assistant_cli.py


@pytest.mark.parametrize("line, prompt", [
    ('0 8 * * * assistant job --user alice "brief"', "brief"),
    ('0 6,7 * * * [ "$(TZ=Europe/Helsinki date +\\%H)" = 09 ] && ~/assistant/bin/assistant job --user alice '
     '--model sonnet --deny mcp__mail__send "Brief: 100\\% of it"; crontab -l | grep -v x | crontab -', "Brief: 100% of it"),
    ("@daily assistant job --user alice it\\'s 'all here' >> log 2>&1", "it's all here"),
    (f'{PAUSED}0 8 * * * assistant job "a \\"quoted\\" word"', 'a "quoted" word'),
    ('0 8 * * * assistant job "x" % the rest is its input', "x"),
    ('0 8 * * * assistant notify --user alice "not a job"', None),
    ('0 8 * * * assistant job "never closed', None),
])
def test_a_jobs_instructions_are_read_from_its_crontab_line_as_the_job_gets_them(r, panel, line, prompt):
    assert r.cronjobs.Job("x", (line,)).prompts == ([prompt] if prompt else [])


# --- what it changes ---

def test_a_setting_is_saved_in_the_persons_own_entry(r, panel):
    tap(panel, "setting", field="timezone", value="Europe/Helsinki")
    tap(panel, "setting", field="display", value=" Alicia ")

    users = read_json(r.paths.USERS)
    assert (users["alice"]["timezone"], users["alice"]["display"]) == ("Europe/Helsinki", "Alicia")
    assert "timezone" not in users["bob"]
    assert last_snapshot(r)["answer"] == {"id": "t-setting", "ok": True, "text": "Saved."}


@pytest.mark.parametrize("field, value", [("timezone", "Mars/Olympus"), ("display", " "), ("role", "owner"),
                                          ("quiet_hours", "00:00-09:00"), ("restore_model", True)])  # add-ins off
def test_a_setting_it_cant_take_is_refused_and_says_why(r, panel, field, value):
    before = read_json(r.paths.USERS)
    tap(panel, "setting", field=field, value=value)
    assert read_json(r.paths.USERS) == before
    answer = last_snapshot(r)["answer"]
    assert not answer["ok"] and answer["text"]


def switches(desk):
    return [(w.setting, w.target, w.keep) for w in desk.users["alice"].queue if type(w).__name__ == "Switch"]


def test_a_model_switches_this_session_only_like_model_opus(r, desk, panel):
    tap(panel, "model", target="sonnet", keep=False)
    assert switches(desk) == [("model", "sonnet", False)]
    assert desk.users["alice"].reply_to.app == "Telegram"


def test_with_keep_a_model_is_also_kept_after_restarts_like_model_opus_default(r, desk, panel):
    tap(panel, "model", target="haiku", keep=True)
    assert switches(desk) == [("model", "haiku", True)]
    assert last_snapshot(r)["answer"]["text"] == "Switching to haiku, also after restarts. Claude confirms in the chat."


def test_max_effort_switches_this_session_only_even_with_keep(r, desk, panel):
    tap(panel, "effort", target="max", keep=True)
    assert switches(desk) == [("effort", "max", False)]
    assert "can't keep max" in last_snapshot(r)["answer"]["text"]


def test_the_panel_shows_the_model_running_now_and_the_one_after_a_restart(r, desk, panel, monkeypatch):
    write_json(r.paths.workspace_settings("alice"), {"model": "opus[1m]", "effortLevel": "high"})
    monkeypatch.setattr(r.usage, "snapshot", lambda user: {"model": {"display_name": "Sonnet 5.5"}, "effort": {"level": "low"}})
    tap(panel, "refresh")
    claude = last_snapshot(r)["claude"]
    assert (claude["current_model"], claude["effort"]) == ("sonnet", "low")
    assert (claude["saved_model"], claude["saved_effort"]) == ("opus", "high")


def test_an_mcp_tap_runs_the_bots_own_mcp_command(r, desk, panel, monkeypatch):
    commands = []
    monkeypatch.setattr(desk, "dispatch", lambda user, msg, now: commands.append((user.name, msg.text)))
    tap(panel, "mcp", command="reconnect", server="google")
    tap(panel, "mcp", command="rm -rf", server="google")
    assert commands == [("alice", "/mcp reconnect google")]
    assert not last_snapshot(r)["answer"]["ok"]


# --- plugins and skills ---

def a_plugin_setup(r):
    """Alice has a plugin with an MCP server and a skill, a language-server plugin he turned off, and
    two skills of her own, one turned off; Bob has nothing."""
    market = r.paths.HOME / "marketplaces" / "tools-market"
    write_json(market / ".claude-plugin" / "marketplace.json", {"name": "tools-market", "plugins": [
        {"name": "tools", "source": "./plugins/tools", "description": "Tools for things."},
        {"name": "pyright-lsp", "source": "./plugins/pyright-lsp", "description": "Python code checking.",
         "lspServers": {"pyright": {"command": "pyright-langserver"}}}]})
    write_json(market / "plugins" / "tools" / ".mcp.json", {"mcpServers": {"tools": {"command": "tools-server"}}})
    write_skill(market / "plugins" / "tools" / "skills" / "tool-skill", "Uses the tools.")
    (market / "plugins" / "pyright-lsp").mkdir(parents=True)
    write_json(r.paths.CLAUDE_DIR / "plugins" / "known_marketplaces.json",
               {"tools-market": {"source": {"source": "directory", "path": str(market)}, "installLocation": str(market)}})
    write_json(r.paths.workspace_settings("alice"), {
        "enabledPlugins": {"tools@tools-market": True, "pyright-lsp@tools-market": False,
                           "telegram@claude-plugins-official": False},
        "skillOverrides": {"quiet-skill": "off"}})
    skills = r.paths.workspace("alice") / ".claude" / "skills"
    write_skill(skills / "my-skill", "Does my thing. Use whenever Alice asks for her thing.")
    write_skill(skills / "quiet-skill", "Stays quiet.")


def write_skill(folder, description):
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {folder.name}\ndescription: {description}\n---\n\n# {folder.name}\n")


def test_the_panel_lists_plugins_with_what_they_bring_and_skills_on_or_off(r, panel):
    a_plugin_setup(r)
    tap(panel, "refresh")

    snapshot = last_snapshot(r)
    assert snapshot["plugins"] == [
        {"id": "pyright-lsp@tools-market", "name": "pyright-lsp", "marketplace": "tools-market", "on": False,
         "shared": False, "description": "Python code checking.", "servers": [], "skills": [], "code": ["pyright"]},
        {"id": "tools@tools-market", "name": "tools", "marketplace": "tools-market", "on": True, "shared": False,
         "description": "Tools for things.", "servers": ["tools"], "skills": ["tool-skill"], "code": []}]
    assert snapshot["skills"] == [
        {"name": "my-skill", "on": True, "shared": False, "description": "Does my thing."},
        {"name": "quiet-skill", "on": False, "shared": False, "description": "Stays quiet."}]


def test_a_plugin_switch_runs_the_bots_own_plugin_command_then_puts_the_panel_again(r, panel, monkeypatch):
    commands = []
    monkeypatch.setattr(r.plugins, "run", lambda user, text: commands.append((user.name, text)))
    tap(panel, "plugin", command="disable", plugin="tools@tools-market")

    assert commands == [("alice", "/plugin disable tools@tools-market")]
    assert last_snapshot(r)["answer"]["text"] == "Turning tools off. Claude confirms in the chat."


def test_a_skill_switch_runs_the_bots_own_skills_command(r, panel, monkeypatch):
    commands = []
    monkeypatch.setattr(r.plugins, "run", lambda user, text: commands.append((user.name, text)))
    tap(panel, "skill", command="on", skill="quiet-skill")
    assert commands == [("alice", "/skills on quiet-skill")]


@pytest.mark.parametrize("action, args", [
    ("plugin", {"command": "uninstall", "plugin": "tools@tools-market"}),
    ("plugin", {"command": "disable", "plugin": "tools; rm -rf ~"}),
    ("skill", {"command": "name-only", "skill": "my-skill"}),
    ("skill", {"command": "off", "skill": "../other"}),
])
def test_a_plugin_or_skill_switch_the_bot_doesnt_know_is_refused(r, panel, monkeypatch, action, args):
    commands = []
    monkeypatch.setattr(r.plugins, "run", lambda user, text: commands.append(text))
    tap(panel, action, **args)
    assert commands == []
    assert not last_snapshot(r)["answer"]["ok"]


@pytest.mark.parametrize("front_matter, expected", [
    ('description: "Quoted. Use when asked."', "Quoted."),
    ("description: >\n  Folded over\n  two lines.\nname: x", "Folded over two lines."),
    ("description: " + "word " * 80, ("word " * 59).strip() + " word…"),
])
def test_a_skills_description_is_what_it_does_kept_short(r, panel, front_matter, expected):
    from multiclaude.sharing import inventory
    assert inventory.shortened(inventory.WHEN_TO_USE_RE.sub("", inventory.frontmatter_value(
        f"---\n{front_matter}\n---\nbody", "description"))) == expected


def test_pausing_a_job_comments_out_its_lines_and_resuming_puts_them_back(r, panel):
    job = r.cronjobs.jobs("alice", CRONTAB)[0]

    tap(panel, "job", command="pause", id=job.id)
    paused = r.crontab[-1]
    assert f'{r.cronjobs.PAUSED}0 6,7 * * * [ "$(TZ=Europe/Helsinki' in paused
    assert r.cronjobs.jobs("alice", paused)[0].paused
    assert last_snapshot(r)["jobs"][0]["paused"] and last_snapshot(r)["jobs"][0]["next"] is None

    tap(panel, "job", command="resume", id=job.id)
    assert r.crontab[-1] == CRONTAB


def test_someone_elses_job_cant_be_paused(r, panel):
    theirs = r.cronjobs.jobs("bob", CRONTAB)[0]
    tap(panel, "job", command="pause", id=theirs.id)
    assert r.crontab == [CRONTAB]
    assert last_snapshot(r)["answer"] == {"id": "t-job", "ok": False, "text": "That job isn't in the crontab any more."}


def test_deleting_a_job_is_asked_of_the_assistant(r, desk, panel):
    job = r.cronjobs.jobs("alice", CRONTAB)[1]
    tap(panel, "job", command="delete", id=job.id)

    delivery = desk.users["alice"].queue[0]
    assert delivery.header.startswith("[Control panel from Alice · ")
    assert "# alice: invoice sync, Sundays 10:00 Helsinki" in delivery.body
    assert 'assistant job --user alice "invoices"' in delivery.body
    assert r.crontab == [CRONTAB]


# --- cron schedules ---

@pytest.mark.parametrize("line, after, expected", [
    ("*/15 * * * * x", "2026-10-11 21:52", "2026-10-11 22:00"),
    ("7-59/15 * * * * x", "2026-10-11 21:52", "2026-10-11 22:07"),
    ("30 7 7 11 * x", "2026-10-11 21:52", "2026-11-07 07:30"),
    ("0 9 1 * mon x", "2026-10-11 21:52", "2026-10-12 09:00"),  # day 1 or a Monday, whichever comes first
    ("0 0 * * sun x", "2026-10-11 21:52", "2026-10-18 00:00"),
    ("@daily x", "2026-10-11 21:52", "2026-10-12 00:00"),
])
def test_next_run_follows_cron(r, panel, line, after, expected):
    start = datetime.fromisoformat(after).replace(tzinfo=timezone.utc)
    assert r.cronjobs.Schedule.of(line).next_run(start) == datetime.fromisoformat(expected).replace(tzinfo=timezone.utc)


def test_an_hour_check_keeps_the_local_hour_across_the_clock_change(r, panel):
    line = '0 6,7 * * * [ "$(TZ=Europe/Helsinki date +\\%H)" = 09 ] && x'
    summer = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    winter = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    assert r.cronjobs.Schedule.of(line).next_run(summer).hour == 6  # 09:00 at UTC+3
    assert r.cronjobs.Schedule.of(line).next_run(winter).hour == 7  # 09:00 at UTC+2


def test_a_job_run_at_reboot_has_no_next_run(r, panel):
    assert r.cronjobs.Schedule.of("@reboot sleep 120 && x") is None
