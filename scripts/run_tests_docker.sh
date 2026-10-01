#!/usr/bin/env bash
# The primary, recommended way to run every backend service's automated
# test suite
# from a fresh clone, using only Docker - no host Python install, no
# `python`/`python3` alias assumption, no host virtualenv, no `pip
# install` on the host machine at all.
#
# This is NOT a separate testing mechanism and does not add, modify,
# weaken, or duplicate a single test. It automates the standard recipe
# for running this project's suites: a vanilla `python:3.11-slim`
# container, a fresh `pip install` of one service's own already-existing
# `requirements.txt` (no image build required), then that service's own
# unmodified `pytest` suite - unit, integration, security, and stress/
# concurrency tests all together, exactly as `cd services/<name> &&
# pytest` already runs them (see docs/testing.md's "Coverage by service"
# table - `web`'s suite already includes `integration/` and `security/`
# subdirectories; there is no separate command for those tiers).
#
# What this does NOT run: tests/system/ (the opt-in System/E2E suites,
# which need a real, purpose-built, disposable multi-container Docker
# topology per suite - wardrobe, catalog success/failure, and forum with
# Redis - not a single service's own pytest run). See README.md's
# "Testing" section and docs/testing.md's "System / end-to-end tests"
# section for those, including the exact Forum flow caveat (the disposable Forum
# `web` container must run WITHOUT TESTING=true, since TESTING=true
# disables the Redis live-notification subscriber - see that section).
#
# Non-destructive and deterministic: every suite already runs against
# mongomock-motor/fakeredis/mocked HTTP (see docs/testing.md) - no real
# Mongo, Redis, network access, or the already-running `docker compose`
# stack (if any) is touched. Each service runs in its own `--rm`
# container; nothing is left running afterward. The repository is bind-
# mounted read-write (some tests need to write to a pytest-provided
# tmp_path, never to a real repo path - see e.g.
# services/web/tests/test_explore_media.py's own `tmp_path`-backed
# upload-dir fixture), but no test in any of these four suites writes
# outside a tmp directory, so the working tree is left unchanged.
#
# Usage:
#   ./scripts/run_tests_docker.sh                 # all four services
#   ./scripts/run_tests_docker.sh web catalog     # only these services
#
# On Windows, run via Git Bash/WSL, same as this project's other
# scripts/*.sh files (start_demo.sh, backup_mongo.sh, restore_mongo.sh, ...).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not available on PATH." >&2
  echo "See README.md's \"Prerequisites\" section." >&2
  exit 1
fi

# A quick, early, actionable check rather than a confusing failure deep
# inside the first `docker run` - see README.md's "Linux Docker
# permissions" note for what a non-zero exit here usually means
# (most commonly: the current user isn't in the local `docker` group yet)
# and does NOT assume every installation needs a workaround.
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: 'docker info' failed - the Docker daemon isn't reachable" >&2
  echo "from this shell/user. See README.md's \"Linux Docker" >&2
  echo "permissions\" note (common on a fresh Linux install: the current" >&2
  echo "user isn't in the local 'docker' group yet)." >&2
  exit 1
fi

ALL_SERVICES=(recommendation web llm catalog)
if [ "$#" -gt 0 ]; then
  SERVICES=("$@")
else
  SERVICES=("${ALL_SERVICES[@]}")
fi

FAILED_SERVICES=()

for svc in "${SERVICES[@]}"; do
  if [ ! -d "services/$svc" ]; then
    echo "ERROR: services/$svc does not exist - skipping." >&2
    FAILED_SERVICES+=("$svc (unknown service)")
    continue
  fi

  echo ""
  echo "=== $svc ==="
  # MSYS_NO_PATHCONV + the doubled leading slash on -w are a no-op on
  # real Linux/macOS (a leading "//" is treated identically to "/") and
  # exist only so this script also works unmodified from Windows Git
  # Bash, which otherwise silently rewrites a bare absolute-looking
  # container path into a host Windows path before docker ever sees it.
  if MSYS_NO_PATHCONV=1 docker run --rm \
      -v "$REPO_ROOT:/repo" \
      -w "//repo/services/$svc" \
      -e TESTING=true \
      python:3.11-slim \
      bash -c "pip install --no-cache-dir -q -r requirements.txt && python -m pytest -q"; then
    echo "=== $svc: PASSED ==="
  else
    echo "=== $svc: FAILED ==="
    FAILED_SERVICES+=("$svc")
  fi
done

echo ""
if [ "${#FAILED_SERVICES[@]}" -eq 0 ]; then
  echo "All service test suites passed."
  exit 0
else
  echo "FAILED service suites: ${FAILED_SERVICES[*]}" >&2
  exit 1
fi

