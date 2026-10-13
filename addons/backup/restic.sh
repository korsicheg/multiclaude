# Sourced by the backup add-in's scripts: where its settings are, its cron line, and restic run on
# its repository.
# shellcheck shell=bash disable=SC2034  # the variables are for the scripts that source this
# shellcheck source-path=SCRIPTDIR source=../../bin/common.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../bin/common.sh"

BACKUP_SETTINGS="$ROOT/state/backup.json"  # {"repository": "<restic repository>", "rclone": "<rclone program>"}
BACKUP_PASSWORD="$ROOT/state/backup-password"
BACKUP_STATE="$ROOT/state/backup"  # the crontab's copy, when the last backup worked, restic's errors, the lock

BACKUP_COMMAND="$ROOT/bin/assistant backup"
BACKUP_CRON_COMMENT="# assistant: backup of the bot and everyone's files, every night at 02:30 UTC"
BACKUP_CRON_LINE="30 2 * * * $BACKUP_COMMAND >> $ROOT/logs/backup.log 2>&1"

# Credentials a repository needs (AWS_ACCESS_KEY_ID for S3, say) may be in the bot's .env.
load_env

backup_setting() { jq -r --arg k "$1" '.[$k] // empty' "$BACKUP_SETTINGS" 2>/dev/null; }

# on_repository ARGS…: restic ARGS on the backup's repository, with its password, through the
# settings' rclone program when it names one.
on_repository() {
  local rclone
  local -a options=()
  rclone=$(backup_setting rclone)
  [ -z "$rclone" ] || options=(-o "rclone.program=$rclone")
  RESTIC_REPOSITORY=$(backup_setting repository) RESTIC_PASSWORD_FILE="$BACKUP_PASSWORD" \
    restic "${options[@]}" "$@"
}
