"""Skill notes (add-in skill-notes): a quiet note in a person's chat, "🧩 Loaded the X skill", whenever
their assistant loads a skill. Claude Code logs each one as a Skill tool call in the session's
transcript."""
from multiclaude import telegram


def on_transcript_entry(user, entry, now):
    for skill in loaded(entry):
        telegram.later(user.say, notice(skill), quiet=True)


def loaded(entry):
    """The skills a transcript entry loads, in order."""
    content = (entry.get("message") or {}).get("content")
    if entry.get("type") != "assistant" or not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
            if skill := (block.get("input") or {}).get("skill"):
                yield skill


def notice(skill):
    return f"🧩 Loaded the `{skill}` skill"
