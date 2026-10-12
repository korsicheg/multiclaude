"""The commands /plugin (/plugins) and /skills, with Claude Code's own syntax, and the way in
for /mcp (mcp.py).

The router runs `shared.py command <name> <text>`, sends back the reply's text, and once the
person's assistant is idle types /reload-plugins into it when the reply asks for a reload,
or the in-session commands the reply lists (for /mcp). Everything lands in the person's own
folder; a sync then shares what everyone has."""
import glob
import json
import os
import re
import traceback
from pathlib import Path

from multiclaude import bot, claude_cli, jsonfile, paths, users
from multiclaude.log import log
from multiclaude.sharing import mcp, skillfiles
from multiclaude.sharing.people import Person, load_person, skill_dirs, user_plugins
from multiclaude.sharing.reply import Reply, status
from multiclaude.sharing.sync import BUILTIN_MARKETPLACES, KINDS, MANIFEST, NEVER_SHARE, USER_SKILLS, Sync, drop_key, singular

DESCRIPTION_MAX = 90
DIRECTORY_SHOWN = 20
MARKETPLACE_SHOWN = 25
LISTING_TIMEOUT = 120
SKILL_LEVELS = ("on", "off", "name-only", "user-invocable-only")
SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
HELP = """Plugins, marketplaces and skills, with Claude Code's syntax. What you add goes into your own folder. Once everyone has the same one, it's shared with everyone.

`/plugin`: what you have
`/plugin install <plugin>@<marketplace>`
`/plugin install <marketplace>`: browse its plugins
`/plugin install <plugin> --marketplace <source>`
`/plugin directory [word]`: search every marketplace
`/plugin enable|disable|uninstall <plugin>`
`/plugin marketplace`: your marketplaces
`/plugin marketplace add <owner/repo, URL or path>`
`/plugin marketplace remove|update <name>`
`/skills`: your skills
`/skills off|on <name>`"""


def run(name, text):
    """The reply to a /plugin, /plugins, /skills or /mcp command from the person called name."""
    try:
        return dispatch(name, text)
    except jsonfile.NotAnObject as e:
        return Reply(f"⚠️ {e.path} isn't valid JSON, so I left it alone. Ask {bot.name()} to fix it.")


def dispatch(name, text):
    p = load_person(name)
    if p is None:
        return Reply(f"No user {name}.")
    words = text.split()
    head = words[0].lstrip("/").partition("@")[0].lower() if words else ""
    args = words[1:]
    if head == "mcp":
        return mcp.command(p, text)
    if p.broken:
        raise jsonfile.NotAnObject(p.broken[0])
    reply = skills_command(p, args) if head == "skills" else plugin_command(p, args)
    # A plugin change reloads; a marketplace everyone now declares should move up right away too.
    if reply.reload or [a.lower() for a in args[:2]] in (["marketplace", "add"], ["marketplace", "remove"]):
        moved = sync_and_report_moves()
        if moved:
            reply.text += f"\n\n🔼 Everyone has it now, so it's shared from ~/.claude: {', '.join(moved)}."
    if reply.reload:
        reply.text += f"\n\n{bot.name()} reloads its plugins and skills once it's idle."
    return reply


def sync_and_report_moves():
    """Run a sync; the items it moved up."""
    before = jsonfile.read_json(MANIFEST, {})
    try:
        Sync().run()
    except Exception:  # the command's own change worked; say so
        log(f"sync after a command failed:\n{traceback.format_exc().rstrip()}")
        return []
    after = jsonfile.read_json(MANIFEST, {})
    return [f"{singular(k)} {i}" for k in KINDS for i in set(after.get(k, [])) - set(before.get(k, []))]


# --- /plugin -----------------------------------------------------------------------------------

def plugin_command(p, args):
    sub, rest = (args[0].lower(), args[1:]) if args else ("", [])
    if sub in ("", "list", "manage"):
        only = "enabled" if "--enabled" in rest else "disabled" if "--disabled" in rest else None
        return Reply("\n".join(plugins_overview(p, only) + ["", "`/plugin help`: all commands"]))
    if sub == "help":
        return Reply(HELP)
    if sub == "marketplace":
        return marketplace_command(p, rest)
    if sub == "directory":
        return Reply(directory(p, " ".join(rest).lower()))
    if sub == "install":
        return install(p, rest)
    if sub in ("enable", "disable", "uninstall"):
        return toggle(p, sub, rest)
    if sub in ("configure", "stats", "validate", "tag"):
        return Reply(f"/plugin {sub} needs Claude Code's screen, so it isn't available here. Ask {bot.name()} to do it.")
    return Reply(HELP)


def plugins_overview(p, only=None):
    shared_on = user_plugins()
    mine = p.enabled_plugins - shared_on
    off = p.disabled_plugins
    lines = ["🔌 Plugins"]
    if only != "disabled":
        lines += [f"Shared with everyone: {', '.join(sorted(shared_on - off)) or 'none'}",
                  f"Yours: {', '.join(sorted(mine)) or 'none'}"]
    if only != "enabled" and off:
        lines.append(f"Turned off for you: {', '.join(sorted(off))}")
    return lines


def available(p):
    """Every plugin in the person's marketplaces, as `claude plugin list --available` lists them."""
    result = claude_cli.run(["plugin", "list", "--available", "--json"], p.ws, timeout=LISTING_TIMEOUT)
    try:
        return json.loads(result.stdout).get("available") or []
    except (ValueError, AttributeError):
        log(f"{p.ws}: no plugin listing: {result.message}")
        return []


def plugin_lines(items, have):
    return [f"{'✓ ' if i['pluginId'] in have else ''}{i['pluginId']}: "
            f"{' '.join((i.get('description') or '').split())[:DESCRIPTION_MAX]}" for i in items]


def most_installed(items):
    return sorted(items, key=lambda i: -(i.get("installCount") or 0))


def directory(p, query):
    items = most_installed(i for i in available(p) if query in f"{i.get('pluginId')} {i.get('description')}".lower())
    if not items:
        return f"Nothing in your marketplaces matches {query!r}." if query else "No plugins available."
    head = (f"🔎 {len(items)} plugins" + (f" match {query!r}" if query else "")
            + (", most installed first:" if len(items) > 1 else ":"))
    have = user_plugins() | set(p.plugins)
    return "\n".join([head, *plugin_lines(items[:DIRECTORY_SHOWN], have), "",
                      "Install: `/plugin install <plugin>@<marketplace>`"])


def install(p, rest):
    if not rest:
        counts = {}
        for i in available(p):
            counts[i.get("marketplaceName")] = counts.get(i.get("marketplaceName"), 0) + 1
        marketplaces = sorted(counts)
        return Reply("\n".join(["Pick a marketplace to browse, or search:"]
                               + [f"`/plugin install {m}` ({counts[m]} plugins)" for m in marketplaces]
                               + ["`/plugin directory <word>`"]),
                     copy=[f"/plugin install {m}" for m in marketplaces])
    target = rest[0]
    listing = available(p) if "@" not in target and len(rest) == 1 else []
    if target in {i.get("marketplaceName") for i in listing}:
        items = most_installed(i for i in listing if i.get("marketplaceName") == target)
        have = user_plugins() | set(p.plugins)
        return Reply("\n".join([f"🏪 {target}: {len(items)} plugins, most installed first:",
                                *plugin_lines(items[:MARKETPLACE_SHOWN], have), "",
                                f"Install: `/plugin install <plugin>@{target}`"]))
    if "--marketplace" in rest:
        target, problem = add_marketplace_for(p, target, rest)
        if problem:
            return Reply(problem)
    pid = resolve(p, target)
    if pid in NEVER_SHARE or target.partition("@")[0] == "telegram":
        return Reply("The bot itself runs on the Telegram plugin, and a second copy would take it over, "
                     "so it can't be installed here.")
    if pid in user_plugins():
        return Reply(f"{pid} is already shared with everyone, so you have it.")
    p.ws.mkdir(parents=True, exist_ok=True)
    result = claude_cli.run(["plugin", "install", target, "--scope", "project", "--json"], p.ws)
    return Reply(status(result), reload=result.ok)


def add_marketplace_for(p, target, rest):
    """/plugin install <plugin> --marketplace <source>: add the marketplace first.
    (the plugin id to install, or a problem)."""
    i = rest.index("--marketplace")
    if i + 1 >= len(rest):
        return target, "Usage: `/plugin install <plugin> --marketplace <source>`"
    source = rest[i + 1]
    p.ws.mkdir(parents=True, exist_ok=True)
    result = claude_cli.run(["plugin", "marketplace", "add", source, "--scope", "project", "--json"], p.ws)
    if not result.ok:
        return target, f"⚠️ Couldn't add marketplace {source}: {result.message}"
    after = Person(p.name, p.display)
    new = [n for n in after.marketplaces if n not in p.marketplaces]
    name = new[0] if new else next((n for n, v in after.merged("extraKnownMarketplaces").items()
                                    if short_source(v.get("source")) in source), None)
    if not name:
        return target, ("Added the marketplace, but couldn't tell its name. "
                        f"Check `/plugin marketplace`, then `/plugin install {target}@<name>`")
    return f"{target.partition('@')[0]}@{name}", None


def toggle(p, sub, rest):
    """/plugin enable|disable|uninstall."""
    if not rest:
        return Reply(f"Usage: `/plugin {sub} <plugin>@<marketplace>`")
    pid = resolve(p, rest[0])
    if sub in ("disable", "uninstall") and pid in user_plugins():
        if p.plugins.get(pid) is False:
            return Reply(f"{pid} is already off for you.")
        result = claude_cli.run(["plugin", "disable", pid, "--scope", "project"], p.ws)
        if not result.ok:
            return Reply(f"⚠️ {result.message}")
        return Reply(f"✅ Turned {pid} off for you. It was shared with everyone, so it goes back into "
                     "the others' own folders and stays theirs.", reload=True)
    if sub == "enable" and p.plugins.get(pid) is False:
        for scope_file in p.files.values():
            jsonfile.edit_object(scope_file, drop_key("enabledPlugins", pid), create=False)
        if pid in user_plugins():
            return Reply(f"✅ {pid} is on again for you.", reload=True)
    args = ["plugin", sub, pid, "--scope", "project"] + (["--keep-data"] if sub == "uninstall" else [])
    result = claude_cli.run(args, p.ws)
    get_it = [f"/plugin install {pid}"] if not result.ok and sub == "enable" else []
    hint = f"\nTo get it: `{get_it[0]}`" if get_it else ""
    return Reply(status(result) + hint, reload=result.ok, copy=get_it)


def resolve(p, name):
    """A full plugin id for a name with or without @marketplace, from what this person has."""
    if "@" in name:
        return name
    found = [i for i in sorted(user_plugins() | set(p.plugins)) if i.partition("@")[0] == name]
    return found[0] if len(found) == 1 else name


def short_source(src):
    src = src or {}
    return src.get("repo") or src.get("url") or src.get("path") or src.get("source") or "?"


# --- /plugin marketplace -----------------------------------------------------------------------

def marketplace_command(p, args):
    sub, rest = (args[0].lower(), args[1:]) if args else ("list", [])
    if sub == "list":
        return Reply("\n".join(marketplaces_overview(p) + ["", "Browse one: `/plugin install <marketplace>`",
                                                           "Add one: `/plugin marketplace add <owner/repo, URL or path>`"]))
    if sub == "add":
        if not rest:
            return Reply("Usage: `/plugin marketplace add <owner/repo, git URL, URL to marketplace.json, or path>`")
        p.ws.mkdir(parents=True, exist_ok=True)
        result = claude_cli.run(["plugin", "marketplace", "add", rest[0], "--scope", "project", "--json"], p.ws)
        name = result.data.get("marketplace")
        browse = f"/plugin install {name or '<its name>'}"
        return Reply(status(result, f"\nBrowse it: `{browse}`"), copy=[browse] if result.ok and name else [])
    if sub == "remove":
        if not rest:
            return Reply("Usage: `/plugin marketplace remove <name>`")
        name = rest[0]
        if name in shared_marketplaces() or name in BUILTIN_MARKETPLACES:
            return Reply(f"{name} is shared with everyone, and shared marketplaces stay shared.")
        return Reply(status(claude_cli.run(["plugin", "marketplace", "remove", name, "--scope", "project"], p.ws)))
    if sub == "update":
        return Reply(status(claude_cli.run(["plugin", "marketplace", "update", *rest[:1]], p.ws)))
    return Reply(HELP)


def shared_marketplaces():
    return jsonfile.read_object(paths.USER_SETTINGS).get("extraKnownMarketplaces") or {}


def marketplaces_overview(p):
    declared = shared_marketplaces()
    lines = ["🏪 Marketplaces"]
    lines += [f"- {n} (built in)" for n in sorted(BUILTIN_MARKETPLACES - set(declared))]
    lines += [f"- {n}: {short_source(v.get('source'))} (shared)" for n, v in sorted(declared.items())]
    mine = {n: v for n, v in p.merged("extraKnownMarketplaces").items() if n not in declared}
    lines += [f"- {n}: {short_source(v.get('source'))} (yours)" for n, v in sorted(mine.items())]
    return lines


# --- /skills -----------------------------------------------------------------------------------

def skills_command(p, args):
    words = [a.lower() for a in args]
    if not words or words == ["list"]:
        return Reply(skills_overview(p))
    level = next((w for w in words if w in SKILL_LEVELS), None)
    names = [a for a, w in zip(args, words, strict=True) if w not in SKILL_LEVELS]
    if not level or len(names) != 1:
        return Reply("Usage: `/skills off <name>`, `/skills on <name>`")
    name = names[0]
    if not SKILL_NAME_RE.fullmatch(name):
        return Reply(f"{name} isn't a skill name.")
    set_skill_level(p, name, level)
    note = ""
    if level == "on" and name not in p.skills and name not in skill_dirs(USER_SKILLS) and name not in plugin_skills(p):
        note = copy_from_someone_else(p, name)
    if name in skill_dirs(USER_SKILLS) and level == "off":
        note = " It was shared with everyone, so it goes back into the others' own folders and stays theirs."
    return Reply(f"✅ Skill {name}: {level}.{note}", reload=True)


def set_skill_level(p, name, level):
    def change(data):
        overrides = data.setdefault("skillOverrides", {})
        if level == "on":
            overrides.pop(name, None)
        else:
            overrides[name] = level
    jsonfile.edit_object(p.files["project"], change, create=False)
    if p.merged("skillOverrides").get(name) and level == "on":
        jsonfile.edit_object(p.files["local"], drop_key("skillOverrides", name), create=False)


def copy_from_someone_else(p, name):
    """Turned off while it was shared, the skill went back to the others: copy theirs."""
    for other in users.load_users().values():
        if other.name != p.name and (other.workspace / ".claude" / "skills" / name / "SKILL.md").is_file():
            skillfiles.copy_skill(other.workspace / ".claude" / "skills" / name, p.skills_dir / name)
            return f" Copied it into your folder from {other.display}'s."
    return ""


def skills_overview(p):
    shared = skill_dirs(USER_SKILLS)
    own = sorted(p.skills)
    from_plugins = plugin_skills(p)
    off = {k: v for k, v in p.merged("skillOverrides").items() if v != "on"}
    lines = ["🧩 Skills",
             f"Yours: {', '.join(own) or 'none'}",
             f"Shared with everyone: {', '.join(n for n in shared if n not in off) or 'none'}"]
    if from_plugins:
        lines.append("From plugins: " + ", ".join(f"{s} ({pl})" for s, pl in sorted(from_plugins.items())))
    builtin = builtin_skills(p, set(own) | set(shared) | set(from_plugins))
    if builtin:
        lines.append(f"Built into Claude Code: {', '.join(builtin)}")
    if off:
        lines.append("Changed for you: " + ", ".join(f"{k} ({v})" for k, v in sorted(off.items())))
    lines += ["", "`/skills off <name>` · `/skills on <name>`",
              f"To add a skill, ask {bot.name()} to write one or install a plugin that has it."]
    return "\n".join(lines)


def plugin_skills(p):
    """{skill: plugin} for the plugins this person has on."""
    on = (user_plugins() | p.enabled_plugins) - p.disabled_plugins
    installed = jsonfile.read_json(paths.CLAUDE_DIR / "plugins" / "installed_plugins.json", {}).get("plugins") or {}
    out = {}
    for pid in sorted(on):
        install_paths = [i.get("installPath") for i in installed.get(pid) or [] if i.get("installPath")]
        if install_paths:
            for name in skill_dirs(Path(install_paths[0]) / "skills"):
                out[name] = pid.partition("@")[0]
    return out


def builtin_skills(p, known):
    """Names from the newest skill listing in this person's transcripts that come from nowhere else."""
    transcripts = sorted(glob.glob(f"{paths.project_dir(p.ws)}/*.jsonl"), key=os.path.getmtime)
    for path in reversed(transcripts[-3:]):
        listing = None
        with open(path, errors="replace") as f:
            for line in f:
                if '"skill_listing"' in line:
                    try:
                        listing = json.loads(line)["attachment"]["content"]
                    except (ValueError, KeyError, TypeError):
                        continue
        if listing:
            names = re.findall(r"^- ([^\s:]+):", listing, re.M)
            return sorted(n for n in names if n not in known)
    return []
