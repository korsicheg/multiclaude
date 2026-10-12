"""`assistant shared list`: what's shared, and what each person has in their own folder."""
from multiclaude import jsonfile
from multiclaude.sharing.people import load_people
from multiclaude.sharing.sync import MANIFEST


def show():
    manifest = jsonfile.read_object(MANIFEST)
    print("Shared by everyone (~/.claude):")
    for kind in ("marketplaces", "plugins", "skills"):
        print(f"  {kind}: {', '.join(manifest.get(kind, [])) or '-'}")
    for p in load_people():
        off = sorted(p.disabled_plugins) + [f"skill {k}" for k, v in p.merged("skillOverrides").items() if v == "off"]
        print(f"{p.name} (~/workspaces/{p.name}/.claude):")
        print(f"  marketplaces: {', '.join(sorted(p.marketplaces)) or '-'}")
        print(f"  plugins: {', '.join(sorted(p.enabled_plugins)) or '-'}")
        print(f"  skills: {', '.join(sorted(p.skills)) or '-'}")
        if off:
            print(f"  turned off: {', '.join(sorted(off))}")
