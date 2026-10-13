# Backup

Every night, everything on the machine that isn't in the repo goes into an encrypted [restic](https://restic.net) backup somewhere else: everyone's settings, memory, chat history and files, and the bot's own settings. A broken or lost VM can then be set up again as it was.

restic encrypts the backups with a password kept in `~/assistant/state/backup-password`, and stores each file only once, so a night that changed little adds little. It keeps the last 7 daily, 4 weekly and 12 monthly backups.

## What it keeps

What the main README lists under "What's not in this repo":

- `~/assistant/.env`, `config.json`, `users.json` and `state/`, with a copy of the crontab (`state/backup/crontab`)
- `~/.claude`: everyone's chat history and memory, the bot token and Telegram allowlist, the shared plugins, skills and settings, and Claude Code's login
- `~/.claude.json`: everyone's MCP servers
- `~/workspaces`: everyone's folder, with their settings, files and logins
- `~/.config/claude-usage`

It leaves out caches that come back by themselves: `.cache` folders, browsers' caches, Claude Code's cache, Python venvs and `node_modules`.

## What it needs

- restic: `sudo apt install restic`.
- Somewhere to keep the backups that isn't this machine: any [restic repository](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html), such as S3, Backblaze B2 or SFTP, or anything [rclone](https://rclone.org) reaches (`rclone:<remote>:<path>`), such as Google Drive or iCloud Drive.

## Setup

1. Say where the backups go, in `~/assistant/state/backup.json`:

   ```json
   {"repository": "rclone:icloud:Claudique backup", "rclone": "/home/ubuntu/workspaces/ada/bin/rclone"}
   ```

   `repository` is the restic repository. `rclone` is optional: it names the rclone program for an `rclone:` repository, such as a person's own wrapper with their own rclone config. Without it, restic runs `rclone` from the PATH with `~/.config/rclone`. Credentials restic reads from the environment (for S3, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`) go in `~/assistant/.env`.
2. `assistant addon enable backup`. The first time, it makes the password. It starts the repository if it's new, and it adds a cron line that runs `assistant backup` every night at 02:30 UTC.
3. **Keep a copy of the password off this machine**, in a password manager. Without it the backups can't be read, and the copy in `state/` is lost with the machine.
4. `assistant backup` makes the first backup now.

A failed night is tried again the next night. Once the last good backup is two days old, the owner gets a Telegram message every night, with restic's error, until a backup works again. The log is `~/assistant/logs/backup.log`.

`assistant addon disable backup` takes the cron line out. The repository and the password stay.

## Getting files back

`assistant backup restic <command>` runs any restic command on the repository:

```sh
assistant backup restic snapshots                                  # the backups there are
assistant backup restic ls latest /home/ubuntu/workspaces/ada      # what one of them holds
assistant backup restic restore latest --target ~/restored --include /home/ubuntu/workspaces/ada/notes.md
```

A restore puts the files under the target folder with their full paths, so the example above writes `~/restored/home/ubuntu/workspaces/ada/notes.md`. Copy back from there what you need.

## Restoring a new machine

The backups hold full paths, so restore as the same user with the same home folder (`ubuntu` on most cloud images).

1. Prepare the machine as the main README says ("Preparing a fresh VM"), and get the code (Setup, step 1). Skip `assistant setup`, since its settings come back with the backup.
2. `sudo apt install restic`. For an `rclone:` repository, install rclone too and set its remote up again with `rclone config`, under the same name.
3. Restore the latest backup. restic asks for the password:

   ```sh
   export RESTIC_REPOSITORY='rclone:icloud:Claudique backup'   # the repository in state/backup.json
   restic restore latest --target /
   ```

4. Put the scheduled jobs back, then set the machine up for what came back, and start the bot:

   ```sh
   crontab ~/assistant/state/backup/crontab
   ~/assistant/bin/assistant install
   assistant start
   ```

Everyone's assistant resumes its conversation. Logins with a limited life may need renewing, such as an iCloud Drive sign-in that Apple trusts for 30 days.

## Files

- `addons/backup/assistant-backup`: `assistant backup`, the nightly backup and `assistant backup restic`.
- `addons/backup/restic.sh`: shared by the add-in's scripts: its settings, its cron line, and restic run on the repository.
- `addons/backup/setup`: run by `assistant addon enable backup`. It makes the password, starts the repository and adds the cron line.
- `addons/backup/teardown`: run by `assistant addon disable backup`. It takes the cron line out.
