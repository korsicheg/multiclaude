"""The sync: move what everyone has up to ~/.claude, and back down when someone turns it off
or someone new joins. See the package docstring for the rules."""
import os
import shutil
import subprocess

from multiclaude import claude_cli, jsonfile, paths
from multiclaude.log import log
from multiclaude.sharing import skillfiles
from multiclaude.sharing.people import canon, everyone_has, load_people, skill_dirs, tree_hash

MANIFEST = paths.STATE / "shared.json"
USER_SKILLS = paths.CLAUDE_DIR / "skills"
FRONT_DESK = paths.ROOT / ".claude" / "settings.local.json"
# Claude Code knows this marketplace without anyone declaring it.
BUILTIN_MARKETPLACES = {"claude-plugins-official"}
# The front desk hosts the bot with this plugin; a second copy anywhere would take the bot over.
NEVER_SHARE = {"telegram@claude-plugins-official"}
KINDS = ("plugins", "marketplaces", "skills")
NOTIFY_TIMEOUT = 60


class Manifest:
    """state/shared.json: what the sync moved to ~/.claude, the people who had it (so the next
    run can tell who is new), what was there before its first run (the baseline), and the strays
    already reported. Saved after every move, so a run that dies midway leaves nothing in
    ~/.claude it doesn't know about."""

    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.data = jsonfile.read_object(MANIFEST)
        self.shared = {k: set(self.data.get(k, [])) for k in KINDS}

    def __contains__(self, kind_item):
        kind, item = kind_item
        return item in self.shared[kind]

    def add(self, kind, item):
        self.shared[kind].add(item)
        self.save()

    def discard(self, kind, item):
        self.shared[kind].discard(item)
        self.save()

    @property
    def people(self):
        """The names of the people who had what's shared, or None before any run noted them."""
        return self.data.get("people")

    def save(self, **extra):
        self.data.update(extra)
        if not self.dry_run:
            jsonfile.write_json(MANIFEST, {**self.data, **{k: sorted(v) for k, v in self.shared.items()}})


class Sync:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.people = load_people()
        self.manifest = Manifest(dry_run)
        self.user = jsonfile.read_object(paths.USER_SETTINGS)
        self.known = self.user.get("extraKnownMarketplaces") or {}  # the marketplaces ~/.claude declares
        self.dedupe = set(self.manifest.shared["marketplaces"])      # marketplaces whose per-person copies go
        # People registered since the last run. Before any run noted who was here, nobody is new.
        had = self.manifest.people
        self.newcomers = [] if had is None else [p for p in self.people if p.name not in had]
        self.up, self.down = [], []

    def run(self):
        if not self.people:
            return
        self.check_workspaces()
        self.empty_old_trash()
        # 1. Shared items go back into the folders of the people who had them: all of them when
        # someone new joined, so newcomers start with nothing shared, else the ones someone
        # turned off for themselves (which go back to everyone else).
        self.return_plugins()
        self.return_skills()
        self.return_marketplaces()
        if self.still_returning():
            self.log(f"{names(self.newcomers)} joined, but not everything shared could go back yet; "
                     "sharing nothing new until it has")
        else:
            self.note_people()
            # 2. What everyone has moves up. Marketplaces are declared first, since a shared plugin
            # needs its marketplace at user scope; the per-person declarations go last, after the
            # plugins that came from them.
            self.share_marketplaces()
            self.share_plugins()
            self.prune_plugin_copies()
            self.prune_marketplace_copies()
            self.share_skills()
            self.prune_skill_copies()
        # 3. The front desk never loads shared plugins.
        self.hide_plugins_from_front_desk()
        # 4. Anything else new in ~/.claude was installed there directly.
        strays = self.find_strays()
        self.report(strays)

    # --- side effects, which a dry run only logs ---

    def log(self, msg):
        log(f"(dry run) {msg}" if self.dry_run else msg)

    def edit(self, path, change):
        """Change a settings file, keeping everything else in it."""
        if not self.dry_run:
            jsonfile.edit_object(path, change)

    def claude(self, args, cwd):
        if self.dry_run:
            self.log(f"  would run in {cwd}: claude {' '.join(args)}")
            return True
        return claude_cli.run(args, cwd).ok

    # --- the steps ---

    def check_workspaces(self):
        for p in self.people:
            if not p.ws.is_dir():
                self.log(f"{p.name} has no workspace at {p.ws}; nothing can be shared until it exists")
            for path in p.broken:
                self.log(f"{path} isn't valid JSON; leaving it alone, and nothing it would hold moves for now")

    def empty_old_trash(self):
        for d in skillfiles.old_trash():
            self.log(f"  delete {d}, trashed over {skillfiles.TRASH_KEEP // 86400} days ago")
            if not self.dry_run:
                shutil.rmtree(d, ignore_errors=True)

    def why_back(self, off=()):
        """Why a shared item goes back to the people who had it, or None while it stays shared."""
        if self.newcomers:
            return f"{displays(self.newcomers)} joined"
        return f"{displays(off)} turned it off" if off else None

    def keeping(self, off=()):
        """Who gets a shared item back in their own folder: everyone who had it and didn't turn it off."""
        return [p for p in self.people if p not in off and p not in self.newcomers]

    def return_plugins(self):
        for pid in sorted(self.manifest.shared["plugins"]):
            off = [p for p in self.people if p.plugins.get(pid) is False]
            why = self.why_back(off)
            if not why:
                continue
            keep = self.keeping(off)
            self.log(f"plugin {pid}: {why}; moving it back to {names(keep) or 'nobody'}")
            if not all(self.claude(["plugin", "install", pid, "--scope", "project"], p.ws)
                       for p in keep if p.ws.is_dir()):
                self.log(f"plugin {pid}: couldn't install it for everyone; leaving it shared for now")
                continue
            if (self.user.get("enabledPlugins") or {}).get(pid) is True and not self.claude(
                    ["plugin", "uninstall", pid, "--scope", "user", "--keep-data"], paths.HOME):
                continue
            for p in off:
                for scope in p.scopes_with("enabledPlugins", pid):
                    self.edit(p.files[scope], drop_key("enabledPlugins", pid))
            self.edit(FRONT_DESK, drop_key("enabledPlugins", pid))
            self.manifest.discard("plugins", pid)
            self.down.append(f"plugin {pid} ({why})")

    def return_skills(self):
        for name in sorted(self.manifest.shared["skills"]):
            off = [p for p in self.people if p.merged("skillOverrides").get(name) == "off"]
            why = self.why_back(off)
            if not why:
                continue
            keep = self.keeping(off)
            self.log(f"skill {name}: {why}; moving it back to {names(keep) or 'nobody'}")
            src = USER_SKILLS / name
            if not self.dry_run and src.is_dir():
                for p in keep:
                    if p.ws.is_dir() and not (p.skills_dir / name).exists():
                        skillfiles.copy_skill(src, p.skills_dir / name)
                skillfiles.to_trash(src, name)
            for p in off:
                for scope in p.scopes_with("skillOverrides", name):
                    self.edit(p.files[scope], drop_key("skillOverrides", name))
            self.manifest.discard("skills", name)
            self.down.append(f"skill {name} ({why})")

    def return_marketplaces(self):
        """Shared marketplaces go back only when someone new joins, each once none of its plugins
        is shared any more: a shared plugin needs its marketplace at user scope."""
        if not self.newcomers:
            return
        why, keep = self.why_back(), self.keeping()
        for name in sorted(self.manifest.shared["marketplaces"]):
            if any(marketplace_of(pid) == name for pid in self.manifest.shared["plugins"]):
                continue
            self.log(f"marketplace {name}: {why}; moving it back to {names(keep) or 'nobody'}")
            self.declare_marketplace(name, keep)
            self.edit(paths.USER_SETTINGS, drop_key("extraKnownMarketplaces", name))
            self.known = {k: v for k, v in self.known.items() if k != name}
            self.dedupe.discard(name)
            self.manifest.discard("marketplaces", name)
            self.down.append(f"marketplace {name} ({why})")

    def declare_marketplace(self, name, people):
        """Declare a shared marketplace in these people's own settings, with the source ~/.claude has for it."""
        source = (self.known.get(name) or {}).get("source")
        if source is None:
            return
        for p in people:
            if p.ws.is_dir() and name not in p.marketplaces:
                self.edit(p.files["project"], set_key("extraKnownMarketplaces", name, {"source": source}))

    def still_returning(self):
        """Whether someone joined but some of what was shared couldn't go back yet (an install
        failed). The newcomers stay new until it has, so the next run tries again."""
        return bool(self.newcomers) and any(self.manifest.shared.values())

    def note_people(self):
        """Note who has what's shared now, so the next run can tell who is new."""
        self.manifest.save(people=sorted(p.name for p in self.people))

    def share_marketplaces(self):
        for name, source in sorted(everyone_has(self.people, lambda p: p.marketplaces).items()):
            if ("marketplaces", name) in self.manifest:
                continue
            if name in self.known:
                if canon(self.known[name].get("source")) == source:
                    self.log(f"marketplace {name}: ~/.claude already declares it; removing the per-person copies")
                    self.dedupe.add(name)
                else:
                    self.log(f"marketplace {name}: ~/.claude declares it with another source; leaving it per person")
                continue
            self.log(f"marketplace {name}: everyone has it; moving it to ~/.claude")
            raw = self.people[0].merged("extraKnownMarketplaces")[name]["source"]
            self.edit(paths.USER_SETTINGS, set_key("extraKnownMarketplaces", name, {"source": raw, "autoUpdate": True}))
            self.known = {**self.known, name: {"source": raw}}
            self.manifest.add("marketplaces", name)
            self.dedupe.add(name)
            self.up.append(f"marketplace {name}")

    def share_plugins(self):
        for pid in sorted(everyone_has(self.people, lambda p: {k: True for k in p.enabled_plugins})):
            if ("plugins", pid) in self.manifest or pid in NEVER_SHARE:
                continue
            market = marketplace_of(pid)
            if market not in self.known and market not in BUILTIN_MARKETPLACES:
                self.log(f"plugin {pid}: everyone has it, but marketplace {market} isn't shared yet")
                continue
            if (self.user.get("enabledPlugins") or {}).get(pid) is True:
                self.log(f"plugin {pid}: already enabled in ~/.claude by someone else; removing the per-person copies")
                for p in self.people:
                    self.remove_plugin_copies(p, pid)
                continue
            self.log(f"plugin {pid}: everyone has it; moving it to ~/.claude")
            # Off for the front desk first, so it never picks up the plugin's tools.
            self.edit(FRONT_DESK, set_key("enabledPlugins", pid, False))
            if not self.claude(["plugin", "install", pid, "--scope", "user"], paths.HOME):
                continue
            self.manifest.add("plugins", pid)
            self.up.append(f"plugin {pid}")

    def prune_plugin_copies(self):
        for pid in sorted(self.manifest.shared["plugins"]):
            for p in self.people:
                self.remove_plugin_copies(p, pid)

    def prune_marketplace_copies(self):
        for name in sorted(self.dedupe):
            for p in self.people:
                if p.marketplaces.get(name) == canon((self.known.get(name) or {}).get("source")):
                    for scope in p.scopes_with("extraKnownMarketplaces", name):
                        self.edit(p.files[scope], drop_key("extraKnownMarketplaces", name))

    def share_skills(self):
        for name, digest in sorted(everyone_has(self.people, lambda p: p.skills).items()):
            if ("skills", name) in self.manifest:
                continue
            dst = USER_SKILLS / name
            if dst.exists():
                if dst.is_dir() and tree_hash(dst) == digest:
                    self.log(f"skill {name}: ~/.claude already has the same copy; removing the per-person copies")
                    for p in self.people:
                        self.remove_skill_copy(p, name, digest)
                else:
                    self.log(f"skill {name}: ~/.claude/skills already has a different {name}; leaving it per person")
                continue
            self.log(f"skill {name}: everyone has the same files; moving it to ~/.claude/skills")
            if not self.dry_run:
                skillfiles.copy_skill(self.people[0].skills_dir / name, dst)
            self.manifest.add("skills", name)
            self.up.append(f"skill {name}")

    def prune_skill_copies(self):
        for name in sorted(self.manifest.shared["skills"]):
            src = USER_SKILLS / name
            if not src.is_dir():
                continue
            digest = tree_hash(src)
            for p in self.people:
                if p.skills.get(name) == digest:  # a copy identical to the shared one
                    self.remove_skill_copy(p, name, digest)

    def hide_plugins_from_front_desk(self):
        for pid in self.manifest.shared["plugins"]:
            self.edit(FRONT_DESK, set_key("enabledPlugins", pid, False))

    def find_strays(self):
        """Items in ~/.claude that the sync didn't put there and nobody was told about yet."""
        user = jsonfile.read_object(paths.USER_SETTINGS)
        present = {
            "plugins": {k for k, v in (user.get("enabledPlugins") or {}).items() if v is True},
            "marketplaces": set(user.get("extraKnownMarketplaces") or {}),
            "skills": set(skill_dirs(USER_SKILLS)),
        }
        shared = self.manifest.shared
        baseline = self.manifest.data.get("baseline") or {k: sorted(v - shared[k]) for k, v in present.items()}
        reported = {k: set(v) for k, v in (self.manifest.data.get("reported") or {}).items()}
        strays = []
        for kind, items in present.items():
            for item in sorted(items - shared[kind] - set(baseline.get(kind, [])) - reported.get(kind, set())):
                strays.append(f"{singular(kind)} {item}")
                reported.setdefault(kind, set()).add(item)
        self.manifest.save(baseline=baseline, reported={k: sorted(v) for k, v in reported.items()})
        return strays

    def report(self, strays):
        lines = []
        if self.up:
            lines += ["🔼 Everyone had these, so they're shared from ~/.claude now and the per-person copies are gone:"]
            lines += [f"- {x}" for x in self.up]
        if self.down:
            lines += ["🔽 Back in each person's own folder:"] + [f"- {x}" for x in self.down]
        if strays:
            lines += ["⚠️ Installed straight into ~/.claude, which every session on the VM loads. "
                      "Each person's assistant should install only into their own folder:"] + [f"- {x}" for x in strays]
        if not lines:
            return
        text = "\n".join(lines)
        self.log(text)
        if not self.dry_run and not os.environ.get("SHARED_NO_NOTIFY"):
            try:
                subprocess.run([str(paths.BIN / "assistant"), "notify", text], check=False, timeout=NOTIFY_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired) as e:
                self.log(f"telling the owner failed: {e}")

    # --- per-person copies ---

    def remove_plugin_copies(self, p, pid):
        for scope in p.scopes_enabling(pid):
            if not self.claude(["plugin", "uninstall", pid, "--scope", scope, "--keep-data"], p.ws):
                self.edit(p.files[scope], drop_key("enabledPlugins", pid))

    def remove_skill_copy(self, p, name, digest):
        """Move a person's copy of a shared skill to the trash, unless its files changed since
        they were compared (the person's assistant may be editing it)."""
        d = p.skills_dir / name
        if not self.dry_run and not d.is_symlink() and tree_hash(d) != digest:
            self.log(f"  {d} changed since it was compared; leaving it")
            return
        self.log(f"  remove {d}")
        if self.dry_run:
            return
        if d.is_symlink():
            d.unlink()
        else:
            skillfiles.to_trash(d, f"{p.name}-{name}")


def drop_key(section, key):
    def change(data):
        if isinstance(data.get(section), dict):
            data[section].pop(key, None)
    return change


def set_key(section, key, value):
    def change(data):
        data.setdefault(section, {})[key] = value
    return change


def marketplace_of(pid):
    """The marketplace part of a plugin id, plugin@marketplace."""
    return pid.rpartition("@")[2]


def names(people):
    return ", ".join(p.name for p in people)


def displays(people):
    return ", ".join(p.display for p in people)


def singular(kind):
    return kind.removesuffix("s")
