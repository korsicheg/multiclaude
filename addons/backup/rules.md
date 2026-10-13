## Backup

Every night at 02:30 UTC, `assistant backup` saves everyone's folder, memory, chat history, settings and MCP servers, and the bot's own settings and crontab, in an encrypted restic backup off this machine. The last 7 daily, 4 weekly and 12 monthly backups are kept.

- To get back an older version of your person's file, memory or settings, list the backups with `assistant backup restic snapshots`. Then restore into a scratch folder: `assistant backup restic restore <snapshot or latest> --target <scratch folder> --include <full path>`. The files land under the target with their full paths. Copy back what's needed, and only with your person's OK when it replaces a file they use.
- Restore only your person's own paths: the backup holds everyone's.
- Where it goes and its password are the owner's (`{{ROOT}}/state/backup.json`, `{{ROOT}}/state/backup-password`). Never show the password, and change them only when the owner asks. `{{ROOT}}/addons/backup/README.md` has the rest, including how to restore a whole machine.
