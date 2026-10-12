# Skill notes

Whenever an assistant loads a skill, its person gets a quiet note in their chat, "🧩 Loaded the `clean-code:clean-code-python` skill", in the app they last wrote from. The router reads it from the Skill tool calls in the assistant's transcript. Skills loaded before the router started aren't noted.

## What it needs

Nothing beyond the core.

## Setup

`assistant addon enable skill-notes`. It takes effect at once.

## Files

- `lib/multiclaude/addons/skill_notes.py`: spotting a loaded skill and the note.
