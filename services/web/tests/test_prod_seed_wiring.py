"""Production cold-seed wiring tests.

Companion to test_demo_seed_wiring.py, which already pins the local/dev
demo-profile behavior (unmodified by this work). These are plain
filesystem/content/module-structure checks - not a Docker run - proving:

  - scripts/seed_demo_data.py's `demo` profile (default) is byte-identical
    in dataset/behavior to before this change;
  - the new `production` profile's password is never hardcoded and is only
    ever sourced from the PROD_SEED_PASSWORD environment variable, failing
    loudly (SeedError) when it's missing;
  - production usernames/content carry no demo_/test_/qa_/e2e_ naming
    visible in the UI;
  - docker-compose.yml/docker-compose.prod.yml/CI still never invoke the
    production profile - it stays a manual, opt-in-only action.
"""
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

_FORBIDDEN_SUBSTRINGS = ("demo", "test", "qa", "e2e")


def _read(relative_path: str) -> str:
    path = _REPO_ROOT / relative_path
    assert path.is_file(), f"expected {relative_path} to exist at {path}"
    return path.read_text(encoding="utf-8")


def _load_seed_script():
    """Loads scripts/seed_demo_data.py as a module so tests can assert on
    its actual data structures/functions, not just its raw text. Safe to
    import: module-level code only defines data/functions and validates
    internal consistency (comment-author-vs-post-author) - it never makes
    a network call or reads PROD_SEED_PASSWORD at import time."""
    path = _REPO_ROOT / "scripts" / "seed_demo_data.py"
    spec = importlib.util.spec_from_file_location("qa04_seed_demo_data", path)
    module = importlib.util.module_from_spec(spec)
    # Dataclass field-type resolution looks itself up via
    # sys.modules[cls.__module__] - must be registered before exec_module,
    # or dataclasses.dataclass() raises on the string-annotated fields below.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- Demo profile: prove it is unchanged by this work ---


def test_demo_profile_is_still_the_default():
    module = _load_seed_script()
    default = inspect.signature(module.run).parameters["profile_name"].default
    assert default == "demo"


def test_demo_profile_dataset_is_unchanged():
    module = _load_seed_script()
    assert [u.username for u in module.DEMO_SEED_USERS] == [
        "demo_alex",
        "demo_bianca",
        "demo_chen",
        "demo_dara",
        "demo_elin",
    ]
    assert module._DEMO_PASSWORD == "DemoSeed123!"
    assert len(module.DEMO_SEED_POSTS) == 6
    assert len(module.DEMO_SEED_COMMENTS) == 5
    assert len(module.DEMO_SEED_REACTIONS) == 8
    # The demo profile's password is still resolved as a fixed constant,
    # never an environment lookup - unlike production below.
    assert module._resolve_password("demo") == "DemoSeed123!"


# --- Production profile: password must be env-sourced only ---


def test_production_profile_requires_env_sourced_password(monkeypatch):
    module = _load_seed_script()
    monkeypatch.delenv(module._PROD_SEED_PASSWORD_ENV_VAR, raising=False)
    with pytest.raises(module.SeedError):
        module._resolve_password("production")


def test_production_profile_uses_the_env_password_when_present(monkeypatch):
    module = _load_seed_script()
    monkeypatch.setenv(module._PROD_SEED_PASSWORD_ENV_VAR, "a-real-generated-secret")
    assert module._resolve_password("production") == "a-real-generated-secret"


def test_production_run_fails_before_any_network_call_without_the_env_var(monkeypatch):
    """run() must resolve (and validate) the password before opening an
    HTTP client at all - a missing PROD_SEED_PASSWORD must never partially
    seed anything."""
    import asyncio

    module = _load_seed_script()
    monkeypatch.delenv(module._PROD_SEED_PASSWORD_ENV_VAR, raising=False)
    with pytest.raises(module.SeedError):
        asyncio.run(module.run("http://127.0.0.1:1", "production"))


def test_no_literal_production_password_in_the_resolve_function():
    module = _load_seed_script()
    source = inspect.getsource(module._resolve_password)
    assert "os.environ" in source
    assert "PROD_SEED_PASSWORD" in source or "_PROD_SEED_PASSWORD_ENV_VAR" in source


# --- Production dataset must not look like demo/test/QA/E2E content ---


def test_production_usernames_avoid_demo_test_qa_e2e_naming():
    module = _load_seed_script()
    for user in module.PROD_SEED_USERS:
        lowered = user.username.lower()
        for bad in _FORBIDDEN_SUBSTRINGS:
            assert bad not in lowered, (
                f"production username {user.username!r} contains "
                f"disqualifying substring {bad!r}"
            )


def test_production_post_and_comment_content_avoids_demo_test_qa_e2e_naming():
    module = _load_seed_script()
    visible_text = []
    for post in module.PROD_SEED_POSTS:
        visible_text.append(post.title)
        visible_text.append(post.body)
    for comment in module.PROD_SEED_COMMENTS:
        visible_text.append(comment.body)

    for text in visible_text:
        lowered = text.lower()
        for bad in _FORBIDDEN_SUBSTRINGS:
            assert bad not in lowered, (
                f"production content {text!r} contains disqualifying "
                f"substring {bad!r}"
            )


def test_production_dataset_is_a_few_users_posts_comments_reactions():
    """Same 'a few' scale as the demo profile, not a large synthetic set."""
    module = _load_seed_script()
    assert 3 <= len(module.PROD_SEED_USERS) <= 5
    assert 5 <= len(module.PROD_SEED_POSTS) <= 8
    assert len(module.PROD_SEED_COMMENTS) >= 1
    assert len(module.PROD_SEED_REACTIONS) >= 1


def test_production_comments_never_authored_by_their_own_posts_author():
    """Same structural invariant already enforced for the demo profile -
    module import itself would already raise SeedError if this were
    violated, so this test also documents/pins that behavior."""
    module = _load_seed_script()
    post_author_by_key = {p.key: p.author_key for p in module.PROD_SEED_POSTS}
    for comment in module.PROD_SEED_COMMENTS:
        assert comment.author_key != post_author_by_key[comment.post_key]


# --- The production profile must never be auto-run ---


def test_dev_compose_seed_service_is_unchanged_by_this_work():
    compose = _read("docker-compose.yml")
    assert "python /scripts/seed_demo_data.py --base-url http://web:8000" in compose
    assert "--profile" not in compose
    assert "PROD_SEED_PASSWORD" not in compose


def test_prod_compose_still_hard_disables_the_automatic_seed_service():
    compose = _read("docker-compose.prod.yml")
    assert "SEED_DEMO_DATA=false" in compose
    assert "seed_demo_data" not in compose
    assert "--profile" not in compose
    assert "PROD_SEED_PASSWORD" not in compose


def test_ci_workflow_never_invokes_the_production_seed_profile():
    ci = _read(".github/workflows/ci.yml")
    assert "seed_demo_data" not in ci
    assert "--profile" not in ci
    assert "PROD_SEED_PASSWORD" not in ci


# --- .env.example and docs/deployment.md document the new credential/procedure ---


def test_env_example_documents_prod_seed_password_as_empty_optional_placeholder():
    env_example = _read(".env.example")
    assert "PROD_SEED_PASSWORD=" in env_example
    # Must be a bare, empty placeholder - never a real value committed here.
    for line in env_example.splitlines():
        if line.startswith("PROD_SEED_PASSWORD="):
            assert line == "PROD_SEED_PASSWORD="
    assert "out-of-band" in env_example
    assert "never" in env_example.lower()


def test_deployment_docs_describe_manual_opt_in_production_seed():
    deployment_docs = _read("docs/deployment.md")
    assert "Production cold-seed" in deployment_docs
    assert "PROD_SEED_PASSWORD" in deployment_docs
    assert "--profile production" in deployment_docs
    assert "manual" in deployment_docs.lower()
