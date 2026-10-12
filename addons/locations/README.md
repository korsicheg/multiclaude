# Locations

A location or place a person shares on Telegram reaches their assistant, listed under the message's attachments with its coordinates. Their latest position, a live location's updates included, is kept in `state/locations/<telegram id>.json` (`latitude`, `longitude`, `accuracy_m`, `heading`, `message_id`, `live_until`, `updated`). A live location's updates only change that file and never reach the assistant, so tools can follow a person without costing a turn per update. Each session gets its person's file as `ASSISTANT_LOCATION`, for tools and plugins that use it.

The Telegram plugin drops locations, so `bin/patch-telegram-plugin` adds handlers for them while this add-in is on, and takes them out again when it's turned off.

## What it needs

Nothing beyond the core.

## Setup

1. `assistant addon enable locations`.
2. `assistant restart`. The front desk patches the plugin as it starts, and each assistant gets `ASSISTANT_LOCATION` as it starts.

## Files

- `lib/multiclaude/addons/locations.py`: the attachment line and the `/help` entry.
- `bin/patch-telegram-plugin`: the plugin patch (`multiclaude: locations`).
- `bin/run-user.sh`: sets `ASSISTANT_LOCATION` while this is on.
