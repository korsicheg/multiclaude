#!/usr/bin/env python3
"""Plugins, skills and marketplaces shared by everyone's assistants; see
lib/multiclaude/sharing for how. Cron runs `sync` every 10 minutes, assistants run it after
installing something, and the router runs `command` for /plugin, /skills and /mcp.

Usage: shared.py sync [--dry-run] | list | command <name> </plugin, /skills or /mcp text>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from multiclaude import jsonfile, paths  # noqa: E402
from multiclaude.log import log  # noqa: E402
from multiclaude.sharing import commands, listing  # noqa: E402
from multiclaude.sharing.sync import Sync  # noqa: E402


def main(args):
    cmd = args[0] if args else "help"
    if cmd == "list":
        listing.show()
    elif cmd == "sync":
        try:
            with jsonfile.locked(paths.SETTINGS_LOCK, wait=0):
                Sync(dry_run="--dry-run" in args[1:]).run()
        except TimeoutError:
            log("another sync is running")
    elif cmd == "command" and len(args) >= 3:
        with jsonfile.locked(paths.SETTINGS_LOCK):  # one change at a time, and never during a sync
            print(json.dumps(commands.run(args[1], " ".join(args[2:])).as_json()))
    else:
        print(__doc__.strip())


if __name__ == "__main__":
    main(sys.argv[1:])
