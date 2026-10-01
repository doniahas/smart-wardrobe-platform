#!/usr/bin/env bash
# Back up the smartwardrobe MongoDB database via mongodump, run
# inside the already-running mongo container (no extra tooling needed -
# mongodump ships with the mongo:7 image already in use).
#
# Safe by default: every credential and path is required via environment
# variables, with no insecure fallback for MONGO_ROOT_USERNAME/PASSWORD -
# same "fail loudly, never silently default a secret" philosophy already
# used for JWT_SECRET in services/web/app/core/config.py.
#
# Not scheduled automatically. Wiring this into cron/systemd, and
# confirming BACKUP_DIR resolves to durable storage on the host, are
# operator responsibilities - see docs/deployment.md.
#
# Usage:
#   MONGO_ROOT_USERNAME=... MONGO_ROOT_PASSWORD=... ./scripts/backup_mongo.sh
#
# Configuration (env vars):
#   MONGO_ROOT_USERNAME     required, no default - the same value used to
#                           create the root user (see docs/deployment.md)
#   MONGO_ROOT_PASSWORD     required, no default
#   MONGO_CONTAINER         default: smartwardrobe_mongo
#   BACKUP_DIR              default: ./backups/mongo - MUST be verified to
#                           live on durable host storage before relying on
#                           this in production, see docs/deployment.md
#   BACKUP_RETENTION_DAYS   default: 7 - local backups older than this are
#                           pruned after a successful new backup; off-host
#                           copies (not handled by this script) are a
#                           separate, manual step, see docs/deployment.md

set -euo pipefail

MONGO_CONTAINER="${MONGO_CONTAINER:-smartwardrobe_mongo}"
BACKUP_DIR="${BACKUP_DIR:-./backups/mongo}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"

if [[ -z "${MONGO_ROOT_USERNAME:-}" || -z "${MONGO_ROOT_PASSWORD:-}" ]]; then
  echo "ERROR: MONGO_ROOT_USERNAME and MONGO_ROOT_PASSWORD must both be set." >&2
  echo "Refusing to guess or fall back to an empty/default credential." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not available on PATH." >&2
  exit 1
fi

if [[ "$(docker inspect -f '{{.State.Running}}' "$MONGO_CONTAINER" 2>/dev/null || true)" != "true" ]]; then
  echo "ERROR: container '$MONGO_CONTAINER' is not running." >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

timestamp="$(date +%Y%m%d-%H%M%S)"
archive_name="mongo-${timestamp}.archive.gz"
container_tmp_path="/tmp/${archive_name}"
host_path="${BACKUP_DIR}/${archive_name}"

echo "Backing up '$MONGO_CONTAINER' -> $host_path"

docker exec "$MONGO_CONTAINER" mongodump \
  --username "$MONGO_ROOT_USERNAME" \
  --password "$MONGO_ROOT_PASSWORD" \
  --authenticationDatabase admin \
  --archive="$container_tmp_path" \
  --gzip

docker cp "${MONGO_CONTAINER}:${container_tmp_path}" "$host_path"
docker exec "$MONGO_CONTAINER" rm -f "$container_tmp_path"

echo "Backup complete: $host_path ($(du -h "$host_path" | cut -f1))"

echo "Pruning backups older than ${BACKUP_RETENTION_DAYS} days in $BACKUP_DIR ..."
find "$BACKUP_DIR" -maxdepth 1 -name 'mongo-*.archive.gz' -mtime "+${BACKUP_RETENTION_DAYS}" -print -delete

echo "Reminder: this backup is only as safe as $BACKUP_DIR - copy it off-host"
echo "periodically (see docs/deployment.md). This script does not do that."
