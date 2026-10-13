"""Reading and writing the JSON files the assistants share with each other and with Claude Code."""
import contextlib
import fcntl
import json
import os
import time
from pathlib import Path


class NotAnObject(Exception):
    """A file that should hold a JSON object doesn't. It's left alone rather than overwritten."""

    def __init__(self, path):
        super().__init__(f"{path} isn't valid JSON")
        self.path = path


def read_json(path, default=None):
    """The file's JSON, or default if it's missing or doesn't parse."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_object(path):
    """A JSON object from a file, {} if there's no file; NotAnObject if it holds anything else."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError:
        raise NotAnObject(path) from None
    if not isinstance(data, dict):
        raise NotAnObject(path)
    return data


def write_json(path, data):
    """Write atomically, keeping the file's permissions: readers see the old file or the new
    one, never half of one."""
    write_atomically(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def write_lines(path, records):
    """Write each record as a line of JSON (JSON Lines), atomically like write_json."""
    write_atomically(path, "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records))


def write_atomically(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def edit_object(path, change, create=True):
    """Apply change(data) to the JSON object in a file and write it back if that changed it.
    Without create, a missing file is written only if the change left something in it."""
    path = Path(path)
    existed = path.exists()
    data = read_object(path)
    before = json.dumps(data, sort_keys=True)
    change(data)
    if json.dumps(data, sort_keys=True) == before:
        return False
    if not existed and not create and not any(data.values()):
        return False
    write_json(path, data)
    return True


@contextlib.contextmanager
def locked(path, wait=None):
    """Hold an exclusive lock on path: wait=None waits as long as it takes, otherwise
    TimeoutError after wait seconds."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as lock:
        deadline = None if wait is None else time.time() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if deadline is None else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise TimeoutError(f"{path} is held") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
