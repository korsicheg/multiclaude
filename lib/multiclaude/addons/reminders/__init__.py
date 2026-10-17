"""Reminders (add-in reminders): an Apple Shortcut on a person's phone keeps their Apple Reminders in
step with the machine through the relay (relay/src/reminders.js), with no Apple sign-in here, which
Apple may answer by turning the machine away. Each sync posts their open reminders, kept as the
latest snapshot (snapshot.py), and takes back the reminders their assistant queued to add on the
phone (outbox.py); sync.py handles the relay's "reminders" envelopes. `assistant reminders`
(addons/reminders/assistant-reminders) makes and revokes keys, and reads and queues reminders."""
from multiclaude.addons.reminders import sync


def relay_envelopes():
    return {"reminders": sync.handle}
