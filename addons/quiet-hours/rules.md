## Quiet hours

- When your person wants the bot silent at night, set `"quiet_hours"` (`"00:00-09:00"`, in their timezone) in their own entry in `{{ROOT}}/users.json`, with their `"timezone"`. During those hours everything the bot sends them on Telegram arrives without a sound. It takes effect at once; remove `quiet_hours` to turn it off. They can also set it in the control panel (`/config`).
- When your person asks what they can set up, list it under **Chat**: quiet hours, `"quiet_hours": "00:00-09:00"` in users.json; in those hours Telegram messages arrive without a sound.
