#!/usr/bin/env bash
# Restore a mongodump archive (as produced by backup_mongo.sh) into
# the REAL, running smartwardrobe MongoDB - this is destructive: existing
# collections that exist in the backup are dropped and replaced.
#
# This is deliberately NOT the script to use to check whether a backup is
# restorable - use scripts/verify_backup.sh for that (it restores into a
# disposable, throwaway container, never this one). Only run this script
# during an actual disaster-recovery event, per docs/deployment.md.
#
# Safety:
#   - BACKUP_FILE must be given explicitly - there is no "latest" auto-
#     detection, so you can never restore the wrong file by omission.
#   - Requires typing the literal word "yes" at an interactive prompt
#     (not just Enter) before anything destructive happens.
#   - Refuses to run at all if BACKUP_FILE doesn't exist, or if required
#     credentials aren't set - fails loudly, never guesses.
#
# Usage:
#   MONGO_ROOT_USERNAME=... MONGO_ROOT_PASSWORD=... \
#     BACKUP_FILE=./backups/mongo/mongo-20260101-020000.archive.gz \
#     ./scripts/restore_mongo.sh
#
# Configuration (env vars):
#   BACKUP_FILE          required, no default - exact path to the archive
#   MONGO_ROOT_USERNAME   required, no default
#   MONGO_ROOT_PASSWORD   required, no default
#   MONGO_CONTAINER        default: smartwardrobe_mongo

set -euo pipefail

MONGO_CONTAINER="${MONGO_CONTAINER:-smartwardrobe_mongo}"

if [[ -z "${BACKUP_FILE:-}" ]]; then
  echo "ERROR: BACKUP_FILE must be set to the exact archive to restore." >&2
  echo "There is no default and no 'latest' auto-detection, by design." >&2
  exit 1
fi

if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "ERROR: BACKUP_FILE '$BACKUP_FILE' does not exist." >&2
  exit 1
fi

if [[ -z "${MONGO_ROOT_USERNAME:-}" || -z "${MONGO_ROOT_PASSWORD:-}" ]]; then
  echo "ERROR: MONGO_ROOT_USERNAME and MONGO_ROOT_PASSWORD must both be set." >&2
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

echo "=============================================================="
echo " DESTRUCTIVE OPERATION"
echo "=============================================================="
echo " Target container : $MONGO_CONTAINER"
echo " Restore file      : $BACKUP_FILE"
echo " Mode              : --drop (every collection present in the"
echo "                      backup will be DROPPED and REPLACED in the"
echo "                      live database before the backup's data is"
echo "                      loaded back in)"
echo "=============================================================="
read -r -p "Type 'yes' (exactly) to proceed, anything else cancels: " confirmation
if [[ "$confirmation" != "yes" ]]; then
  echo "Cancelled. No changes were made."
  exit 1
fi

archive_name="$(basename "$BACKUP_FILE")"
container_tmp_path="/tmp/${archive_name}"

echo "Copying archive into '$MONGO_CONTAINER' ..."
docker cp "$BACKUP_FILE" "${MONGO_CONTAINER}:${container_tmp_path}"

echo "Running mongorestore --drop ..."
docker exec "$MONGO_CONTAINER" mongorestore \
  --username "$MONGO_ROOT_USERNAME" \
  --password "$MONGO_ROOT_PASSWORD" \
  --authenticationDatabase admin \
  --archive="$container_tmp_path" \
  --gzip \
  --drop

docker exec "$MONGO_CONTAINER" rm -f "$container_tmp_path"

echo "Restore complete. Verify application behavior (e.g. GET /health/full,"
echo "spot-check a known collection count) before considering this done -"
echo "see docs/deployment.md's disaster-recovery section."
