"""The command /mcp, with Claude Code's syntax.

MCP servers are never shared. A server often carries a login (OAuth, a token), and everyone's
assistant runs as the same Linux user, so each person keeps their own servers at Claude Code's
local scope: ~/.claude.json, under their workspace. Claude Code keys stored OAuth tokens by
server name, type, URL and headers, so URL servers get an X-Assistant-User header naming the
person; two people adding the same server then get separate logins. `/mcp login` needs a
terminal for the pasted redirect URL, so the router runs it itself."""
import json
import re
import shlex
from typing import NamedTuple

from multiclaude import bot, claude_cli, jsonfile, paths
from multiclaude.sharing.reply import Reply, status

MARKER = "X-Assistant-User"
STATUS_RE = re.compile(r"^(?P<name>\S+): .* - (?P<status>[^\w\s].*)$")
LIST_TIMEOUT = 180
TIMEOUT = 120
HELP = """MCP servers, with Claude Code's syntax. They're yours only and never shared: each person connects their own accounts.

`/mcp`: your servers and their status
`/mcp get <name>`
`/mcp add <name> -- <command> [args]`
`/mcp add <name> -e KEY=value -- <command> [args]`
`/mcp add --transport http <name> <url> [-H "Header: value"]`
`/mcp add-json <name> <json>`
`/mcp remove <name>`
`/mcp login <name>`: sign in to a server that needs it
`/mcp logout <name>`
`/mcp enable|disable|reconnect <name>`"""


def command(p, text):
    """The reply to an /mcp command, with the in-session commands for the router to type."""
    body = phone_text(text).split(None, 1)
    sub, _, rest = (body[1] if len(body) > 1 else "").strip().partition(" ")
    sub, rest = sub.lower(), rest.strip()
    name = rest.split()[0] if rest else ""
    if sub in ("", "list"):
        return Reply("\n".join(server_list(p) + ["", "`/mcp help`: all commands"]), session=[])
    if sub == "help":
        return Reply(HELP, session=[])
    if sub == "get":
        return Reply(server_details(p, name) if name else "Usage: `/mcp get <name>`", session=[])
    p.ws.mkdir(parents=True, exist_ok=True)
    if sub in ("add", "add-json"):
        return add(p, sub, rest)
    if sub == "remove":
        return remove(p, name)
    if sub == "logout":
        if not name:
            return Reply("Usage: `/mcp logout <name>`", session=[])
        result = claude_cli.run(["mcp", "logout", name], p.ws)
        return Reply(status(result), session=[f"/mcp reconnect {name}"] if result.ok else [])
    if sub in ("enable", "disable", "reconnect"):
        if not name:
            return Reply(f"Usage: `/mcp {sub} <name>`" + (" (or all)" if sub == "reconnect" else ""), session=[])
        # /reload-plugins first, so a server added outside /mcp (by the assistant) is known.
        return Reply(session=(["/reload-plugins"] if sub != "disable" else []) + [f"/mcp {sub} {name}"])
    return Reply(HELP, session=[])


def phone_text(text):
    """Undo what phone keyboards do to commands: smart quotes, and -- turned into a dash."""
    text = text.translate(str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'"}))
    return re.sub(r"(^|\s)[—–]", r"\1--", text)


def local_servers(p):
    """{name: config} of this person's own servers."""
    return project_config(p).get("mcpServers") or {}


def turned_off(p):
    return set(project_config(p).get("disabledMcpServers") or [])


def project_config(p):
    return (jsonfile.read_json(paths.CLAUDE_JSON, {}).get("projects") or {}).get(str(p.ws)) or {}


class Server(NamedTuple):
    name: str
    kind: str    # "stdio", "http" or "sse"; "" for one loaded from elsewhere (a plugin's)
    symbol: str  # from `claude mcp list`: ✔, ✗, ⚠…, ⏸ when turned off, ? when it wasn't listed
    state: str   # its words: "Connected", "Failed to connect"…

    @property
    def own(self):
        """Whether it's in the person's folder, rather than a plugin's."""
        return bool(self.kind)

    @property
    def health(self):
        """ok, auth (needs a sign-in), off, failed or unknown."""
        words = self.state.lower()
        if self.symbol in ("✔", "✓"):
            return "ok"
        if "auth" in words:
            return "auth"
        if self.symbol == "⏸" or "disabled" in words:
            return "off"
        if self.symbol in ("✗", "✘") or "fail" in words:
            return "failed"
        return "unknown"


def servers(p):
    """The person's servers with their health from `claude mcp list`: their own first, by name,
    then those loaded from elsewhere."""
    local, off = local_servers(p), turned_off(p)
    result = claude_cli.run(["mcp", "list"], p.ws, timeout=LIST_TIMEOUT)
    health = {m["name"]: m["status"] for m in map(STATUS_RE.match, result.output.splitlines()) if m}

    def server(name, kind):
        symbol, _, state = health.get(name, "⏸ disabled" if name in off else "? not checked").partition(" ")
        return Server(name, kind, symbol, state)

    return ([server(n, local[n].get("type") or "stdio") for n in sorted(local)]
            + [server(n, "") for n in health if n not in local])


def server_list(p):
    found = servers(p)

    def row(s):
        return f"{s.symbol} {s.name}" + (f" ({s.kind})" if s.own else "") + f": {s.state}"

    lines = ["🔌 MCP servers"] + [row(s) for s in found if s.own]
    others = [row(s) for s in found if not s.own]
    if others:
        lines += ["", "Also loaded (not in your folder):"] + others
    if len(lines) == 1:
        lines.append("None yet.")
    return lines


def server_details(p, name):
    """`claude mcp get`, with secrets hidden: a chat is no place for tokens. The names of env
    vars and headers stay; their values go."""
    result = claude_cli.run(["mcp", "get", name], p.ws, timeout=TIMEOUT)
    if result.returncode == -1:
        return f"⚠️ {result.message}"
    text = result.output.strip()
    text = re.sub(r"(?m)^(\s{4}[A-Za-z_][A-Za-z0-9_]*=).+$", r"\1•••", text)
    text = re.sub(r"(?m)^(\s{4}(?!" + MARKER + r")[A-Za-z0-9-]+: ).+$", r"\1•••", text)
    text = re.sub(r"(https?://[^\s?]+)\?\S+", r"\1?•••", text)
    text = re.sub(r"(?<![/\w-])[A-Za-z0-9_-]{32,}", "•••", text)  # API keys passed as arguments
    return re.sub(r"\n*To remove this server, run:.*", "", text)


def add(p, sub, rest):
    before, off = local_servers(p), turned_off(p)
    args, problem = add_args(p, rest) if sub == "add" else add_json_args(p, rest)
    if problem:
        return Reply(problem, session=[])
    result = claude_cli.run(args, p.ws, timeout=TIMEOUT)
    new = sorted(set(local_servers(p)) - set(before))
    if result.returncode != 0 or not new:
        return Reply("⚠️ " + ("\n".join(result.lines[-2:]) if result.lines else f"claude mcp {sub} failed"), session=[])
    name = new[0]
    reply, sign_in = f"✅ Added {name} to your folder.", []
    if (local_servers(p)[name].get("type") or "stdio") in ("http", "sse"):
        sign_in = [f"/mcp login {name}"]
        reply += f"\nIf it needs you to sign in: `{sign_in[0]}`"
    return Reply(reply, session=["/reload-plugins"] + ([f"/mcp enable {name}"] if name in off else [])
                 + [f"/mcp reconnect {name}"], copy=sign_in)


def add_args(p, rest):
    """`claude mcp add` arguments for what the person typed: always local scope, http when
    the target is a URL, and the per-person header on URL servers. (args, problem)."""
    try:
        typed = shlex.split(rest)
    except ValueError as e:
        return None, f"⚠️ Couldn't read that: {e}"
    if len(typed) < 2:
        return None, "Usage:\n`/mcp add <name> -- <command> [args]`\n`/mcp add --transport http <name> <url>`"
    out, transport, i = [], None, 0
    while i < len(typed):
        a = typed[i]
        if a in ("-s", "--scope"):
            i += 2
            continue
        if a.startswith("--scope="):
            i += 1
            continue
        if a == "--client-secret":
            return None, f"--client-secret asks for the secret at a prompt. Ask {bot.name()} to add this one."
        if a in ("-t", "--transport") and i + 1 < len(typed):
            transport = typed[i + 1].lower()
        elif a.startswith("--transport="):
            transport = a.partition("=")[2].lower()
        out.append(a)
        i += 1
    if "--" not in out and transport is None and any(re.match(r"https?://", a) for a in out):
        transport = "http"
        out = ["--transport", "http", *out]
    head = ["-H", f"{MARKER}: {p.name}"] if transport in ("http", "sse") else []
    # A variadic option like -H takes values up to the next option, so --scope closes it.
    return ["mcp", "add", *head, "--scope", "local", *out], None


def add_json_args(p, rest):
    name, _, raw = rest.partition(" ")
    try:
        config = json.loads(raw)
    except ValueError as e:
        return None, f"Usage: `/mcp add-json <name> <json>`\n⚠️ That JSON doesn't parse: {e}"
    if not name or not isinstance(config, dict):
        return None, "Usage: `/mcp add-json <name> <json>`"
    if config.get("type") in ("http", "sse"):
        config["headers"] = {MARKER: p.name, **(config.get("headers") or {})}
    return ["mcp", "add-json", "--scope", "local", name, json.dumps(config)], None


def remove(p, name):
    if not name:
        return Reply("Usage: `/mcp remove <name>`", session=[])
    if name not in local_servers(p):
        return Reply(f"{name} isn't in your folder, so there's nothing to remove. To stop using it: `/mcp disable {name}`",
                     session=[], copy=[f"/mcp disable {name}"])
    result = claude_cli.run(["mcp", "remove", name, "--scope", "local"], p.ws)
    if not result.ok:
        return Reply(f"⚠️ {result.message}", session=[])
    # The running session keeps a removed server until it disconnects it; /mcp add enables the name again.
    return Reply(f"✅ Removed {name}.", session=[f"/mcp disable {name}"])
