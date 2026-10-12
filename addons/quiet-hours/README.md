# Quiet hours

During a person's quiet hours, everything the bot sends them on Telegram arrives silently, with no sound or vibration (`disable_notification`): the router's messages, their assistant's `tg`, scheduled jobs and `assistant notify` alike. The hours are in the person's own timezone and may pass midnight. `users.json` is read on every send, so a change takes effect at once.

Not covered: the Telegram plugin's own few messages (the "Paired!" reply) and Telegram's notices about reactions. Edits never notify anyway. Slack has no quiet hours.

## What it needs

Nothing beyond the core.

## Setup

1. `assistant addon enable quiet-hours`. It takes effect at once.
2. Give a person hours in their `users.json` entry, `"timezone": "Europe/Berlin", "quiet_hours": "00:00-09:00"`, or let them set it in the control panel (`/config`), which shows a Quiet hours section while this add-in is on. Their assistant can set it for them too. Without a timezone the hours are in UTC.

## Files

- `lib/multiclaude/addons/quiet_hours.py`: the check, run before every message is posted, and the control panel's setting.
