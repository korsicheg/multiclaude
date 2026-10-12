"""What each person has in their own workspace."""
import hashlib
import json

from multiclaude import jsonfile, paths, users

SCOPES = ("project", "local")  # in this order: local settings win over project settings


def canon(value):
    return json.dumps(value, sort_keys=True)


def tree_hash(folder):
    """A digest of a folder's files, so identical skill folders can be told apart from others."""
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder)
        if "__pycache__" in rel.parts or p.name == ".DS_Store" or not p.is_file():
            continue
        h.update(str(rel).encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def skill_dirs(base):
    """{name: folder} for the skills in base."""
    if not base.is_dir():
        return {}
    return {d.name: d for d in sorted(base.iterdir()) if (d / "SKILL.md").is_file()}


class Person:
    """One person's workspace settings and skills, read once."""

    def __init__(self, name, display):
        self.name, self.display = name, display
        self.ws = paths.workspace(name)
        self.files = {s: paths.workspace_settings(name, s) for s in SCOPES}
        self.settings, self.broken = {}, []
        for s in SCOPES:
            try:
                self.settings[s] = jsonfile.read_object(self.files[s])
            except jsonfile.NotAnObject:
                self.settings[s] = {}  # counts as having nothing, and is never written
                self.broken.append(self.files[s])
        self.skills_dir = self.ws / ".claude" / "skills"
        self.skills = {n: tree_hash(d) for n, d in skill_dirs(self.skills_dir).items()}

    def merged(self, section):
        out = {}
        for s in SCOPES:
            if isinstance(self.settings[s].get(section), dict):
                out.update(self.settings[s][section])
        return out

    @property
    def plugins(self):
        """{plugin id: true or false} as set in their settings."""
        return self.merged("enabledPlugins")

    @property
    def enabled_plugins(self):
        return {k for k, v in self.plugins.items() if v is True}

    @property
    def disabled_plugins(self):
        return {k for k, v in self.plugins.items() if v is False}

    @property
    def marketplaces(self):
        """{name: its source, canonical} for the marketplaces they declare."""
        return {n: canon(v.get("source")) for n, v in self.merged("extraKnownMarketplaces").items()
                if isinstance(v, dict)}

    def scopes_with(self, section, key):
        return [s for s in SCOPES if key in (self.settings[s].get(section) or {})]

    def scopes_enabling(self, pid):
        """The scopes whose settings turn the plugin on; a `false` entry is the person's choice."""
        return [s for s in SCOPES if (self.settings[s].get("enabledPlugins") or {}).get(pid) is True]


def load_people():
    return [Person(u.name, u.display) for u in users.load_users().values()]


def load_person(name):
    """The registered person called name, or None."""
    user = users.load_users().get(name)
    return Person(user.name, user.display) if user else None


def everyone_has(people, per_person):
    """Items (and their identity) that every person has, with the same identity."""
    first = per_person(people[0])
    return {k: v for k, v in first.items() if all(per_person(p).get(k) == v for p in people[1:])}


def user_plugins():
    """The plugins turned on in ~/.claude, which every session gets."""
    return {k for k, v in (jsonfile.read_object(paths.USER_SETTINGS).get("enabledPlugins") or {}).items() if v is True}
