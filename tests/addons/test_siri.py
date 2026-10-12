"""Tests for the siri add-in (lib/multiclaude/addons/siri): voice keys, the reply mailbox, Siri's
requests from the relay, `tg voice`, and nothing of it while the add-in is off."""
import io
import json
import threading
import urllib.error
from types import SimpleNamespace

import pytest

from conftest import ALICE_CHAT, BOB_CHAT, RELAY, channel, fresh_multiclaude, sent


@pytest.fixture
def v(tmp_path, monkeypatch):
    """The add-in's voice keys and mailbox, with their state in tmp_path."""
    monkeypatch.setenv("ASSISTANT_ROOT", str(tmp_path / "assistant"))
    fresh_multiclaude(monkeypatch)
    from multiclaude.addons.siri import voice
    monkeypatch.setattr(voice, "POLL", 0.01)
    return voice


@pytest.fixture
def siri(v, monkeypatch):
    """The add-in, on the same state as v, with a relay set up and every PUT on it recorded in
    siri.answered as (path, body)."""
    from multiclaude.addons import siri
    from multiclaude.router import relay_entrance
    monkeypatch.setattr(relay_entrance, "settings", lambda: RELAY)
    siri.answered = []
    monkeypatch.setattr(relay_entrance, "put", lambda url, token, body: siri.answered.append(
        (url.removeprefix("https://relay.test"), json.loads(body))))
    return siri


# --- keys --------------------------------------------------------------------------------------

def owner_of(v, key):
    return v.owner(v.digest(key))


def test_a_key_is_kept_only_as_its_hash(v):
    key = v.new_key("alice")

    assert owner_of(v, key) == "alice"
    assert key not in v.KEYS.read_text()


def test_a_new_key_replaces_the_old_one(v):
    old = v.new_key("alice")
    bob = v.new_key("bob")
    new = v.new_key("alice")

    assert (owner_of(v, old), owner_of(v, new), owner_of(v, bob)) == (None, "alice", "bob")


def test_revoke_forgets_the_key(v):
    key = v.new_key("alice")

    assert v.revoke("alice")
    assert owner_of(v, key) is None
    assert not v.revoke("alice")


def test_no_key_belongs_to_nobody(v):
    v.new_key("alice")

    assert v.owner("") is None
    assert v.owner(None) is None
    assert owner_of(v, "guess") is None


# --- mailbox -----------------------------------------------------------------------------------

def test_a_reply_reaches_the_waiting_request(v):
    request_id = "0badcafe"
    v.open_request(request_id, "alice", "640")
    threading.Timer(0.05, v.post_reply, (request_id, "Two meetings tomorrow.")).start()

    assert v.await_reply(request_id, timeout=2) == "Two meetings tomorrow."


def test_posting_a_reply_returns_the_request(v):
    v.open_request("0badcafe", "alice", "640")

    assert v.post_reply("0badcafe", "ok")["msg_id"] == "640"
    assert v.post_reply("feedbeef", "late") == {}


def test_waiting_gives_up_after_the_timeout(v):
    v.open_request("0badcafe", "alice", "640")

    assert v.await_reply("0badcafe", timeout=0.1) is None


@pytest.mark.parametrize("bad", ["../../etc/x", "ABCDEF12", "1234567", ""])
def test_a_malformed_request_id_is_refused(v, bad):
    with pytest.raises(ValueError):
        v.post_reply(bad, "text")


# --- Siri's requests from the relay ---------------------------------------------------------------

class Assistant:
    """Stands in for hear(): records the words and answers the way a quick assistant would."""

    def __init__(self, v, answer="It's sunny.", set_up=True):
        self.v, self.answer, self.set_up, self.heard = v, answer, set_up, []

    def __call__(self, user, words, request_id, said):
        self.heard.append((user.name if user else None, words, request_id, said))
        if user:
            self.v.open_request(request_id, user.name, "640")
            if self.answer:
                self.v.post_reply(request_id, self.answer)
        return bool(user)


def spoken(v, key, text="Weather tomorrow?", request_id="0badcafe"):
    """The payload of the voice envelope the relay sends for these words."""
    return {"id": request_id, "key_hash": v.digest(key) if key else "", "text": text, "said": 1791499800.0}


def handled(siri, assistant, payload, monkeypatch):
    """What the add-in put on the relay for a voice envelope, with assistant hearing the words."""
    monkeypatch.setattr(siri, "hear", assistant)
    users = {"alice": SimpleNamespace(name="alice")} if assistant.set_up else {}
    siri.handle(SimpleNamespace(users=users, background=lambda what, fn, *args: fn(*args)), payload)
    return siri.answered


def test_spoken_words_reach_the_key_owner_and_the_answer_goes_back_to_the_relay(v, siri, monkeypatch):
    assistant = Assistant(v)

    answered = handled(siri, assistant, spoken(v, v.new_key("alice")), monkeypatch)

    assert answered == [("/voice/0badcafe", {"reply": "It's sunny."})]
    assert assistant.heard == [("alice", "Weather tomorrow?", "0badcafe", 1791499800.0)]


@pytest.mark.parametrize("key", [None, "wrong"])
def test_an_unknown_key_is_turned_away(v, siri, key, monkeypatch):
    v.new_key("alice")
    assistant = Assistant(v)

    assert handled(siri, assistant, spoken(v, key), monkeypatch) == [("/voice/0badcafe", {"error": "unknown voice key"})]
    assert assistant.heard == []


def test_someone_no_longer_set_up_is_told_so(v, siri, monkeypatch):
    answered = handled(siri, Assistant(v, set_up=False), spoken(v, v.new_key("alice")), monkeypatch)

    assert answered == [("/voice/0badcafe", {"reply": siri.not_set_up_reply()})]


@pytest.mark.parametrize("bad", ["../panel", "NOTHEX00", ""])
def test_a_request_with_a_bad_id_is_dropped(v, siri, bad, monkeypatch):
    assistant = Assistant(v)

    assert handled(siri, assistant, spoken(v, v.new_key("alice"), request_id=bad), monkeypatch) == []
    assert assistant.heard == []


def test_an_answer_too_slow_for_siri_is_left_in_the_chat(v, siri, monkeypatch):
    monkeypatch.setattr(siri, "WAIT_FOR_REPLY", 0.1)

    assert handled(siri, Assistant(v, answer=None), spoken(v, v.new_key("alice")), monkeypatch) == []


def test_an_answer_siri_stopped_waiting_for_breaks_nothing(v, siri, monkeypatch):
    from multiclaude.router import relay_entrance

    def gone(url, token, body):
        raise urllib.error.HTTPError(url, relay_entrance.GONE, "Gone", {}, io.BytesIO())

    monkeypatch.setattr(relay_entrance, "put", gone)
    handled(siri, Assistant(v), spoken(v, v.new_key("alice")), monkeypatch)


# --- the router ------------------------------------------------------------------------------------

@pytest.fixture
def siri_on(r, addons_on, monkeypatch):
    """The siri add-in on for the router's modules, answering no faster than at once."""
    addons_on("siri")
    from multiclaude.addons import siri
    monkeypatch.setattr(siri, "WAIT_FOR_REPLY", 0)
    return siri


def test_words_spoken_to_siri_come_from_the_relay_shown_in_the_chat_and_queued(r, desk, siri_on):
    key_hash = siri_on.voice.digest(siri_on.voice.new_key("alice"))
    desk.relay_inbound({"type": "voice", "payload": {
        "id": "0badcafe", "key_hash": key_hash, "text": "what's on tomorrow?", "said": 1791499800}})

    (item,) = desk.users["alice"].queue
    method, params = r.api_calls[-1]
    assert (method, params["chat_id"], params["disable_notification"]) == ("sendRichMessage", ALICE_CHAT, True)
    assert item.header == (f"[Siri from Alice · voice 0badcafe · msg {item.msg_id} · 2026-10-08 22:50 UTC] I said this "
                           "to Siri; it is my own request to you. Answer it with tg voice 0badcafe:")
    assert item.body == "what's on tomorrow?"
    assert desk.users["alice"].turn.busy


def test_siri_words_for_someone_not_set_up_go_nowhere(r, desk, siri_on):
    assert siri_on.hear(None, "hello", "0badcafe", 1791499800) is False
    assert r.api_calls == []


def test_help_names_siri_for_someone_with_a_voice_key(r, desk, siri_on):
    siri_on.voice.new_key("alice")
    desk.inbound(channel("/help"))
    desk.inbound(channel("/help", chat=BOB_CHAT, msg_id="6"))

    assert "You can also reach me through Siri." in sent(r, ALICE_CHAT)[0]
    assert "reach me through" not in sent(r, BOB_CHAT)[0]


def test_the_bots_description_names_siri(r, desk, siri_on):
    desk.describe_bot()
    line = next(p["short_description"] for m, p in r.api_calls if m == "setMyShortDescription")
    assert line == "Claude. Claude on Alice's VM, on Telegram and Siri."


def test_while_siri_is_off_words_spoken_to_it_go_nowhere(r, desk, addons_on):
    from multiclaude.addons.siri import voice
    key_hash = voice.digest(voice.new_key("alice"))
    addons_on()
    desk.relay_inbound({"type": "voice", "payload": {
        "id": "0badcafe", "key_hash": key_hash, "text": "what's on tomorrow?", "said": 1791499800}})
    desk.inbound(channel("/help"))

    assert [kind(w) for w in desk.users["alice"].queue] == ["help"]
    assert "Siri" not in sent(r, ALICE_CHAT)[0]


def kind(work):
    return "help" if work.header.startswith("[Router · ") else work.header


# --- tg voice ---------------------------------------------------------------------------------------

@pytest.fixture
def tg_voice(tg):
    """tg, with the siri add-in on in its copy of the repo."""
    (tg.root / "config.json").write_text(json.dumps({"addons": ["siri"]}))
    return tg


def rich(call):
    return json.loads(call[1]["rich_message"])


def test_voice_leaves_the_answer_for_siri_and_posts_it_under_the_question(tg_voice):
    request = tg_voice.root / "state" / "voice" / "1a2b3c4d.json"
    request.parent.mkdir(parents=True)
    request.write_text(json.dumps({"user": "alice", "msg_id": "640"}))

    tg_voice("voice", "1a2b3c4d", "Two", "meetings.")

    assert (tg_voice.root / "state" / "voice" / "1a2b3c4d.reply").read_text() == "Two meetings."
    call = tg_voice.api.calls[-1]
    assert rich(call)["markdown"] == "🔊 Two meetings."
    assert json.loads(call[1]["reply_parameters"])["message_id"] == 640


def test_a_late_voice_answer_is_still_posted(tg_voice):
    tg_voice("voice", "0badcafe", "Done.")

    assert rich(tg_voice.api.calls[-1])["markdown"] == "🔊 Done."
    assert not (tg_voice.root / "state" / "voice" / "0badcafe.reply").exists()


def test_voice_refuses_a_malformed_request_id(tg_voice):
    r = tg_voice("voice", "../x", "hi", check=False)

    assert r.returncode == 1 and "not a voice request id" in r.stderr
    assert tg_voice.api.calls == []




def test_tg_lists_voice_while_siri_is_on(tg_voice):
    assert "tg voice ID TEXT" in tg_voice("--help", check=False).stdout
