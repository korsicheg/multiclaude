# Model fallback

When a model's safety classifier stops one of an assistant's replies, its person is told, with the classifier's category and how to switch back. Claude Code moves the conversation to another model when that happens and stays there; it logs a `model_refusal_fallback` entry in the transcript, which the router reads.

If the person's `users.json` entry has `"restore_model": true`, the router also switches them back to the model's family (`/model opus`, for this session only) once the assistant is idle. If the model is stopped again within 30 minutes of that, the conversation itself keeps setting it off, so it stays on the other model and suggests `/clear`. Fallbacks logged before the router started are ignored.

## What it needs

Nothing beyond the core.

## Setup

1. `assistant addon enable model-fallback`. It takes effect at once.
2. A person turns switching back on with `"restore_model": true` in their `users.json` entry, or with **Back to my model** in the control panel (`/config`), which shows it while this add-in is on.

## Files

- `lib/multiclaude/addons/model_fallback.py`: spotting the fallback in the transcript, the message, the switch back and the control panel's setting.
