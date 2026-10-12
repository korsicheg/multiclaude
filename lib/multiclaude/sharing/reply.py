"""What a /plugin, /skills or /mcp command sends back through the router."""
from dataclasses import dataclass, field

from multiclaude.telegram import RICH_LIMIT


@dataclass
class Reply:
    text: str = ""
    reload: bool = False                  # type /reload-plugins once the assistant is idle
    session: list = None                  # /mcp: the in-session commands to type instead
    copy: list = field(default_factory=list)  # what the person may send next, each under a 📋 button

    def as_json(self):
        out = {"text": truncate(self.text), "reload": self.reload}
        if self.session is not None:
            out["session"] = self.session
        if self.copy:
            out["copy"] = self.copy
        return out


def status(result, extra=""):
    """✅ or ⚠️ with what claude said."""
    return f"✅ {result.message}{extra}" if result.ok else f"⚠️ {result.message}"


def truncate(text):
    if len(text) <= RICH_LIMIT:
        return text
    return text[:RICH_LIMIT].rsplit("\n", 1)[0] + "\n…"
