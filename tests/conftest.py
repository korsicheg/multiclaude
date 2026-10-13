"""Fixtures shared by the tests.

Nothing here may reach the live system: every path points into tmp_path, the
`claude` CLI is a fake that records its calls, and tmux and the Bot API are
replaced by recorders.
"""
import hashlib
import hmac
import html
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
ALICE_CHAT, BOB_CHAT = "100", "101"

FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Records each call in $FAKE_CLAUDE_DIR/calls.jsonl and answers from rules.json:
the first rule whose "args" is a prefix of argv wins. A rule may print stdout,
exit with rc, and merge JSON into files ("merge": {path: data})."""
import json, os, select, sys, time
from pathlib import Path

home = Path(os.environ["FAKE_CLAUDE_DIR"])
args = sys.argv[1:]
stdin = sys.stdin.read() if select.select([sys.stdin], [], [], 1)[0] else ""
with open(home / "calls.jsonl", "a") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd(), "stdin": stdin}) + "\n")


def merge(into, data):
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            merge(into[k], v)
        else:
            into[k] = v


rules = json.loads((home / "rules.json").read_text()) if (home / "rules.json").exists() else []
for rule in rules:
    if args[:len(rule["args"])] == rule["args"]:
        for path, data in (rule.get("merge") or {}).items():
            p = Path(path)
            current = json.loads(p.read_text()) if p.exists() else {}
            merge(current, data)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(current))
        time.sleep(rule.get("sleep", 0))
        sys.stdout.write(rule.get("stdout", ""))
        sys.exit(rule.get("rc", 0))
'''


class FakeClaude:
    """The fake `claude` CLI: add rules before running, read calls after."""

    def __init__(self, home):
        self.home = home
        self.bin = home / "bin"
        self.bin.mkdir(parents=True)
        exe = self.bin / "claude"
        exe.write_text(FAKE_CLAUDE)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self.path = str(exe)
        self.rules = []

    def on(self, args, stdout="", rc=0, merge=None, sleep=0):
        self.rules.append({"args": args, "stdout": stdout, "rc": rc, "merge": merge or {}, "sleep": sleep})
        (self.home / "rules.json").write_text(json.dumps(self.rules))

    @property
    def calls(self):
        log = self.home / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def argvs(self):
        return [c["args"] for c in self.calls]


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def read_json(path):
    return json.loads(Path(path).read_text())


def enable_addons(root, *names):
    """Turn these add-ins on, and the others off, in the config.json at root, as the owner lists them."""
    path = Path(root) / "config.json"
    config = read_json(path) if path.exists() else {}
    write_json(path, {**config, "addons": list(names)})


def nowhere():
    """A Bot API address where nothing listens."""
    sock = socket.create_server(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def fake_claude(tmp_path):
    return FakeClaude(tmp_path / "fake-claude")


class Box:
    """The temp-dir layout the sharing tests run in."""

    def __init__(self, tmp_path, fake_claude):
        self.root = tmp_path / "assistant"
        self.workspaces = tmp_path / "workspaces"
        self.config = tmp_path / "dot-claude"
        self.claude_json = tmp_path / "dot-claude.json"
        self.home = tmp_path / "home"
        self.fake = fake_claude
        self.home.mkdir()
        self.root.mkdir()
        self.workspaces.mkdir()
        self.config.mkdir()

    def env(self):
        return {**os.environ, "HOME": str(self.home),
                "ASSISTANT_ROOT": str(self.root), "ASSISTANT_WORKSPACES": str(self.workspaces),
                "CLAUDE_CONFIG_DIR": str(self.config), "CLAUDE_JSON": str(self.claude_json),
                "SHARED_NO_NOTIFY": "1", "FAKE_CLAUDE_DIR": str(self.fake.home),
                "PATH": f"{self.fake.bin}:{os.environ['PATH']}"}

    def users(self, *names):
        write_json(self.root / "users.json",
                   {n: {"telegram_id": str(100 + i), "display": n.title()} for i, n in enumerate(names)})
        for n in names:
            (self.workspaces / n).mkdir(exist_ok=True)

    def settings(self, name, data, scope="project"):
        write_json(self.ws_settings(name, scope), data)

    def ws_settings(self, name, scope="project"):
        return self.workspaces / name / ".claude" / ("settings.json" if scope == "project" else "settings.local.json")

    def skill(self, base, name, text="---\nname: x\n---\nbody\n"):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(text)
        return d

    def person_skills(self, name):
        return self.workspaces / name / ".claude" / "skills"

    def manifest(self):
        path = self.root / "state" / "shared.json"
        return read_json(path) if path.exists() else {}

    def shared(self, *args):
        """Run bin/shared.py the way cron and the router do; (exit code, stdout, stderr)."""
        r = subprocess.run([sys.executable, "-I", str(BIN / "shared.py"), *args], env=self.env(),
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def command(self, name, text):
        rc, out, err = self.shared("command", name, text)
        assert rc == 0, err
        return json.loads(out)


@pytest.fixture
def box(tmp_path, fake_claude):
    return Box(tmp_path, fake_claude)


def fresh_multiclaude(monkeypatch):
    """Forget any imported multiclaude modules, so the next import reads the environment again."""
    monkeypatch.syspath_prepend(str(REPO / "lib"))
    for name in [m for m in sys.modules if m == "multiclaude" or m.startswith("multiclaude.")]:
        del sys.modules[name]


@pytest.fixture
def shared(box, monkeypatch):
    """The sharing package imported in-process, configured for the box (paths come from the env)."""
    for key, value in box.env().items():
        monkeypatch.setenv(key, value)
    fresh_multiclaude(monkeypatch)
    import multiclaude.sharing.people
    import multiclaude.sharing.skillfiles
    import multiclaude.sharing.sync
    return multiclaude.sharing


class FakeBotApi:
    """A local stand-in for api.telegram.org that records every call.

    calls holds (method, fields, files) for each POST; files maps a field to
    (filename, bytes). fail maps a method to the error it answers with: a
    description, or a dict of the error reply's fields. getFile answers with
    a path whose download returns FILE_BYTES, and getMe with ME."""

    FILE_BYTES = b"attachment bytes"
    ME = {"id": 42, "is_bot": True, "first_name": "Ada", "username": "ada_bot"}  # the bot, for getMe

    def __init__(self):
        import email.parser
        import http.server
        import threading
        import urllib.parse

        self.calls, self.next_id, self.fail = [], 500, {}
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                me = self.path.endswith("/getMe")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "result": api.ME}).encode() if me else api.FILE_BYTES)

            def do_POST(self):
                method = self.path.rsplit("/", 1)[1]
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                ctype = self.headers.get("Content-Type", "")
                fields, files = {}, {}
                if ctype.startswith("multipart/"):
                    msg = email.parser.BytesParser().parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
                    for part in msg.get_payload():
                        name = part.get_param("name", header="content-disposition")
                        data = part.get_payload(decode=True)
                        if part.get_filename():
                            files[name] = (part.get_filename(), data)
                        else:
                            fields[name] = data.decode()
                elif ctype.startswith("application/json"):
                    fields = {k: v if isinstance(v, str) else json.dumps(v) for k, v in json.loads(body).items()}
                else:
                    fields = dict(urllib.parse.parse_qsl(body.decode()))
                api.calls.append((method, fields, files))
                if method in api.fail:
                    failure = api.fail[method]
                    reply = {"ok": False, **(failure if isinstance(failure, dict) else {"description": failure})}
                elif method == "getFile":
                    reply = {"ok": True, "result": {"file_path": "documents/report.pdf"}}
                else:
                    api.next_id += 1
                    reply = {"ok": True, "result": {"message_id": api.next_id} if method.startswith("send") else True}
                out = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def methods(self):
        return [m for m, _, _ in self.calls]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def bot_api():
    api = FakeBotApi()
    yield api
    api.close()


def fresh_multiclaude_in(tmp_path, monkeypatch):
    """Point every multiclaude location into tmp_path/home, and forget the imported modules."""
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {"HOME": home, "ASSISTANT_ROOT": home / "assistant", "ASSISTANT_WORKSPACES": home / "workspaces",
                       "CLAUDE_CONFIG_DIR": home / ".claude", "CLAUDE_JSON": home / ".claude.json"}.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv("TELEGRAM_STATE_DIR", raising=False)
    fresh_multiclaude(monkeypatch)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A fresh import of the multiclaude library with every location in tmp_path."""
    fresh_multiclaude_in(tmp_path, monkeypatch)
    import multiclaude
    return multiclaude


# --- the Telegram plugin patcher (test_patch_telegram_plugin.py) ---

HANDLER = """bot.on('callback_query:data', async ctx => {
  const data = ctx.callbackQuery.data
  const m = /^perm:(allow|deny):(.+)$/.exec(data)
  if (!m) {
    await ctx.answerCallbackQuery().catch(() => {})
    return
  }
  // permission handling
})
"""
INBOUND = """async function handleInbound(
  ctx: Context,
) {
  mcp.notification({
    params: {
      meta: {
        user_id: String(from.id),
        ts: new Date((ctx.message?.date ?? 0) * 1000).toISOString(),
        ...(imagePath ? { image_path: imagePath } : {}),
      },
    },
  })
}

type AttachmentMeta = {
  kind: string
}
"""
TEXT = """bot.on('message:text', async ctx => {
  await handleInbound(ctx, ctx.message.text, undefined)
})
"""
HELP = """bot.command('help', async ctx => {
  if (!dmCommandGate(ctx)) return
  await ctx.reply('pairing help')
})
"""

TAKEOVER = """mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 })
try {
  const stale = parseInt(readFileSync(PID_FILE, 'utf8'), 10)
  if (stale > 1 && stale !== process.pid) {
    process.kill(stale, 'SIGTERM')
  }
} catch {}
writeFileSync(PID_FILE, String(process.pid))
"""
POLL_LOOP = """void (async () => {
  for (let attempt = 1; ; attempt++) {
    await bot.start()
  }
})()
"""

PLUGIN = TAKEOVER + HANDLER + INBOUND + TEXT + HELP + POLL_LOOP


def plugin_version(home, version, code=PLUGIN):
    """A cached version of the Telegram plugin, as Claude Code installs it; its server.ts."""
    d = home / ".claude" / "plugins" / "cache" / "claude-plugins-official" / "telegram" / version
    d.mkdir(parents=True)
    (d / "server.ts").write_text(code)
    return d / "server.ts"


def patch(home):
    """Run bin/patch-telegram-plugin for that $HOME, with the bot in $HOME/assistant; (exit code, what it printed)."""
    r = subprocess.run([sys.executable, "-I", str(BIN / "patch-telegram-plugin")],
                       env={**os.environ, "HOME": str(home), "ASSISTANT_ROOT": str(home / "assistant")},
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout + r.stderr


# --- tg (test_tg.py) ---

CHAT = ALICE_CHAT  # tg's chat, $TG_CHAT_ID


class Tg:
    """bin/tg, run from a copy of the repo in tmp_path against the fake Bot API: tg(*args) runs it."""

    def __init__(self, tmp_path, bot_api):
        self.root = tmp_path / "assistant"
        (self.root / "bin").mkdir(parents=True)
        shutil.copytree(BIN.parent / "lib", self.root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        for path in BIN.iterdir():  # tg finds state/ next to its own folder, so it runs from a copy
            if path.is_file():
                shutil.copy2(path, self.root / "bin" / path.name)
        self.channel = tmp_path / "channel"
        self.channel.mkdir()
        (self.channel / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
        self.api = bot_api

    def __call__(self, *args, stdin="", env=None, check=True):
        r = subprocess.run([str(self.root / "bin" / "tg"), *args], input=stdin, capture_output=True, text=True,
                           timeout=30, env={**os.environ, "TELEGRAM_STATE_DIR": str(self.channel),
                                            "TG_API_BASE": self.api.url, "TG_CHAT_ID": CHAT, **(env or {})})
        if check:
            assert r.returncode == 0, r.stderr
        return r

    def recorded(self, chat=CHAT):
        path = self.root / "state" / "messages" / chat
        return [int(line.split()[0]) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def tg(tmp_path, bot_api):
    return Tg(tmp_path, bot_api)


# --- the router (test_router.py, test_panel.py) ---

@pytest.fixture
def r(tmp_path, monkeypatch):
    """The router's modules with every location in tmp_path, and tmux and the Bot API recorded."""
    fresh_multiclaude_in(tmp_path, monkeypatch)
    from multiclaude import paths, telegram, tmux
    from multiclaude.router import (about, asks, claude_ui, desk, inbound, limits, plugins, poller, recovery, session,
                                    switching, upgrade, usage, work)
    r = SimpleNamespace(paths=paths, telegram=telegram, tmux=tmux, about=about, asks=asks, claude_ui=claude_ui, desk=desk,
                        messages=inbound, limits=limits, plugins=plugins, poller=poller, recovery=recovery, session=session,
                        switching=switching, upgrade=upgrade, usage=usage, work=work,
                        api_calls=[], keystrokes=[], unreachable=False)

    class RecordingBot:
        """The router's BotApi: records each call; with r.unreachable set, Telegram can't be reached."""

        def call(self, method, files=None, **params):
            if r.unreachable:
                raise telegram.Unreachable(f"{method}: no network")
            r.api_calls.append((method, params))
            return {"message_id": 900 + len(r.api_calls)} if method.startswith("send") else True

    def run_tmux(*args, stdin=None):
        r.keystrokes.append((args, stdin))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    bot = RecordingBot()
    monkeypatch.setattr(telegram, "bot", lambda: bot)
    monkeypatch.setattr(telegram, "later", lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(desk, "in_background", lambda fn, *a: fn(*a))
    monkeypatch.setattr(work.Worker, "start", lambda self: None)  # tests drive the queues themselves
    monkeypatch.setattr(recovery.Network, "start", lambda self: None)  # tests set what it finds
    monkeypatch.setattr(tmux, "run", run_tmux)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(switching, "SETTINGS_LOCK_WAIT", 0.2)
    return r


@pytest.fixture
def desk(r):
    write_json(r.paths.USERS, {"alice": {"telegram_id": ALICE_CHAT, "display": "Alice", "role": "owner"},
                                    "bob": {"telegram_id": BOB_CHAT, "display": "Bob"}})
    d = r.desk.Desk()
    d.load_users()
    return d


@pytest.fixture
def addons_on(r):
    """addons_on("voice-notes", …) turns those add-ins on for the router's modules (and the rest off)."""
    return lambda *names: enable_addons(r.paths.ROOT, *names)


def sent(r, chat=None):
    """The texts of the rich messages the router sent, to chat or to anyone."""
    return [shown(p) for m, p in r.api_calls if m == "sendRichMessage" and (chat is None or p["chat_id"] == chat)]


def shown(params):
    """The text of a rich message, with its code spans back in backticks."""
    rich = params["rich_message"]
    if "markdown" in rich:
        return rich["markdown"]
    return "\n\n".join(block_text(b) for b in rich["blocks"])


def block_text(block):
    text = block["text"]
    return text if isinstance(text, str) else "".join(t if isinstance(t, str) else f"`{t['text']}`" for t in text)


def append(path, *entries):
    """Add entries to a transcript, as Claude Code writes them."""
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def channel(text, chat=ALICE_CHAT, msg_id="5", **attrs):
    """A front desk transcript entry carrying a Telegram message, as the plugin writes it."""
    attrs = {"chat_id": chat, "message_id": msg_id, "user": "someone", "user_id": chat,
             "ts": "2026-10-08T18:53:12.000Z", **attrs}
    head = " ".join(f'{k}="{html.escape(str(v))}"' for k, v in attrs.items())
    return {"type": "queue-operation", "operation": "enqueue",
            "content": f'<channel source="plugin:telegram:telegram" {head}>\n{html.escape(text)}\n</channel>'}


def transcript(user):
    """The file a person's assistant's transcript entries go to (append)."""
    Path(user.tail.dir).mkdir(parents=True, exist_ok=True)
    return Path(user.tail.dir) / "t.jsonl"


# --- Slack (tests/addons/test_slack.py) ---

ALICE_SLACK, ALICE_DM = "U0ALICE", "D0ALICE"
RELAY = {"relay_url": "wss://relay.test/queue", "relay_token": "vm-token"}


@pytest.fixture
def slack_desk(r, addons_on, monkeypatch):
    """A desk with the slack add-in on, whose people include Alice on Slack, with the relay set up
    and the Slack Web API recorded."""
    addons_on("slack")
    from multiclaude.addons.slack import api
    from multiclaude.router import relay_entrance
    write_json(r.paths.USERS, {"alice": {"telegram_id": ALICE_CHAT, "display": "Alice", "role": "owner",
                                             "slack_id": ALICE_SLACK},
                                    "bob": {"telegram_id": BOB_CHAT, "display": "Bob"}})
    write_json(api.TOKENS, {"bot_token": "xoxb-1"})
    monkeypatch.setattr(relay_entrance, "settings", lambda: RELAY)
    r.slack_calls = []

    class RecordingSlack:
        def call(self, method, **params):
            r.slack_calls.append((method, params))
            if method == "users.info":
                return {"user": {"real_name": "Stranger Danger"}}
            return {"ts": f"{len(r.slack_calls)}.0", "channel": {"id": "D0NEW"}}

    monkeypatch.setattr(api, "bot", lambda: RecordingSlack())
    d = r.desk.Desk()
    d.load_users()
    return d


def slack_dm(text, user=ALICE_SLACK, ts="1791529806.264069", **extra):
    return {"type": "events_api", "envelope_id": "e", "payload": {"event": {
        "type": "message", "channel_type": "im", "channel": ALICE_DM if user == ALICE_SLACK else "D0NEW",
        "user": user, "ts": ts, "text": text, **extra}}}


def slack_posts(r):
    return [(p["channel"], p.get("markdown_text") or p.get("text")) for m, p in r.slack_calls if m == "chat.postMessage"]


# --- the control panel (test_panel.py, and the add-ins' settings) ---

TOKEN = "123:bot-token"
CRONTAB = """\
# backups: not anyone's job
*/10 * * * * /usr/local/bin/backup
# alice: morning brief at 09:00 Helsinki. Runs at 06 and 07 UTC (see ~/workspaces/alice/jobs.md)
0 6,7 * * * [ "$(TZ=Europe/Helsinki date +\\%H)" = 09 ] && assistant job --user alice "brief"
# bob: her own job
0 8 * * * assistant job --user bob "hers"
# alice: invoice sync, Sundays 10:00 Helsinki: file new invoices (see ~/workspaces/alice/jobs.md)
0 7,8 * * 0 [ "$(TZ=Europe/Helsinki date +\\%H)" = 10 ] && assistant job --user alice "invoices"
"""


def signed(user_id, token=TOKEN, auth_date=None, **extra):
    """Init data as Telegram signs it for a Mini App."""
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAH",
              "user": json.dumps({"id": int(user_id), "first_name": "Alice"}), "signature": "sig", **extra}
    checked = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, checked.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


@pytest.fixture
def panel(r, desk, monkeypatch):
    """The desk's control panel, with a bot token, a crontab in memory, and its pushes recorded."""
    from multiclaude import cronjobs
    from multiclaude import users as users_module
    from multiclaude.router import panel as panel_module
    from multiclaude.router import relay_entrance
    from multiclaude.sharing import mcp
    r.panel_module, r.cronjobs, r.crontab, r.pushed = panel_module, cronjobs, [CRONTAB], []
    r.users_module = users_module
    monkeypatch.setattr(r.telegram, "bot_token", lambda: TOKEN)
    monkeypatch.setattr(cronjobs, "read_crontab", lambda: r.crontab[-1])
    monkeypatch.setattr(cronjobs, "write_crontab", r.crontab.append)
    monkeypatch.setattr(relay_entrance, "settings", lambda: RELAY)
    monkeypatch.setattr(relay_entrance, "put", lambda url, token, body: r.pushed.append((url, token, json.loads(body))))
    monkeypatch.setattr(r.usage, "plan_limits", lambda: ([("5-hour", 12.0, 1_791_540_000.0)], 30.0))
    monkeypatch.setattr(mcp, "servers", lambda p: [mcp.Server("google", "stdio", "✔", "Connected"),
                                                    mcp.Server("maps", "http", "⚠", "Needs authentication")])
    return desk.panel


def tap(panel, action, chat=ALICE_CHAT, **args):
    panel.handle({"init_data": signed(chat), "id": f"t-{action}", "action": action, "args": args})


def last_snapshot(r):
    return r.pushed[-1][2]


# --- the machine's health (test_health.py, test_assistant_cli.py) ---

class FakeProc:
    """A /proc of our own: the machine's files, and a folder for each process."""

    PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
    MB = 1024 * 1024

    def __init__(self, root):
        self.root = root
        root.mkdir()

    def machine(self, total_mb=8192, available_mb=4096, swap_mb=0, swap_free_mb=0, busy=0, idle=1000):
        (self.root / "stat").write_text(f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 {busy} 0 0 {idle} 0 0 0 0 0 0\n")
        (self.root / "meminfo").write_text(f"MemTotal: {total_mb * 1024} kB\nMemFree: 1000 kB\n"
                                           f"MemAvailable: {available_mb * 1024} kB\nSwapTotal: {swap_mb * 1024} kB\n"
                                           f"SwapFree: {swap_free_mb * 1024} kB\n")
        (self.root / "loadavg").write_text("0.52 0.40 0.35 2/300 4000\n")
        (self.root / "uptime").write_text("47663.03 274092.79\n")

    def process(self, pid, name, mb, *args, ppid=1, cwd=None, pss_mb=None):
        """A process holding mb resident; with pss_mb, the kernel tells its proportional share too."""
        folder = self.root / str(pid)
        folder.mkdir()
        (folder / "stat").write_text(f"{pid} ({name}) S {ppid} {pid} {pid} 0 -1 4194304 0 0\n")
        (folder / "statm").write_text(f"9999 {round(mb * self.MB / self.PAGE_SIZE)} 100 5 0 123 0\n")
        (folder / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in args))
        if cwd:
            os.symlink(cwd, folder / "cwd")
        if pss_mb is not None:
            (folder / "smaps_rollup").write_text(f"00400000-7fff00000000 ---p 00000000 00:00 0  [rollup]\n"
                                                 f"Rss: {mb * 1024} kB\nPss: {round(pss_mb * 1024)} kB\n")
