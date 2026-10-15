"""Tests for the slack add-in (lib/multiclaude/addons/slack): Slack's envelopes as messages, the
router's answers going back to Slack, `slack ask` questions, wiping a DM, signing in, and voice
clips. Nothing here reaches Slack."""
import json
import time

import pytest

from conftest import (ALICE_CHAT, ALICE_DM, ALICE_SLACK, BOB_CHAT, RELAY, channel, fresh_multiclaude, sent, slack_dm,
                      slack_posts)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """The add-in's modules, with every location in tmp_path and the relay set up."""
    monkeypatch.setenv("ASSISTANT_ROOT", str(tmp_path / "assistant"))
    fresh_multiclaude(monkeypatch)
    from multiclaude.addons.slack import api, chat, messages
    from multiclaude.router import relay_entrance
    monkeypatch.setattr(relay_entrance, "settings", lambda: RELAY)
    return type("Lib", (), {"slack": api, "chat": chat, "messages": messages})


def test_a_dm_becomes_a_message_with_its_files(lib):
    msg = lib.messages.message({
        "type": "message", "channel_type": "im", "channel": "D1", "user": "U1", "ts": "1791529806.264069",
        "text": "see <https://example.com|this> &amp; that", "subtype": "file_share",
        "files": [{"id": "F1", "name": "note.m4a", "mimetype": "audio/mp4", "size": 1200, "subtype": "slack_audio"}]})
    assert (msg.chat, msg.sender, msg.msg_id, msg.app) == ("D1", "U1", "1791529806.264069", "Slack")
    assert msg.text == "see this (https://example.com) & that"
    assert msg.attrs["slack_files"] == [{"id": "F1", "name": "note.m4a", "mime": "audio/mp4", "size": 1200, "voice": True}]
    assert msg.when == "2026-10-09 07:10 UTC"


def test_the_bots_own_messages_and_edits_are_ignored(lib):
    def event(**extra):
        return {"type": "message", "channel_type": "im", "channel": "D1", "user": "U1", "ts": "1.0", "text": "x", **extra}
    assert lib.messages.message(event(bot_id="B1")) is None
    assert lib.messages.message(event(subtype="message_changed")) is None
    assert lib.messages.message(event(channel_type="channel")) is None


def test_a_slash_command_reads_like_its_telegram_twin(lib):
    msg = lib.messages.command({"command": "/model", "text": "opus default", "user_id": "U1", "channel_id": "D1",
                                "trigger_id": "T9"})
    assert msg.text == "/model opus default" and msg.msg_id == "cmd-T9"


def test_a_button_tap_names_the_question_and_the_option(lib):
    msg = lib.messages.tap({
        "type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "D1"}, "message": {"ts": "5.5"},
        "actions": [{"action_id": "ask:1", "action_ts": "6.6", "text": {"type": "plain_text", "text": "No"}}]})
    assert (msg.chat, msg.msg_id, msg.text, msg.tap) == ("D1", "5.5", "No", "6.6")
    assert msg.attrs["callback_data"] == "ask:1"


def test_questions_get_one_button_per_option_and_show_the_choice_after(lib):
    asked = lib.slack.question_blocks("Push it?", ["Push", "Wait"])
    assert [b["action_id"] for b in asked[1]["elements"]] == ["ask:0", "ask:1"]
    answered = lib.slack.question_blocks("Push it?", ["Push", "Wait"], choice="Push")
    assert answered[-1] == {"type": "markdown", "text": "👉 Push"}


def test_slack_shows_what_to_paste_as_code(lib, monkeypatch):
    posted = []
    monkeypatch.setattr(lib.slack, "post", lambda channel, text: posted.append((channel, text)) or "1.0")
    lib.chat.SlackChat("D1", "U1").send("Run `/mcp login github`", copy=["/mcp login github", "assistant status"])
    assert posted == [("D1", "Run `/mcp login github`\n`assistant status`")]


def test_tokens_are_read_from_state(lib, tmp_path, monkeypatch):
    assert not lib.slack.configured()
    (tmp_path / "assistant" / "state").mkdir(parents=True)
    (tmp_path / "assistant" / "state" / "slack.json").write_text(json.dumps({"bot_token": "b"}))
    assert lib.slack.configured() and lib.slack.bot().token == "b"
    from multiclaude.router import relay_entrance
    monkeypatch.setattr(relay_entrance, "settings", lambda: {})
    assert not lib.slack.configured()  # with no relay, Slack can't reach the VM


class FakeDm:
    """The Web API over one DM: Alice's messages, the bot's, and a thread under a voice clip."""
    PAGES = {None: ([{"ts": "1.0", "user": "U1"}, {"ts": "2.0", "user": "UBOT", "bot_id": "B1"}], "next"),
             "next": ([{"ts": "3.0", "user": "U1", "reply_count": 2}], "")}
    THREAD = [{"ts": "3.0", "user": "U1"}, {"ts": "3.5", "user": "UBOT", "bot_id": "B1"}, {"ts": "3.6", "user": "U1"}]

    def __init__(self):
        self.calls = []

    def call_form(self, method, **params):
        self.calls.append((method, params))
        if method == "conversations.replies":
            return {"messages": self.THREAD}
        messages, cursor = self.PAGES[params["cursor"]]
        return {"messages": messages, "response_metadata": {"next_cursor": cursor}}

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}


def deleted(api):
    return [p["ts"] for m, p in api.calls if m == "chat.delete"]


def test_wiping_a_dm_deletes_what_the_bot_said_before_then_threads_included(lib, monkeypatch):
    api = FakeDm()
    monkeypatch.setattr(lib.slack, "bot", lambda: api)
    monkeypatch.setattr(lib.slack, "dm_channel", lambda person, api=None: "D1")

    lib.slack.wipe_dm("U1", before=1791532945.5)

    assert deleted(api) == ["2.0", "3.5"]
    assert all(p["latest"] == "1791532945.500000" for m, p in api.calls if m.startswith("conversations."))


def test_once_signed_in_the_persons_own_messages_go_too_as_them_except_those_kept(lib, monkeypatch):
    api, theirs = FakeDm(), FakeDm()
    monkeypatch.setattr(lib.slack, "bot", lambda: api)
    monkeypatch.setattr(lib.slack, "dm_channel", lambda person, api=None: "D1")
    monkeypatch.setattr(lib.slack, "person_api", lambda person: theirs)

    lib.slack.wipe_dm("U1", before=1791532945.5, keep={"3.6"})

    assert deleted(api) == ["2.0", "3.5"]
    assert deleted(theirs) == ["1.0", "3.0"]


@pytest.fixture
def app(lib, tmp_path):
    """Slack set up with the app's client id and secret, as sign-in needs."""
    (tmp_path / "assistant" / "state").mkdir(parents=True)
    (tmp_path / "assistant" / "state" / "slack.json").write_text(json.dumps({
        "bot_token": "b", "client_id": "C1", "client_secret": "secret"}))
    return lib


def exchange(monkeypatch, slack, person):
    """oauth.v2.access answering for person; the calls made, with the token they used."""
    calls = []

    def call_form(self, method, **params):
        calls.append((self.token, method, params))
        return {"authed_user": {"id": person, "access_token": f"xoxp-{person}"}}

    monkeypatch.setattr(slack.SlackApi, "call_form", call_form)
    monkeypatch.setattr(slack.SlackApi, "call", lambda self, method, **params: calls.append((self.token, method, params)))
    return calls


def test_a_sign_in_link_comes_back_to_the_relay_and_its_token_is_kept(app, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    link = parse_qs(urlsplit(app.slack.sign_in_link("U1")).query)
    assert (link["user_scope"], link["redirect_uri"]) == (["chat:write"], ["https://relay.test/slack/oauth"])
    calls = exchange(monkeypatch, app.slack, "U1")

    assert app.slack.sign_in("code", link["state"][0]) == "U1"

    assert calls[0][:2] == (None, "oauth.v2.access")
    assert calls[0][2]["redirect_uri"] == "https://relay.test/slack/oauth"
    assert app.slack.person_api("U1").token == "xoxp-U1"


def test_a_sign_in_with_someone_elses_link_is_revoked_and_not_kept(app, monkeypatch):
    state = app.slack.sign_in_state("U1", app.slack.tokens())
    calls = exchange(monkeypatch, app.slack, "U2")

    with pytest.raises(app.slack.SlackError, match="someone else"):
        app.slack.sign_in("code", state)

    assert calls[-1][:2] == ("xoxp-U2", "auth.revoke")
    assert app.slack.person_api("U2") is None


def test_no_sign_in_link_without_the_apps_client_secret(lib):
    assert lib.slack.sign_in_link("U1") is None


# --- the router ------------------------------------------------------------------------------

def test_a_slack_dm_reaches_the_persons_assistant_and_answers_go_back_to_slack(r, slack_desk):
    slack_desk.relay_inbound(slack_dm("040 to work"))

    alice = slack_desk.users["alice"]
    delivered = alice.queue[0]
    assert delivered.header.startswith("[Slack from Alice · msg 1791529806.264069 · ")
    assert delivered.body == "040 to work"
    assert ("reactions.add", {"channel": ALICE_DM, "timestamp": "1791529806.264069", "name": "eyes"}) in r.slack_calls
    alice.say("on it")
    assert slack_posts(r) == [(ALICE_DM, "on it")]
    assert sent(r) == []  # nothing went to Telegram


def test_a_telegram_message_brings_the_answers_back_to_telegram(r, slack_desk):
    slack_desk.relay_inbound(slack_dm("hi"))
    slack_desk.inbound(channel("hello again"))
    slack_desk.users["alice"].say("here")
    assert sent(r, ALICE_CHAT) == ["here"]


def test_a_slack_slash_command_runs_and_answers_in_slack(r, slack_desk):
    slack_desk.relay_inbound({"type": "slash_commands", "payload": {
        "command": "/model", "text": "", "user_id": ALICE_SLACK, "channel_id": ALICE_DM, "trigger_id": "T1"}})
    assert slack_posts(r) and slack_posts(r)[0][0] == ALICE_DM
    assert not any(m == "reactions.add" for m, _ in r.slack_calls)  # a command isn't a message to react to


def test_a_tap_on_a_slack_question_shows_the_choice_and_tells_the_assistant(r, slack_desk):
    from multiclaude import jsonfile
    from multiclaude.addons.slack import api
    jsonfile.write_json(api.asks_folder(ALICE_DM) / "5.5.json", {"question": "Push it?", "options": ["Push", "Wait"]})
    tap = {"type": "interactive", "payload": {
        "type": "block_actions", "user": {"id": ALICE_SLACK}, "channel": {"id": ALICE_DM}, "message": {"ts": "5.5"},
        "actions": [{"action_id": "ask:0", "action_ts": "6.6", "text": {"type": "plain_text", "text": "Push"}}]}}
    slack_desk.relay_inbound(tap)
    slack_desk.relay_inbound(tap)  # Slack can send it twice

    updates = [p for m, p in r.slack_calls if m == "chat.update"]
    assert len(updates) == 1 and updates[0]["blocks"][-1] == {"type": "markdown", "text": "👉 Push"}
    alice = slack_desk.users["alice"]
    assert len(alice.queue) == 1
    assert alice.queue[0].header.startswith("[Slack from Alice · tapped a button on your question msg 5.5")
    assert 'slack edit 5.5 "..."' in alice.queue[0].body


def test_someone_unlinked_on_slack_is_told_and_the_owner_gets_ready_commands(r, slack_desk):
    slack_desk.relay_inbound(slack_dm("hello?", user="U0NEW", ts="7.0"))
    slack_desk.relay_inbound(slack_dm("anyone?", user="U0NEW", ts="8.0"))

    assert [c for c, _ in slack_posts(r)] == ["D0NEW", "D0NEW"]
    told = sent(r, ALICE_CHAT)
    assert len(told) == 1 and "Stranger Danger" in told[0]
    buttons = [m for m in r.api_calls if m[0] == "sendRichMessage"][0][1]["reply_markup"]
    assert "assistant slack-link bob U0NEW" in json.dumps(buttons)
    assert all(not u.queue for u in slack_desk.users.values())


def test_clear_from_slack_empties_both_chats(r, slack_desk, monkeypatch):
    wiped = []
    from multiclaude import addons
    from multiclaude.addons.slack import api
    monkeypatch.setattr(addons.slack.threading, "Thread", InlineThread)
    monkeypatch.setattr(r.work, "delete_history", lambda chat, recorded, keep: wiped.append(("telegram", chat)))
    monkeypatch.setattr(api, "wipe_dm", lambda person, before, keep: wiped.append(("slack", person)))
    slack_desk.relay_inbound({"type": "slash_commands", "payload": {
        "command": "/clear", "text": "", "user_id": ALICE_SLACK, "channel_id": ALICE_DM, "trigger_id": "T2"}})
    alice = slack_desk.users["alice"]
    monkeypatch.setattr(alice, "clear", lambda: True)

    r.work.serve(alice, time.time())

    assert wiped == [("telegram", ALICE_CHAT), ("slack", ALICE_SLACK)]
    assert slack_posts(r)[-1] == (ALICE_DM, "🧹 Context cleared. Starting fresh.")


def test_clear_on_slack_offers_the_sign_in_that_lets_it_delete_the_persons_own_messages(r, slack_desk, monkeypatch):
    monkeypatch.setattr(r.work, "delete_history", lambda chat, recorded, keep: None)
    from multiclaude.addons.slack import api
    monkeypatch.setattr(api, "wipe_dm", lambda person, before, keep: None)
    monkeypatch.setattr(api, "sign_in_link", lambda person: f"https://slack.test/sign-in/{person}")
    slack_desk.relay_inbound({"type": "slash_commands", "payload": {
        "command": "/clear", "text": "", "user_id": ALICE_SLACK, "channel_id": ALICE_DM, "trigger_id": "T3"}})
    alice = slack_desk.users["alice"]
    monkeypatch.setattr(alice, "clear", lambda: True)

    r.work.serve(alice, time.time())

    assert f"[Sign in with Slack](https://slack.test/sign-in/{ALICE_SLACK})" in slack_posts(r)[-1][1]


def test_someone_back_from_signing_in_to_slack_is_told_it_worked(r, slack_desk, monkeypatch):
    from multiclaude.addons.slack import api
    signed = []
    monkeypatch.setattr(api, "sign_in", lambda code, state: signed.append((code, state)) or ALICE_SLACK)
    monkeypatch.setattr(api, "dm_channel", lambda person: ALICE_DM)

    slack_desk.relay_inbound({"envelope_id": "sign-in:C1", "type": "sign_in", "payload": {"code": "C1", "state": "S1"}})

    assert signed == [("C1", "S1")]
    assert slack_posts(r) == [(ALICE_DM, "✅ Signed in. From now on /clear deletes your own messages here too.")]



def test_a_clear_on_slack_that_waits_offers_cancel_there(r, slack_desk):
    alice = busy(slack_desk)
    slack_desk.relay_inbound({"type": "slash_commands", "payload": {
        "command": "/clear", "text": "", "user_id": ALICE_SLACK, "channel_id": ALICE_DM, "trigger_id": "T4"}})
    r.work.serve(alice, time.time())
    (posted,) = [p for m, p in r.slack_calls if m == "chat.postMessage"]
    assert posted["blocks"][-1]["elements"][0]["text"]["text"] == "Cancel"
    notice = alice.queue[0].notice

    slack_desk.relay_inbound({"type": "interactive", "payload": {
        "type": "block_actions", "user": {"id": ALICE_SLACK}, "channel": {"id": ALICE_DM}, "message": {"ts": notice},
        "actions": [{"action_id": "ask:0", "action_ts": "9.9", "text": {"type": "plain_text", "text": "Cancel"}}]}})

    (update,) = [p for m, p in r.slack_calls if m == "chat.update"]
    assert (update["ts"], update["text"]) == (notice, r.asks.CLEAR_CANCELLED)
    assert alice.queue[0].cancelled



def test_help_on_slack_lists_what_slack_takes_and_is_answered_there(r, slack_desk):
    slack_desk.relay_inbound({"type": "slash_commands", "payload": {
        "command": "/help", "text": "", "user_id": ALICE_SLACK, "channel_id": ALICE_DM, "trigger_id": "T2"}})

    [(dm, text)] = slack_posts(r)
    assert dm == ALICE_DM and "• files and images" in text and "location" not in text
    assert "You can also reach me through Telegram." in text
    assert "`slack send`" in slack_desk.users["alice"].queue[0].header

def busy(desk, name="alice"):
    user = desk.users[name]
    user.turn.expect(time.time())
    return user


class InlineThread:
    """threading.Thread that runs its target on start(), so tests see the result at once."""

    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def test_config_on_slack_says_the_control_panel_opens_in_telegram(r, slack_desk):
    slack_desk.relay_inbound({"type": "slash_commands", "payload": {
        "command": "/config", "text": "", "user_id": ALICE_SLACK, "channel_id": ALICE_DM, "trigger_id": "T6"}})

    assert slack_posts(r) == [(ALICE_DM, "The control panel opens in Telegram: send /config to Claude there.")]
    assert sent(r) == []


def test_help_on_telegram_names_slack_for_someone_linked(r, slack_desk):
    slack_desk.inbound(channel("/help"))
    slack_desk.inbound(channel("/help", chat=BOB_CHAT, msg_id="6"))

    assert "You can also reach me through Slack." in sent(r, ALICE_CHAT)[0]
    assert "reach me through" not in sent(r, BOB_CHAT)[0]


def test_the_bots_description_names_slack(r, slack_desk):
    slack_desk.describe_bot()
    line = next(p["short_description"] for m, p in r.api_calls if m == "setMyShortDescription")
    assert line == "Claude. Claude on Alice's VM, on Telegram and Slack."


def test_a_slack_voice_clip_is_transcribed_with_voice_notes_on_and_its_words_go_in_its_thread(r, slack_desk, addons_on,
                                                                                               monkeypatch):
    addons_on("slack", "voice-notes")
    from multiclaude.addons import voice_notes
    alice = slack_desk.users["alice"]
    monkeypatch.setattr(voice_notes, "transcribe", lambda source, note_id: f"words of {note_id}")
    typed = []
    monkeypatch.setattr(alice, "type_message", lambda header, body: typed.append((header, body)))
    clip = {"id": "F1", "name": "clip.m4a", "mimetype": "audio/mp4", "subtype": "slack_audio"}
    slack_desk.relay_inbound(slack_dm("", subtype="file_share", files=[clip]))

    alice.queue[0].run(alice, time.time())

    ((header, body),) = typed
    assert header.startswith("[Slack from Alice · voice note · msg 1791529806.264069")
    assert body.startswith("words of F1\n\nAttachments:\nslack file: F1")
    assert slack_posts(r)[-1] == (ALICE_DM, "🎙️ words of F1")
    assert r.slack_calls[-1][1]["thread_ts"] == "1791529806.264069"
    assert "• voice clips (I transcribe them)" in r.about.can_send("Slack")


def test_a_slack_voice_clip_goes_in_as_a_file_while_voice_notes_is_off(r, slack_desk):
    clip = {"id": "F1", "name": "clip.m4a", "mimetype": "audio/mp4", "subtype": "slack_audio"}
    slack_desk.relay_inbound(slack_dm("", subtype="file_share", files=[clip]))

    (item,) = slack_desk.users["alice"].queue
    assert type(item).__name__ == "Deliver"
    assert item.body == "Attachments:\nslack file: F1 (clip.m4a, audio/mp4)"


# --- while it's off -------------------------------------------------------------------------------

def test_while_slack_is_off_nothing_from_slack_is_routed_and_no_answer_goes_there(r, slack_desk, addons_on, monkeypatch):
    addons_on()
    from multiclaude.addons.slack import api
    monkeypatch.setattr(api, "wipe_dm", lambda *args: pytest.fail("a Slack DM wiped while slack is off"))
    alice = slack_desk.users["alice"]
    slack_desk.relay_inbound(slack_dm("040 to work"))
    slack_desk.relay_inbound({"type": "slash_commands", "payload": {
        "command": "/clear", "text": "", "user_id": ALICE_SLACK, "channel_id": ALICE_DM, "trigger_id": "T5"}})
    assert alice.queue == []

    monkeypatch.setattr(alice, "clear", lambda: True)
    slack_desk.inbound(channel("/clear"))
    r.work.serve(alice, time.time())
    slack_desk.inbound(channel("/help", msg_id="6"))

    assert r.slack_calls == []
    assert sent(r, ALICE_CHAT)[0] == "🧹 Context cleared. Starting fresh."
    assert "Slack" not in sent(r, ALICE_CHAT)[1]
