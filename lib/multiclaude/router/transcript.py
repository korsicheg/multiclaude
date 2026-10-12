"""Reading Claude Code's JSONL transcripts."""
import glob
import json
import os
import re

CLEAR_MARKER = "<command-name>/clear</command-name>"
# Claude Code's note when Escape stops a turn: "[Request interrupted by user]", or "… for tool use]".
INTERRUPTED_MARKER = "[Request interrupted by user"
RESULT_RE = re.compile(r"<local-command-(stdout|stderr)>(.*?)</local-command-\1>", re.S)


class Tail:
    """Follows the transcripts in one Claude Code project folder."""

    def __init__(self, project_dir):
        self.dir = str(project_dir)
        self.offsets = {}  # transcript path -> bytes already read

    def files(self):
        return glob.glob(f"{self.dir}/*.jsonl")

    def newest(self):
        files = self.files()
        return max(files, key=os.path.getmtime) if files else None

    def sizes(self):
        return {p: os.path.getsize(p) for p in self.files()}

    def skip_existing(self):
        self.offsets.update(self.sizes())

    def read(self, path):
        """The entries written to path since the last read; a half-written last line waits."""
        offset = self.offsets.get(path, 0)
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return []
        end = data.rfind(b"\n") + 1
        self.offsets[path] = offset + end
        return list(parse_lines(data[:end].splitlines()))

    def read_new(self):
        """New entries in every transcript of the folder."""
        out = []
        for path in self.files():
            if os.path.getsize(path) != self.offsets.get(path, 0):
                out.extend(self.read(path))
        return out

    def latest_subagent_write(self):
        """When the session's subagents last wrote to their own transcripts, 0 if never."""
        latest = 0.0
        for path in glob.glob(f"{self.dir}/*/subagents/*.jsonl"):
            try:
                latest = max(latest, os.path.getmtime(path))
            except OSError:
                pass
        return latest

    def command_result(self, command, sizes):
        """The output Claude Code logged for a slash command since the transcripts had these sizes."""
        for path in self.files():
            try:
                with open(path, "rb") as f:
                    f.seek(sizes.get(path, 0))
                    lines = f.read().splitlines()
            except OSError:
                continue
            if (result := find_command_result(parse_lines(lines), command)) is not None:
                return result
        return None

    def written_since(self, sizes, marker):
        """Whether text containing marker was written to the transcripts since they had these sizes."""
        for path in self.files():
            try:
                with open(path, errors="replace") as f:
                    f.seek(sizes.get(path, 0))
                    if marker in f.read():
                        return True
            except OSError:
                pass
        return False

    def new_file_with(self, before, marker):
        """A transcript started since `before` (a set of paths) whose text contains marker."""
        for path in set(self.files()) - before:
            try:
                with open(path, errors="replace") as f:
                    if marker in f.read():
                        return path
            except OSError:
                pass
        return None


def parse_lines(lines):
    for line in lines:
        try:
            yield json.loads(line)
        except ValueError:
            continue  # half written (read again on the next poll), or not JSON


def find_command_result(entries, command):
    """The output that follows `command` in transcript entries, or None."""
    seen = False
    for entry in entries:
        # Some commands log their output as a system entry with top-level content.
        content = (entry.get("message") or {}).get("content") or entry.get("content")
        if not isinstance(content, str):
            continue
        if f"<command-name>{command}</command-name>" in content:
            seen = True
        elif seen and (m := RESULT_RE.search(content)):
            return m[2].strip()
    return None
