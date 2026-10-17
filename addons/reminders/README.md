# Reminders

An Apple Shortcut on a person's iPhone keeps their Apple Reminders in step with their assistant, with no Apple sign-in on the machine. Signing in to iCloud from a server works for a while, until Apple starts turning that machine away. So the phone does the work instead. Each time the shortcut runs, it posts the person's open reminders to the relay with their reminders key. In the same round trip, it gets back the reminders their assistant queued, and adds them on the phone.

The machine keeps the latest open reminders in `state/reminders/<telegram id>.json`: `updated`, the Unix time of the sync, and `reminders`. Each reminder has its `title` and whichever of `list`, `notes`, `due`, `priority`, `flagged` and `completed` it has. What waits to be added is in `<telegram id>.outbox.json`, along with the last 50 that went. What the last sync sent, as it came, is in `<telegram id>.sent.txt`. The relay passes on only the key's SHA-256. The machine keeps only those hashes, in `state/reminders-keys.json`, and turns an unknown one away with a 401.

The snapshot is only as fresh as the last sync. With the automations below, that's a few times a day and whenever the person closes Reminders. A queued reminder reaches the phone at the next sync. When the machine is offline or slow to answer, the shortcut hears there's nothing to add, and what's queued waits for the following sync.

## What it needs

- The relay, set up as in the main README ("The control panel and the relay").
- The Shortcuts app on the person's iPhone.

## Setup

1. `assistant addon enable reminders`, then `assistant relay deploy`, which mounts the add-in's route on the relay.
2. `assistant reminders key <name>` prints that person's key once. `assistant reminders revoke <name>` forgets it.
3. The shortcut. In Shortcuts, make a new shortcut named **Sync Reminders** with these actions, in this order:
   1. **Find Reminders**. Add a filter: **Is Completed**, switched off. Leave **Limit** off. This finds the open reminders in every list.
   2. **Repeat with Each** item in **Reminders**, and inside it:
      - **Dictionary** with these Text keys, each value the **Repeat Item** variable showing a different detail (tap the variable to pick one):
        - `list`: **List**
        - `title`: **Title**
        - `notes`: **Notes**
        - `due`: **Due Date**, then tap it again and set **Date Format** to **ISO 8601**, with the time on
        - `flagged`: **Is Flagged** (optional)
   3. After **End Repeat**: **Get Contents of URL**:
      - URL `https://<relay address>/reminders`: take `relay_url` in `state/relay.json`, with `https` for `wss` and no `/queue`.
      - Under **Show More**: Method **POST**.
      - Header `Authorization` with the value `Bearer <key>`.
      - Request Body **JSON**, with one Text field `reminders` whose value is **Repeat Results**.
   4. **Get Dictionary Value**: Value for `add` in **Contents of URL**.
   5. **Repeat with Each** item in **Dictionary Value**, and inside it:
      - **If** **Repeat Item** (tap it and set **Key** to `list`) **is** the name of one of the person's lists:
        - **Add New Reminder**: the title is **Repeat Item** with Key `title`, in that list, and under **Show More** the Notes are **Repeat Item** with Key `notes`.
      - **Otherwise**: the same **Add New Reminder**, in the list they use most. With more lists, nest one more **If** per list in **Otherwise**.
4. Two personal automations, under Automation in Shortcuts:
   - **Time of Day**, for example 07:30, Daily, **Run Immediately**, with the action **Run Shortcut** › Sync Reminders. Make one each for 12:00, 17:00 and 21:00 too.
   - **App** › Reminders › **Is Closed**, **Run Immediately**, with **Run Shortcut** › Sync Reminders.
5. Run the shortcut once by hand. Then `assistant reminders list --user <name>` should show the open reminders, and `state/reminders/<telegram id>.sent.txt` shows exactly what the shortcut sent.

What the shortcut sends is read leniently. A list of dictionaries turned into text, as above, arrives as JSON objects one after another. A real JSON array works too, and so does plain text, one title per line. Field names may have any case, and empty fields are left out.

Not covered:

- **Ticking off.** Shortcuts' Reminders actions find, add and remove reminders, but none of them clearly marks one completed, so the add-in only adds. A reminder ticked off on the phone drops out of the next snapshot.
- **Due dates on the phone.** Each queued reminder carries its `due` time, if it has one, but the steps above don't set it. The assistants are told to put a time that matters into the title.

## Using it

```sh
assistant reminders list [--user NAME]                     # the open reminders and when they were synced
assistant reminders add [--user NAME] --list LIST [--notes TEXT] [--due ISO-8601] TITLE
assistant reminders queue [--user NAME]                    # what waits for the next sync, and what went lately
assistant reminders cancel [--user NAME] ID
```

`--user` defaults to `$ASSISTANT_USER`, which every assistant's session has.

## Files

- `lib/multiclaude/addons/reminders/`: in `__init__`, the add-in's hook. `sync` handles the relay's syncs and the keys. `snapshot` reads what the shortcut sent and keeps the latest. `outbox` holds what waits to be added.
- `lib/multiclaude/keyring.py`: the keys, kept as hashes (shared with the siri and locations add-ins).
- `relay/src/reminders.js`: the relay's routes, mounted while this is on.
- `addons/reminders/assistant-reminders`: `assistant reminders`.
