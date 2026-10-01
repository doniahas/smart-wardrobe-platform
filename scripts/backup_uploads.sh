#!/usr/bin/env bash
# Back up the wardrobe/Explore/DM uploaded-media directory (the
# ./services/web/uploads bind mount in docker-compose.yml) as a plain
# tar.gz archive. No Docker involvement needed - it's a host directory.
#
# Not scheduled automatically - see docs/deployment.md for the operator
# steps (cron/systemd wiring, confirming UPLOADS_DIR sits on durable
# storage, and where the off-host copy goes).
#
# Usage:
#   ./scripts/backup_uploads.sh
#
# Configuration (env vars):
#   UPLOADS_DIR             default: ./services/web/uploads - on a server
#                           this must point at the actual bind-mount host
#                           path, and MUST be verified to live on durable
#                           storage before relying on this in production,
#                           see docs/deployment.md
#   BACKUP_DIR              default: ./backups/uploads
#   BACKUP_RETENTION_DAYS   default: 7 - local backups older than this are
#                           pruned after a successful new backup; off-host
#                           copies are a separate, manual step

set -euo pipefail

UPLOADS_DIR="${UPLOADS_DIR:-./services/web/uploads}"
BACKUP_DIR="${BACKUP_DIR:-./backups/uploads}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"

if [[ ! -d "$UPLOADS_DIR" ]]; then
  echo "ERROR: UPLOADS_DIR '$UPLOADS_DIR' does not exist." >&2
  exit 1
fi

if [[ -z "$(find "$UPLOADS_DIR" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "WARNING: '$UPLOADS_DIR' is empty - proceeding anyway (an empty" >&2
  echo "archive is still a valid, honest backup of 'nothing uploaded yet')." >&2
fi

mkdir -p "$BACKUP_DIR"

timestamp="$(date +%Y%m%d-%H%M%S)"
archive_name="uploads-${timestamp}.tar.gz"
host_path="${BACKUP_DIR}/${archive_name}"

echo "Backing up '$UPLOADS_DIR' -> $host_path"

tar czf "$host_path" -C "$(dirname "$UPLOADS_DIR")" "$(basename "$UPLOADS_DIR")"

echo "Backup complete: $host_path ($(du -h "$host_path" | cut -f1))"

echo "Pruning backups older than ${BACKUP_RETENTION_DAYS} days in $BACKUP_DIR ..."
find "$BACKUP_DIR" -maxdepth 1 -name 'uploads-*.tar.gz' -mtime "+${BACKUP_RETENTION_DAYS}" -print -delete

echo "Reminder: this backup is only as safe as $BACKUP_DIR - copy it off-host"
echo "periodically (see docs/deployment.md). This script does not do that."
