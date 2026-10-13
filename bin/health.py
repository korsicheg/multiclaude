#!/usr/bin/env python3
"""The machine's health (lib/multiclaude/health): CPU, memory, swap, disks, the router and each
person's assistant. Cron runs `sample` every 5 minutes, which keeps a week of samples and tells the
owner when something runs high or stops, and when it's fine again; `status` prints the machine now.

Usage: health.py [status] | sample
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from multiclaude.health import sample, status  # noqa: E402


def main(args):
    command = args[0] if args else "status"
    if command == "sample":
        sample.run()
    elif command == "status":
        print(status.report())
    else:
        print(__doc__.strip())
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
