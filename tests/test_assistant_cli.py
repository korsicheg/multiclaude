"""Tests for the `assistant` CLI's setup, notify, job and addon, with a fake claude, systemctl, sudo
and crontab and a fake Bot API."""
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
        # `assistant install`'s check, and crontab so the real one is never touched.
        for program in ("systemctl", "sudo", "crontab", "bun", "tmux"):
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
case "PROGRAM $1" in "sudo tee"|"crontab -") cat >/dev/null ;; esac  # they read what they're given
exit 0
"""


@pytest.fixture
def home(tmp_path, bot_api):
    return Home(tmp_path, bot_api)


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
    assert "--tools" not in home.fake.calls[0]["args"]


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
        ["locations", "model-fallback", "quiet-hours", "siri", "skill-notes", "slack", "voice-notes"])]
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
