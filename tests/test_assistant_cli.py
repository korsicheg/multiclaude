"""Tests for the `assistant` CLI's setup, notify, job and addon, with a fake claude, systemctl, sudo
and crontab and a fake Bot API."""
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time

import pytest

from conftest import ALICE_CHAT, BIN, FakeClaude


class Home:
    """A temp $HOME with ~/assistant, one user and a fake claude first on the PATH."""

    def __init__(self, tmp_path, bot_api):
        self.home = tmp_path / "home"
        self.root = self.home / "assistant"
        (self.root / "bin").mkdir(parents=True)
        shutil.copytree(BIN.parent / "lib", self.root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        for path in BIN.iterdir():
            if path.is_file():
                shutil.copy2(path, self.root / "bin" / path.name)
        for folder in ("addons", "system", "templates"):
            shutil.copytree(BIN.parent / folder, self.root / folder)
        (self.root / "users.json").write_text(json.dumps(
            {"alice": {"telegram_id": ALICE_CHAT, "display": "Alice", "role": "owner"}}))
        (self.home / "workspaces" / "alice").mkdir(parents=True)
        self.channel = self.home / ".claude" / "channels" / "telegram"
        self.channel.mkdir(parents=True)
        (self.channel / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
        self.fake = FakeClaude(tmp_path / "fake-claude")
        (self.home / ".local").mkdir()
        os.symlink(self.fake.bin, self.home / ".local" / "bin")
        # Record what they're asked; systemctl shows self.execstart. bun and tmux are there for
        # `assistant install`'s check, uv and cloudflared so they aren't installed, and crontab so the
        # real one is never touched.
        for program in ("systemctl", "sudo", "crontab", "bun", "tmux", "uv", "cloudflared"):
            fake = self.fake.bin / program
            fake.write_text(SYSTEM_FAKE.replace("PROGRAM", program))
            fake.chmod(0o755)
        self.api = bot_api

    def run(self, *args, background=False, typed="", env=None):
        """Run `assistant ARGS`, with typed as what the owner types in."""
        cmd = [str(self.root / "bin" / "assistant"), *args]
        own = {k: v for k, v in os.environ.items() if k not in OWN_SETTINGS}
        env = {**own, "HOME": str(self.home), "TG_API_BASE": self.api.url, "FAKE_CLAUDE_DIR": str(self.fake.home),
               **(env or {})}
        if background:
            return subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        return subprocess.run(cmd, env=env, input=typed, capture_output=True, text=True, timeout=60)

    def sent(self):
        return [json.loads(f["rich_message"])["markdown"] for m, f, _ in self.api.calls if m == "sendRichMessage"]

    def jobs_log(self):
        path = self.root / "logs" / "jobs.log"
        return path.read_text() if path.exists() else ""

    def jobs_err(self):
        path = self.root / "logs" / "jobs.err"
        return path.read_text() if path.exists() else ""

    @property
    def runs_record(self):
        return self.root / "state" / "job-runs" / "alice.jsonl"

    def runs(self):
        """Alice's recorded job runs, oldest first."""
        return [json.loads(line) for line in self.runs_record.read_text().splitlines()]

    def system_calls(self):
        log = self.fake.home / "system.log"
        return log.read_text().splitlines() if log.exists() else []

    def router_runs(self, root):
        """systemctl shows the router of the install at root as the one running."""
        watcher = f"/usr/bin/python3 -I {root}/bin/watcher.py"
        (self.fake.home / "execstart").write_text(f"{{ path=/usr/bin/python3 ; argv[]={watcher} }}\n")

    def config(self):
        return json.loads((self.root / "config.json").read_text())

    def rules(self):
        return (self.home / "workspaces" / ".claude" / "CLAUDE.md").read_text()


OWN_SETTINGS = ("TELEGRAM_STATE_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")  # this shell's, kept out of the tests

SYSTEM_FAKE = """#!/bin/sh
echo "PROGRAM $*" >> "$FAKE_CLAUDE_DIR/system.log"
[ "PROGRAM $1" = "systemctl show" ] && cat "$FAKE_CLAUDE_DIR/execstart" 2>/dev/null
case "PROGRAM $1" in  # they read what they're given; sudo tee keeps it, as the file's name
  "sudo tee") cat > "$FAKE_CLAUDE_DIR/$(basename "$2")" ;;
  "crontab -") cat >/dev/null ;;
esac
exit 0
"""


@pytest.fixture
def home(tmp_path, bot_api):
    return Home(tmp_path, bot_api)


KEPT_CRONTAB = """#!/bin/sh
case "$1" in
  -l) cat "$FAKE_CLAUDE_DIR/crontab" 2>/dev/null || { echo "no crontab for this user" >&2; exit 1; } ;;
  -) cat > "$FAKE_CLAUDE_DIR/crontab.new" && mv "$FAKE_CLAUDE_DIR/crontab.new" "$FAKE_CLAUDE_DIR/crontab" ;;  # as cron: all or nothing
esac
"""


@pytest.fixture
def kept_crontab(home):
    """home with a crontab that keeps what it's given, starting with none at all, as on a new machine."""
    (home.fake.bin / "crontab").write_text(KEPT_CRONTAB)
    return home


def crontab(home):
    path = home.fake.home / "crontab"
    return path.read_text() if path.exists() else ""


def job_answer(result, cost=0.02):
    return json.dumps({"result": result, "total_cost_usd": cost, "session_id": "s1"})


def test_notify_sends_to_the_owner_by_default(home):
    assert home.run("notify", "hello").returncode == 0
    assert home.sent() == ["hello"]


def test_notify_fails_when_telegram_does(home):
    home.api.fail["sendRichMessage"] = "Bad Request: chat not found"
    r = home.run("notify", "hello")
    assert r.returncode != 0 and "failed" in r.stderr


def test_a_job_sends_its_answer_and_records_its_cost(home):
    home.fake.on(["-p"], stdout=job_answer("Rain at 5pm."))

    assert home.run("job", "--user", "alice", "check the weather").returncode == 0

    assert home.sent() == ["Rain at 5pm."]
    call = home.fake.calls[0]
    assert call["cwd"] == str(home.home / "workspaces" / "alice")
    assert call["stdin"] == "check the weather"
    ledger = json.loads((home.home / ".claude" / "state" / "costs" / "job-s1.json").read_text())
    assert (ledger["name"], ledger["last"]) == ("alice", 0.02)
    assert "Rain at 5pm." in home.jobs_log()


def test_a_job_prompt_that_starts_with_a_dash_is_still_the_prompt(home):
    home.fake.on(["-p"], stdout=job_answer("done"))
    home.run("job", "--user", "alice", "--weekly summary, please")
    assert home.fake.calls[0]["stdin"] == "--weekly summary, please"


def test_a_job_can_be_limited_to_some_tools_and_no_mcp_servers(home):
    home.fake.on(["-p"], stdout=job_answer("NOTHING"))
    home.run("job", "--user", "alice", "--tools", "Read,Edit", "learn from today")
    args = home.fake.calls[0]["args"]
    assert args[args.index("--tools") + 1] == "Read,Edit" and "--strict-mcp-config" in args
    assert home.fake.calls[0]["stdin"] == "learn from today"


def test_a_job_without_tools_gets_them_all(home):
    home.fake.on(["-p"], stdout=job_answer("NOTHING"))
    home.run("job", "--user", "alice", "anything")
    args = home.fake.calls[0]["args"]
    assert "--tools" not in args and "--disallowedTools" not in args and "--model" not in args


def test_a_job_can_be_denied_some_tools_and_given_a_model(home):
    home.fake.on(["-p"], stdout=job_answer("NOTHING"))
    home.run("job", "--user", "alice", "--model", "sonnet", "--deny", "mcp__mail__send", "look around")
    args = home.fake.calls[0]["args"]
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--disallowedTools") + 1] == "mcp__mail__send"
    assert "--strict-mcp-config" not in args
    assert home.fake.calls[0]["stdin"] == "look around"


def test_a_job_with_nothing_to_say_sends_nothing(home):
    home.fake.on(["-p"], stdout=job_answer("NOTHING"))
    home.run("job", "--user", "alice", "anything new?")
    assert home.sent() == []


def test_a_failed_job_says_so(home):
    home.fake.on(["-p"], rc=1)
    home.run("job", "--user", "alice", "do it")
    assert home.sent() == ["Scheduled job failed: do it (details in ~/assistant/logs/jobs.err)"]


def test_a_failed_job_is_named_by_its_first_line_only(home):
    home.fake.on(["-p"], rc=1)
    home.run("job", "--user", "alice", "Learn from what changed in Alice's apps.\n\nPages of instructions…")
    assert home.sent() == ["Scheduled job failed: Learn from what changed in Alice's apps. "
                           "(details in ~/assistant/logs/jobs.err)"]


def test_a_job_still_running_is_not_started_again(home):
    home.fake.on(["-p"], stdout=job_answer("slow one"), sleep=3)
    first = home.run("job", "--user", "alice", "slow task", background=True)
    for _ in range(50):
        if home.fake.calls:
            break
        time.sleep(0.1)

    second = home.run("job", "--user", "alice", "slow task")
    first.wait(timeout=30)

    assert second.returncode == 0
    assert len(home.fake.calls) == 1
    assert "still running" in home.jobs_log()
    assert home.sent() == ["slow one"]


# --- each run's record, for the control panel's Activity ---

def test_a_job_run_is_recorded_with_its_title_outcome_cost_and_the_start_of_its_answer(home):
    answer = "Rain at 5pm. " + "Take an umbrella. " * 30
    home.fake.on(["-p"], stdout=job_answer(answer, cost=0.12))
    before = time.time()

    home.run("job", "--user", "alice", "check the weather\n\nThe details, at length…")

    [run] = home.runs()
    assert before - 1 <= run["started"] <= run["finished"] <= time.time()
    assert (run["title"], run["outcome"], run["cost_usd"]) == ("check the weather", "sent", 0.12)
    assert run["preview"] == answer[:300]


def test_a_run_with_nothing_to_say_is_recorded_without_a_preview(home):
    home.fake.on(["-p"], stdout=job_answer("NOTHING"))
    home.run("job", "--user", "alice", "anything new?")
    [run] = home.runs()
    assert run["outcome"] == "nothing" and "preview" not in run


def test_a_run_is_titled_by_its_first_line_cut_as_the_failure_notice_cuts_it(home):
    home.fake.on(["-p"], stdout=job_answer("NOTHING"))
    home.run("job", "--user", "alice", "é" * 150)
    assert home.runs()[0]["title"] == "é" * 99 + "…"


def test_a_failed_run_is_recorded_with_why_it_failed(home):
    home.fake.on(["-p"], rc=1)
    home.run("job", "--user", "alice", "do it")
    [run] = home.runs()
    assert run["outcome"] == "failed" and "cost_usd" not in run
    assert run["preview"] == "Claude Code stopped with exit code 1; details in ~/assistant/logs/jobs.err."


def test_a_failed_run_keeps_claude_codes_own_error_and_what_it_cost(home):
    home.fake.on(["-p"], rc=1, stdout=json.dumps(
        {"result": "API Error: 529 Overloaded", "is_error": True, "total_cost_usd": 0.03, "session_id": "s2"}))
    home.run("job", "--user", "alice", "do it")

    [run] = home.runs()
    assert (run["outcome"], run["preview"], run["cost_usd"]) == ("failed", "API Error: 529 Overloaded", 0.03)
    assert home.sent() == ["Scheduled job failed: do it (details in ~/assistant/logs/jobs.err)"]
    assert json.loads((home.home / ".claude" / "state" / "costs" / "job-s2.json").read_text())["last"] == 0.03


def test_a_run_whose_answer_telegram_refused_is_recorded_as_failed_with_the_answer(home):
    home.fake.on(["-p"], stdout=job_answer("Rain at 5pm."))
    home.api.fail["sendRichMessage"] = "Bad Request: chat not found"

    assert home.run("job", "--user", "alice", "check the weather").returncode != 0

    [run] = home.runs()
    assert (run["outcome"], run["preview"]) == ("failed", "Couldn't send it on Telegram: Rain at 5pm.")


def test_a_run_skipped_while_the_last_one_still_goes_is_recorded(home):
    key = hashlib.sha256(b"alice\nslow task").hexdigest()[:16]
    lock = home.root / "state" / "jobs" / f"{key}.lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "w") as running:
        fcntl.flock(running, fcntl.LOCK_EX)
        home.run("job", "--user", "alice", "slow task")

    assert home.fake.calls == []
    [run] = home.runs()
    assert (run["title"], run["outcome"]) == ("slow task", "skipped")


def test_only_the_last_200_runs_are_kept(home):
    home.runs_record.parent.mkdir(parents=True)
    home.runs_record.write_text("".join(
        json.dumps({"started": n, "finished": n, "title": "old", "outcome": "nothing"}) + "\n" for n in range(200)))
    home.fake.on(["-p"], stdout=job_answer("NOTHING"))

    home.run("job", "--user", "alice", "anything new?")

    runs = home.runs()
    assert len(runs) == 200
    assert runs[0]["started"] == 1 and runs[-1]["title"] == "anything new?"


def test_a_record_that_isnt_json_still_gets_the_run(home):
    home.runs_record.parent.mkdir(parents=True)
    home.runs_record.write_text("not json\n{broken")
    home.fake.on(["-p"], stdout=job_answer("Rain at 5pm."))

    assert home.run("job", "--user", "alice", "check the weather").returncode == 0

    assert home.sent() == ["Rain at 5pm."]
    assert json.loads(home.runs_record.read_text().splitlines()[-1])["outcome"] == "sent"


def test_a_record_that_cant_be_written_never_costs_the_answer(home):
    home.runs_record.mkdir(parents=True)  # a folder where the file should be
    home.fake.on(["-p"], stdout=job_answer("Rain at 5pm."))

    assert home.run("job", "--user", "alice", "check the weather").returncode == 0

    assert home.sent() == ["Rain at 5pm."]
    assert "alice: can't record the job run" in home.jobs_err()


def test_reload_plugins_leaves_a_request_for_the_router(home):
    r = home.run("reload-plugins", "--user", "alice")
    assert r.returncode == 0
    assert (home.root / "state" / "reload-requests" / "alice").exists()


def test_reload_plugins_refuses_an_unknown_user(home):
    r = home.run("reload-plugins", "--user", "nobody")
    assert r.returncode != 0 and "unknown user" in r.stderr
    assert not (home.root / "state" / "reload-requests" / "nobody").exists()


def test_add_user_gives_whats_shared_back_before_the_newcomers_session_starts(home):
    foo = "foo@claude-plugins-official"
    (home.root / "state").mkdir()
    (home.root / "state" / "shared.json").write_text(json.dumps(
        {"plugins": [foo], "marketplaces": [], "skills": [], "people": ["alice"]}))
    (home.home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {foo: True}}))

    r = home.run("add-user", "bob", "555", "Bob")

    assert r.returncode == 0, r.stderr
    assert home.fake.argvs()[:2] == [["plugin", "install", foo, "--scope", "project"],
                                     ["plugin", "uninstall", foo, "--scope", "user", "--keep-data"]]
    assert home.fake.calls[0]["cwd"].endswith("/workspaces/alice")
    assert json.loads((home.root / "state" / "shared.json").read_text())["people"] == ["alice", "bob"]
    assert home.sent() == [f"🔽 Back in each person's own folder:\n- plugin {foo} (Bob joined)"]


def test_the_front_desk_starts_trusted_so_no_trust_prompt_holds_it_up(home):
    """On a new machine Claude Code asks whether to trust ~/assistant, and nobody watches that terminal."""
    seen = home.fake.home / "front-desk-sandboxed"
    (home.fake.bin / "claude").write_text(f'#!/bin/sh\necho "$CLAUDE_CODE_SANDBOXED" > {seen}\nkill $PPID\n')

    outside = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SANDBOXED"}  # as systemd starts it
    subprocess.run([str(home.root / "bin" / "run.sh")], env={**outside, "HOME": str(home.home)},
                   capture_output=True, timeout=60)

    assert seen.read_text().strip() == "1"


def test_notify_succeeds_when_tg_holds_the_message_for_later(home, monkeypatch):
    import socket
    monkeypatch.setenv("TG_CONNECT_WITHIN", "1")
    sock = socket.create_server(("127.0.0.1", 0))
    home.api.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    sock.close()

    r = home.run("notify", "hello")

    assert r.returncode == 0 and "held" in r.stderr
    assert len(list((home.root / "state" / "outbox" / ALICE_CHAT).glob("*.json"))) == 1


# --- install and setup ---------------------------------------------------------------------------

def test_install_sets_the_machine_up_for_the_user_running_it(home):
    r = home.run("install")

    assert r.returncode == 0, r.stderr
    assert str(home.root) in home.rules() and "{{" not in home.rules()
    settings = json.loads((home.home / ".claude" / "settings.json").read_text())
    assert settings["statusLine"]["command"] == "~/.claude/statusline.sh"
    calls = home.system_calls()
    assert "sudo tee /etc/systemd/system/assistant-watcher.service" in calls and "crontab -" in calls
    assert ["plugin", "install", "telegram@claude-plugins-official", "--scope", "project"] in home.fake.argvs()
    linked = home.home / ".local" / "bin"
    assert all((linked / command).resolve() == home.root / "bin" / command for command in ("assistant", "tg", "takeover"))


def test_install_schedules_the_shared_sync_on_a_machine_with_no_crontab_yet(kept_crontab):
    r = kept_crontab.run("install")

    assert r.returncode == 0, r.stderr
    assert f"{kept_crontab.root}/bin/assistant shared sync" in crontab(kept_crontab)


def test_install_installs_uv_when_it_is_missing(home):
    (home.fake.bin / "uv").unlink()
    (home.fake.bin / "curl").write_text('#!/bin/sh\necho "$*" > "$FAKE_CLAUDE_DIR/curl"\n'
                                        'echo \'echo "$UV_NO_MODIFY_PATH" > "$FAKE_CLAUDE_DIR/uv-installer"\'\n')
    (home.fake.bin / "curl").chmod(0o755)

    r = home.run("install")

    assert r.returncode == 0, r.stderr
    assert "https://astral.sh/uv/install.sh" in (home.fake.home / "curl").read_text()
    assert (home.fake.home / "uv-installer").read_text().strip() == "1"  # it leaves the shell profiles alone


def test_the_router_gets_the_path_the_mcp_servers_it_checks_need(home):
    """uv, uvx and bun live in ~/.local/bin and ~/.bun/bin, which systemd's own PATH leaves out."""
    home.run("install")

    unit = (home.fake.home / "assistant-watcher.service").read_text()
    path = next(line for line in unit.splitlines() if line.startswith("Environment=PATH=")).split("=", 2)[2]
    assert path.split(":")[:2] == [f"{home.home}/.local/bin", f"{home.home}/.bun/bin"]


def test_setup_saves_the_bot_and_an_api_key(home):
    r = home.run("setup", typed="123:abc\nsk-ant-test\n", env={"ANTHROPIC_BASE_URL": "https://proxy.example"})

    assert r.returncode == 0, r.stderr
    assert "bot is Ada (@ada_bot)" in r.stdout and "the sessions use this API key" in r.stdout
    env = (home.root / ".env").read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-test\n" in env and "ANTHROPIC_BASE_URL=https://proxy.example\n" in env
    assert (home.root / ".env").stat().st_mode & 0o077 == 0
    assert home.config()["username"] == "ada_bot" and "@ada_bot" in home.rules()
    assert "sudo systemctl restart assistant" in home.system_calls()


def test_setup_without_an_api_key_leaves_the_sessions_on_claude_codes_own_login(home):
    r = home.run("setup", typed="123:abc\n\n")

    assert r.returncode == 0, r.stderr
    assert "Claude Code's own login" in r.stdout
    assert "ANTHROPIC" not in (home.root / ".env").read_text()


# --- add-ins -------------------------------------------------------------------------------------

def test_addon_list_shows_each_add_in_off_with_what_it_does(home):
    out = home.run("addon", "list").stdout.splitlines()

    assert [line.split()[:2] for line in out] == [[name, "off"] for name in sorted(
        ["backup", "locations", "model-fallback", "quiet-hours", "siri", "skill-notes", "slack", "voice-notes"])]
    assert out[-1].endswith("off  Telegram voice notes are transcribed on the machine before the person's assistant gets them, "
                            "and so are voice clips recorded in Slack while the slack add-in is on too.")


def test_enabling_an_add_in_lists_it_in_config_json_and_adds_its_rules(home):
    (home.root / "config.json").write_text(json.dumps({"name": "Ada", "addons": ["skill-notes"]}))

    r = home.run("addon", "enable", "quiet-hours")

    assert r.returncode == 0 and "quiet-hours is enabled." in r.stdout
    assert home.config() == {"name": "Ada", "addons": ["skill-notes", "quiet-hours"]}
    assert "## Quiet hours" in home.rules() and "## Skill notes" in home.rules()
    assert "Personal assistants on this machine" in home.rules() and "{{" not in home.rules()


def test_disabling_an_add_in_takes_it_and_its_rules_out(home):
    home.run("addon", "enable", "quiet-hours")

    assert home.run("addon", "disable", "quiet-hours").returncode == 0

    assert home.config()["addons"] == [] and "## Quiet hours" not in home.rules()


def test_an_add_in_whose_setup_fails_stays_off(home):
    r = home.run("addon", "enable", "slack")

    assert r.returncode != 0 and "no bot_token" in r.stderr and "stays off" in r.stderr
    assert not (home.root / "config.json").exists()


def test_enabling_slack_puts_its_command_on_the_path_and_says_to_deploy_the_relay(home):
    (home.root / "state").mkdir()
    (home.root / "state" / "slack.json").write_text(json.dumps({"bot_token": "xoxb-1", "signing_secret": "s"}))

    r = home.run("addon", "enable", "slack")

    assert r.returncode == 0 and "assistant relay deploy" in r.stdout
    assert os.readlink(home.home / ".local" / "bin" / "slack") == str(home.root / "addons" / "slack" / "slack")


def test_no_such_add_in_is_refused(home):
    r = home.run("addon", "enable", "../bin")
    assert r.returncode != 0 and "no add-in named" in r.stderr


def test_switching_an_add_in_restarts_this_installs_router_only(home):
    home.router_runs("/elsewhere/assistant")
    home.run("addon", "enable", "skill-notes")
    assert not any(call.startswith("sudo") for call in home.system_calls())

    home.router_runs(home.root)
    r = home.run("addon", "disable", "skill-notes")
    assert "sudo systemctl restart assistant-watcher" in home.system_calls() and "Restarted the router." in r.stdout


def test_an_add_ins_command_works_only_while_the_add_in_is_on(home):
    r = home.run("voice", "key", "alice")
    assert r.returncode != 0 and "comes with the siri add-in, which is off: assistant addon enable siri" in r.stderr

    home.run("addon", "enable", "siri")
    key = home.run("voice", "key", "alice").stdout.strip()
    assert len(key) > 30 and key not in (home.root / "state" / "voice-keys.json").read_text()


def test_slack_link_saves_the_slack_id_while_slack_is_on(home):
    assert "comes with the slack add-in" in home.run("slack-link", "alice", "U0ALICE").stderr
    (home.root / "config.json").write_text(json.dumps({"addons": ["slack"]}))

    assert home.run("slack-link", "alice", "U0ALICE").returncode == 0
    assert json.loads((home.root / "users.json").read_text())["alice"]["slack_id"] == "U0ALICE"


def test_help_lists_the_add_ins_commands(home):
    out = home.run("help").stdout
    assert "assistant addon list" in out
    assert "assistant voice key|revoke <name>" in out and "(siri)" in out
    assert "assistant slack-link <name> <Slack id>" in out and "(slack)" in out


def test_relay_deploy_needs_the_cloudflare_token_first(home):
    r = home.run("relay", "deploy")
    assert r.returncode != 0 and "cloudflare.json" in r.stderr


# --- the backup add-in ---------------------------------------------------------------------------

FAKE_RESTIC = """#!/bin/sh
echo "$RESTIC_REPOSITORY|$RESTIC_PASSWORD_FILE|$*" >> "$FAKE_CLAUDE_DIR/restic.log"
for word in "$@"; do  # restic-fails-<word> holds "<exit code> <error>" for a restic command with that word
  if [ -f "$FAKE_CLAUDE_DIR/restic-fails-$word" ]; then
    read -r code error < "$FAKE_CLAUDE_DIR/restic-fails-$word"; echo "$error" >&2; exit "$code"
  fi
done
"""

RCLONE = "-o rclone.program=/opt/rclone"


@pytest.fixture
def backup(kept_crontab):
    """A home with a fake restic, a crontab that keeps its lines, and the backup's repository named."""
    home = kept_crontab
    (home.fake.bin / "restic").write_text(FAKE_RESTIC)
    (home.fake.bin / "restic").chmod(0o755)
    (home.root / "state").mkdir()
    (home.root / "state" / "backup.json").write_text(json.dumps({"repository": "rclone:cloud:Backups", "rclone": "/opt/rclone"}))
    return home


def backup_on(home):
    (home.root / "config.json").write_text(json.dumps({"addons": ["backup"]}))


def restic_fails(home, word, code, error):
    (home.fake.home / f"restic-fails-{word}").write_text(f"{code} {error}\n")


def restic_calls(home):
    """(repository, password file, arguments) of each restic run."""
    log = home.fake.home / "restic.log"
    return [tuple(line.split("|")) for line in log.read_text().splitlines()] if log.exists() else []


def last_success(home, days_ago):
    state = home.root / "state" / "backup"
    state.mkdir(exist_ok=True)
    (state / "last-success").write_text(f"{int(time.time() - days_ago * 86400)}\n")


def test_backup_stays_off_until_its_repository_is_named(backup):
    (backup.root / "state" / "backup.json").unlink()

    r = backup.run("addon", "enable", "backup")

    assert r.returncode != 0 and "no repository in" in r.stderr and "stays off" in r.stderr
    assert restic_calls(backup) == []


def test_enabling_backup_makes_a_private_password_starts_the_repository_and_runs_it_nightly(backup):
    restic_fails(backup, "cat", 10, "Fatal: repository does not exist")

    r = backup.run("addon", "enable", "backup")

    password = backup.root / "state" / "backup-password"
    assert r.returncode == 0 and "Keep a copy off this machine" in r.stdout
    assert password.stat().st_mode & 0o777 == 0o600 and len(password.read_text().strip()) >= 40
    assert restic_calls(backup)[-1] == ("rclone:cloud:Backups", str(password), f"{RCLONE} init")
    root = backup.root
    assert f"30 2 * * * {root}/bin/assistant backup >> {root}/logs/backup.log 2>&1" in crontab(backup).splitlines()


def test_enabling_backup_again_keeps_its_password_its_repository_and_one_cron_line(backup):
    backup.run("addon", "enable", "backup")
    password = (backup.root / "state" / "backup-password").read_text()

    assert backup.run("addon", "enable", "backup").returncode == 0

    assert (backup.root / "state" / "backup-password").read_text() == password
    assert [args for _, _, args in restic_calls(backup)] == [f"{RCLONE} cat config"] * 2
    assert crontab(backup).count("assistant backup") == 1


def test_disabling_backup_takes_its_cron_lines_out_and_keeps_the_rest(backup):
    (backup.fake.home / "crontab").write_text("# alice: brief\n0 7 * * * brief\n")
    backup.run("addon", "enable", "backup")

    assert backup.run("addon", "disable", "backup").returncode == 0

    assert crontab(backup) == "# alice: brief\n0 7 * * * brief\n"
    assert (backup.root / "state" / "backup-password").exists()


def test_a_backup_saves_what_isnt_in_the_repo_and_keeps_some_older_ones(backup):
    backup_on(backup)
    (backup.fake.home / "crontab").write_text("0 7 * * * brief\n")

    assert backup.run("backup").returncode == 0

    (_, _, unlock), (_, _, snapshot), (_, _, forget) = restic_calls(backup)
    root, home = backup.root, backup.home
    assert unlock == f"{RCLONE} unlock"
    assert snapshot.startswith(f"{RCLONE} backup --exclude-caches --exclude {home}/.claude/cache --exclude .cache")
    assert snapshot.endswith(f" {root}/config.json {root}/users.json {root}/state {home}/.claude {home}/workspaces")
    assert forget == f"{RCLONE} forget --prune --keep-daily 7 --keep-weekly 4 --keep-monthly 12"
    assert (root / "state" / "backup" / "crontab").read_text() == "0 7 * * * brief\n"
    assert (root / "state" / "backup" / "last-success").exists()


def test_a_file_restic_could_not_read_still_counts_as_a_backup(backup):
    backup_on(backup)
    restic_fails(backup, "backup", 3, "Warning: at least one source file could not be read")

    r = backup.run("backup")

    assert r.returncode == 0 and "could not be read" in r.stderr
    assert (backup.root / "state" / "backup" / "last-success").exists()


def test_one_failed_night_says_nothing(backup):
    backup_on(backup)
    last_success(backup, days_ago=1)
    restic_fails(backup, "backup", 1, "Fatal: unable to open repository")

    assert backup.run("backup").returncode == 1

    assert backup.sent() == []


def test_once_the_last_good_backup_is_two_days_old_the_owner_hears_of_each_failure(backup):
    backup_on(backup)
    last_success(backup, days_ago=3)
    restic_fails(backup, "backup", 1, "Fatal: unable to open repository")

    assert backup.run("backup").returncode == 1

    [message] = backup.sent()
    assert "The nightly backup keeps failing: the last good one is from" in message
    assert "`Fatal: unable to open repository`" in message


def test_backup_restic_runs_any_restic_command_on_the_repository(backup):
    backup_on(backup)

    assert backup.run("backup", "restic", "snapshots", "--latest", "1").returncode == 0

    password = str(backup.root / "state" / "backup-password")
    assert restic_calls(backup) == [("rclone:cloud:Backups", password, f"{RCLONE} snapshots --latest 1")]
