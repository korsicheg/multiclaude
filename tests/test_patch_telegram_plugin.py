"""Tests for bin/patch-telegram-plugin, run against a fake plugin cache."""
import json
import subprocess
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

from conftest import BIN, HANDLER, PLUGIN, enable_addons, patch, plugin_version


def original(home, version):
    return home / "assistant" / "state" / "telegram-plugin" / f"{version}-server.ts"


def test_patches_the_handler_once_and_keeps_the_original(tmp_path):
    server = plugin_version(tmp_path, "0.0.7")

    code, output = patch(tmp_path)
    assert code == 0 and output == "telegram plugin 0.0.7: patched for tg ask buttons, replies, reactions, rich messages, /help, single poller\n"
    patched = server.read_text()
    assert patched.count("multiclaude: tg ask buttons") == 1
    assert "/^ask:\\d+$/.test(data)" in patched
    assert "  // permission handling\n})\n" in patched
    assert original(tmp_path, "0.0.7").read_text() == PLUGIN

    assert patch(tmp_path) == (0, "")  # nothing changed, nothing written
    assert server.read_text() == patched


def test_a_plugin_update_of_the_same_version_is_kept_as_its_new_original(tmp_path):
    server = plugin_version(tmp_path, "0.0.7")
    patch(tmp_path)
    updated = PLUGIN + "// a fix upstream\n"
    server.write_text(updated)

    assert patch(tmp_path)[0] == 0
    assert original(tmp_path, "0.0.7").read_text() == updated
    assert server.read_text().endswith("// a fix upstream\n") and "multiclaude: replies" in server.read_text()


def test_a_patched_version_whose_original_was_lost_is_left_alone(tmp_path):
    server = plugin_version(tmp_path, "0.0.7")
    patch(tmp_path)
    patched = server.read_text()
    original(tmp_path, "0.0.7").unlink()

    code, output = patch(tmp_path)

    assert code == 1 and "its original isn't in" in output
    assert server.read_text() == patched


def test_an_add_ins_patch_comes_and_goes_with_the_add_in(tmp_path):
    server = plugin_version(tmp_path, "0.0.7")
    patch(tmp_path)
    core_only = server.read_text()
    assert "multiclaude: locations" not in core_only

    enable_addons(tmp_path / "assistant", "locations")
    assert patch(tmp_path)[0] == 0
    assert "multiclaude: locations" in server.read_text()
    assert f'const MULTICLAUDE_LOCATIONS_DIR = "{tmp_path}/assistant/state/locations"' in server.read_text()

    enable_addons(tmp_path / "assistant")
    assert patch(tmp_path)[0] == 0
    assert server.read_text() == core_only


def test_a_version_that_no_longer_matches_does_not_stop_the_others(tmp_path):
    plugin_version(tmp_path, "0.0.6", "something else entirely")
    newer = plugin_version(tmp_path, "0.0.7")

    code, output = patch(tmp_path)

    assert code == 1
    assert "0.0.6: code for 'multiclaude: tg ask buttons' not found" in output
    assert "multiclaude: tg ask buttons" in newer.read_text()


def test_replies_carry_what_they_reply_to(tmp_path):
    server = plugin_version(tmp_path, "0.0.7")

    assert patch(tmp_path)[0] == 0
    patched = server.read_text()
    assert patched.count("multiclaude: replies") == 2  # the helper and the meta
    assert patched.index("function multiclaudePlainText") < patched.index("async function handleInbound(")
    meta = patched[patched.index("ts: new Date((ctx.message"):]
    assert "reply_to_message_id: String(ctx.message.reply_to_message.message_id)" in meta
    assert "reply_quote: ctx.message.quote.text" in meta


def test_one_patch_that_no_longer_fits_leaves_the_others_in(tmp_path):
    server = plugin_version(tmp_path, "0.0.8", HANDLER)  # no inbound handler to patch

    code, output = patch(tmp_path)

    assert code == 1 and "code for 'multiclaude: replies' not found" in output
    assert "multiclaude: tg ask buttons" in server.read_text()


def test_reactions_are_asked_for_and_reach_claude(tmp_path):
    server = plugin_version(tmp_path, "0.0.7")

    assert patch(tmp_path)[0] == 0
    patched = server.read_text()
    assert "import { API_CONSTANTS } from 'grammy'" in patched
    assert "allowed_updates: [...API_CONSTANTS.DEFAULT_UPDATE_TYPES, 'message_reaction']," in patched
    handler = patched[patched.index("bot.on('message_reaction', async ctx => {"):]
    assert handler.index("loadAccess().allowFrom.includes(String(from.id))") < handler.index("mcp.notification(")
    assert "message_id: String(reaction.message_id)," in handler and "reaction: added," in handler


def test_a_paired_senders_help_goes_on_to_claude(tmp_path):
    server = plugin_version(tmp_path, "0.0.7")

    assert patch(tmp_path)[0] == 0
    patched = server.read_text()
    assert "bot.command('help', async (ctx, next) => {" in patched
    assert patched.index("return next()") < patched.index("await ctx.reply('pairing help')")


def test_rich_messages_reach_claude(tmp_path):
    server = plugin_version(tmp_path, "0.0.7")

    assert patch(tmp_path)[0] == 0
    patched = server.read_text()
    assert patched.count("multiclaude: rich messages") == 2  # the handler and the Markdown
    handler = patched[patched.index("bot.on('message', async (ctx, next) => {"):]
    assert patched.index("bot.on('message:text'") < patched.index("bot.on('message', async (ctx, next)")
    assert handler.index("if (!rich) return next()") < handler.index("multiclaudeRichMarkdown(rich.blocks)")


def patcher():
    loader = SourceFileLoader("patch_telegram_plugin", str(BIN / "patch-telegram-plugin"))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def run_ts(tmp_path, code, *args):
    """What TypeScript code prints, run on Node."""
    script = tmp_path / "patch.ts"
    script.write_text(code)
    r = subprocess.run(["node", "--disable-warning=ExperimentalWarning", str(script), *args],
                       capture_output=True, text=True, timeout=30, check=True)
    return r.stdout


def rich_markdown(tmp_path, blocks):
    """What the patch's TypeScript makes of a rich message's blocks."""
    return run_ts(tmp_path, patcher().RICH_MARKDOWN + "process.stdout.write(multiclaudeRichMarkdown(JSON.parse(process.argv[2])))\n",
                  json.dumps(blocks))


def test_a_rich_message_keeps_its_headings_lists_and_inline_formatting(tmp_path):
    blocks = [
        {"type": "paragraph", "text": "Set up the CLI."},
        {"type": "paragraph", "text": ""},
        {"type": "heading", "text": "Rules", "size": 2},
        {"type": "list", "items": [
            {"label": "•", "blocks": [{"type": "paragraph", "text": [
                "Run ", {"type": "code", "text": "$CLI --version"}, ", see ",
                {"type": "url", "text": "the docs", "url": "https://example.com/docs"}, " or ",
                {"type": "url", "text": "https://example.com", "url": "https://example.com"}, "."]}]},
            {"label": "•", "blocks": [
                {"type": "paragraph", "text": "Nested:"},
                {"type": "list", "items": [{"label": "•", "blocks": [{"type": "paragraph", "text": "inner"}]}]}]}]},
        {"type": "list", "items": [{"label": "1.", "type": "1", "value": 1, "blocks": [
            {"type": "paragraph", "text": {"type": "bold", "text": "Find the CLI."}}]}]},
    ]

    assert rich_markdown(tmp_path, blocks) == (
        "Set up the CLI.\n\n## Rules\n\n"
        "- Run `$CLI --version`, see [the docs](https://example.com/docs) or https://example.com.\n"
        "- Nested:\n  - inner\n\n"
        "1. **Find the CLI.**")


def test_a_rich_message_keeps_tables_code_quotes_details_and_the_file_ids_of_its_media(tmp_path):
    blocks = [
        {"type": "table", "cells": [[{"text": "ID", "is_header": True}, {"text": "Name", "is_header": True}],
                                    [{"text": "gpt-sol"}, {"text": "a|b"}]]},
        {"type": "pre", "language": "bash", "text": "mycli models"},
        {"type": "blockquote", "blocks": [{"type": "paragraph", "text": "Quoted"}], "credit": "Alice"},
        {"type": "details", "summary": "More", "blocks": [{"type": "paragraph", "text": "Hidden"}]},
        {"type": "list", "items": [{"label": "•", "has_checkbox": True, "is_checked": True,
                                    "blocks": [{"type": "paragraph", "text": "done"}]}]},
        {"type": "document", "document": {"file_name": "bill.pdf", "file_id": "DOC"}, "caption": {"text": "June"}},
        {"type": "photo", "photo": [{"file_id": "SMALL"}, {"file_id": "LARGE"}]},
        {"type": "map", "location": {"latitude": 52.52, "longitude": 13.4}, "zoom": 15},
    ]

    assert rich_markdown(tmp_path, blocks) == (
        "| ID | Name |\n| --- | --- |\n| gpt-sol | a\\|b |\n\n"
        "```bash\nmycli models\n```\n\n"
        "> Quoted\n>\n> — Alice\n\n"
        "<details><summary>More</summary>\n\nHidden\n\n</details>\n\n"
        "- [x] done\n\n"
        "(document: bill.pdf · file_id: DOC)\n\nJune\n\n"
        "(photo · file_id: LARGE)\n\n"
        "(map: 52.52, 13.4)")


def test_only_a_channel_session_takes_over_and_polls(tmp_path):
    server = plugin_version(tmp_path, "0.0.7")

    assert patch(tmp_path)[0] == 0
    patched = server.read_text()
    assert patched.index("const MULTICLAUDE_POLLS = ") < patched.index("mkdirSync(STATE_DIR")
    assert "if (MULTICLAUDE_POLLS && stale > 1 && stale !== process.pid) {" in patched
    assert "if (MULTICLAUDE_POLLS) writeFileSync(PID_FILE, String(process.pid))" in patched
    loop = patched[patched.index("void (async () => {"):]
    assert loop.index("if (!MULTICLAUDE_POLLS) return") < loop.index("await bot.start(")


def started_as_channel(tmp_path, ps):
    """What the patch's TypeScript says of the server whose parent is pid 30, with ps as
    `ps -o ppid=,args=` for each pid; a pid not in it fails like ps on a dead process."""
    return run_ts(tmp_path, patcher().STARTED_AS_CHANNEL + """
const table = JSON.parse(process.argv[2])
process.stdout.write(String(multiclaudeStartedAsChannel(30, pid => {
  if (!(pid in table)) throw new Error('no such process')
  return table[pid]
})))
""", json.dumps(ps)) == "true"


BUN_RUN = "20 /home/x/.bun/bin/bun run --cwd /home/x/.claude/plugins/cache/claude-plugins-official/telegram/0.0.7 start"


def test_the_front_desk_polls(tmp_path):
    assert started_as_channel(tmp_path, {
        30: BUN_RUN, 20: "10 claude --channels plugin:telegram@claude-plugins-official --name assistant"})
    assert started_as_channel(tmp_path, {
        30: BUN_RUN, 20: "10 /home/x/.local/share/claude/versions/2.1.296 --channels=plugin:telegram@claude-plugins-official"})


def test_another_claude_session_only_gets_the_tools(tmp_path):
    assert not started_as_channel(tmp_path, {30: BUN_RUN, 20: "10 claude mcp list"})
    assert not started_as_channel(tmp_path, {30: BUN_RUN, 20: "10 node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js -p hi"})
    assert not started_as_channel(tmp_path, {  # the plugin named only in its settings, as user sessions are started
        30: BUN_RUN, 20: '10 claude --settings {"enabledPlugins":{"telegram@claude-plugins-official":false}}'})


def test_a_server_whose_client_cant_be_told_polls_as_the_plugin_always_did(tmp_path):
    assert started_as_channel(tmp_path, {30: BUN_RUN, 20: "1 some-other-mcp-client --serve"})
    assert started_as_channel(tmp_path, {30: BUN_RUN})  # its launcher gone, ps fails
    assert started_as_channel(tmp_path, {30: "1 /home/x/.bun/bin/bun run start"})  # reparented to init
