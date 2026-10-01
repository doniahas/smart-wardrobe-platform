"""Demo-seed wiring: configuration/content assertions proving

  - `scripts/start_demo.sh` still reuses the real, unmodified
    `docker compose`/`scripts/seed_demo_data.py` flow (never a direct
    Mongo write, never a rewritten seeding mechanism);
  - local/dev `docker-compose.yml` now runs that same, unmodified
    `scripts/seed_demo_data.py` automatically on `docker compose up -d`
    via a one-shot `seed` service, defaulting `SEED_DEMO_DATA` to `true`;
  - production (`docker-compose.prod.yml`) hard-disables it
    (`SEED_DEMO_DATA=false`), regardless of any deployment `.env`; and
  - CI never invokes any seed path at all.

These are plain filesystem/content checks against real repository files -
not a Docker run.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(relative_path: str) -> str:
    path = _REPO_ROOT / relative_path
    assert path.is_file(), f"expected {relative_path} to exist at {path}"
    return path.read_text(encoding="utf-8")


def test_start_demo_script_exists_and_is_executable():
    path = _REPO_ROOT / "scripts" / "start_demo.sh"
    assert path.is_file()
    # Executable bit for the owner - mirrors every other scripts/*.sh file
    # in this repo (backup_mongo.sh, restore_mongo.sh, verify_backup.sh).
    assert path.stat().st_mode & 0o100


def test_start_demo_script_invokes_real_compose_and_real_seed_script():
    """Proves the wrapper reuses the existing, unmodified startup/seed
    mechanisms rather than introducing a new one - no direct-Mongo writes,
    no bypass of the real HTTP API."""
    script = _read("scripts/start_demo.sh")
    assert "docker compose up -d" in script
    assert "docker compose cp scripts/seed_demo_data.py" in script
    assert "seed_demo_data.py" in script
    # No direct database shortcut anywhere in the wrapper.
    assert "mongosh" not in script
    assert "mongoimport" not in script


def test_seed_demo_data_script_itself_is_unmodified_in_approach():
    """Regression guard: the follow-up must not have rewritten the seed
    script's own content/approach (task instruction: don't rewrite it
    unless absolutely necessary) - it still talks to the real API only."""
    script = _read("scripts/seed_demo_data.py")
    assert "httpx" in script
    assert "/auth/register" in script
    assert "insert_one" not in script  # never a direct-Mongo write


def test_dev_compose_seeds_automatically_via_the_existing_seed_script():
    """docker-compose.yml (the file `docker compose up -d` reads by
    default) must run the real, unmodified scripts/seed_demo_data.py
    automatically - not a reimplementation, not a direct-Mongo shortcut -
    and must default SEED_DEMO_DATA to true so a genuinely fresh
    local/dev deployment is seeded with no manual step."""
    compose = _read("docker-compose.yml")
    assert "seed:" in compose
    assert "SEED_DEMO_DATA=${SEED_DEMO_DATA:-true}" in compose
    assert "seed_demo_data.py" in compose
    # No direct database shortcut in the compose-level trigger itself.
    assert "insert_one" not in compose


def test_production_compose_hard_disables_automatic_seed():
    """docker-compose.prod.yml must override SEED_DEMO_DATA to false for
    the seed service, regardless of what a deployment's own .env (shared/
    .env, provisioned out-of-band per docs/deployment.md) contains - a
    public production deployment must never be auto-seeded with the fixed
    demo credentials."""
    compose = _read("docker-compose.prod.yml")
    assert "seed:" in compose
    assert "SEED_DEMO_DATA=false" in compose


def test_dev_and_prod_seed_service_both_target_the_same_unmodified_script():
    """Both compose files must point at the one real scripts/seed_demo_data.py
    - the production override only flips SEED_DEMO_DATA, it must not fork
    into a separate/rewritten seeding path for either environment."""
    dev_compose = _read("docker-compose.yml")
    assert "python /scripts/seed_demo_data.py" in dev_compose
    assert "--base-url http://web:8000" in dev_compose
    # The prod override only changes the env flag - no separate command,
    # image, or script reference of its own.
    prod_compose = _read("docker-compose.prod.yml")
    assert "seed_demo_data" not in prod_compose


def test_ci_workflow_does_not_invoke_demo_seeding():
    """CI must never run the demo/seed path - it stays a deliberate,
    manually-invoked developer/demo action only."""
    ci = _read(".github/workflows/ci.yml").lower()
    assert "start_demo" not in ci
    assert "seed_demo_data" not in ci


def test_demo_password_is_scoped_as_demo_only_not_a_production_credential():
    """The deterministic demo password must be documented as demo/local
    scope, not presented as a real production credential - checked in both
    the seed script and the new wrapper's own output."""
    seed_script = _read("scripts/seed_demo_data.py")
    assert "DemoSeed123!" in seed_script

    wrapper = _read("scripts/start_demo.sh")
    assert "DemoSeed123!" in wrapper
    assert "production" in wrapper.lower()

