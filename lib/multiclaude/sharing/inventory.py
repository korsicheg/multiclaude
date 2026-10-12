"""What a person has, as the control panel lists it: their plugins, each with what it brings (MCP
servers, skills, language servers), and their skills, each on or off for them. Read from the
settings and the plugins' own files only, so it's cheap enough for every look for changes."""
import functools
import re
from pathlib import Path

from multiclaude import jsonfile, paths
from multiclaude.sharing.people import skill_dirs, user_plugins
from multiclaude.sharing.sync import NEVER_SHARE, USER_SKILLS

DESCRIPTION_MAX = 300
PLUGINS_DIR = paths.CLAUDE_DIR / "plugins"
# A skill's description ends with when to use it ("Use when…", "TRIGGER…"): the panel shows what it does.
WHEN_TO_USE_RE = re.compile(r"(?<=[.!?])\s+(?:Use (?:it |this )?(?:when|whenever|for|to|before)\b|TRIGGER\b).*", re.S)


def plugins(p):
    """[{id, name, marketplace, on, shared, description, servers, skills, code}] for every plugin the
    person has on or has turned off, by name."""
    shared = user_plugins()
    marketplaces = jsonfile.read_json(PLUGINS_DIR / "known_marketplaces.json", {})
    installed = jsonfile.read_json(PLUGINS_DIR / "installed_plugins.json", {}).get("plugins") or {}
    rows = []
    for pid in sorted((shared | p.enabled_plugins | p.disabled_plugins) - NEVER_SHARE):
        name, _, marketplace = pid.partition("@")
        entry, folder = locate(name, marketplaces.get(marketplace) or {}, installed.get(pid) or [], p.ws)
        manifest = read_object(folder / ".claude-plugin" / "plugin.json") if folder else {}
        rows.append({"id": pid, "name": name, "marketplace": marketplace,
                     "on": pid not in p.disabled_plugins, "shared": pid in shared,
                     "description": shortened(entry.get("description") or manifest.get("description")),
                     "servers": declared(folder, ".mcp.json", "mcpServers", manifest, entry),
                     "skills": plugin_skills(name, folder, manifest, entry),
                     "code": declared(folder, ".lsp.json", "lspServers", manifest, entry)})
    return rows


def skills(p):
    """[{name, on, shared, description}] for the person's own skills, the shared ones, and any other
    they turned off (a plugin's or a built-in one), by name. A plugin's skills otherwise come and go
    with it, so they aren't listed."""
    own, shared = skill_dirs(p.skills_dir), skill_dirs(USER_SKILLS)
    turned_off = {n for n, level in p.merged("skillOverrides").items() if level != "on"}
    return [{"name": n, "on": n not in turned_off, "shared": n in shared and n not in own,
             "description": skill_description(own.get(n) or shared.get(n))}
            for n in sorted(set(own) | set(shared) | turned_off)]


# --- where a plugin's files are ---

def locate(name, marketplace, installs, workspace):
    """The plugin's marketplace entry, and its folder: in the marketplace when it's there (a
    directory marketplace is read in place), else where Claude Code installed it, for this
    workspace if it can."""
    location = Path(marketplace.get("installLocation") or "/nonexistent")
    catalog = read_object(location / ".claude-plugin" / "marketplace.json")
    entry = next((e for e in catalog.get("plugins") or [] if isinstance(e, dict) and e.get("name") == name), {})
    source = entry.get("source")
    if isinstance(source, str) and (location / source).is_dir():
        return entry, (location / source).resolve()
    ours = [i for i in installs if i.get("projectPath") == str(workspace)] + installs
    folder = next((Path(i["installPath"]) for i in ours if i.get("installPath")), None)
    return entry, folder if folder and folder.is_dir() else None


def declared(folder, default_file, key, *manifests):
    """The names of the servers (MCP or language) a plugin declares: in its default file, and under
    key in its plugin.json and marketplace entry, each an inline map, a path to a JSON file, or a list."""
    names = set(server_names(read_object(folder / default_file), key)) if folder else set()
    for manifest in manifests:
        names |= declared_value(folder, manifest.get(key), key)
    return sorted(names)


def declared_value(folder, value, key):
    if isinstance(value, dict):
        return set(value)
    if isinstance(value, list):
        return set().union(*(declared_value(folder, v, key) for v in value))
    if isinstance(value, str) and value.endswith(".json") and folder:
        return set(server_names(read_object(folder / value), key))
    return set()  # an .mcpb bundle or a URL: its servers are inside it


def server_names(config, key):
    """A server file is {key: {name: …}} or the map itself."""
    servers = config.get(key) if key in config else config
    return list(servers) if isinstance(servers, dict) else []


def plugin_skills(name, folder, manifest, entry):
    """The skills a plugin brings: skills/ and the folders its manifest names, or the plugin itself
    when it's a single skill (SKILL.md at its root)."""
    if not folder:
        return []
    found = set(skill_dirs(folder / "skills"))
    for path in listed(manifest.get("skills")) + listed(entry.get("skills")):
        where = folder / path
        found |= {where.name} if (where / "SKILL.md").is_file() else set(skill_dirs(where))
    if not found and (folder / "SKILL.md").is_file():
        found.add(name)
    return sorted(found)


def listed(value):
    return [value] if isinstance(value, str) else [v for v in value or [] if isinstance(v, str)]


# --- descriptions ---

def skill_description(folder):
    if not folder:
        return ""
    try:
        text = (folder / "SKILL.md").read_text(errors="replace")
    except OSError:
        return ""
    return shortened(WHEN_TO_USE_RE.sub("", frontmatter_value(text, "description")))


def frontmatter_value(text, key):
    """A plain, quoted or folded (> or |) value in a Markdown file's YAML front matter."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return ""
    head = lines[1:lines.index("---", 1)] if "---" in lines[1:] else []
    for i, line in enumerate(head):
        if line.startswith(f"{key}:"):
            value = line.partition(":")[2].strip()
            if value[:1] in (">", "|"):
                follow = [ln.strip() for ln in head[i + 1:]]
                stop = next((j for j, ln in enumerate(head[i + 1:]) if ln and not ln[0].isspace()), len(follow))
                value = " ".join(follow[:stop])
            elif len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""


def shortened(text):
    """One paragraph of at most DESCRIPTION_MAX characters, cut at a word."""
    text = " ".join(str(text or "").split())
    if len(text) <= DESCRIPTION_MAX:
        return text
    return text[:DESCRIPTION_MAX].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def read_object(path):
    """A JSON file's object, or {} when it's missing or isn't one."""
    try:
        return cached_object(path, path.stat().st_mtime_ns)
    except OSError:
        return {}


@functools.lru_cache(maxsize=64)
def cached_object(path, _mtime):
    """Marketplace catalogs are big and change rarely: each version is read once."""
    data = jsonfile.read_json(path, {})
    return data if isinstance(data, dict) else {}
