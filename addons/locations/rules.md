## Locations

- A location or place your person shares on Telegram is listed under Attachments as `location: lat, lon (…)`. Their latest shared position is always in `{{ROOT}}/state/locations/<telegram id>.json` (`latitude`, `longitude`, `accuracy_m`, `live_until`, `updated`), and `$ASSISTANT_LOCATION` names that file. For a live location the note names that file too: its later positions only update the file and never reach you.
