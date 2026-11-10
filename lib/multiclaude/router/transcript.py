"""Reading Claude Code's JSONL transcripts."""
import glob
import json
import os
import re
import time

CLEAR_MARKER = "<command-name>/clear</command-name>"
# Claude Code's note when Escape stops a turn: "[Request interrupted by user]", or "… for tool use]".
INTERRUPTED_MARKER = "[Request interrupted by user"
RECENT = 4 * 1024 * 1024  # bytes at the end of a transcript read to pick up a session's state (Tail.recent)
SETTLED_AFTER = 60 * 60   # a transcript nothing has written to for this long is no longer followed (Tail.settle)
SETTLE_EVERY = 60         # how often the transcripts are sorted into followed and settled
RESULT_RE = re.compile(r"<local-command-(stdout|stderr)>(.*?)</local-command-\1>", re.S)


class Tail:
    """Follows the transcripts in one Claude Code project folder."""

    def __init__(self, project_dir):
        self.dir = str(project_dir)
        self.offsets = {}  # transcript path -> bytes already read, of the transcripts it follows
        self.settled = {}  # the same of those gone quiet, which are looked at only now and then (settle)
        self.next_settle = 0.0

    def files(self):
        return glob.glob(f"{self.dir}/*.jsonl")

    def newest(self):
        files = self.files()
        return max(files, key=os.path.getmtime) if files else None

    def sizes(self):
        return {p: os.path.getsize(p) for p in self.files()}

    def skip_existing(self):
        self.settled.clear()
        self.offsets.update(self.sizes())

    def recent(self, path, telling, window=RECENT):
        """The last entries of path, without reading it whole: those of the lines that start in
        its last `window` bytes, or in four times as many each time none of them is one that
        telling(entry) holds for, up to the whole file. A conversation resumed with --continue
        only grows, pictures included, and read whole it took several times its size in memory."""
        while True:
            try:
                start = max(0, os.path.getsize(path) - window)
                if start:
                    with open(path, "rb") as f:
                        f.seek(start - 1)  # from the byte before, so a line starting right at the window is kept
                        start += len(f.readline()) - 1  # the rest of the line the window cuts through
            except OSError:
                return []
            self.offsets[path] = start
            entries = self.read(path)
            if not start or any(telling(entry) for entry in entries):
                return entries
            window *= 4

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

    def read_new(self, now=None):
        """New entries in every transcript of the folder that is followed."""
        now = time.time() if now is None else now
        if now >= self.next_settle:
            self.settle(now)
        out = []
        for path in self.files():
            if path not in self.settled and os.path.getsize(path) != self.offsets.get(path, 0):
                out.extend(self.read(path))
        return out

    def settle(self, now):
        """Stop following the transcripts read to their end that nothing has written to for
        SETTLED_AFTER, so a folder's old conversations aren't looked at on every poll, or kept
        with the offsets a restart resumes from. The newest stays followed however old it is,
        since a resumed conversation goes on in it. One that grew after all (a conversation
        resumed by hand) is followed again, from where it was read to."""
        self.next_settle = now + SETTLE_EVERY
        for path, offset in list(self.settled.items()):
            if (size := size_of(path)) is None:
                del self.settled[path]  # deleted
            elif size != offset:
                self.offsets[path] = self.settled.pop(path)
        written = {path: os.path.getmtime(path) for path in self.offsets if os.path.exists(path)}
        newest = max(written, key=written.get, default=None)
        for path, at in written.items():
            if path != newest and now - at > SETTLED_AFTER and size_of(path) == self.offsets[path]:
                self.settled[path] = self.offsets.pop(path)

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


def size_of(path):
    try:
        return os.path.getsize(path)
    except OSError:
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
