# Siri

A person can talk to their assistant through an Apple Shortcut, on an iPhone, a Mac or a HomePod. The shortcut posts the spoken words to the relay with the person's voice key. The relay holds the request while the router types the words into their assistant, and the answer the assistant gives with `tg voice` comes back for Siri to speak; it's posted in their Telegram chat too. Siri waits up to 25 seconds; a longer answer comes on Telegram. Nothing on the machine is reachable from the internet, and words said while the machine is offline wait at the relay.

The relay can't check a key, so it passes on the key's SHA-256. The machine keeps only those hashes (`state/voice-keys.json`) and turns an unknown one away.

## What it needs

- The relay, set up as in the main README ("The control panel and the relay").
- The Shortcuts app on each person's Apple device.

## Setup

1. `assistant addon enable siri`, then `assistant relay deploy`, which mounts Siri's routes on the relay.
2. `assistant voice key <name>` prints that person's key once. `assistant voice revoke <name>` forgets it.
3. The shortcut: **Ask for Input** (or **Dictate Text**) → **Get Contents of URL**: POST to `https://<relay address>/voice` (`relay_url` in `state/relay.json`, with `https` for `wss` and no `/queue`), header `Authorization: Bearer <key>`, JSON body `text` = the words → **Get Dictionary Value** `reply` → **Speak Text** or **Show Result**.

## Files

- `lib/multiclaude/addons/siri/`: `voice` (keys and the reply mailbox, `state/voice/`) and, in `__init__`, the words from the relay, `tg voice` and the add-in's hooks.
- `relay/src/voice.js`: the relay's routes, mounted while this is on.
- `addons/siri/assistant-voice`: `assistant voice key|revoke`.
