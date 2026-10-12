"""Moving skill folders between workspaces, ~/.claude/skills and the trash."""
import os
import shutil
import time

from multiclaude import paths

SCRATCH = paths.STATE / "shared-tmp"
TRASH = paths.STATE / "shared-trash"
TRASH_KEEP = 30 * 86400  # trashed skill folders are deleted after this many seconds


def copy_skill(src, dst):
    """Copy a skill folder whole or not at all: a half-copied one would be taken for the real thing."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    tmp = SCRATCH / f"{dst.name}.{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        shutil.copytree(src, tmp)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(tmp, dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def to_trash(path, label):
    """Move a skill folder into TRASH instead of deleting it."""
    TRASH.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = TRASH / f"{stamp}-{label}"
    n = 1
    while dst.exists():
        n += 1
        dst = TRASH / f"{stamp}-{label}-{n}"
    shutil.move(path, dst)


def old_trash():
    """Trashed folders older than TRASH_KEEP."""
    return [d for d in (TRASH.glob("*") if TRASH.is_dir() else []) if time.time() - d.stat().st_mtime > TRASH_KEEP]
