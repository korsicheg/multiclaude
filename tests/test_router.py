"""Tests for the router (lib/multiclaude/router), with tmux and the Bot API recorded."""
import fcntl
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import (ALICE_CHAT, BOB_CHAT, append, block_text, channel, read_json, sent, shown, transcript,
                      write_json)


class InlineThread:
    """threading.Thread that runs its target on start(), so tests see the result at once."""

    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)

    def is_alive(self):
        return False


def project(r, folder):
    d = r.paths.project_dir(folder)
    d.mkdir(parents=True, exist_ok=True)
    return d


def kinds(user):
    return [type(w).__name__ for w in user.queue]


# --- parsing -----------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("/model", ("model", None, False)),
    ("/model opus", ("model", "opus", False)),
    ("/model Opus default", ("model", "opus", True)),
    ("/effort@mybot high", ("effort", "high", False)),
    ("/model opus please", None),
    ("/model is too slow today", None),
    ("/models", None),
])
def test_parse_switch(r, text, expected):
    assert r.messages.parse_switch(text) == expected


@pytest.mark.parametrize("regex, text, matches", [
    ("CLEAR_RE", "/clear", True), ("CLEAR_RE", "/clear@bot", True), ("CLEAR_RE", "/clear now", False),
    ("STOP_RE", "/stop", True), ("STOP_RE", "/stop@bot", True), ("STOP_RE", "/stop it", False),
    ("USAGE_RE", "/usage", True), ("USAGE_RE", "/usage please", False),
    ("HELP_RE", "/help", True), ("HELP_RE", "/help@bot", True), ("HELP_RE", "/help me", False),
    ("RESTART_RE", "/restart", True), ("RESTART_RE", "/restart@bot", True), ("RESTART_RE", "/restart now", False),
    ("CONFIG_RE", "/config", True), ("CONFIG_RE", "/config@bot", True), ("CONFIG_RE", "/configure", False),
    ("PLUGIN_RE", "/plugin install x@y", True), ("PLUGIN_RE", "/plugins", True),
    ("PLUGIN_RE", "/skills off x", True), ("PLUGIN_RE", "/mcp add x", True), ("PLUGIN_RE", "/pluginx", False),
    ("MCP_LOGIN_RE", "/mcp login srv", True), ("MCP_LOGIN_RE", "/mcp LOGIN", True),
    ("MCP_LOGIN_RE", "/mcp logout srv", False),
    ("RELOAD_RE", "/reload-plugins", True), ("RELOAD_RE", "/reload-plugins --force", True),
    ("RELOAD_RE", "/reload-plugins now", False), ("RELOAD_RE", "/reload", False),
])
def test_command_regexes(r, regex, text, matches):
    assert bool(getattr(r.messages, regex).fullmatch(text)) is matches


def test_redirect_url_is_found_in_a_pasted_message(r):
    text = "here: http://localhost:51234/callback?code=abc&state=xyz thanks"
    assert r.messages.REDIRECT_RE.search(text)[0] == "http://localhost:51234/callback?code=abc&state=xyz"


def test_picker_rows_reads_the_newest_picker(r):
    screen = "\n".join(["old stuff", "   1. Stale", " Select model", "   1. Default (recommended)",
                        " ❯ 2. Opus      Opus 5.5", "   3. Sonnet"])
    assert r.claude_ui.picker_rows(screen) == [(False, "Default"), (True, "Opus"), (False, "Sonnet")]


def test_effort_slider_reads_levels_and_pointer(r):
    levels = " low   medium   high   xhigh   max"
    pointer = " " * levels.index("high") + " ▲"
    assert r.claude_ui.effort_slider("\n".join(["Effort", pointer, levels])) == (
        ["low", "medium", "high", "xhigh", "max"], "high")
    assert r.claude_ui.effort_slider("nothing here") == ([], None)


@pytest.mark.parametrize("n, text", [(999, "999"), (1500, "1.5k"), (2_000_000, "2M"), (1_250_000, "1.2M")])
def test_fmt_tokens(r, n, text):
    assert r.usage.fmt_tokens(n) == text


@pytest.mark.parametrize("s, text", [(-5, "0m"), (125, "2m"), (3 * 3600 + 120, "3h 2m"), (90000, "1d 1h")])
def test_fmt_duration(r, s, text):
    assert r.usage.fmt_duration(s) == text


def test_version_of(r):
    assert r.session.version_of("/home/x/.local/share/claude/versions/2.1.300") == "2.1.300"
    assert r.session.version_of("/home/x/.local/share/claude/versions/2.1.300 (deleted)") == "2.1.300"
    assert r.session.version_of("/usr/bin/node") is None


def test_a_bash_tool_command_anywhere_under_claude_blocks_a_restart(r):
    ps = "\n".join(["  10     1 claude", "  11    10 /bin/bash -c sleep 99",
                    "  12    11 bash /home/x/.claude/shell-snapshots/snap.sh", "  20     1 other"])
    assert r.session.has_tool_descendant(ps, 10)
    assert not r.session.has_tool_descendant(ps, 20)


# --- transcripts -------------------------------------------------------------------------------

def test_tail_leaves_a_half_written_line_for_next_time(r, tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"a": 1}\n{"b"')
    tail = r.session.Tail(tmp_path)

    assert tail.read(str(path)) == [{"a": 1}]
    with open(path, "a") as f:
        f.write(': 2}\n')
    assert tail.read(str(path)) == [{"b": 2}]


def test_tail_skip_existing_reads_only_what_comes_after(r, tmp_path):
    path = tmp_path / "t.jsonl"
    append(path, {"old": 1})
    tail = r.session.Tail(tmp_path)
    tail.skip_existing()
    append(path, {"new": 1})

    assert tail.read_new() == [{"new": 1}]


def session(r, tmp_path, name="alice"):
    return r.session.Session(name, f"u-{name}", tmp_path / "proj", ALICE_CHAT, "Alice")


def test_session_turn_lifecycle(r, tmp_path):
    turn = session(r, tmp_path).turn

    turn.handle({"type": "queue-operation", "operation": "enqueue"})
    assert (turn.busy, turn.pending) == (True, 1)
    turn.handle({"type": "queue-operation", "operation": "dequeue"})
    turn.handle({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
    assert (turn.busy, turn.in_turn, turn.pending) == (True, True, 0)
    turn.handle({"type": "system", "subtype": "turn_duration"})
    assert (turn.busy, turn.in_turn) == (False, False)


def test_session_stays_busy_while_messages_are_queued(r, tmp_path):
    turn = session(r, tmp_path).turn
    turn.handle({"type": "queue-operation", "operation": "enqueue"})
    turn.handle({"type": "system", "subtype": "turn_duration"})
    assert turn.busy


@pytest.mark.parametrize("content", ["[Request interrupted by user]",
                                     [{"type": "text", "text": "[Request interrupted by user for tool use]"}]])
def test_escape_ends_the_turn(r, tmp_path, content):
    turn = session(r, tmp_path).turn
    turn.handle({"type": "assistant", "message": {"content": [{"type": "text", "text": "Once upon a time"}]}})

    turn.handle({"type": "user", "message": {"content": content}})

    assert (turn.busy, turn.in_turn) == (False, False)


def test_after_escape_the_turn_goes_on_with_messages_sent_meanwhile(r, tmp_path):
    turn = session(r, tmp_path).turn
    turn.handle({"type": "queue-operation", "operation": "enqueue"})
    turn.handle({"type": "user", "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}})
    assert turn.busy


LEFTOVER_TASK_REPORT = {"type": "user", "queueTranscriptOnly": True, "message": {
    "content": "<task-notification>\n<status>stopped</status>\n<summary>Background shell command didn't finish "
               "before the previous session ended</summary>\n</task-notification>"}}


def test_a_queued_report_written_down_without_a_turn_leaves_the_session_idle(r, tmp_path):
    turn = session(r, tmp_path).turn
    turn.handle({"type": "queue-operation", "operation": "enqueue"})
    turn.handle({"type": "queue-operation", "operation": "dequeue"})

    turn.handle(LEFTOVER_TASK_REPORT)

    assert not turn.busy


def test_a_queued_report_written_down_mid_turn_leaves_the_turn_running(r, tmp_path):
    turn = session(r, tmp_path).turn
    turn.handle({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it"}]}})

    turn.handle(LEFTOVER_TASK_REPORT)

    assert (turn.busy, turn.in_turn) == (True, True)


def test_session_notes_replies_sent_with_tg(r, tmp_path):
    turn = session(r, tmp_path).turn
    turn.handle({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "tg send 'done'"}}]}})
    assert turn.last_reply > 0


def test_session_api_error_ends_the_turn_and_is_kept_for_the_desk(r, tmp_path):
    turn = session(r, tmp_path).turn
    turn.handle({"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 529, "error": "server_error",
                 "message": {"content": [{"type": "text", "text": "API Error: 529 overloaded\nmore"}]}})
    assert not turn.busy
    assert (turn.failed.text, turn.failed.retryable) == ("API Error: 529 overloaded", True)


@pytest.mark.parametrize("entry, retryable", [
    ({"error": "unknown"}, True),                              # no answer at all: the network
    ({"error": "server_error", "apiErrorStatus": 503}, True),  # the API (or the proxy) failing on its side
    ({"apiErrorStatus": 408}, True),
    ({"error": "authentication_failed", "apiErrorStatus": 401}, False),
    ({"error": "authentication_failed", "apiErrorStatus": 403}, False),
    ({"error": "rate_limit", "apiErrorStatus": 429}, False),
    ({"error": "invalid_request", "apiErrorStatus": 400}, False),
])
def test_only_errors_that_may_pass_are_retryable(r, entry, retryable):
    from multiclaude.router import turn
    assert turn.retryable(entry) is retryable


def test_session_start_recovers_state_without_old_notices(r, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    append(proj / "a.jsonl", {"type": "assistant", "isApiErrorMessage": True, "message": {"content": "boom"}},
           {"type": "assistant", "message": {"content": []}})
    s = session(r, tmp_path)

    assert s.turn.in_turn and s.turn.busy
    assert s.turn.notice is None
    assert s.tail.read_new() == []


def test_session_start_forgets_a_turn_that_went_quiet_long_ago(r, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    append(proj / "a.jsonl", {"type": "assistant", "message": {"content": []}})
    old = time.time() - r.session.STALE_AFTER - 60
    os.utime(proj / "a.jsonl", (old, old))

    assert not session(r, tmp_path).turn.busy


def test_session_read_reports_a_stuck_turn_and_saves_its_screen(r, tmp_path):
    s = session(r, tmp_path)
    s.turn.busy, s.turn.last_write = True, time.time() - r.session.STALE_AFTER - 1
    s.read(time.time())
    assert not s.turn.busy
    assert "no progress on your request for 15 minutes" in s.turn.notice
    [saved] = (r.paths.SCREENS / "alice").iterdir()
    assert saved.name.endswith("-stuck.txt") and saved.read_text() == captured("message-box-empty.txt")


def test_a_turn_waiting_on_an_approval_is_not_stuck(r, tmp_path):
    s = session(r, tmp_path)
    s.turn.busy, s.turn.last_write = True, time.time() - r.session.STALE_AFTER - 1
    s.approval = r.work.approvals.Pending(901, [], "Bash command")
    s.read(time.time())
    assert s.turn.busy and s.turn.notice is None


def test_only_a_few_screens_are_kept(r, tmp_path):
    s = session(r, tmp_path)
    folder = r.paths.SCREENS / "alice"
    folder.mkdir(parents=True)
    for n in range(r.session.SCREENS_KEPT):
        (folder / f"20261001T0000{n:02d}Z-stuck.txt").write_text("old")
    s.save_screen("stuck")
    kept = sorted(folder.iterdir())
    assert len(kept) == r.session.SCREENS_KEPT and kept[0].name == "20261001T000001Z-stuck.txt"


def test_a_subagent_still_writing_is_not_stuck(r, tmp_path):
    s = session(r, tmp_path)
    sub = tmp_path / "proj" / "sess" / "subagents"
    sub.mkdir(parents=True)
    (sub / "a.jsonl").write_text("{}\n")
    s.turn.busy, s.turn.last_write = True, time.time() - r.session.STALE_AFTER - 1
    s.read(time.time())
    assert s.turn.busy and s.turn.notice is None


def test_type_message_types_the_header_and_pastes_the_body(r, tmp_path):
    s = session(r, tmp_path)
    s.type_message("[header]", "line 1\nline 2\x1b[31m")

    assert r.keystrokes == [
        (("capture-pane", "-p", "-t", "=u-alice:"), None),  # no approval dialog open
        (("load-buffer", "-b", "tg-alice", "-"), b"\nline 1\nline 2[31m"),
        (("send-keys", "-t", "=u-alice:", "-l", "[header]"), None),
        (("paste-buffer", "-p", "-d", "-b", "tg-alice", "-t", "=u-alice:"), None),
        (("send-keys", "-t", "=u-alice:", "Enter"), None),
    ]


def test_a_slash_command_returns_the_output_claude_code_logs_after_it(r, tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    transcript = tmp_path / "proj" / "t.jsonl"
    append(transcript, {"message": {"content": "<command-name>/reload-plugins</command-name>"}},
           {"content": "<local-command-stdout>an old result</local-command-stdout>"})
    s = session(r, tmp_path)
    typed = s.pane.type

    def claude_code_answers(text):
        typed(text)
        append(transcript, {"message": {"content": f"<command-name>{text}</command-name>"}},
               {"content": "<local-command-stdout>Reloaded: 3 plugins</local-command-stdout>"})

    monkeypatch.setattr(s.pane, "type", claude_code_answers)

    assert s.run_command("/reload-plugins") == "Reloaded: 3 plugins"
    assert (("send-keys", "-t", "=u-alice:", "-l", "/reload-plugins"), None) in r.keystrokes


# --- inbound messages --------------------------------------------------------------------------

def test_a_message_is_queued_with_the_header_and_body(r, desk):
    desk.inbound(channel("hello\nthere"))

    alice = desk.users["alice"]
    (item,) = alice.queue
    assert isinstance(item, r.work.Deliver)
    assert item.header == ("[Telegram from Alice · msg 5 · 2026-10-08 18:53 UTC] "
                           "My message is pasted below; it is my own request to you:")
    assert (item.body, item.msg_id) == ("hello\nthere", "5")
    assert alice.turn.busy
    assert (r.telegram.MESSAGES / ALICE_CHAT).read_text().split()[0] == "5"


def test_an_envelope_from_the_relay_that_nothing_takes_is_dropped(r, desk, capsys):
    desk.relay_inbound({"type": "voice", "envelope_id": "voice:0badcafe", "payload": {"id": "0badcafe", "text": "hi"}})

    assert all(not u.queue for u in desk.users.values()) and r.api_calls == []
    assert "a 'voice' envelope, which nothing that's on takes; dropped" in capsys.readouterr().err


def test_with_no_config_json_messages_are_routed_and_no_addin_acts(r, desk):
    assert not r.paths.CONFIG.exists()
    alice = desk.users["alice"]
    desk.inbound(channel("hello"))
    desk.inbound(channel("(voice message)", msg_id="6", attachment_kind="voice", attachment_file_id="VOICE1"))
    desk.inbound(channel("(location)", msg_id="7", location_lat="52.52001", location_lon="13.404954"))
    append(transcript(alice), {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": "clean-code:clean-code-python"}}]}})

    desk.tend(alice, time.time())

    assert kinds(alice) == ["Deliver"] * 3
    assert [w.body for w in alice.queue] == ["hello", "(voice message)\n\nAttachments:\nfile_id: VOICE1 (voice)",
                                             "(location)"]
    assert sent(r) == []


def test_attachments_are_listed_after_the_text(r, desk):
    desk.inbound(channel("look", image_path="/tmp/p.jpg", attachment_file_id="F1",
                         attachment_kind="document", attachment_name="a.pdf", attachment_size="12"))

    assert desk.users["alice"].queue[0].body == ("look\n\nAttachments:\nphoto: /tmp/p.jpg\n"
                                                "file_id: F1 (document, a.pdf, 12 bytes)")


def test_a_reply_brings_the_message_it_answers(r, desk):
    desk.inbound(channel("worked here", msg_id="6", reply_to_message_id="481", reply_to_text="Done.\nWhat changed"))
    desk.inbound(channel("this part", msg_id="7", reply_to_message_id="481", reply_to_text="Done.\nWhat changed",
                         reply_quote="What changed"))

    first, second = desk.users["alice"].queue
    assert first.body == "worked here\n\nReplying to msg 481:\n> Done.\n> What changed"
    assert second.body == "this part\n\nReplying to msg 481, quoting:\n> What changed"


def test_the_same_message_is_routed_once(r, desk):
    desk.inbound(channel("hi"))
    desk.inbound(channel("hi"))
    assert len(desk.users["alice"].queue) == 1


def test_a_reaction_is_one_line_about_the_message_it_is_on(r, desk):
    desk.inbound(channel("hi", msg_id="481"))
    reaction = channel("👍", msg_id="481", reaction="👍", ts="2026-10-08T18:55:40.000Z")
    desk.inbound(reaction)
    desk.inbound(reaction)  # brought twice

    _, item = desk.users["alice"].queue
    assert item.header == "[Telegram from Alice · reaction on msg 481 · 2026-10-08 18:55 UTC] I reacted with 👍."
    assert (item.body, item.msg_id) == ("", "481")
    assert (r.telegram.MESSAGES / ALICE_CHAT).read_text().split()[::2] == ["481"]


def test_a_stranger_is_told_they_have_no_assistant(r, desk):
    desk.inbound(channel("hi", chat="555"))
    assert "Ask the VM owner to run: `assistant add-user <name> 555`" in sent(r, "555")[0]


def test_clear_is_queued(r, desk):
    desk.inbound(channel("/clear"))
    assert kinds(desk.users["alice"]) == ["Clear"]


@pytest.mark.parametrize("typed", ["/сдуфк", "/ψλεαρ"])
def test_clear_typed_in_the_russian_or_greek_layout_is_queued(r, desk, typed):
    desk.inbound(channel(typed))
    assert kinds(desk.users["alice"]) == ["Clear"]


def test_a_switch_typed_in_the_greek_layout_keeps_its_arguments(r, desk):
    desk.inbound(channel("/μοδελ οπθσ δεφαθλτ"))

    (item,) = desk.users["alice"].queue
    assert (type(item).__name__, item.setting, item.target, item.keep) == ("Switch", "model", "opus", True)


def test_a_russian_word_after_a_slash_goes_to_the_assistant_as_typed(r, desk):
    desk.inbound(channel("/привет"))

    (item,) = desk.users["alice"].queue
    assert item.body == "/привет"


def test_model_without_a_target_shows_the_current_setting(r, desk):
    desk.inbound(channel("/model"))
    assert sent(r, ALICE_CHAT)[0].startswith("🧠 Model: not known yet\nAfter a restart: the shared default")


def test_model_switch_waits_for_a_busy_assistant(r, desk):
    desk.users["alice"].turn.busy = True
    desk.inbound(channel("/model opus default"))

    (item,) = desk.users["alice"].queue
    assert (type(item).__name__, item.setting, item.target, item.keep) == ("Switch", "model", "opus", True)
    assert sent(r, ALICE_CHAT) == ["⏳ I'll switch your model to opus once I finish what I'm doing."]


def test_plugin_commands_run_in_the_background(r, desk, monkeypatch):
    calls = []
    monkeypatch.setattr(r.desk.threading, "Thread", InlineThread)
    monkeypatch.setattr(r.plugins, "run", lambda user, text: calls.append((user.name, text)))

    desk.inbound(channel("/plugin install x@y"))

    assert calls == [("alice", "/plugin install x@y")]
    assert desk.users["alice"].queue == []


# --- tg ask buttons ----------------------------------------------------------------------------

def test_a_button_tap_shows_the_choice_and_queues_it(r, desk):
    path = r.paths.ASKS / ALICE_CHAT / "42.json"
    write_json(path, {"question": "Which?", "options": ["Postgres in Docker", "SQLite"]})

    desk.inbound(channel("Postgres in Do", msg_id="42", callback_id="cb1", callback_data="ask:0"))

    assert ("editMessageText", {"chat_id": ALICE_CHAT, "message_id": "42",
                                "rich_message": {"markdown": "Which?\n\n👉 Postgres in Docker"}}) in r.api_calls
    assert read_json(path)["answer"] == "Postgres in Docker"
    item = desk.users["alice"].queue[0]
    assert item.header.startswith("[Telegram from Alice · tapped a button on your question msg 42")
    assert item.body.startswith("Your question: Which?\nMy choice: Postgres in Docker\n\n")


def test_a_tapped_choice_is_shown_as_written_under_the_markdown_question(r, desk):
    write_json(r.paths.ASKS / ALICE_CHAT / "42.json", {"question": "**Which?**", "options": ["a < b *c*", "d"]})

    desk.inbound(channel("a < b *c*", msg_id="42", callback_id="cb1", callback_data="ask:0"))

    method, params = r.api_calls[0]
    assert (method, shown(params)) == ("editMessageText", "**Which?**\n\n👉 a &lt; b \\*c\\*")


def test_a_second_tap_on_an_answered_question_is_ignored(r, desk):
    write_json(r.paths.ASKS / ALICE_CHAT / "42.json", {"question": "Which?", "options": ["A"], "answer": "A"})
    desk.inbound(channel("A", msg_id="42", callback_id="cb2", callback_data="ask:0"))
    assert desk.users["alice"].queue == []


# --- /clear history ----------------------------------------------------------------------------

def test_wipe_chats_deletes_recent_messages_and_keeps_those_still_on_their_way(r, desk, monkeypatch):
    deleted = []
    monkeypatch.setattr(r.work.threading, "Thread", InlineThread)
    monkeypatch.setattr(r.work, "delete_messages", lambda chat, ids: deleted.append((chat, ids)))
    now = int(time.time())
    r.telegram.MESSAGES.mkdir(parents=True)
    (r.telegram.MESSAGES / ALICE_CHAT).write_text(f"1 {now - 3 * 86400}\n2 {now - 60}\n3 {now - 30}\nbad line\n2 {now - 10}\n")
    alice = desk.users["alice"]
    alice.queue = [deliver(r, "h", "3")]

    r.work.wipe_chats(alice, "🧹 Cleared.")

    assert deleted == [(ALICE_CHAT, [2])]
    assert (r.telegram.MESSAGES / ALICE_CHAT).read_text().split()[0] == "3"


def test_wipe_chats_posts_the_note_before_deleting_anything_and_keeps_it(r, desk, monkeypatch):
    monkeypatch.setattr(r.work.threading, "Thread", InlineThread)
    r.telegram.ChatHistory(ALICE_CHAT).remember(40)

    r.work.wipe_chats(desk.users["alice"], "🧹 Cleared.")

    methods = [m for m, _ in r.api_calls]
    note = 900 + methods.index("sendRichMessage") + 1
    deleted = [m for method, p in r.api_calls if method == "deleteMessages" for m in p["message_ids"]]
    assert methods.index("sendRichMessage") < methods.index("deleteMessages")
    assert deleted == [40]
    assert (r.telegram.MESSAGES / ALICE_CHAT).read_text().split()[0] == str(note)  # for the next /clear


def test_delete_history_sweeps_the_ids_it_never_recorded_between_the_recorded_ones(r, monkeypatch):
    deleted, swept = [], []
    monkeypatch.setattr(r.work.threading, "Thread", InlineThread)
    monkeypatch.setattr(r.work, "delete_messages", lambda chat, ids: deleted.append(ids))
    monkeypatch.setattr(r.work, "sweep", lambda chat, ids: swept.append(ids))

    r.work.delete_history(ALICE_CHAT, [10, 12, 17], keep={15})

    assert deleted == [[10, 12, 17]]
    assert swept == [[11, 13, 14, 16]]


def test_sweep_asks_telegram_to_delete_the_ids_in_batches(r, monkeypatch):
    calls = []
    monkeypatch.setattr(r.telegram, "api", lambda method, **params: calls.append((method, params["message_ids"])))
    r.work.sweep(ALICE_CHAT, list(range(1, 151)))
    assert calls == [("deleteMessages", list(range(1, 101))), ("deleteMessages", list(range(101, 151)))]


def test_delete_messages_retries_one_by_one_when_a_batch_fails(r, monkeypatch):
    calls = []

    def api(method, **params):
        calls.append(method)
        return None if method == "deleteMessages" else True

    monkeypatch.setattr(r.telegram, "api", api)
    r.work.delete_messages(ALICE_CHAT, [1, 2, 3])
    assert calls == ["deleteMessages", "deleteMessage", "deleteMessage", "deleteMessage"]


# --- the queue ---------------------------------------------------------------------------------

def deliver(r, header, msg_id, since=None):
    return r.work.Deliver(time.time() if since is None else since, header, "body", msg_id)


def test_serve_delivers_messages_in_order(r, desk, monkeypatch):
    alice = desk.users["alice"]
    typed = []
    monkeypatch.setattr(alice, "ready", lambda: True)
    monkeypatch.setattr(alice, "type_message", lambda h, b: typed.append(h))
    alice.queue = [deliver(r, "h1", "1"), deliver(r, "h2", "2")]

    r.work.serve(alice, time.time())

    assert typed == ["h1", "h2"]
    assert alice.queue == [] and alice.turn.busy


def test_serve_holds_a_switch_until_the_turn_ends(r, desk, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "ready", lambda: True)
    alice.turn.busy = True
    alice.queue = [r.work.Switch(1.0, "model", "opus")]

    r.work.serve(alice, 50.0)

    assert len(alice.queue) == 1 and alice.queue[0].since == 50.0


def test_serve_clears_once_idle_and_keeps_messages_sent_after(r, desk, monkeypatch):
    alice = desk.users["alice"]
    wiped = []
    monkeypatch.setattr(alice, "clear", lambda: True)
    monkeypatch.setattr(alice, "ready", lambda: False)
    monkeypatch.setattr(r.work, "delete_history", lambda chat, recorded, keep: wiped.append((chat, keep)))
    alice.queue = [r.work.Clear(time.time()), deliver(r, "h", "7")]

    r.work.serve(alice, time.time())

    assert wiped == [(ALICE_CHAT, {7})]
    assert sent(r, ALICE_CHAT) == ["🧹 Context cleared. Starting fresh."]
    assert kinds(alice) == ["Deliver"]


def test_clear_reloads_the_plugins_first_so_the_new_conversation_starts_on_them(r, desk, monkeypatch):
    alice = desk.users["alice"]
    typed = []
    monkeypatch.setattr(alice, "ready", lambda: True)
    monkeypatch.setattr(alice, "run_command", lambda command: typed.append(command) or "Reloaded: 3 plugins")
    monkeypatch.setattr(alice, "clear", lambda: typed.append("/clear") or True)
    monkeypatch.setattr(r.work, "delete_history", lambda chat, recorded, keep: None)
    alice.queue = [r.work.Clear(time.time())]

    r.work.serve(alice, time.time())

    assert typed == ["/reload-plugins --force", "/clear"]


def test_serve_drops_a_message_when_the_assistant_never_comes_up(r, desk, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "ready", lambda: False)
    alice.queue = [deliver(r, "h", "1", since=time.time() - r.work.DELIVER_TIMEOUT - 1)]

    r.work.serve(alice, time.time())

    assert alice.queue == []
    assert sent(r, ALICE_CHAT)[0].startswith("⚠️ Claude isn't running right now")


def test_serve_reports_session_command_results(r, desk, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "ready", lambda: True)
    results = {"/reload-plugins": "Reloaded: 3 plugins", "/mcp reconnect srv": "Use /mcp to authenticate."}
    monkeypatch.setattr(alice, "run_command", lambda c: results[c])
    alice.queue = [r.work.SessionCommands(time.time(), commands=["/reload-plugins", "/mcp reconnect srv"])]

    r.work.serve(alice, time.time())

    assert sent(r, ALICE_CHAT) == ["🔌 Sign in: `/mcp login srv`"]
    keyboard = [p["reply_markup"] for m, p in r.api_calls if m == "sendRichMessage"][-1]
    assert keyboard == {"inline_keyboard": [[{"text": "📋 /mcp login srv", "copy_text": {"text": "/mcp login srv"}}]]}


def test_a_message_that_keeps_failing_to_type_is_given_up_on(r, desk, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "ready", lambda: True)

    def broken(header, body):
        raise subprocess.CalledProcessError(1, "tmux")

    monkeypatch.setattr(alice, "type_message", broken)
    alice.queue = [deliver(r, "h", "1")]
    r.work.serve(alice, time.time())
    assert len(alice.queue) == 1 and sent(r, ALICE_CHAT) == []

    r.work.serve(alice, time.time() + r.work.DELIVER_TIMEOUT + 1)
    assert alice.queue == []
    assert sent(r, ALICE_CHAT)[0].startswith("⚠️ Your message couldn't be typed into Claude")


# --- /plugin through shared.py -----------------------------------------------------------------

def shared_replies(r, monkeypatch, reply, returncode=0):
    out = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
    monkeypatch.setattr(r.plugins.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, returncode, out))


def test_plugin_command_sends_the_reply_and_queues_a_reload(r, desk, monkeypatch):
    shared_replies(r, monkeypatch, {"text": "✅ Installed", "reload": True})
    alice = desk.users["alice"]

    r.plugins.run(alice, "/plugin install x@y")

    assert sent(r, ALICE_CHAT) == ["✅ Installed"]
    assert kinds(alice) == ["Reload"]


def test_a_reload_request_queues_one_reload_and_is_forgotten(r, desk):
    requests = r.paths.RELOAD_REQUESTS
    requests.mkdir(parents=True)
    (requests / "alice").touch()
    (requests / "nobody").touch()
    alice = desk.users["alice"]
    alice.enqueue(r.work.Reload(0))

    desk.poll()

    assert kinds(alice) == ["Reload"]
    assert list(requests.iterdir()) == []


def test_a_reload_request_waits_its_turn_behind_a_message(r, desk):
    r.paths.RELOAD_REQUESTS.mkdir(parents=True)
    (r.paths.RELOAD_REQUESTS / "bob").touch()
    bob = desk.users["bob"]
    bob.enqueue(r.work.Deliver(0, "[Telegram from Bob]", "hi"))

    desk.poll()

    assert kinds(bob) == ["Deliver", "Reload"]


def test_reload_plugins_from_the_person_is_typed_into_their_session(r, desk):
    alice = desk.users["alice"]
    desk.inbound(channel("/reload-plugins"))
    assert kinds(alice) == ["Reload"] and not alice.queue[0].force


def test_reload_plugins_with_force_upgrades_a_waiting_reload(r, desk):
    alice = desk.users["alice"]
    alice.enqueue(r.work.Reload(0))
    desk.inbound(channel("/reload-plugins --force"))
    assert kinds(alice) == ["Reload"] and alice.queue[0].force


def test_a_reload_held_back_for_mcp_tools_offers_the_forced_one(r, desk, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "ready", lambda: True)
    typed = []
    held = "This reload changes MCP tools (7 MCP servers). Run /reload-plugins --force to apply."
    monkeypatch.setattr(alice, "run_command", lambda c: typed.append(c) or (held if c == "/reload-plugins" else "Reloaded"))
    alice.queue = [r.work.Reload(time.time())]

    r.work.serve(alice, time.time())
    keyboard = [p["reply_markup"] for m, p in r.api_calls if m == "sendRichMessage"][-1]
    assert keyboard["inline_keyboard"][0][0]["copy_text"] == {"text": "/reload-plugins --force"}

    alice.queue = [r.work.Reload(time.time(), force=True)]
    r.work.serve(alice, time.time())
    assert typed == ["/reload-plugins", "/reload-plugins --force"]
    assert sent(r, ALICE_CHAT)[-1] == "🔌 Reloaded"


def test_plugin_command_queues_session_commands(r, desk, monkeypatch):
    shared_replies(r, monkeypatch, {"text": "", "reload": False, "session": ["/mcp disable srv"]})
    alice = desk.users["alice"]

    r.plugins.run(alice, "/mcp disable srv")

    assert sent(r, ALICE_CHAT) == []
    assert alice.queue[0].commands == ["/mcp disable srv"]


def test_plugin_command_failure_points_at_the_log(r, desk, monkeypatch):
    shared_replies(r, monkeypatch, b"Traceback", returncode=1)
    r.plugins.run(desk.users["alice"], "/plugin x")
    assert sent(r, ALICE_CHAT) == ["⚠️ /plugin failed. Details are in `~/assistant/logs/shared.log` on the VM."]


# --- /model and /effort ------------------------------------------------------------------------

def test_switch_for_this_session_only(r, desk, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "switch", lambda s, t: ("Set model to `Opus 5.5`", False))

    r.switching.switch(alice, "model", "opus", False)

    assert sent(r, ALICE_CHAT) == ["✅ Model: Opus 5.5 (this session only)"]
    assert alice.session_switches == {"model": "opus"}


def test_switch_with_default_saves_it(r, desk, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "switch", lambda s, t: ("Set effort level to high (saved)", True))

    r.switching.switch(alice, "effort", "high", True)

    assert sent(r, ALICE_CHAT) == ["✅ Effort: high. Also saved as your default after restarts\n\n"
                                  "Your next reply will be slower, since it re-reads the whole conversation."]
    assert read_json(r.paths.workspace_settings("alice")) == {"effortLevel": "high"}


def test_switch_failure_is_reported(r, desk, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "switch", lambda s, t: ("the /model picker didn't open", False))
    r.switching.switch(alice, "model", "opus", False)
    assert sent(r, ALICE_CHAT) == ["⚠️ Couldn't switch model: the /model picker didn't open"]


def test_a_failed_switch_back_after_a_restart_says_so(r, desk, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "switch", lambda s, t: ("the /model picker didn't open", False))
    r.switching.switch(alice, "model", "opus", False, quiet=True)
    assert sent(r, ALICE_CHAT) == ["⚠️ Claude restarted for a Claude Code update and couldn't switch its "
                                  "model back to opus: the /model picker didn't open"]


def test_save_default(r, desk):
    alice = desk.users["alice"]
    path = r.paths.workspace_settings("alice")
    write_json(path, {"model": "opus", "other": 1})

    assert r.switching.save_default(alice, "effort", "max") == "Claude Code keeps max for one session only"
    assert r.switching.save_default(alice, "model", "default") is None
    assert read_json(path) == {"other": 1}

    path.write_text("{broken")
    assert "isn't valid JSON" in r.switching.save_default(alice, "model", "opus")


def test_saving_a_default_waits_for_plugin_sharing_to_finish_with_the_settings(r, desk):
    path = r.paths.workspace_settings("alice")
    write_json(path, {"model": "opus"})
    r.paths.SETTINGS_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(r.paths.SETTINGS_LOCK, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        problem = r.switching.save_default(desk.users["alice"], "model", "sonnet")

    assert "busy" in problem
    assert read_json(path) == {"model": "opus"}
    assert r.switching.save_default(desk.users["alice"], "model", "sonnet") is None


def test_an_effort_that_cannot_be_saved_is_still_put_back_after_restarts(r, desk, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "switch", lambda s, t: ("Set effort level to max (this session only)", False))

    r.switching.switch(alice, "effort", "max", True)

    assert alice.session_switches == {"effort": "max"}
    assert "(this session only). Not saved as your default: Claude Code keeps max for one session only" in sent(r)[0]


def test_a_picker_that_did_not_open_is_closed_in_case_it_opens_late(r, tmp_path, monkeypatch):
    s = session(r, tmp_path)
    monkeypatch.setattr(s, "wait_screen", lambda test, timeout=5.0: None)

    assert s.switch("model", "opus") == ("the /model picker didn't open", False)
    assert r.keystrokes[-1] == (("send-keys", "-t", "=u-alice:", "Escape"), None)


# --- /usage and limits -------------------------------------------------------------------------

def iso_in(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + seconds))


def test_usage_text_with_a_snapshot(r, desk):
    alice = desk.users["alice"]
    write_json(r.usage.STATUSLINE_SNAPSHOT.format(name="alice"), {
        "transcript_path": f"{alice.tail.dir}/x.jsonl",
        "context_window": {"total_input_tokens": 52_000, "context_window_size": 200_000},
        "cost": {"total_cost_usd": 1.5}, "model": {"display_name": "Opus 5.5"}, "effort": {"level": "high"}})

    assert r.usage.usage_text(alice, desk.displays()) == ("📊 Context: 52.0k of 200.0k tokens (26%)\n"
                                                         "Session cost: $1.50\nModel: Opus 5.5, high effort")


def test_spending_adds_up_costs_per_person(r, desk):
    paths = r.paths
    for i, (folder, base, last) in enumerate([
            (paths.WORKSPACES / "alice", 1.0, 2.0), (paths.WORKSPACES / "bob", 0, 0.5),
            (paths.ROOT, 0, 0.25), ("/tmp", 0, 0.1)]):
        write_json(r.usage.COSTS / f"s{i}.json", {"dir": str(folder), "base": base, "last": last, "since": 1759900000})

    assert r.usage.spending(desk.displays()) == ["", "💸 Spent so far, all sessions: $3.85",
                                                 "Alice $3.00 · Bob $0.50 · front desk $0.25 · other $0.10",
                                                 "Claude Code's own estimates, tracked since Oct 8."]


def test_limits_warn_once_per_level_and_window(r, desk):
    alice = desk.users["alice"]
    resets = iso_in(3600)
    write_json(r.usage.PLAN_CACHE, {"five_hour": {"utilization": 85, "resets_at": resets}})

    desk.limits.warn(alice)
    desk.limits.warn(alice)
    assert len(sent(r, ALICE_CHAT)) == 1
    assert sent(r, ALICE_CHAT)[0].startswith("⚠️ Be wary: the plan's 5-hour limit is 85% used.")

    write_json(r.usage.PLAN_CACHE, {"five_hour": {"utilization": 99.5, "resets_at": resets}})
    desk.limits.warn(alice)
    assert sent(r, ALICE_CHAT)[1].startswith("🛑 The plan's 5-hour limit is 99% used.")
    assert read_json(r.limits.ALERTS)["alice"]["five_hour"]["level"] == 99


def test_weekly_limits_note_each_quarter_then_warn(r, desk):
    alice = desk.users["alice"]
    resets = iso_in(3 * 86400)
    for used in (30, 40, 55, 80, 95):
        write_json(r.usage.PLAN_CACHE, {"seven_day": {"utilization": used, "resets_at": resets}})
        desk.limits.warn(alice)

    assert [text.split(" limit")[0] for text in sent(r, ALICE_CHAT)] == [
        "📊 The plan's Weekly", "📊 The plan's Weekly", "⚠️ Be wary: the plan's Weekly", "⚠️ Be wary: the plan's Weekly"]
    assert read_json(r.limits.ALERTS)["alice"]["seven_day"]["level"] == 90


def test_limits_start_over_in_a_new_window(r, desk):
    alice = desk.users["alice"]
    write_json(r.usage.PLAN_CACHE, {"five_hour": {"utilization": 85, "resets_at": iso_in(3600)}})
    desk.limits.warn(alice)
    write_json(r.usage.PLAN_CACHE, {"five_hour": {"utilization": 85, "resets_at": iso_in(5 * 3600)}})
    desk.limits.warn(alice)
    assert len(sent(r, ALICE_CHAT)) == 2


# --- users -------------------------------------------------------------------------------------

def test_load_users_gives_each_session_its_entry_as_it_is(r, desk):
    write_json(r.paths.USERS, {"alice": {"telegram_id": ALICE_CHAT, "display": "Alice", "something_new": [1]}})
    os.utime(r.paths.USERS, (time.time() + 5, time.time() + 5))
    desk.load_users()
    assert desk.users["alice"].entry["something_new"] == [1]


def test_load_users_follows_users_json(r, desk):
    assert set(desk.users) == {"alice", "bob"} and desk.owner == "alice"
    assert desk.users["alice"].tail.dir.endswith("-workspaces-alice")

    write_json(r.paths.USERS, {"alice": {"telegram_id": ALICE_CHAT, "display": "Alicia"}})
    os.utime(r.paths.USERS, (time.time() + 5, time.time() + 5))
    desk.load_users()

    assert set(desk.users) == {"alice"} and desk.users["alice"].display == "Alicia"
    assert desk.owner is None


# --- resilience --------------------------------------------------------------------------------

def desk_transcript(r):
    return project(r, r.paths.ROOT) / "desk.jsonl"


def quiet(r, desk, monkeypatch):
    """Keep poll() to routing: no Claude Code updates, and no assistant is up."""
    monkeypatch.setattr(desk.upgrader, "tick", lambda now, sessions, owner: None)
    for user in desk.users.values():
        monkeypatch.setattr(user, "ready", lambda: False)
        monkeypatch.setattr(user, "check_restart", lambda now: None)
        monkeypatch.setattr(user, "ensure_started", lambda now: None)


def test_one_bad_message_does_not_drop_the_rest_of_the_batch(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    real = desk.inbound

    def inbound(entry):
        if "boom" in entry.get("content", ""):
            raise RuntimeError("odd entry")
        real(entry)

    monkeypatch.setattr(desk, "inbound", inbound)
    append(desk_transcript(r), channel("boom", msg_id="1"), channel("hello", msg_id="2"))

    desk.poll()

    assert [w.msg_id for w in desk.users["alice"].queue] == ["2"]


def test_one_users_failure_does_not_hold_up_the_others(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    monkeypatch.setattr(desk.users["alice"], "read", lambda now, on_entry=None: 1 / 0)
    tended = []
    monkeypatch.setattr(desk.users["bob"], "check_restart", lambda now: tended.append("bob"))

    desk.poll()

    assert tended == ["bob"]


def test_a_redirect_url_racing_the_end_of_a_sign_in_is_harmless(r, desk):
    class EndsWhenRead(dict):
        def __getitem__(self, key):
            desk.users["alice"].pending_login = None  # the login thread's finally, mid-check
            return super().__getitem__(key)

    inbox = queue.Queue()
    desk.users["alice"].pending_login = EndsWhenRead(server="srv", inbox=inbox)

    desk.inbound(channel("http://localhost:1234/cb?code=abc"))

    assert inbox.get_nowait() == "http://localhost:1234/cb?code=abc"


def restarted_desk(r, monkeypatch):
    d = r.desk.Desk()
    d.start()
    quiet(r, d, monkeypatch)
    return d


def test_messages_that_arrive_while_the_router_restarts_are_routed(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    append(desk_transcript(r), channel("before", msg_id="1"))
    desk.poll()
    append(desk_transcript(r), channel("during the restart", msg_id="2"))

    after = restarted_desk(r, monkeypatch)
    after.poll()

    assert [w.body for w in after.users["alice"].queue] == ["during the restart"]


def test_a_front_desk_transcript_started_during_the_restart_is_read_from_the_top(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    append(desk_transcript(r), channel("before", msg_id="1"))
    desk.poll()
    newer = time.time() + 5
    fresh = project(r, r.paths.ROOT) / "after-clear.jsonl"
    append(fresh, channel("in the new transcript", msg_id="2"))
    os.utime(fresh, (newer, newer))

    after = restarted_desk(r, monkeypatch)
    after.poll()

    assert [w.body for w in after.users["alice"].queue] == ["in the new transcript"]


def test_after_a_long_outage_messages_from_the_gap_are_not_delivered_late(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    desk.poll()
    saved = read_json(r.desk.DESK_OFFSETS)
    write_json(r.desk.DESK_OFFSETS, {**saved, "saved": time.time() - r.desk.RESUME_WITHIN - 1})
    append(desk_transcript(r), channel("hours ago", msg_id="2"))

    after = restarted_desk(r, monkeypatch)
    after.poll()

    assert after.users["alice"].queue == []


def test_resuming_does_not_count_the_front_desks_queue_twice(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    desk.poll()
    append(desk_transcript(r), channel("during the restart", msg_id="2"))

    after = restarted_desk(r, monkeypatch)
    after.poll()

    assert after.desk.turn.pending == 1


def test_the_front_desk_is_reset_by_its_worker(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    monkeypatch.setattr(desk.desk, "clear", lambda: True)
    desk.routed = r.desk.DESK_RESET_EVERY

    desk.poll()
    assert kinds(desk.desk) == ["ResetContext"]
    r.work.serve(desk.desk, time.time())

    assert desk.routed == 0 and desk.desk.queue == []


def test_a_failed_front_desk_reset_waits_before_trying_again(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    tries = []
    monkeypatch.setattr(desk.desk, "clear", lambda: tries.append(1) or False)
    desk.routed = r.desk.DESK_RESET_EVERY

    for _ in range(2):
        desk.poll()
        r.work.serve(desk.desk, time.time())

    assert len(tries) == 1


def test_a_bad_allowlist_entry_does_not_stop_the_router(r, desk):
    write_json(r.paths.TELEGRAM_DIR / "access.json", {"allowFrom": ["100", "oops"]})
    desk.start()
    assert [p["scope"]["chat_id"] for m, p in r.api_calls if m == "setMyCommands"] == [100]


# --- network outages and failed turns ----------------------------------------------------------

def test_the_vm_is_offline_after_failed_probes_in_a_row_until_one_gets_through(r, monkeypatch):
    net = r.recovery.Network([("example.org", 443)])
    answers = iter([["refused"], [], ["refused"], ["refused"], ["refused"], []])
    monkeypatch.setattr(net, "problems", lambda: next(answers))

    net.probe(100)
    net.probe(110)
    assert net.online() and net.take_ended() == []  # one failed probe is a blip
    net.probe(120)
    net.probe(130)
    assert not net.online() and net.down_since == 120
    net.probe(140)
    net.probe(150)

    assert net.online() and net.last_outage == r.recovery.Outage(120, 150)
    assert net.take_ended() == [r.recovery.Outage(120, 150)] and net.take_ended() == []


def test_a_probe_connects_to_each_target(r, monkeypatch):
    import socket
    monkeypatch.setattr(r.recovery, "PROBE_TIMEOUT", 1)
    listening = socket.create_server(("127.0.0.1", 0))
    closed = socket.create_server(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    up = ("127.0.0.1", listening.getsockname()[1])

    assert r.recovery.Network([up]).problems() == []
    (problem,) = r.recovery.Network([up, ("127.0.0.1", closed_port)]).problems()
    assert problem.startswith(f"127.0.0.1:{closed_port}: ")
    listening.close()


def test_the_claude_api_probed_is_the_one_the_sessions_use(r, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    r.paths.ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    r.paths.ENV_FILE.write_text("ANTHROPIC_API_KEY=sk-x\nANTHROPIC_BASE_URL='https://router.internal:8443'\n")
    assert r.recovery.targets() == [("api.telegram.org", 443), ("router.internal", 8443)]

    r.paths.ENV_FILE.write_text("ANTHROPIC_API_KEY=sk-x\n")
    assert r.recovery.targets()[1] == ("api.anthropic.com", 443)


def api_error(user, status=None, error="unknown", text="API Error: Connection error."):
    Path(user.tail.dir).mkdir(parents=True, exist_ok=True)
    append(Path(user.tail.dir) / "t.jsonl", {"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": status,
                                              "error": error, "message": {"content": [{"type": "text", "text": text}]}})


def test_a_turn_that_fails_offline_is_picked_up_again_once_the_network_is_back(r, desk):
    alice, now = desk.users["alice"], time.time()
    desk.network.down_since = now - 600
    api_error(alice)

    desk.tend(alice, now)
    desk.tend(alice, now + 3600)  # long past the wait, but still offline

    assert sent(r, ALICE_CHAT) == ["⚠️ Claude hit an error before finishing: API Error: Connection error.\n\n"
                                  "It will pick up where it left off by itself in about a minute, once Claude can be "
                                  "reached."]
    assert alice.queue == []
    desk.network.down_since, desk.network.last_outage = None, r.recovery.Outage(now - 600, now + 3500)
    desk.tend(alice, now + 3600)
    desk.tend(alice, now + 3601)

    (note,) = alice.queue
    assert note.header.startswith("[Router · ") and note.body == ""
    assert "stopped on an API error before it was done (API Error: Connection error)." in note.header
    assert f"The VM was offline from {r.recovery.utc_time(now - 600)} to {r.recovery.utc_time(now + 3500)} UTC." \
        in note.header
    assert "Alice was told you would carry on by yourself" in note.header
    assert alice.turn.busy  # typing… shows again


def test_a_failed_turn_waits_for_its_assistant_to_be_idle(r, desk):
    alice, now = desk.users["alice"], time.time()
    api_error(alice)
    desk.tend(alice, now)
    alice.turn.busy = True

    desk.tend(alice, now + 60)
    assert alice.queue == []
    alice.turn.end()
    desk.tend(alice, now + 61)
    assert kinds(alice) == ["Deliver"]


def test_an_assistant_is_picked_up_again_at_most_twice_an_hour(r, desk):
    alice, now = desk.users["alice"], time.time()
    for wait in r.recovery.RESUME_AFTER:
        api_error(alice, 503, "server_error", "API Error: 503 upstream cooling down")
        desk.tend(alice, now)
        desk.tend(alice, now + wait - 1)
        assert alice.queue == []
        now += wait
        desk.tend(alice, now)
        assert alice.queue.pop().header.startswith("[Router · ")
        alice.turn.end()
    api_error(alice, 503, "server_error", "API Error: 503 upstream cooling down")
    desk.tend(alice, now)

    assert [text.split("\n\n")[1] for text in sent(r, ALICE_CHAT)] == [
        "It will pick up where it left off by itself in about a minute, once Claude can be reached.",
        "It will pick up where it left off by itself in about 5 minutes, once Claude can be reached.",
        "Send your message again in a minute. If it keeps happening, run `assistant status` on the VM."]
    assert alice.pickups.waiting is None


def test_a_failure_that_wont_pass_by_itself_asks_for_the_message_again(r, desk):
    alice = desk.users["alice"]
    api_error(alice, 401, "authentication_failed", "Please run /login · API Error: 401 OAuth access token is invalid.")

    desk.tend(alice, time.time())

    assert sent(r, ALICE_CHAT) == ["⚠️ Claude hit an error and stopped before finishing: Please run /login · "
                                  "API Error: 401 OAuth access token is invalid.\n\nSend your message again in a "
                                  "minute. If it keeps happening, run `assistant status` on the VM."]
    assert alice.pickups.waiting is None


def test_a_new_message_takes_the_place_of_picking_the_old_one_up(r, desk):
    alice, now = desk.users["alice"], time.time()
    api_error(alice)
    desk.tend(alice, now)

    desk.inbound(channel("hello??", msg_id="9"))
    alice.turn.end()
    desk.tend(alice, now + 3600)

    assert [w.msg_id for w in alice.queue] == ["9"] and alice.pickups.waiting is None


def test_no_turn_is_judged_stuck_while_the_vm_is_offline(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    alice, now = desk.users["alice"], time.time()
    alice.turn.busy, alice.turn.last_write = True, now - r.session.STALE_AFTER - 1
    desk.network.down_since = now

    desk.poll()
    assert alice.turn.busy and alice.turn.notice is None

    desk.network.down_since = None
    alice.read(time.time() + r.session.STALE_AFTER - 1)
    assert alice.turn.busy
    alice.read(time.time() + r.session.STALE_AFTER + 1)
    assert not alice.turn.busy


def test_typing_is_not_shown_while_the_vm_is_offline(r, desk, monkeypatch):
    monkeypatch.setattr(r.desk.threading, "Thread", InlineThread)
    alice = desk.users["alice"]
    alice.turn.expect(time.time())
    desk.network.down_since = time.time()

    desk.tend(alice, time.time())
    assert r.api_calls == []
    desk.network.down_since = None
    desk.tend(alice, time.time())
    assert [m for m, _ in r.api_calls] == ["sendChatAction"]


def test_a_message_is_held_while_telegram_cant_be_reached_and_keeps_its_place(r):
    r.unreachable = True
    assert r.telegram.send(ALICE_CHAT, "one") is None
    assert r.api_calls == [] and r.telegram.Outbox.chats() == [ALICE_CHAT]

    r.unreachable = False
    r.telegram.send(ALICE_CHAT, "two")

    assert sent(r, ALICE_CHAT) == ["one", "two"] and r.telegram.Outbox.chats() == []
    assert r.telegram.ChatHistory(ALICE_CHAT).take_deletable() == [901, 902]


def test_held_messages_go_out_once_telegram_can_be_reached(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    r.unreachable = True
    r.telegram.send(BOB_CHAT, "held")
    desk.poll()
    assert r.telegram.Outbox.chats() == [BOB_CHAT]  # still out of reach: kept

    r.unreachable = False
    desk.network.down_since = time.time()
    desk.next_send_held = 0
    desk.poll()
    assert sent(r) == []  # not while the router knows the VM is offline
    desk.network.down_since = None
    desk.poll()

    assert sent(r, BOB_CHAT) == ["held"] and r.telegram.Outbox.chats() == []


def test_after_a_long_outage_held_messages_go_out_and_the_owner_hears_of_it(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    r.unreachable = True
    r.telegram.send(BOB_CHAT, "held")
    r.unreachable = False
    start = time.time() - 900
    desk.network.ended.append(r.recovery.Outage(start, start + 840))

    desk.poll()

    assert sent(r, BOB_CHAT) == ["held"]
    assert sent(r, ALICE_CHAT) == [
        f"🌐 The VM lost its network from {time.strftime('%H:%M', time.gmtime(start))} UTC to "
        f"{time.strftime('%H:%M', time.gmtime(start + 840))} UTC (14m). Any request it interrupted is picked up "
        "again by itself."]
    assert [p.get("disable_notification") for m, p in r.api_calls if p.get("chat_id") == ALICE_CHAT] == [True]


def test_a_short_outage_is_not_reported(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    desk.network.ended.append(r.recovery.Outage(time.time() - 90, time.time() - 30))
    desk.poll()
    assert sent(r) == []


# --- Claude Code updates -----------------------------------------------------------------------

def rollout_in_progress(r, desk, monkeypatch, started):
    monkeypatch.setattr(r.upgrade, "installed_version", lambda: "2.0")
    monkeypatch.setattr(r.upgrade, "update_claude", lambda: None)
    monkeypatch.setattr(desk.desk, "claude_proc", lambda: None)
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "claude_proc", lambda: (1234, 999))
    monkeypatch.setattr(alice, "running_version", lambda proc: "1.0")
    monkeypatch.setattr(alice, "can_upgrade", lambda now, proc: False)
    monkeypatch.setattr(desk.users["bob"], "claude_proc", lambda: None)
    desk.upgrader.rollout = {"to": "2.0", "from": {"1.0"}, "restarted": ["Bob"], "stuck": set(), "started": started}


def tick(r, desk):
    desk.upgrader.tick(time.time() + r.upgrade.CHECK_EVERY, [desk.desk, *desk.users.values()], desk.users["alice"])


def test_an_update_report_waits_for_busy_sessions(r, desk, monkeypatch):
    rollout_in_progress(r, desk, monkeypatch, started=time.time())
    tick(r, desk)
    assert sent(r) == []


def test_an_update_is_reported_even_if_a_session_stays_busy_for_hours(r, desk, monkeypatch):
    rollout_in_progress(r, desk, monkeypatch, started=time.time() - r.upgrade.REPORT_AFTER - 1)

    tick(r, desk)

    text = sent(r, ALICE_CHAT)[0]
    assert text.startswith("🔄 Claude Code updated to 2.0 (from 1.0). Restarted once idle: Bob.")
    assert "Not moved yet, busy ever since the update 6h 1m ago: Alice. They move once idle." in text
    assert desk.upgrader.rollout is None


def test_an_idle_session_on_an_old_version_is_restarted_by_its_worker_with_its_switches(r, desk, monkeypatch):
    rollout_in_progress(r, desk, monkeypatch, started=time.time())
    desk.upgrader.rollout = None
    alice = desk.users["alice"]
    restarted = []
    monkeypatch.setattr(alice, "can_upgrade", lambda now, proc: True)
    monkeypatch.setattr(alice, "restart", lambda pid: restarted.append(pid))
    alice.session_switches = {"model": "sonnet"}

    tick(r, desk)
    assert kinds(alice) == ["Restart"] and alice.upgrade_tried == "2.0"
    assert desk.upgrader.rollout["restarted"] == ["Alice"]
    monkeypatch.setattr(alice, "ready", lambda: False)  # the switch waits for the restarted claude
    r.work.serve(alice, time.time())

    assert restarted == [1234]
    (item,) = alice.queue
    assert (type(item).__name__, item.setting, item.target, item.keep, item.quiet) == ("Switch", "model", "sonnet", False, True)


def test_a_session_that_got_busy_meanwhile_is_not_restarted(r, desk, monkeypatch):
    alice = desk.users["alice"]
    restarted = []
    monkeypatch.setattr(alice, "claude_proc", lambda: (1234, 999))
    monkeypatch.setattr(alice, "can_upgrade", lambda now, proc: False)
    monkeypatch.setattr(alice, "restart", lambda pid: restarted.append(pid))
    alice.upgrade_tried = "2.0"
    alice.queue = [r.work.Restart(time.time(), pid=1234)]

    r.work.serve(alice, time.time())

    assert restarted == [] and alice.queue == [] and alice.upgrade_tried is None


# --- one worker per session --------------------------------------------------------------------

def test_a_slow_terminal_does_not_hold_up_routing_or_the_others(r, desk, monkeypatch):
    quiet(r, desk, monkeypatch)
    alice = desk.users["alice"]
    gate, clearing = threading.Event(), threading.Event()
    monkeypatch.setattr(alice, "clear", lambda: clearing.set() or gate.wait(10))
    monkeypatch.setattr(r.work, "delete_history", lambda chat, recorded, keep: None)
    worker = r.work.Worker(alice)
    loop = threading.Thread(target=worker.loop, daemon=True)
    loop.start()
    try:
        alice.enqueue(r.work.Clear(time.time()))
        assert clearing.wait(5)  # alice's worker is now stuck in /clear
        append(desk_transcript(r), channel("hi", chat=BOB_CHAT, msg_id="9"))

        started = time.time()
        desk.poll()

        assert time.time() - started < 2
        assert [w.msg_id for w in desk.users["bob"].queue] == ["9"]
    finally:
        gate.set()
        worker.stop()
        loop.join(5)  # it finishes the /clear while the fake Bot API is still in place


def test_removing_someone_stops_their_worker(r, desk):
    stopped = []
    desk.workers["bob"].stop = lambda: stopped.append("bob")
    write_json(r.paths.USERS, {"alice": {"telegram_id": ALICE_CHAT, "display": "Alice", "role": "owner"}})
    os.utime(r.paths.USERS, (time.time() + 5, time.time() + 5))

    desk.load_users()

    assert stopped == ["bob"] and set(desk.workers) == {"alice"}


def test_several_plugin_commands_in_one_message_run_each_in_order(r, desk, monkeypatch):
    ran = []
    monkeypatch.setattr(r.plugins, "run", lambda user, text: ran.append(text))

    desk.inbound(channel("/plugin marketplace add upstash/context7\n/plugin install context7@context7-marketplace\n"))

    assert ran == ["/plugin marketplace add upstash/context7", "/plugin install context7@context7-marketplace"]


def test_a_command_whose_value_spans_lines_stays_one_command(r, desk, monkeypatch):
    ran = []
    monkeypatch.setattr(r.plugins, "run", lambda user, text: ran.append(text))

    desk.inbound(channel('/mcp add-json srv {\n  "type": "http"\n}'))

    assert ran == ['/mcp add-json srv {\n  "type": "http"\n}']


# --- approval dialogs --------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def captured(name):
    return (FIXTURES / name).read_text()


def test_the_approval_dialog_is_read_off_the_screen(r):
    dialog = r.claude_ui.approval_dialog(captured("approval-dialog.txt"))

    assert dialog.text == ("Bash command\nRemove empty directory /tmp/permtest/outer\nrmdir /tmp/permtest/outer\n"
                           "Dangerous rmdir operation on working directory or its ancestor: /tmp/permtest/outer")
    assert dialog.options == ((True, "Yes"), (False, "No")) and dialog.deny_in == 119
    assert r.claude_ui.approval_dialog(captured("approval-dialog-no.txt")).cursor == 1
    assert r.claude_ui.approval_dialog(captured("approval-after.txt")) is None


def test_a_message_put_back_in_the_box_is_read_off_the_screen(r):
    put_back = captured("message-put-back.txt")
    assert r.claude_ui.message_box(put_back) == "[Test C · msg 4] pasted below:\nWrite a 200 word poem about dogs."
    for screen in (put_back, captured("message-put-back-long.txt"), beside_a_side_panel(put_back)):
        assert r.claude_ui.holds_typed_message(screen)
    assert not r.claude_ui.holds_typed_message(captured("message-box-empty.txt"))


def beside_a_side_panel(screen):
    """screen as Claude Code shows it with its panel open on the right."""
    lines = screen.splitlines()
    width = max(map(len, lines)) + 2
    panel = ["  ✕", "  source: Current ▾", "", "  No changes this session"]
    return "\n".join(line.ljust(width) + "│" + (panel[i] if i < len(panel) else "") for i, line in enumerate(lines))


def test_the_approval_dialog_is_read_beside_the_side_panel(r):
    # Beside the panel, a typed message once answered a dialog nobody had been asked about.
    for name in ("approval-dialog.txt", "approval-dialog-no.txt"):
        screen = captured(name)
        assert r.claude_ui.approval_dialog(beside_a_side_panel(screen)) == r.claude_ui.approval_dialog(screen)


BORDERLESS = "approval-dialog-borderless-panel.txt"


def blinked(screen):
    """screen a moment later, when the status dot of the step at work has blinked off."""
    return screen.replace("● Merging the merge request", "  Merging the merge request")


def test_a_dialog_beside_a_borderless_panel_is_read_alone(r):
    # The panel's text on the rule's line once made the whole screen read as the dialog.
    dialog = r.claude_ui.approval_dialog(captured(BORDERLESS))

    assert dialog.text.startswith("Bash command\nMerge the merge request, deleting the branch\n")
    assert dialog.text.endswith("Permission rule Bash(bin/write:*) requires confirmation for this command.\n"
                                "/permissions to update rules")
    assert r.claude_ui.approval_dialog(blinked(captured(BORDERLESS))) == dialog


def test_a_dialog_beside_the_diff_of_changed_files_is_read(r):
    # Beside the diff, which runs down past "Do you want to proceed?", two dialogs went unseen and
    # the person's next messages answered them (29 Oct 2026).
    dialog = r.claude_ui.approval_dialog(captured("approval-dialog-diff-panel.txt"))

    assert dialog.text == ("Bash command\nRun gated close for ticket DEMO-12\nbin/gated close DEMO-12 2>&1 | tail -5\n"
                           "Permission rule Bash(bin/gated:*) requires confirmation for this command.\n"
                           "/permissions to update rules")
    assert dialog.options == ((True, "Yes"), (False, "No"))


@pytest.mark.parametrize("name, shown", [
    ("message-box-empty.txt", True), ("message-put-back.txt", True), ("message-box-diff-panel.txt", True),
    ("approval-dialog.txt", False), ("approval-dialog-diff-panel.txt", False), (BORDERLESS, False)])
def test_the_message_box_is_shown_only_when_nothing_stands_in_its_place(r, name, shown):
    assert r.claude_ui.message_box_shown(captured(name)) is shown
    assert not r.claude_ui.message_box_shown("")


def test_a_dialog_without_its_rule_stops_at_the_conversation(r):
    screen = "\n".join(line for line in captured(BORDERLESS).splitlines() if not line.startswith("───"))
    dialog = r.claude_ui.approval_dialog(screen)

    assert "One hiccup" not in dialog.text and "Merging" not in dialog.text
    assert r.claude_ui.approval_dialog(blinked(screen)) == dialog


def test_the_side_panel_is_cut_off_after_a_wide_character_too(r):
    narrow, wide, full_width = "a" * 50, "🔧" + "b" * 47, "─" * 60
    screen = "\n".join([narrow + "│ panel"] * 4 + [wide + "│ panel", full_width])

    assert r.claude_ui.without_side_panel(screen).splitlines() == [narrow] * 4 + [wide, full_width]
    assert r.claude_ui.without_side_panel(narrow + "\n" + full_width) == narrow + "\n" + full_width


@pytest.fixture
def dialog_on_screen(r, desk, monkeypatch):
    """Alice's assistant at work with the approval dialog on its screen; switch screens with .now."""
    alice = desk.users["alice"]
    state = SimpleNamespace(now="approval-dialog.txt")

    def screen():
        moved = any(args[-1] == "Down" for args, _ in r.keystrokes)
        return captured("approval-dialog-no.txt" if moved and state.now == "approval-dialog.txt" else state.now)

    monkeypatch.setattr(alice.pane, "screen", screen)
    alice.turn.busy = True
    return SimpleNamespace(alice=alice, worker=r.work.Worker(alice), screen=state)


def tap(desk, msg_id, data, label):
    desk.inbound(channel(label, msg_id=str(msg_id), callback_id=f"cb-{data}", callback_data=data))


def test_an_approval_dialog_is_relayed_with_buttons(r, desk, dialog_on_screen):
    dialog_on_screen.worker.step(time.time())

    method, params = r.api_calls[-1]
    assert method == "sendRichMessage" and params["chat_id"] == ALICE_CHAT
    intro, dialog, deadline = params["rich_message"]["blocks"]
    assert block_text(intro).startswith("🔐 Claude wants to do something that Claude Code's safety check stopped.")
    assert dialog["type"] == "pre" and "rmdir /tmp/permtest/outer" in dialog["text"]
    assert block_text(deadline) == "If you don't answer, Claude Code denies it by itself in 1:59."
    assert params["reply_markup"]["inline_keyboard"] == [[{"text": "✅ Approve", "callback_data": "ask:0"}],
                                                         [{"text": "🚫 Deny", "callback_data": "ask:1"}]]
    msg_id = dialog_on_screen.alice.approval.message_id
    assert read_json(r.paths.ASKS / ALICE_CHAT / f"{msg_id}.json")["kind"] == "approval"


def test_approving_presses_yes_in_the_dialog(r, desk, dialog_on_screen):
    alice, worker = dialog_on_screen.alice, dialog_on_screen.worker
    worker.step(time.time())
    msg_id = alice.approval.message_id

    tap(desk, msg_id, "ask:0", "✅ Approve")
    assert alice.queue == []  # nothing is typed to the assistant itself
    assert shown(r.api_calls[-1][1]).endswith("👉 ✅ Approve")
    r.keystrokes.clear()
    worker.step(time.time())

    assert r.keystrokes == [(("send-keys", "-t", "=u-alice:", "Enter"), None)]
    dialog_on_screen.screen.now = "approval-after.txt"
    worker.step(time.time() + 2)
    assert alice.approval is None


def test_denying_moves_to_no_first(r, desk, dialog_on_screen):
    alice, worker = dialog_on_screen.alice, dialog_on_screen.worker
    worker.step(time.time())
    tap(desk, alice.approval.message_id, "ask:1", "🚫 Deny")
    r.keystrokes.clear()

    worker.step(time.time())

    assert [args[-1] for args, _ in r.keystrokes] == ["Down", "Enter"]


def test_a_dialog_that_closes_unanswered_says_so_and_a_late_tap_is_told(r, desk, dialog_on_screen):
    alice, worker = dialog_on_screen.alice, dialog_on_screen.worker
    worker.step(time.time())
    msg_id = alice.approval.message_id
    dialog_on_screen.screen.now = "approval-after.txt"

    worker.step(time.time() + 2)

    assert alice.approval is None
    assert shown(r.api_calls[-1][1]).endswith("⌛ It closed before you answered, so Claude Code denied it.")
    r.api_calls.clear()
    tap(desk, msg_id, "ask:0", "✅ Approve")
    assert r.api_calls == [] and alice.queue == []  # already closed: the question says so


def test_a_tap_after_the_dialog_closed_is_too_late(r, desk, dialog_on_screen):
    alice, worker = dialog_on_screen.alice, dialog_on_screen.worker
    worker.step(time.time())
    msg_id = alice.approval.message_id
    alice.approval = None  # closed, and the worker hasn't noticed yet

    tap(desk, msg_id, "ask:0", "✅ Approve")

    assert shown(r.api_calls[-1][1]).endswith("⌛ Too late: the dialog had already closed, so Claude Code denied it.")


def questions_sent(r):
    return [params for method, params in r.api_calls if method == "sendRichMessage"]


def test_a_redrawn_dialog_keeps_its_one_question(r, desk, dialog_on_screen):
    alice, worker = dialog_on_screen.alice, dialog_on_screen.worker
    worker.step(time.time())
    msg_id = alice.approval.message_id
    dialog_on_screen.screen.now = BORDERLESS

    worker.step(time.time() + 1)

    assert len(questions_sent(r)) == 1
    method, params = r.api_calls[-1]
    assert method == "editMessageText" and params["message_id"] == msg_id
    assert "bin/write PUT" in params["rich_message"]["blocks"][1]["text"]
    assert params["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Approve"
    assert alice.approval.message_id == msg_id and alice.approval.answer is None


def test_after_an_answer_the_next_dialog_gets_a_question_of_its_own(r, desk, dialog_on_screen):
    alice, worker = dialog_on_screen.alice, dialog_on_screen.worker
    worker.step(time.time())
    first = alice.approval.message_id
    tap(desk, first, "ask:0", "✅ Approve")
    worker.step(time.time())
    dialog_on_screen.screen.now = BORDERLESS

    worker.step(time.time() + 1)

    assert len(questions_sent(r)) == 2 and alice.approval.message_id != first


def test_nothing_is_typed_into_an_open_dialog(r, desk, dialog_on_screen, monkeypatch):
    alice, worker = dialog_on_screen.alice, dialog_on_screen.worker
    monkeypatch.setattr(alice, "ready", lambda: True)
    alice.queue = [deliver(r, "[Telegram from Alice · msg 1 · 2026-10-08 20:15 UTC]", "1")]

    r.work.serve(alice, time.time())  # typing straight into it would have answered it: "1" approves
    assert r.keystrokes == [] and len(alice.queue) == 1

    worker.step(time.time())  # relays it, and waits
    assert alice.approval and len(alice.queue) == 1 and r.keystrokes == []


def test_a_dialog_that_opens_after_the_turn_looked_stuck_is_still_relayed(r, desk, dialog_on_screen):
    alice = dialog_on_screen.alice
    alice.turn.busy, alice.turn.stalled = False, True

    dialog_on_screen.worker.step(time.time())

    assert alice.approval and "Approve it?" in sent(r, ALICE_CHAT)[-1]


def unreadable(screen):
    """screen with a dialog the router doesn't know how to read."""
    return screen.replace("Do you want to proceed?", "Shall I go ahead?")


def test_nothing_is_typed_while_something_unreadable_hides_the_box_and_the_person_hears_why(r, desk, monkeypatch):
    alice = desk.users["alice"]
    screen = SimpleNamespace(now=unreadable(captured("approval-dialog.txt")))
    monkeypatch.setattr(alice.pane, "screen", lambda: screen.now)
    monkeypatch.setattr(alice, "ready", lambda: True)
    alice.queue = [deliver(r, "[Telegram from Alice · msg 1 · 2026-10-29 16:07 UTC]", "1")]
    worker, start = r.work.Worker(alice), time.time()

    for later in (0, r.work.BOX_HIDDEN_TELL - 1, r.work.BOX_HIDDEN_TELL, r.work.BOX_HIDDEN_TELL * 3):
        worker.step(start + later)

    assert r.keystrokes == [] and len(alice.queue) == 1 and alice.approval is None
    told = [text for text in sent(r, ALICE_CHAT) if "Your message is waiting" in text]
    assert len(told) == 1 and "/stop" in told[0]
    [saved] = (r.paths.SCREENS / "alice").iterdir()
    assert saved.name.endswith("-box-hidden.txt")

    screen.now = captured("message-box-empty.txt")
    worker.step(start + r.work.BOX_HIDDEN_TELL * 4)
    assert alice.queue == [] and alice.box_hidden_since is None


def test_the_front_desk_is_never_watched_for_dialogs(r, desk, monkeypatch):
    monkeypatch.setattr(desk.desk.pane, "screen", lambda: captured("approval-dialog.txt"))
    desk.desk.turn.busy = True
    r.work.Worker(desk.desk).step(time.time())
    assert r.api_calls == []


# --- /stop and a waiting /clear ----------------------------------------------------------------

ESCAPE = (("send-keys", "-t", "=u-alice:", "Escape"), None)


def busy(desk, name="alice"):
    user = desk.users[name]
    user.turn.expect(time.time())
    return user


@pytest.fixture
def escape_stops(r, desk, monkeypatch):
    """Escape in Alice's terminal makes Claude Code note the interrupt in its transcript."""
    alice = desk.users["alice"]
    transcript = Path(alice.tail.dir) / "t.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.touch()
    record = r.tmux.run

    def run(*args, stdin=None):
        if args[-1] == "Escape":
            append(transcript, {"type": "user", "message": {"content": [
                {"type": "text", "text": "[Request interrupted by user]"}]}})
        return record(*args, stdin=stdin)

    monkeypatch.setattr(r.tmux, "run", run)
    return alice


def test_stop_presses_escape_in_the_persons_terminal(r, desk, escape_stops):
    busy(desk)
    desk.inbound(channel("/stop"))
    assert ESCAPE not in r.keystrokes  # the worker presses it, the only thread typing into the terminal

    desk.workers["alice"].step(time.time())

    assert ESCAPE in r.keystrokes
    assert sent(r, ALICE_CHAT) == ["⏹️ Stopped."]
    assert not escape_stops.stop_requested


def test_stop_typed_in_the_russian_layout_works_too(r, desk, escape_stops):
    busy(desk)
    desk.inbound(channel("/ыещз"))
    assert escape_stops.stop_requested


def test_stop_with_nothing_running_presses_nothing(r, desk):
    desk.inbound(channel("/stop"))
    desk.workers["alice"].step(time.time())

    assert ESCAPE not in r.keystrokes
    assert sent(r, ALICE_CHAT) == ["Claude isn't working on anything, so there's nothing to stop."]


def test_stop_drops_a_planned_pickup(r, desk):
    alice = desk.users["alice"]
    from multiclaude.router.turn import Failure
    alice.pickups.plan(Failure("API Error: Connection error.", retryable=True), time.time())
    assert alice.pickups.waiting
    desk.inbound(channel("/stop"))
    assert alice.pickups.waiting is None


def test_stop_says_so_when_escape_showed_no_sign_of_stopping(r, desk, monkeypatch):
    alice = busy(desk)
    Path(alice.tail.dir).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(r.session, "STOP_TIMEOUT", 0)
    desk.inbound(channel("/stop"))

    desk.workers["alice"].step(time.time())

    assert ESCAPE in r.keystrokes
    assert sent(r, ALICE_CHAT)[-1].startswith("⚠️ Escape was pressed, but Claude didn't show that it stopped.")


@pytest.fixture
def escape_puts_back(r, monkeypatch):
    """Escape before any reply: Claude Code writes nothing and puts the message back in the
    message box, which the next two Escapes empty."""
    record = r.tmux.run

    def run(*args, stdin=None):
        result = record(*args, stdin=stdin)
        if args[0] != "capture-pane":
            return result
        escapes = r.keystrokes.count(ESCAPE)
        screen = captured("message-put-back.txt" if escapes == 1 else "message-box-empty.txt")
        return subprocess.CompletedProcess(args, 0, screen.encode(), b"")

    monkeypatch.setattr(r.tmux, "run", run)


def test_stop_before_any_reply_empties_the_box_the_message_was_put_back_in(r, desk, escape_puts_back):
    # 27 Oct 2026: a /stop just after a tap had the router say Escape hadn't worked, and the tap,
    # left in the box, went out again joined to the person's next message.
    alice = busy(desk)
    Path(alice.tail.dir).mkdir(parents=True, exist_ok=True)
    desk.inbound(channel("/stop"))

    desk.workers["alice"].step(time.time())

    assert r.keystrokes.count(ESCAPE) == 3
    assert sent(r, ALICE_CHAT) == ["⏹️ Stopped."]
    assert not alice.turn.busy


def test_a_clear_that_waits_offers_cancel_once(r, desk):
    alice = busy(desk)
    desk.inbound(channel("/clear"))

    r.work.serve(alice, time.time())
    r.work.serve(alice, time.time())

    asked = [p for m, p in r.api_calls if m == "sendRichMessage"]
    assert len(asked) == 1
    assert asked[0]["rich_message"]["markdown"].startswith("🧹 I'll clear once I finish")
    assert asked[0]["reply_markup"] == {"inline_keyboard": [[{"text": "Cancel", "callback_data": "ask:0"}]]}
    notice = alice.queue[0].notice
    assert read_json(r.paths.ASKS / ALICE_CHAT / f"{notice}.json")["kind"] == "cancel-clear"
    assert kinds(alice) == ["Clear"]


def test_cancel_drops_the_waiting_clear_and_lets_messages_through(r, desk, monkeypatch):
    alice = busy(desk)
    monkeypatch.setattr(alice, "clear", lambda: pytest.fail("cleared after all"))
    monkeypatch.setattr(alice, "ready", lambda: True)
    desk.inbound(channel("/clear"))
    r.work.serve(alice, time.time())
    desk.inbound(channel("NO CANCEL", msg_id="6"))
    notice = alice.queue[0].notice

    tap(desk, notice, "ask:0", "Cancel")
    tap(desk, notice, "ask:0", "Cancel")  # a second tap changes nothing
    r.work.serve(alice, time.time())

    edits = [p for m, p in r.api_calls if m == "editMessageText"]
    assert [e["rich_message"]["markdown"] for e in edits] == [r.asks.CLEAR_CANCELLED]
    assert kinds(alice) == []
    assert any("[header]" not in str(k) and "NO CANCEL" in str(k) for k in r.keystrokes)


def test_cancel_after_the_clear_started_is_too_late(r, desk):
    alice = busy(desk)
    desk.inbound(channel("/clear"))
    r.work.serve(alice, time.time())
    clear = alice.queue[0]
    clear.start()

    tap(desk, clear.notice, "ask:0", "Cancel")

    edits = [p for m, p in r.api_calls if m == "editMessageText"]
    assert [e["rich_message"]["markdown"] for e in edits] == [r.asks.CLEAR_TOO_LATE]
    assert not clear.cancelled


# --- /help and the bot's description -----------------------------------------------------------

def test_the_router_describes_the_bot_with_its_owner_on_start(r, desk):
    desk.start()
    calls = dict((m, p) for m, p in r.api_calls if m in ("setMyDescription", "setMyShortDescription"))
    box, line = calls["setMyDescription"]["description"], calls["setMyShortDescription"]["short_description"]

    assert "Claude on Alice's VM" in box and "• photos and files, audio and video" in box and "/help" in box
    assert "voice notes" not in box and "location" not in box  # their add-ins are off
    assert line == "Claude. Claude on Alice's VM, on Telegram."  # the add-ins with other ways in are off
    assert len(box) <= r.about.DESCRIPTION_MAX and len(line) <= r.about.SHORT_DESCRIPTION_MAX


def test_help_is_in_the_command_menu(r, desk):
    write_json(r.paths.TELEGRAM_DIR / "access.json", {"allowFrom": [ALICE_CHAT]})
    desk.start()
    menu = next(p["commands"] for m, p in r.api_calls if m == "setMyCommands")
    assert menu[-1]["command"] == "help"


def test_help_sends_the_fixed_part_and_has_the_assistant_add_its_mcp_servers(r, desk):
    desk.inbound(channel("/help"))

    [text] = sent(r, ALICE_CHAT)
    assert text.startswith("You can send me:\n• text, in any language")
    assert "`/clear` — Clear my conversation context" in text and "`/help` — What you can send me" in text
    assert "`/mcp` — Your MCP servers: `/mcp add`, `/mcp login name`" in text  # examples as code, not links
    assert text.endswith("What I can do with your MCP servers comes next.")
    assert "reach me through" not in text  # no add-in with another way in is on
    [note] = desk.users["alice"].queue
    assert note.header.startswith("[Router · ") and "Alice sent /help" in note.header
    assert "`tg send`" in note.header and "What I can do with your MCP servers" in note.header


def test_help_while_busy_says_the_mcp_part_waits(r, desk):
    busy(desk)
    desk.inbound(channel("/help"))
    assert sent(r, ALICE_CHAT)[0].endswith("comes once I finish what I'm doing.")


def test_help_typed_in_the_greek_layout_works_too(r, desk):
    desk.inbound(channel("/ηελπ"))
    assert sent(r, ALICE_CHAT)[0].startswith("You can send me:")


# --- /restart ----------------------------------------------------------------------------------

def test_restart_has_the_worker_start_over_without_waiting_for_the_turn(r, desk):
    alice = busy(desk)
    from multiclaude.router.turn import Failure
    alice.pickups.plan(Failure("API Error: Connection error.", retryable=True), time.time())
    desk.inbound(channel("/куыефке"))  # /restart in the Russian layout

    assert alice.restart_requested and alice.pickups.waiting is None
    assert sent(r, ALICE_CHAT) == ["🔄 Restarting Claude with a new conversation…"]


def test_the_worker_restarts_drops_what_it_supersedes_and_empties_the_chats(r, desk, monkeypatch):
    alice = desk.users["alice"]
    r.telegram.ChatHistory(ALICE_CHAT).remember(40)
    monkeypatch.setattr(alice, "restart_fresh", lambda: True)
    monkeypatch.setattr(alice, "ready", lambda: False)  # what's left waits for the new claude
    alice.session_switches = {"model": "sonnet"}
    alice.queue = [r.work.Clear(time.time()), r.work.Deliver(time.time(), "[Telegram from Alice · msg 41]", "hi", "41"),
                  r.work.Restart(time.time(), pid=1234)]
    alice.restart_requested = True

    desk.workers["alice"].step(time.time())

    assert kinds(alice) == ["Deliver", "Switch"] and not alice.restart_requested
    assert any(m == "deleteMessages" and 40 in p["message_ids"] for m, p in r.api_calls)
    assert sent(r, ALICE_CHAT)[-1] == r.work.RESTARTED


def test_a_restart_that_does_not_come_back_says_so(r, desk, monkeypatch):
    alice = desk.users["alice"]
    monkeypatch.setattr(alice, "restart_fresh", lambda: False)
    alice.restart_requested = True

    desk.workers["alice"].step(time.time())

    assert sent(r, ALICE_CHAT) == ["⚠️ I ended Claude's session, but it hasn't come back yet. "
                                  "Run `assistant status` on the VM to check."]


def test_restart_fresh_ends_claude_and_asks_its_run_loop_for_a_new_conversation(r, desk, monkeypatch):
    alice = busy(desk)
    procs = iter([(111, 500), (111, 500), None, (222, 3), (222, r.session.READY_AFTER)])
    ended = []
    monkeypatch.setattr(alice, "claude_proc", lambda: next(procs))
    monkeypatch.setattr(r.session, "end_process", ended.append)

    assert alice.restart_fresh()

    assert ended == [111]
    respawn = next(args for args, _ in r.keystrokes if args[0] == "respawn-pane")
    assert respawn[-3:] == (str(r.paths.BIN / "run-user.sh"), "alice", "fresh")
    assert not alice.turn.busy and alice.pid is None


# --- the front desk's poller -------------------------------------------------------------------

PS = "\n".join(["  10     1 claude --channels plugin:telegram@claude-plugins-official",
                "  11    10 bun run --cwd /p start", "  12    11 /home/x/.bun/bin/bun server.ts",
                "  20     1 claude mcp list", "  21    20 bun run --cwd /p start", "  22    21 bun server.ts",
                "  30    10 sleep 99"])


def test_the_poller_must_be_a_server_under_the_front_desk(r):
    assert r.poller.poller_under(PS, 12, 10)
    assert not r.poller.poller_under(PS, 22, 10)  # another session's copy took over
    assert not r.poller.poller_under(PS, 30, 10)  # its pid reused by something else
    assert not r.poller.poller_under(PS, 99, 10)  # gone


def test_no_pid_file_means_no_poller(r):
    assert not r.poller.polling(10)  # the poller removes it as it exits


def watch(r, desk, monkeypatch, proc=(10, 600), polls=False):
    """The desk's PollerWatch, with the front desk's claude at proc and its poller there or not;
    it returns the restarts it hands to the background."""
    monkeypatch.setattr(desk.desk, "claude_proc", lambda: proc)
    monkeypatch.setattr(r.poller, "polling", lambda pid: polls)
    restarts = []
    return desk.poller, lambda now: desk.poller.tick(now, desk.users["alice"], lambda what, fn, *args: restarts.append(args)), restarts


def test_a_front_desk_without_a_poller_is_restarted_and_the_owner_told(r, desk, monkeypatch):
    _, tick, restarts = watch(r, desk, monkeypatch)

    tick(1000)
    tick(1000 + r.poller.DOWN_AFTER - r.poller.CHECK_EVERY)
    assert restarts == [] and sent(r) == []  # it may be starting one

    tick(1000 + r.poller.DOWN_AFTER)
    assert restarts == [(10,)]
    assert sent(r, ALICE_CHAT) == [r.poller.RESTARTED.format(bot="Claude")]


def test_a_restart_that_didnt_help_waits_before_the_next(r, desk, monkeypatch):
    _, tick, restarts = watch(r, desk, monkeypatch)
    for now in range(1000, 1000 + r.poller.RESTART_GAP, r.poller.CHECK_EVERY):
        tick(now)
    assert len(restarts) == 1
    tick(1000 + r.poller.DOWN_AFTER + r.poller.RESTART_GAP)
    assert len(restarts) == 2


def test_a_poller_coming_back_or_a_front_desk_starting_counts_as_fine(r, desk, monkeypatch):
    poller, tick, restarts = watch(r, desk, monkeypatch)
    tick(1000)
    monkeypatch.setattr(r.poller, "polling", lambda pid: True)
    tick(1000 + r.poller.CHECK_EVERY)
    assert poller.missing_since is None

    monkeypatch.setattr(desk.desk, "claude_proc", lambda: None)
    for now in range(1000, 1000 + 2 * r.poller.DOWN_AFTER, r.poller.CHECK_EVERY):
        tick(now)
    assert restarts == []
