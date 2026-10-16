# Locations

A location or place a person shares on Telegram reaches their assistant, listed under the message's attachments with its coordinates. Their latest position, a live location's updates included, is kept in `state/locations/<telegram id>.json` (`latitude`, `longitude`, `accuracy_m`, `heading`, `message_id`, `live_until`, `updated`). A live location's updates only change that file and never reach the assistant, so tools can follow a person without costing a turn per update. Each session gets its person's file as `ASSISTANT_LOCATION`, for tools and plugins that use it.

The [OwnTracks](https://owntracks.org) app can keep the same file up to date from a person's phone, without signing in to Apple or Google from the machine. It posts each position to the relay with the person's OwnTracks key; the relay passes on only the key's SHA-256, and the machine keeps only those hashes (`state/owntracks-keys.json`) and turns an unknown one away, which the app shows as a 401. An OwnTracks position adds `source` "owntracks", and, when the app sends them, `velocity_kmh`, `battery`, `regions` (the app's regions the phone is in) and `trigger`; entering or leaving a region also gives `event` (`enter` or `leave`) and `region`. Whichever is newer, an OwnTracks position or a Telegram live location, is the one kept.

The Telegram plugin drops locations, so `bin/patch-telegram-plugin` adds handlers for them while this add-in is on, and takes them out again when it's turned off.

## What it needs

Nothing beyond the core. OwnTracks needs the relay, set up as in the main README ("The control panel and the relay"), and the OwnTracks app on the person's phone.

## Setup

1. `assistant addon enable locations`.
2. `assistant restart`. The front desk patches the plugin as it starts, and each assistant gets `ASSISTANT_LOCATION` as it starts.
3. For OwnTracks: `assistant relay deploy` once, which mounts its routes on the relay. Then `assistant location key <name>` prints that person's key once (`assistant location revoke <name>` forgets it). In OwnTracks, under Settings: **Mode** HTTP, **URL** `https://<relay address>/owntracks` (`relay_url` in `state/relay.json`, with `https` for `wss` and no `/queue`), **Authentication** on, any **UserID**, the key as **Password**. Or let the app import these settings from a `.otrc` file opened on the phone:

   ```json
   {"_type": "configuration", "mode": 3, "url": "https://<relay address>/owntracks", "auth": true,
    "username": "<name>", "password": "<key>", "tid": "<two letters>", "monitoring": 1, "extendedData": true}
   ```

   Monitoring 1 (Significant) reports a move of about 500 m, or every few minutes while moving, and is light on the battery; 2 (Move) reports more often and uses more. Regions set in the app (home, the office) are reported as they're entered and left.

## Files

- `lib/multiclaude/addons/locations/`: in `__init__`, the attachment line, the `/help` entry and the add-in's hooks; `owntracks` (OwnTracks' updates from the relay, and its keys).
- `lib/multiclaude/keyring.py`: the keys, kept as hashes (shared with the siri add-in).
- `relay/src/owntracks.js`: the relay's routes, mounted while this is on.
- `addons/locations/assistant-location`: `assistant location key|revoke`.
- `bin/patch-telegram-plugin`: the plugin patch (`multiclaude: locations`).
- `bin/run-user.sh`: sets `ASSISTANT_LOCATION` while this is on.
