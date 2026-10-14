"""Contract tests for tg, the assistants' Telegram client, against a fake Bot API."""
import json

import pytest

from conftest import CHAT, nowhere


def rich(call):
    """The rich message a sendRichMessage or editMessageText call carried."""
    return json.loads(call[1]["rich_message"])


def test_send_text_prints_and_records_the_message_id(tg):
    out = tg("send", "hello **there**").stdout.split()

    assert tg.api.methods() == ["sendRichMessage"] and tg.api.calls[0][1]["chat_id"] == CHAT
    assert rich(tg.api.calls[0]) == {"markdown": "hello **there**"}
    assert out == ["501"] and tg.recorded() == [501]


def test_code_formatting_alone_adds_no_copy_button(tg):
    tg("send", "It's in `users.json`, set by `assistant notify`:\n\n```\nls\n```")

    assert "reply_markup" not in tg.api.calls[0][1]


def test_copy_adds_a_button_for_each_text_under_the_last_message(tg):
    key = "Bearer " + "k" * 60
    tg("send", "--copy", "/mcp login github", "--copy", key, "--copy", "/mcp login github", "Send this, then paste the key")

    keyboard = json.loads(tg.api.calls[0][1]["reply_markup"])
    assert keyboard == {"inline_keyboard": [
        [{"text": "📋 /mcp login github", "copy_text": {"text": "/mcp login github"}}],
        [{"text": f"📋 Bearer {'k' * 32}…", "copy_text": {"text": key}}]]}
    assert rich(tg.api.calls[0]) == {"markdown": "Send this, then paste the key"}


def test_copy_buttons_go_under_the_last_part_of_a_long_text(tg):
    tg("send", "--copy", "ls", stdin="line\n" * 8000)

    assert len(tg.api.calls) == 2
    assert "reply_markup" not in tg.api.calls[0][1] and "reply_markup" in tg.api.calls[1][1]


@pytest.mark.parametrize("copy, error", [
    (["x" * 257], "a button holds at most 256 characters"),
    (["a", "b", "c", "d", "e", "f", "g", "h", "i"], "at most 8 per message"),
    ([" "], "the text is empty"),
])
def test_copy_refuses_what_a_button_cannot_hold(tg, copy, error):
    r = tg("send", *[arg for text in copy for arg in ("--copy", text)], "hi", check=False)

    assert r.returncode == 1 and error in r.stderr and tg.api.calls == []


def test_send_reads_the_text_from_stdin(tg):
    tg("send", stdin="line 1\nline 2")
    assert rich(tg.api.calls[0])["markdown"] == "line 1\nline 2"


def test_empty_text_sends_nothing(tg):
    assert tg("send", stdin="  \n").stdout == "" and tg.api.calls == []


def test_long_text_is_split_into_several_messages(tg):
    out = tg("send", "a" * 32000 + "b" * 10).stdout.split()

    assert [rich(c)["markdown"] for c in tg.api.calls] == ["a" * 32000, "b" * 10]
    assert out == ["501", "502"]


def test_a_long_text_is_cut_between_lines(tg):
    lines = [f"- **item {i}**" for i in range(3000)]
    tg("send", "\n".join(lines))

    texts = [rich(c)["markdown"] for c in tg.api.calls]
    assert len(texts) > 1 and all(t.startswith("- **") and t.rstrip("\n").endswith("**") for t in texts)


def test_long_greek_text_is_split_between_characters(tg):
    text = "αβγδ" * 8001
    tg("send", text, env={"LC_ALL": "C", "LANG": "C"})
    assert "".join(rich(c)["markdown"] for c in tg.api.calls) == text


def test_reply_to_applies_to_the_first_message_only(tg):
    tg("send", "--reply-to", "42", "x" * 32001)

    first, second = (f for _, f, _ in tg.api.calls)
    assert json.loads(first["reply_parameters"]) == {"message_id": 42, "allow_sending_without_reply": True}
    assert "reply_parameters" not in second


def test_files_go_below_the_text_in_the_same_message(tg, tmp_path):
    photo, doc = tmp_path / "shot.PNG", tmp_path / "report.pdf"
    photo.write_bytes(b"png")
    doc.write_bytes(b"pdf")

    out = tg("send", "--file", str(photo), "--file", str(doc), "the results").stdout.split()

    (method, _, files), = tg.api.calls
    assert method == "sendRichMessage" and out == ["501"]
    assert files == {"file0": ("shot.PNG", b"png"), "file1": ("report.pdf", b"pdf")}
    assert rich(tg.api.calls[0]) == {
        "markdown": "the results\n\n![](tg://photo?id=m0)\n\n![](tg://document?id=m1)",
        "media": [{"id": "m0", "media": {"type": "photo", "media": "attach://file0"}},
                  {"id": "m1", "media": {"type": "document", "media": "attach://file1"}}]}


def test_separate_files_follow_the_text_each_as_a_message_of_its_own(tg, tmp_path):
    photo, doc = tmp_path / "chart.png", tmp_path / "chart.pdf"
    photo.write_bytes(b"png")
    doc.write_bytes(b"pdf")

    out = tg("send", "--reply-to", "42", "--copy", "/config", "--file", str(photo), "--file", str(doc), "--separate",
             "the chart, two ways").stdout.split()

    (text, fields, _), (as_photo, photo_fields, photo_files), (as_doc, _, doc_files) = tg.api.calls
    assert (text, as_photo, as_doc) == ("sendRichMessage", "sendPhoto", "sendDocument")
    assert rich(tg.api.calls[0]) == {"markdown": "the chart, two ways"}
    assert "reply_parameters" in fields and "reply_markup" in fields
    assert "reply_parameters" not in photo_fields and "reply_markup" not in photo_fields
    assert (photo_files, doc_files) == ({"photo": ("chart.png", b"png")}, {"document": ("chart.pdf", b"pdf")})
    assert out == ["501", "502", "503"] and tg.recorded() == [501, 502, 503]


def test_sound_asks_for_a_notification_on_every_message_it_sends(tg, tmp_path):
    doc = tmp_path / "brief.pdf"
    doc.write_bytes(b"pdf")
    tg("send", "--sound", "--file", str(doc), "--separate", "Good morning")
    tg("send", "later")

    assert [fields.get("disable_notification") for _, fields, _ in tg.api.calls] == ["False", "False", None]


def test_separate_files_without_text_are_just_the_files(tg, tmp_path):
    doc = tmp_path / "chart.pdf"
    doc.write_bytes(b"pdf")

    tg("send", "--separate", "--reply-to", "42", "--file", str(doc))

    (method, fields, _), = tg.api.calls
    assert method == "sendDocument" and json.loads(fields["reply_parameters"])["message_id"] == 42


@pytest.mark.parametrize("text", ["@Alice here's the report", "<~/.env", "plain; type=text/html"])
def test_text_with_files_is_sent_as_written_never_read_as_a_file(tg, tmp_path, text):
    photo = tmp_path / "shot.png"
    photo.write_bytes(b"png")

    tg("send", "--file", str(photo), text)

    assert rich(tg.api.calls[0])["markdown"].startswith(f"{text}\n\n![](")


def test_a_missing_file_is_an_error(tg, tmp_path):
    r = tg("send", "--file", str(tmp_path / "nope.png"), "x", check=False)
    assert r.returncode == 1 and "no such file" in r.stderr
    assert tg.api.calls == []


def test_ask_sends_one_button_per_option_and_saves_the_question(tg):
    out = tg("ask", "Which one?", "Postgres", "SQLite").stdout.split()

    method, fields, _ = tg.api.calls[0]
    assert (method, rich(tg.api.calls[0])) == ("sendRichMessage", {"markdown": "Which one?"})
    assert json.loads(fields["reply_markup"]) == {"inline_keyboard": [
        [{"text": "Postgres", "callback_data": "ask:0"}], [{"text": "SQLite", "callback_data": "ask:1"}]]}
    saved = json.loads((tg.root / "state" / "asks" / CHAT / f"{out[0]}.json").read_text())
    assert saved == {"question": "Which one?", "options": ["Postgres", "SQLite"]}
    assert tg.recorded() == [int(out[0])]


def test_ask_takes_two_to_ten_options(tg):
    assert tg("ask", "Q?", "only one", check=False).returncode == 1
    assert tg("ask", "Q?", *[str(i) for i in range(11)], check=False).returncode == 1
    assert tg.api.calls == []


def test_send_edit_and_ask_turn_highlights_into_bold(tg):
    tg("send", "Mind ==this==")
    tg("edit", "7", "Mind ==that==")
    tg("ask", "Pick ==one==?", "A", "B")

    assert [rich(call)["markdown"] for call in tg.api.calls] == ["Mind **this**", "Mind **that**", "Pick **one**?"]


def test_edit_and_react(tg):
    tg("edit", "42", "new", "text")
    tg("react", "42", "👍")

    assert tg.api.calls[0][:2] == ("editMessageText", {"chat_id": CHAT, "message_id": "42",
                                                       "rich_message": json.dumps({"markdown": "new text"})})
    tg("edit", "43", "--copy", "ls", "run `ls`")
    assert json.loads(tg.api.calls[-1][1]["reply_markup"])["inline_keyboard"][0][0]["copy_text"] == {"text": "ls"}
    assert rich(tg.api.calls[-1]) == {"markdown": "run `ls`"}
    tg("edit", "43", "run `ls`")
    assert "reply_markup" not in tg.api.calls[-1][1]
    method, fields, _ = tg.api.calls[1]
    assert (method, fields["message_id"]) == ("setMessageReaction", "42")
    assert json.loads(fields["reaction"]) == [{"type": "emoji", "emoji": "👍"}]


def test_typing(tg):
    tg("typing")
    assert tg.api.calls == [("sendChatAction", {"chat_id": CHAT, "action": "typing"}, {})]


def test_download_saves_the_file_and_prints_its_path(tg, tmp_path):
    path = tg("download", "FILE1", str(tmp_path / "inbox")).stdout.strip()

    assert tg.api.calls[0] == ("getFile", {"file_id": "FILE1"}, {})
    assert path.startswith(str(tmp_path / "inbox") + "/") and path.endswith("-report.pdf")
    assert open(path, "rb").read() == tg.api.FILE_BYTES


def test_tg_has_no_voice_command_while_siri_is_off(tg):
    r = tg("voice", "1a2b3c4d", "hi", check=False)

    assert r.returncode == 2 and "tg voice" not in r.stdout
    assert tg.api.calls == []


def test_chat_flag_overrides_the_default_chat(tg):
    tg("--chat", "777", "send", "hi")
    assert tg.api.calls[0][1]["chat_id"] == "777"
    assert tg.recorded("777") == [501]


def test_an_api_error_is_reported(tg):
    tg.api.fail["sendRichMessage"] = "Bad Request: chat not found"
    r = tg("send", "hi", check=False)
    assert r.returncode == 1 and "chat not found" in r.stderr


def test_no_token_is_a_clear_error(tg):
    (tg.channel / ".env").unlink()
    r = tg("send", "hi", check=False)
    assert r.returncode == 1 and "no bot token" in r.stderr


def test_usage(tg):
    r = tg(check=False)
    assert r.returncode == 2 and "tg send" in r.stdout


def test_a_message_telegram_cant_be_reached_for_is_held_and_goes_out_first_next_time(tg, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")

    r = tg("send", "--file", str(shot), "while offline", env={"TG_API_BASE": nowhere(), "TG_CONNECT_WITHIN": "1"},
           check=False)
    shot.unlink()  # the outbox keeps its own copy

    assert r.returncode == 75 and r.stdout == ""
    assert "1 message is held and will go out by itself" in r.stderr and "Don't send it again" in r.stderr
    assert tg.api.calls == [] and tg.recorded() == []

    out = tg("send", "back online").stdout.split()

    (first, held, files), (_, second, _) = tg.api.calls
    assert first == "sendRichMessage" and rich((first, held))["markdown"].startswith("while offline")
    assert files == {"file0": ("shot.png", b"png")}
    assert rich(tg.api.calls[1]) == {"markdown": "back online"}
    assert out == ["502"] and tg.recorded() == [501, 502]
    assert not any((tg.root / "state" / "outbox" / CHAT).iterdir())
