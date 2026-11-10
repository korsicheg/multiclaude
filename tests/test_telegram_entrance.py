"""Tests for the router polling Telegram itself ("frontdesk": "router"): who gets in
(lib/multiclaude/access.py), Telegram's updates as the messages the patched plugin would have
passed on (router/updates.py, router/rich.py), and the polling (router/telegram_entrance.py).
The Bot API is a recorder; nothing here reaches Telegram."""
import calendar
import json
import time
from pathlib import Path

import pytest

from conftest import ALICE_CHAT, BOB_CHAT, RICH_MESSAGES, channel, enable_addons, read_json, write_json

WHEN = calendar.timegm((2026, 10, 8, 18, 53, 12))  # as conftest.channel's ts
STRANGER = "555"


class Polled:
    """The Bot API as the entrance's own client sees it: getUpdates answers with each batch in turn."""

    def __init__(self, *batches):
        self.batches, self.calls, self.fail = list(batches), [], {}

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in self.fail:
            raise self.fail[method]
        if method == "getUpdates":
            return self.batches.pop(0) if self.batches else []
        return {"getMe": {"username": "ada_bot"}, "getFile": {"file_path": "photos/file_7.jpg"},
                "getCustomEmojiStickers": [{"custom_emoji_id": "777", "emoji": "🦊"}]}.get(method, True)

    def download(self, file_path, dest, timeout=300):
        Path(dest).write_bytes(b"a jpeg")

    def asked(self, method):
        return [params for called, params in self.calls if called == method]


def allow(r, *senders, policy="allowlist", **more):
    write_json(r.access.FILE, {"dmPolicy": policy, "allowFrom": list(senders), "groups": {}, "pending": {}, **more})


def entrance(r, *updates):
    """An entrance whose next getUpdates brings these updates; .told is what the owner was told."""
    told = []
    found = r.entrance.TelegramEntrance(told.append)
    found.api, found.told = Polled([{"update_id": 100 + n, **update} for n, update in enumerate(updates)]), told
    return found


def message(text=None, sender=ALICE_CHAT, msg_id=5, kind="private", chat=None, **more):
    """A Bot API Message from a person, in their private chat unless told otherwise."""
    return {"message_id": msg_id, "date": WHEN, "chat": {"id": int(chat or sender), "type": kind},
            "from": {"id": int(sender), "username": "someone"}, **({"text": text} if text else {}), **more}


def taken(r, *updates):
    """The messages the entrance queues for the router's loop from these updates."""
    found = entrance(r, *updates)
    found.poll()
    return found.waiting()


def replies(r):
    """The plain messages the entrance itself sent, as (chat, text)."""
    return [(p["chat_id"], p["text"]) for m, p in r.api_calls if m == "sendMessage"]


# --- who gets in ---------------------------------------------------------------------------------

def test_a_paired_persons_message_comes_out_as_the_plugin_passed_it_on(r):
    allow(r, ALICE_CHAT)

    (msg,) = taken(r, {"message": message("hello <there> & \"you\"")})

    assert msg == r.messages.parse(channel("hello <there> & \"you\""))
    assert replies(r) == []


def test_a_stranger_gets_a_pairing_code_then_one_reminder_then_silence(r):
    allow(r, ALICE_CHAT, policy="pairing")

    assert taken(r, *[{"message": message("hi", sender=STRANGER)}] * 3) == []

    (code, entry), = read_json(r.access.FILE)["pending"].items()
    assert len(code) == 6 and (entry["senderId"], entry["chatId"], entry["replies"]) == (STRANGER, STRANGER, 2)
    assert entry["expiresAt"] - entry["createdAt"] == 3_600_000
    assert replies(r) == [(int(STRANGER), f"Pairing required. The owner of the machine runs:\n\nassistant pair {code}"),
                          (int(STRANGER), f"Still pending. The owner of the machine runs:\n\nassistant pair {code}")]
    assert read_json(r.access.FILE)["allowFrom"] == [ALICE_CHAT]  # a message never changes who is allowed


def test_only_three_pairing_codes_wait_at_once(r):
    allow(r, policy="pairing")

    taken(r, *({"message": message("hi", sender=str(600 + n))} for n in range(4)))

    assert len(read_json(r.access.FILE)["pending"]) == 3 and len(replies(r)) == 3


def test_once_pairing_is_closed_a_stranger_gets_no_answer_at_all(r):
    allow(r, ALICE_CHAT)
    before = r.access.FILE.read_text()

    assert taken(r, {"message": message("hi", sender=STRANGER)}, {"message": message("/start", sender=STRANGER)}) == []

    assert replies(r) == [] and r.access.FILE.read_text() == before


def test_a_pairing_code_expires_after_an_hour(r):
    allow(r, policy="pairing", pending={"abc123": {"senderId": STRANGER, "chatId": STRANGER, "replies": 2,
                                                   "createdAt": 0, "expiresAt": (time.time() - 1) * 1000}})

    taken(r, {"message": message("hi again", sender=STRANGER)})

    (code,) = read_json(r.access.FILE)["pending"]
    assert code != "abc123" and replies(r)[0][1].startswith("Pairing required.")


def test_an_access_file_that_cannot_be_read_is_moved_aside_and_lets_nobody_in(r):
    r.access.FILE.parent.mkdir(parents=True, exist_ok=True)
    r.access.FILE.write_text("{not json")

    access = r.access.read()

    assert (access["dmPolicy"], access["allowFrom"]) == ("pairing", [])
    assert [p.read_text() for p in r.access.FILE.parent.glob("access.json.corrupt-*")] == ["{not json"]


def test_a_group_is_heard_only_when_listed_and_as_it_asks(r):
    allow(r, ALICE_CHAT, groups={"-1001": {"requireMention": True, "allowFrom": []}, "-1002": {"requireMention": False,
                                                                                            "allowFrom": [BOB_CHAT]}})
    in_group = lambda text, chat, sender=ALICE_CHAT, **more: {"message": message(text, sender, kind="supergroup",  # noqa: E731
                                                                                 chat=chat, **more)}
    mention = {"entities": [{"type": "mention", "offset": 4, "length": 8}]}

    heard = taken(r, in_group("just chatting", "-1001"), in_group("hey @Ada_Bot, look", "-1001", **mention),
                  in_group("to the bot", "-1001", reply_to_message={"message_id": 3, "from": {"username": "ada_bot"}}),
                  in_group("not from Bob", "-1002"), in_group("from Bob", "-1002", sender=BOB_CHAT),
                  in_group("elsewhere", "-1003"))

    assert [(m.chat, m.sender, m.text) for m in heard] == [("-1001", ALICE_CHAT, "hey @Ada_Bot, look"),
                                                          ("-1001", ALICE_CHAT, "to the bot"),
                                                          ("-1002", BOB_CHAT, "from Bob")]


def test_the_owners_ack_reaction_goes_on_what_is_passed_on(r):
    allow(r, ALICE_CHAT, ackReaction="👀")

    taken(r, {"message": message("hello", msg_id=9)})

    assert ("setMessageReaction", {"chat_id": int(ALICE_CHAT), "message_id": 9,
                                   "reaction": [{"type": "emoji", "emoji": "👀"}]}) in r.api_calls


def test_someone_assistant_pair_approved_is_told_once(r):
    r.access.APPROVED.mkdir(parents=True)
    (r.access.APPROVED / STRANGER).write_text(STRANGER)
    found = entrance(r)

    found.tick(time.time())
    found.tick(time.time() + r.entrance.APPROVALS_EVERY)

    assert replies(r) == [(STRANGER, "Paired! Say hi to Claude.")] and list(r.access.APPROVED.iterdir()) == []


# --- the plugin's own commands -------------------------------------------------------------------

def test_start_and_status_are_answered_as_the_plugin_answered_them(r):
    allow(r, ALICE_CHAT, policy="pairing", pending={"abc123": {"senderId": STRANGER, "chatId": STRANGER, "replies": 1,
                                                               "createdAt": 0, "expiresAt": (time.time() + 60) * 1000}})

    heard = taken(r, {"message": message("/start")}, {"message": message("/status@ada_bot")},
                  {"message": message("/status", sender=STRANGER)}, {"message": message("/status", sender="777")},
                  {"message": message("/help", sender="777")})

    assert heard == []
    assert [text.splitlines()[0] for _, text in replies(r)] == [
        "This bot gives each person their own Claude Code assistant.", "Paired as @someone.",
        "Pending pairing. The owner of the machine runs:", "Not paired. Send me a message to get a pairing code.",
        "What you send here goes to your own assistant once you're paired."]


def test_a_paired_persons_help_and_another_bots_command_go_on_to_the_router(r):
    allow(r, ALICE_CHAT)

    heard = taken(r, {"message": message("/help")}, {"message": message("/start@other_bot now")})

    assert [m.text for m in heard] == ["/help", "/start@other_bot now"] and replies(r) == []


# --- what a message carries ----------------------------------------------------------------------

@pytest.mark.parametrize("fields, text, attrs", [
    ({"document": {"file_id": "DOC", "file_size": 1234, "mime_type": "application/pdf", "file_name": "bill[1];x.pdf"}},
     "(document: bill_1__x.pdf)", {"attachment_kind": "document", "attachment_file_id": "DOC", "attachment_size": "1234",
                                   "attachment_mime": "application/pdf", "attachment_name": "bill_1__x.pdf"}),
    ({"document": {"file_id": "DOC"}, "caption": "the June bill"}, "the June bill",
     {"attachment_kind": "document", "attachment_file_id": "DOC"}),
    ({"voice": {"file_id": "V", "file_size": 900, "mime_type": "audio/ogg"}}, "(voice message)",
     {"attachment_kind": "voice", "attachment_file_id": "V", "attachment_size": "900", "attachment_mime": "audio/ogg"}),
    ({"audio": {"file_id": "A", "title": "Song", "file_name": "song.mp3"}}, "(audio: Song)",
     {"attachment_kind": "audio", "attachment_file_id": "A", "attachment_name": "song.mp3"}),
    ({"video": {"file_id": "VID", "mime_type": "video/mp4"}}, "(video)",
     {"attachment_kind": "video", "attachment_file_id": "VID", "attachment_mime": "video/mp4"}),
    ({"video_note": {"file_id": "VN", "file_size": 5}}, "(video note)",
     {"attachment_kind": "video_note", "attachment_file_id": "VN", "attachment_size": "5"}),
    ({"sticker": {"file_id": "S", "emoji": "🫤", "file_size": 7}}, "(sticker 🫤)",
     {"attachment_kind": "sticker", "attachment_file_id": "S", "attachment_size": "7"}),
])
def test_a_file_comes_with_what_the_assistant_needs_to_fetch_it(r, fields, text, attrs):
    allow(r, ALICE_CHAT)

    (msg,) = taken(r, {"message": message(**fields)})

    assert msg == r.messages.parse(channel(text, **attrs))
    assert "file_id: " + attrs["attachment_file_id"] in r.messages.body(msg)


def test_a_photo_is_fetched_for_the_assistant_to_look_at(r):
    allow(r, ALICE_CHAT)
    photo = {"photo": [{"file_id": "SMALL", "file_unique_id": "u1"}, {"file_id": "LARGE", "file_unique_id": "u2"}]}
    found = entrance(r, {"message": message(**photo)}, {"message": message(caption="look", msg_id=6, **photo)})

    found.poll()
    first, second = found.waiting()

    assert found.api.asked("getFile") == [{"file_id": "LARGE"}] * 2
    path = Path(first.attrs["image_path"])
    assert path.parent == r.entrance.INBOX and path.name.endswith("-u2.jpg") and path.read_bytes() == b"a jpeg"
    assert (first.text, second.text) == ("(photo)", "look") and f"photo: {path}" in r.messages.body(first)


def test_a_photo_that_cannot_be_fetched_still_arrives_as_a_message(r):
    allow(r, ALICE_CHAT)
    found = entrance(r, {"message": message(photo=[{"file_id": "P", "file_unique_id": "u"}])})
    found.api.fail["getFile"] = r.telegram.TelegramError("getFile: file is too big")

    found.poll()

    (msg,) = found.waiting()
    assert msg.text == "(photo)" and "image_path" not in msg.attrs


def test_a_reply_carries_the_message_it_answers_and_the_part_quoted(r):
    allow(r, ALICE_CHAT)
    rich_reply = {"message_id": 3, "rich_message": {"blocks": [{"type": "heading", "text": "Plan"},
                                                               {"type": "paragraph", "text": ["Buy ", {"type": "bold",
                                                                                                       "text": "milk"}]}]}}

    plain, rich = taken(r, {"message": message("the second", reply_to_message={"message_id": 2, "text": "one or two?"},
                                               quote={"text": "two"})},
                        {"message": message("ok", msg_id=6, reply_to_message=rich_reply)})

    assert plain == r.messages.parse(channel("the second", reply_to_message_id="2", reply_to_text="one or two?",
                                             reply_quote="two"))
    assert (rich.attrs["reply_to_message_id"], rich.attrs["reply_to_text"]) == ("3", "Plan\nBuy milk")


@pytest.mark.parametrize("blocks, markdown", RICH_MESSAGES)
def test_a_formatted_message_arrives_as_the_markdown_the_plugin_patch_makes_of_it(r, blocks, markdown):
    allow(r, ALICE_CHAT)

    (msg,) = taken(r, {"message": message(rich_message={"blocks": blocks})})

    assert msg.text == markdown


def test_what_nothing_passes_on_is_dropped_without_a_word(r):
    allow(r, policy="pairing")

    assert taken(r, {"message": message(sender=STRANGER, contact={"phone_number": "1"})},
                 {"message": message(sender=STRANGER, location={"latitude": 1.5, "longitude": 2.5})},
                 {"edited_message": message("fixed a typo", sender=STRANGER)}, {"my_chat_member": {}}) == []

    assert replies(r) == [] and read_json(r.access.FILE)["pending"] == {}


# --- taps and reactions --------------------------------------------------------------------------

def tap(data="ask:1", sender=ALICE_CHAT, kind="private"):
    keyboard = {"inline_keyboard": [[{"text": "Tea", "callback_data": "ask:0"}], [{"text": "Coffee", "callback_data": "ask:1"}]]}
    return {"callback_query": {"id": "cb-1", "from": {"id": int(sender), "username": "someone"}, "data": data,
                               "message": {"message_id": 40, "chat": {"id": int(sender), "type": kind},
                                           "reply_markup": keyboard}}}


def test_a_tap_on_an_ask_button_comes_with_the_buttons_label(r):
    allow(r, ALICE_CHAT)

    (msg,) = taken(r, tap())

    assert (msg.chat, msg.sender, msg.msg_id, msg.text, msg.tap) == (ALICE_CHAT, ALICE_CHAT, "40", "Coffee", "cb-1")
    assert msg.attrs["callback_data"] == "ask:1"
    assert ("answerCallbackQuery", {"callback_query_id": "cb-1"}) in r.api_calls


def test_other_taps_and_strangers_taps_are_only_answered(r):
    allow(r, ALICE_CHAT)

    assert taken(r, tap(data="perm:allow:abcde"), tap(sender=STRANGER), tap(kind="supergroup")) == []

    assert [m for m, _ in r.api_calls if m != "setMyCommands"] == ["answerCallbackQuery"] * 3  # so the button stops spinning


def reaction(new, old=(), sender=ALICE_CHAT):
    def kinds(emoji):
        return [{"type": "custom_emoji", "custom_emoji_id": e} if e.isdigit() else {"type": "emoji", "emoji": e} for e in emoji]

    return {"message_reaction": {"chat": {"id": int(sender), "type": "private"}, "message_id": 31, "date": WHEN,
                                 "user": {"id": int(sender), "username": "someone"},
                                 "old_reaction": kinds(old), "new_reaction": kinds(new)}}


def test_a_reaction_comes_as_the_emoji_added_on_the_message_reacted_to(r):
    allow(r, ALICE_CHAT)

    added, custom = taken(r, reaction(["👍", "🔥"], old=["👍"]), reaction(["777", "888"]))

    assert added == r.messages.parse(channel("🔥", msg_id="31", reaction="🔥"))
    assert custom.text == "🦊 (custom emoji)"  # a Premium custom emoji as the emoji its sticker stands for
    assert r.messages.typed("Alice", added)[0].endswith("I reacted with 🔥.")


def test_taking_a_reaction_off_and_a_strangers_reaction_say_nothing(r):
    allow(r, ALICE_CHAT)

    assert taken(r, reaction([], old=["👍"]), reaction(["👍"], sender=STRANGER)) == []


# --- the locations add-in ------------------------------------------------------------------------

def test_a_shared_location_arrives_and_is_kept_while_the_locations_add_in_is_on(r):
    allow(r, ALICE_CHAT)
    enable_addons(r.paths.ROOT, "locations")
    live = {"latitude": 52.52, "longitude": 13.4, "horizontal_accuracy": 14.6, "live_period": 900}
    place = {"location": {"latitude": 1.5, "longitude": 2.5}, "venue": {"title": "Cafe", "address": "Main St 1"}}

    shared, at = taken(r, {"message": message(location=live)}, {"message": message(msg_id=6, **place)},
                       {"edited_message": message(location={**live, "latitude": 52.53}, edit_date=WHEN + 60)})

    assert shared == r.messages.parse(channel("(live location)", location_lat="52.52", location_lon="13.4",
                                              location_accuracy_m="15", location_live_period="900"))
    assert at.text == "(place: Cafe, Main St 1)"
    assert "location: 52.52, 13.4 (±15 m, live for 15 min" in r.messages.body(shared)
    kept = read_json(r.paths.STATE / "locations" / f"{ALICE_CHAT}.json")
    assert kept == {"latitude": 52.53, "longitude": 13.4, "accuracy_m": 14.6, "message_id": 5, "live_until": WHEN + 900,
                    "updated": WHEN + 60}  # the live update only changed the file


# --- polling -------------------------------------------------------------------------------------

def test_each_poll_asks_from_where_the_last_one_ended_and_a_restart_carries_on_there(r):
    allow(r, ALICE_CHAT)
    found = entrance(r, {"message": message("one")}, {"message": message("two", msg_id=6)})

    found.poll()
    found.poll()

    first, second = found.api.asked("getUpdates")
    assert (first["offset"], first["timeout"], second["offset"]) == (None, r.entrance.LONG_POLL, 102)
    assert "message_reaction" in first["allowed_updates"]  # Telegram sends reactions only to a poller that asks
    assert read_json(r.entrance.STATE)["offset"] == 102
    assert r.entrance.TelegramEntrance().offset == 102 and [m.text for m in found.waiting()] == ["one", "two"]
    assert ("setMyCommands", {"commands": r.entrance.STRANGER_COMMANDS, "scope": {"type": "all_private_chats"}}) in r.api_calls


def test_one_update_that_fails_does_not_cost_the_rest_or_come_again(r, monkeypatch):
    allow(r, ALICE_CHAT)
    found = entrance(r, {"message": {"message_id": 1, "from": {"id": int(ALICE_CHAT)}, "text": "no chat"}},
                     {"message": message("fine")})
    monkeypatch.setattr(r.entrance.updates, "inbound", lambda msg, text, attrs: 1 / 0 if text == "no chat" else (msg, text))

    found.poll()

    assert [text for _, text in found.waiting()] == ["fine"] and found.offset == 102


def test_after_a_long_outage_what_telegram_kept_is_skipped_rather_than_delivered_late(r):
    allow(r, ALICE_CHAT)
    write_json(r.entrance.STATE, {"offset": 50, "saved": time.time() - r.entrance.RESUME_WITHIN - 1})
    found = r.entrance.TelegramEntrance()
    found.api = Polled([{"update_id": 99, "message": message("hours ago")}], [{"update_id": 100, "message": message("now")}])

    found.poll()

    skip, poll = found.api.asked("getUpdates")
    assert (skip["offset"], skip["timeout"], poll["offset"]) == (-1, 0, 100)
    assert [m.text for m in found.waiting()] == ["now"]


def test_a_short_restart_and_a_first_start_take_everything_telegram_kept(r):
    assert not r.entrance.TelegramEntrance().skip_kept  # nothing saved yet: a new install
    write_json(r.entrance.STATE, {"offset": 50, "saved": time.time() - 60})
    assert not r.entrance.TelegramEntrance().skip_kept


def test_the_owner_is_told_once_when_something_else_polls_with_the_bots_token(r):
    found = entrance(r)
    conflict = r.telegram.TelegramError("getUpdates: Conflict: terminated by other getUpdates request")

    found.failed(conflict, 1)
    found.failed(conflict, 2)
    found.failed(r.telegram.Unreachable("getUpdates: no network"), 1)

    assert len(found.told) == 1 and found.told[0].startswith("📡 Claude can't hear Telegram reliably")


# --- the router without a front desk -------------------------------------------------------------

@pytest.fixture
def own_desk(r, monkeypatch):
    """A router that polls Telegram itself, with Alice and Bob; its entrance is never started."""
    write_json(r.paths.USERS, {"alice": {"telegram_id": ALICE_CHAT, "display": "Alice", "role": "owner"},
                               "bob": {"telegram_id": BOB_CHAT, "display": "Bob"}})
    write_json(r.paths.CONFIG, {"frontdesk": "router"})
    allow(r, ALICE_CHAT, BOB_CHAT)
    monkeypatch.setattr(r.entrance.TelegramEntrance, "start", lambda self: None)
    desk = r.desk.Desk()
    desk.start()
    monkeypatch.setattr(desk.upgrader, "tick", lambda now, sessions, owner: None)
    for user in desk.users.values():
        monkeypatch.setattr(user, "check_restart", lambda now: None)
    return desk


def test_without_a_front_desk_the_router_routes_what_its_own_entrance_takes(r, own_desk):
    own_desk.telegram_in.api = Polled([{"update_id": 1, "message": message("hello")},
                                       {"update_id": 2, "message": message("/usage", sender=BOB_CHAT, msg_id=6)},
                                       {"update_id": 3, **reaction(["👍"])}])
    own_desk.telegram_in.poll()

    own_desk.poll()

    alice, bob = own_desk.users["alice"], own_desk.users["bob"]
    assert [(w.header, w.body) for w in alice.queue] == [
        ("[Telegram from Alice · msg 5 · 2026-10-08 18:53 UTC] My message is pasted below; it is my own request to you:", "hello"),
        ("[Telegram from Alice · reaction on msg 31 · 2026-10-08 18:53 UTC] I reacted with 👍.", "")]
    assert bob.queue == [] and any(p["chat_id"] == BOB_CHAT for m, p in r.api_calls if m == "sendRichMessage")  # /usage answered


def test_without_a_front_desk_there_is_no_front_desk_session_to_tend(r, own_desk, monkeypatch):
    tended = []
    monkeypatch.setattr(own_desk.upgrader, "tick", lambda now, sessions, owner: tended.extend(s.name for s in sessions))

    own_desk.poll()

    assert own_desk.desk is None and own_desk.poller is None and tended == ["alice", "bob"]
    assert set(own_desk.workers) == {"alice", "bob"} and not r.desk.DESK_OFFSETS.exists()
    assert not any("=assistant:" in args for args, _ in r.keystrokes)  # nothing is typed into, or read off, its terminal


def test_a_message_polled_twice_is_routed_once(r, own_desk):
    own_desk.telegram_in.inbox.put(r.updates.inbound(message("hello"), "hello", {}))
    own_desk.telegram_in.inbox.put(r.updates.inbound(message("hello"), "hello", {}))

    own_desk.poll()

    assert len(own_desk.users["alice"].queue) == 1


def test_with_a_front_desk_nothing_polls(r, desk):
    assert desk.desk is not None and desk.telegram_in is None and desk.poller is not None


def test_the_python_and_the_plugin_patch_agree_on_what_a_formatted_message_reads_as(r):
    for blocks, markdown in RICH_MESSAGES:
        assert r.rich.markdown(blocks) == markdown
    assert r.rich.markdown(json.loads(json.dumps([{"type": "divider"}, {"type": "pullquote", "text": "Big"}]))) == "---\n\n> Big"
