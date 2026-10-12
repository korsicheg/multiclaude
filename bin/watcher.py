#!/usr/bin/env python3
"""The router (assistant-watcher.service); see lib/multiclaude/router and ARCHITECTURE.md."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from multiclaude.router.desk import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
