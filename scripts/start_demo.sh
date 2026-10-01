#!/usr/bin/env bash
# Starts the local Smart Wardrobe stack and makes sure the demo forum data
# is present - a single, documented command for trying the app locally.
#
# What it does:
#   1. `docker compose up -d` - starts (or leaves running) the normal local
#      stack. On a fresh local/dev database, the one-shot `seed` compose
#      service already seeds the forum automatically (gated by
#      SEED_DEMO_DATA, see docker-compose.yml).
#   2. Copies scripts/seed_demo_data.py into the running `web` container and
#      runs it there, forcing an explicit reconciliation pass of the demo
#      seed data through the real HTTP API (no direct database writes).
#
# Idempotent: `docker compose up -d` is a no-op on an already-running stack,
# and seed_demo_data.py reconciles every seeded object individually rather
# than blindly re-inserting, so running this script repeatedly never
# duplicates demo users, posts, comments, or reactions.
#
# Local/dev only. Production (docker-compose.prod.yml) hard-disables
# automatic demo seeding (SEED_DEMO_DATA=false), and this script never
# touches the production overlay.
#
# Usage:
#   ./scripts/start_demo.sh
#
# On Windows, run via Git Bash or WSL, like the project's other .sh scripts.

set -euo pipefail

SEED_SCRIPT_CONTAINER_PATH="/tmp/seed_demo_data.py"

# Repo root = parent of this script's own directory (scripts/) - makes the
# script location-independent (runnable as ./scripts/start_demo.sh from
# the repo root, or via an absolute/relative path from elsewhere) rather
# than silently assuming the caller's current directory.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not available on PATH." >&2
  exit 1
fi

echo "==> Starting the Smart Wardrobe stack (docker compose up -d) ..."
docker compose up -d

echo "==> Copying seed_demo_data.py into the running 'web' service container ..."
docker compose cp scripts/seed_demo_data.py "web:${SEED_SCRIPT_CONTAINER_PATH}"

echo "==> Running the cold-seed script against the real API (idempotent) ..."
# seed_demo_data.py already waits/retries for GET /health itself (see its
# own _wait_for_web) - no extra readiness-polling logic is duplicated here.
docker compose exec -T web python "${SEED_SCRIPT_CONTAINER_PATH}"

cat <<'EOF'

==> Demo stack ready.
    UI:  http://localhost:8000/ui/
    Demo accounts: demo_alex, demo_bianca, demo_chen, demo_dara, demo_elin
    Demo password: DemoSeed123!  (deterministic - demo/local use only, see
    README.md's "Demo data" section - never reuse this account
    or password as a real production credential.)

    The forum's Explore feed already contains the seeded posts/comments;
    log in as any real (registered) account and open the Explore tab to
    see them, or log in as one of the demo accounts above directly.
EOF

