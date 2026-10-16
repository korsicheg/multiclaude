"""Tests for the shared library in lib/multiclaude."""
import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import nowhere


def test_paths_follow_the_environment(lib, tmp_path):
    from multiclaude import paths
    assert paths.ROOT == tmp_path / "home" / "assistant"
    assert paths.project_dir("/home/ada/workspaces/alice") == tmp_path / "home/.claude/projects/-home-ada-workspaces-alice"


def test_read_object_refuses_what_isnt_an_object(lib, tmp_path):
    from multiclaude import jsonfile
    path = tmp_path / "s.json"
    assert jsonfile.read_object(path) == {}
    for text in ("{oops", "[1, 2]"):
        path.write_text(text)
        with pytest.raises(jsonfile.NotAnObject):
            jsonfile.read_object(path)


def test_edit_object_keeps_the_rest_and_the_mode(lib, tmp_path):
    from multiclaude import jsonfile
    path = tmp_path / "s.json"
    jsonfile.write_json(path, {"keep": 1, "drop": 2})
    os.chmod(path, 0o600)

    assert jsonfile.edit_object(path, lambda d: d.pop("drop")) is True
    assert jsonfile.edit_object(path, lambda d: None) is False
    assert jsonfile.read_object(path) == {"keep": 1}
    assert path.stat().st_mode & 0o777 == 0o600


def test_edit_object_without_create_writes_nothing_empty(lib, tmp_path):
    from multiclaude import jsonfile
    path = tmp_path / "s.json"
    assert jsonfile.edit_object(path, lambda d: d.setdefault("x", {}), create=False) is False
    assert not path.exists()


def test_locked_times_out_while_someone_else_holds_it(lib, tmp_path):
    from multiclaude import jsonfile
    import fcntl
    lock = tmp_path / "x.lock"
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError):
            with jsonfile.locked(lock, wait=0.2):
                pass


def test_chat_history_keeps_what_it_is_told_to_keep_with_its_time(lib):
    from multiclaude import telegram
    history = telegram.ChatHistory("100")
    telegram.MESSAGES.mkdir(parents=True)
    now = int(time.time())
    history.path.write_text(f"1 {now - 3 * 86400}\n2 {now - 60}\n3 {now - 30}\nbad\n2 {now - 10}\n")

    assert history.take_deletable(keep={3}) == [2]
    assert history.path.read_text() == f"3 {now - 30}\n"
    history.remember(4)
    assert [int(line.split()[0]) for line in history.path.read_text().splitlines()] == [3, 4]


def test_split_text(lib):
    from multiclaude.telegram import split_text
    assert split_text("") == []
    assert split_text("abcde", limit=2) == ["ab", "cd", "e"]


def test_rich_blocks_keep_lines_and_turn_backticks_into_code(lib):
    from multiclaude.telegram import rich_blocks
    assert rich_blocks("🔌 Done\nRun `a <b> && c` *now*\n\n`/x`\n") == [
        {"type": "paragraph", "text": ["🔌 Done\nRun ", {"type": "code", "text": "a <b> && c"}, " *now*"]},
        {"type": "paragraph", "text": [{"type": "code", "text": "/x"}]}]
    assert rich_blocks("a lone ` stays") == [{"type": "paragraph", "text": ["a lone ` stays"]}]


@pytest.mark.parametrize("markdown, sent", [
    ("Do ==this== now", "Do **this** now"),
    ("==one== and ==two words==", "**one** and **two words**"),
    ("x == y and a == b", "x == y and a == b"),
    ("`a ==b== c` stays ==code==", "`a ==b== c` stays **code**"),
    ("```\nif x ==y== z:\n```\nthen ==done==", "```\nif x ==y== z:\n```\nthen **done**"),
    ("==not\nacross lines==", "==not\nacross lines=="),
])
def test_highlights_are_sent_as_bold_outside_code(lib, markdown, sent):
    from multiclaude.telegram import without_highlights
    assert without_highlights(markdown) == sent


def test_markdown_literal_escapes_what_markdown_would_read(lib):
    from multiclaude.telegram import markdown_literal
    assert markdown_literal("a <b> & *c* [d] $5 x_y `e`") == r"a &lt;b&gt; &amp; \*c\* \[d\] \$5 x\_y \`e\`"


def test_split_text_cuts_at_a_line_break_where_it_can(lib):
    from multiclaude.telegram import split_text
    assert split_text("<b>one</b>\n<b>two</b>", limit=15) == ["<b>one</b>\n", "<b>two</b>"]
    assert split_text("\nabcdef", limit=3) == ["\nab", "cde", "f"]


def test_claude_outcome_prefers_the_json_line(lib):
    from multiclaude.claude_cli import outcome
    ok_line = '{"outcome": "ok", "message": "Cloning…\\nInstalled"}'
    assert outcome([ok_line], 0) == (True, "Installed", {"outcome": "ok", "message": "Cloning…\nInstalled"})
    assert outcome(['{"outcome": "error", "error": "Not found"}'], 1)[:2] == (False, "Not found")
    assert outcome(["Fetching…", "✘ Failed to install foo"], 0) == (False, "Fetching…\nFailed to install foo", {})
    assert outcome([], 3) == (False, "exit 3", {})


def test_claude_runs_with_the_env_its_folders_settings_give_sessions(lib, tmp_path, monkeypatch):
    from multiclaude import claude_cli
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "settings.json").write_text(json.dumps({"env": {"MC_LOGIN": "/mine", "MC_SCOPE": "project"}}))
    (ws / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"MC_SCOPE": "local"}}))
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\necho "$MC_LOGIN $MC_SCOPE $MC_KEPT"\n')
    fake.chmod(0o755)
    monkeypatch.setattr(claude_cli.paths, "CLAUDE", str(fake))
    monkeypatch.setenv("MC_KEPT", "kept")

    assert claude_cli.run(["plugin", "list"], ws).lines == ["/mine local kept"]
    (ws / ".claude" / "settings.local.json").write_text("{oops")  # a broken file adds nothing
    assert claude_cli.env_for(ws)["MC_SCOPE"] == "project"
    assert "MC_LOGIN" not in claude_cli.env_for(tmp_path)


def test_users(lib):
    from multiclaude import jsonfile, paths, users
    jsonfile.write_json(paths.USERS, {"b": {"telegram_id": 2, "display": "Bee"}, "a": {"telegram_id": "1", "role": "owner"}})
    loaded = users.load_users()
    assert list(loaded) == ["a", "b"]
    assert loaded["a"].display == "a" and loaded["b"].chat_id == "2"
    assert users.owner(loaded).name == "a"
    assert loaded["b"].pane == "u-b" and loaded["b"].workspace == paths.WORKSPACES / "b"
    assert loaded["b"].timezone == "UTC"
    assert (loaded["b"].entry, loaded["b"].get("quiet_hours")) == ({"telegram_id": 2, "display": "Bee"}, None)


def test_local_time_is_in_the_persons_own_timezone(lib):
    from multiclaude import jsonfile, paths, users
    jsonfile.write_json(paths.USERS, {"alice": {"telegram_id": "1", "timezone": "Europe/Helsinki"},
                                      "bo": {"telegram_id": "2"}})
    when = datetime(2026, 10, 9, 6, 18, tzinfo=timezone.utc).timestamp()
    assert users.local_time("1", when) == "09:18"
    assert users.local_time("2", when) == "06:18 UTC"
    assert users.local_time("3", when) == "06:18 UTC"


# --- held messages -----------------------------------------------------------------------------

@pytest.fixture
def outbox(lib):
    """The outbox of chat 100, with a bot token to send with."""
    from multiclaude import paths, telegram
    paths.TELEGRAM_DIR.mkdir(parents=True)
    (paths.TELEGRAM_DIR / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
    return telegram.Outbox("100")


def held_since(outbox, n, when):
    from multiclaude import jsonfile
    path = sorted(outbox.dir.glob("*.json"))[n]
    jsonfile.write_json(path, {**jsonfile.read_json(path), "held": when})


def test_a_call_that_never_got_to_telegram_is_told_from_one_it_refused(outbox, bot_api):
    from multiclaude import telegram
    with pytest.raises(telegram.Unreachable):
        telegram.BotApi(timeout=1, base=nowhere()).call("sendRichMessage", chat_id=100, rich_message={"markdown": "hi"})

    bot_api.fail["sendRichMessage"] = "Bad Request: chat not found"
    with pytest.raises(telegram.TelegramError) as refused:
        telegram.BotApi(base=bot_api.url).call("sendRichMessage", chat_id=100, rich_message={"markdown": "hi"})
    assert not isinstance(refused.value, telegram.Unreachable) and refused.value.retry_after is None


def test_held_messages_go_out_in_order_with_their_files_and_say_when_they_were_written(outbox, bot_api, tmp_path):
    from multiclaude import jsonfile, paths, telegram
    jsonfile.write_json(paths.USERS, {"alice": {"telegram_id": "100", "timezone": "Europe/Helsinki"}})
    photo = tmp_path / "shot.png"
    photo.write_bytes(b"png")
    outbox.hold("sendRichMessage", {"chat_id": "100", "rich_message": {"markdown": "late **reply**"}}, {"file0": photo})
    photo.unlink()  # the sender may clean up; the outbox has a copy
    outbox.hold("sendRichMessage", {"chat_id": "100", "rich_message": {"blocks": [{"type": "paragraph", "text": ["now"]}]}})
    written = time.time() - 900
    held_since(outbox, 0, written)

    assert outbox.flush(telegram.BotApi(base=bot_api.url)) is True

    (_, late, files), (_, prompt, _) = bot_api.calls
    note = (f"⏳ Written at {datetime.fromtimestamp(written, ZoneInfo('Europe/Helsinki')):%H:%M} and held until now, "
            "while Telegram was out of reach.")
    assert json.loads(late["rich_message"]) == {"markdown": f"late **reply**\n\n*{note}*"}
    assert files == {"file0": ("shot.png", b"png")}
    assert json.loads(prompt["rich_message"]) == {"blocks": [{"type": "paragraph", "text": ["now"]}]}
    assert not outbox.waiting() and list(outbox.dir.iterdir()) == []
    assert telegram.ChatHistory("100").take_deletable() == [501, 502]


def test_a_held_message_refused_or_held_too_long_is_dropped(outbox, bot_api):
    from multiclaude import telegram
    outbox.hold("sendRichMessage", {"chat_id": "100", "rich_message": {"markdown": "from yesterday"}})
    held_since(outbox, 0, time.time() - telegram.HELD_FOR - 1)
    outbox.hold("sendDocument", {"chat_id": "100", "document": "gone"})
    outbox.hold("sendRichMessage", {"chat_id": "100", "rich_message": {"markdown": "fine"}})
    bot_api.fail["sendDocument"] = "Bad Request: wrong file identifier"

    assert outbox.flush(telegram.BotApi(base=bot_api.url)) is True

    assert bot_api.methods() == ["sendDocument", "sendRichMessage"] and not outbox.waiting()


def test_held_messages_wait_while_telegram_cant_be_reached_or_asks_to_slow_down(outbox, bot_api):
    from multiclaude import telegram
    for text in ("one", "two"):
        outbox.hold("sendRichMessage", {"chat_id": "100", "rich_message": {"markdown": text}})

    assert outbox.flush(telegram.BotApi(timeout=1, base=nowhere())) is False
    bot_api.fail["sendRichMessage"] = {"description": "Too Many Requests: retry after 5", "parameters": {"retry_after": 5}}
    assert outbox.flush(telegram.BotApi(base=bot_api.url)) is False
    assert len(list(outbox.dir.glob("*.json"))) == 2 and telegram.Outbox.chats() == ["100"]

    del bot_api.fail["sendRichMessage"]
    assert outbox.flush(telegram.BotApi(base=bot_api.url)) is True
    assert [json.loads(f["rich_message"])["markdown"] for _, f, _ in bot_api.calls[-2:]] == ["one", "two"]


# --- keyring ---

def test_a_key_is_kept_only_as_its_hash_and_a_new_one_replaces_the_old(lib, tmp_path):
    from multiclaude import keyring
    ring = keyring.KeyRing(tmp_path / "keys.json")
    old, bob = ring.new_key("alice"), ring.new_key("bob")
    new = ring.new_key("alice")

    assert [ring.owner(keyring.digest(k)) for k in (old, new, bob)] == [None, "alice", "bob"]
    assert new not in (tmp_path / "keys.json").read_text()


def test_a_revoked_key_is_forgotten(lib, tmp_path):
    from multiclaude import keyring
    ring = keyring.KeyRing(tmp_path / "keys.json")
    key = ring.new_key("alice")

    assert ring.has_key("alice") and ring.revoke("alice")
    assert not ring.has_key("alice") and ring.owner(keyring.digest(key)) is None
    assert ring.owner("") is None and ring.owner(None) is None
