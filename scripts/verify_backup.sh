#!/usr/bin/env bash
# Prove a mongodump archive is actually restorable, without going
# anywhere near the real database. Restores into a disposable, throwaway
# mongo:7 container (unauthenticated, on a separate port, torn down when
# this script exits) and prints per-collection document counts for you to
# compare against what you expect.
#
# This never touches the real smartwardrobe_mongo container or its
# volume - safe to run at any time, including against production backups,
# without any risk to live data. This is the recommended way to answer
# "does this backup actually work?" - not scripts/restore_mongo.sh, which
# is destructive and only for a real recovery event.
#
# Usage:
#   BACKUP_FILE=./backups/mongo/mongo-20260101-020000.archive.gz \
#     ./scripts/verify_backup.sh
#
# Configuration (env vars):
#   BACKUP_FILE     required, no default - exact archive to test-restore
#   VERIFY_PORT     default: 27099 - host port for the throwaway container,
#                   deliberately not 27017 so it can never collide with a
#                   real mongo service

set -euo pipefail

VERIFY_PORT="${VERIFY_PORT:-27099}"
VERIFY_CONTAINER="verify_backup_mongo_$$"

if [[ -z "${BACKUP_FILE:-}" ]]; then
  echo "ERROR: BACKUP_FILE must be set to the exact archive to verify." >&2
  exit 1
fi

if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "ERROR: BACKUP_FILE '$BACKUP_FILE' does not exist." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not available on PATH." >&2
  exit 1
fi

cleanup() {
  docker stop "$VERIFY_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Starting throwaway mongo:7 container '$VERIFY_CONTAINER' on port $VERIFY_PORT ..."
docker run -d --rm --name "$VERIFY_CONTAINER" -p "${VERIFY_PORT}:27017" mongo:7 >/dev/null

echo "Waiting for it to be ready..."
for _ in $(seq 1 30); do
  if docker exec "$VERIFY_CONTAINER" mongosh --quiet --eval 'db.adminCommand("ping")' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "${ready:-0}" != "1" ]]; then
  echo "ERROR: throwaway container did not become ready in time." >&2
  exit 1
fi

archive_name="$(basename "$BACKUP_FILE")"
container_tmp_path="/tmp/${archive_name}"

echo "Copying archive in and restoring (no --drop needed - container is empty) ..."
docker cp "$BACKUP_FILE" "${VERIFY_CONTAINER}:${container_tmp_path}"
docker exec "$VERIFY_CONTAINER" mongorestore --archive="$container_tmp_path" --gzip

echo ""
echo "=== Restored collection counts (compare against what you expect) ==="
docker exec "$VERIFY_CONTAINER" mongosh --quiet smartwardrobe --eval '
db.getCollectionNames().sort().forEach(function(c) {
  print(c + ": " + db.getCollection(c).countDocuments({}));
});
'
echo "======================================================================"
echo "Throwaway container will now be stopped and removed (--rm)."
