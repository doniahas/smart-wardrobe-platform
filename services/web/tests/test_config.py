import importlib

import pytest

from app.core import config as config_module

_RATE_LIMIT_VARS = (
    "RATE_LIMIT_DEFAULT",
    "RATE_LIMIT_AUTH",
    "RATE_LIMIT_RECOMMENDATION",
    "RATE_LIMIT_WARDROBE_WRITE",
    "RATE_LIMIT_FEEDBACK",
    "RATE_LIMIT_OUTFIT_WRITE",
    "RATE_LIMIT_MEMORY_WRITE",
)


def test_rate_limit_defaults_when_env_unset(monkeypatch):
    for var in _RATE_LIMIT_VARS:
        monkeypatch.delenv(var, raising=False)

    try:
        reloaded = importlib.reload(config_module)
        assert reloaded.RATE_LIMIT_DEFAULT == "100/minute"
        assert reloaded.RATE_LIMIT_AUTH == "5/minute"
        assert reloaded.RATE_LIMIT_RECOMMENDATION == "20/minute"
        assert reloaded.RATE_LIMIT_WARDROBE_WRITE == "30/minute"
        assert reloaded.RATE_LIMIT_FEEDBACK == "30/minute"
        assert reloaded.RATE_LIMIT_OUTFIT_WRITE == "30/minute"
        assert reloaded.RATE_LIMIT_MEMORY_WRITE == "30/minute"
    finally:
        importlib.reload(config_module)  # restore real process env for other tests


def test_rate_limit_env_override_is_respected(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_AUTH", "2/minute")

    try:
        reloaded = importlib.reload(config_module)
        assert reloaded.RATE_LIMIT_AUTH == "2/minute"
    finally:
        monkeypatch.delenv("RATE_LIMIT_AUTH", raising=False)
        importlib.reload(config_module)  # restore real process env for other tests


# --- CHAT_JOB_STALE_TIMEOUT_SECONDS / LLM_SERVICE_TIMEOUT_SECONDS safety
# margin ---


def test_validate_chat_job_stale_timeout_accepts_the_documented_default_relationship():
    """The project's own default (2x LLM_SERVICE_TIMEOUT_SECONDS) must
    always satisfy its own safety margin."""
    config_module.validate_chat_job_stale_timeout(
        stale_timeout_seconds=130.0, llm_timeout_seconds=65.0
    )  # does not raise


def test_validate_chat_job_stale_timeout_accepts_exactly_the_minimum_margin():
    config_module.validate_chat_job_stale_timeout(
        stale_timeout_seconds=150.0, llm_timeout_seconds=100.0, min_margin=1.5
    )  # 150 == 100 * 1.5 exactly - the boundary itself must be accepted


def test_validate_chat_job_stale_timeout_rejects_a_shorter_stale_timeout():
    """Reproduces the exact unsafe configuration live validation
    hit: a stale timeout shorter than the LLM timeout it's
    supposed to sit comfortably above."""
    with pytest.raises(RuntimeError):
        config_module.validate_chat_job_stale_timeout(
            stale_timeout_seconds=15.0, llm_timeout_seconds=65.0
        )


def test_validate_chat_job_stale_timeout_rejects_an_insufficient_margin():
    """Longer than the LLM timeout is not enough by itself - it must clear
    the configured safety margin, not just be numerically larger."""
    with pytest.raises(RuntimeError):
        config_module.validate_chat_job_stale_timeout(
            stale_timeout_seconds=70.0, llm_timeout_seconds=65.0, min_margin=1.5
        )


def test_app_startup_rejects_an_unsafe_stale_timeout_env_combination(monkeypatch):
    """Integration-level: an obviously unsafe CHAT_JOB_STALE_TIMEOUT_SECONDS/
    LLM_SERVICE_TIMEOUT_SECONDS pair in the environment must fail application
    startup (module import), not silently load."""
    monkeypatch.setenv("LLM_SERVICE_TIMEOUT_SECONDS", "65.0")
    monkeypatch.setenv("CHAT_JOB_STALE_TIMEOUT_SECONDS", "15.0")

    try:
        with pytest.raises(RuntimeError):
            importlib.reload(config_module)
    finally:
        monkeypatch.delenv("LLM_SERVICE_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("CHAT_JOB_STALE_TIMEOUT_SECONDS", raising=False)
        importlib.reload(config_module)  # restore real process env for other tests


def test_app_startup_accepts_a_safe_stale_timeout_env_combination(monkeypatch):
    monkeypatch.setenv("LLM_SERVICE_TIMEOUT_SECONDS", "65.0")
    monkeypatch.setenv("CHAT_JOB_STALE_TIMEOUT_SECONDS", "130.0")

    try:
        reloaded = importlib.reload(config_module)
        assert reloaded.CHAT_JOB_STALE_TIMEOUT_SECONDS == 130.0
    finally:
        monkeypatch.delenv("LLM_SERVICE_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("CHAT_JOB_STALE_TIMEOUT_SECONDS", raising=False)
        importlib.reload(config_module)  # restore real process env for other tests


# --- JWT_SECRET rejection outside TESTING mode (hardening) ---
#
# app.core.config already refuses to import (raises RuntimeError) outside
# TESTING mode if JWT_SECRET is missing or left at the checked-in
# placeholder "change-this-secret" (see the module's own security
# comment). That behavior was previously never exercised by the
# test suite at all - every test here runs under conftest.py's
# `os.environ.setdefault("TESTING", "true")`, which always takes the *other*
# branch. These tests close that gap using the same importlib.reload
# pattern as the stale-timeout tests above, so the suite would actually
# catch a future regression to this check.


def test_app_startup_rejects_missing_jwt_secret_outside_testing_mode(monkeypatch):
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.delenv("JWT_SECRET", raising=False)

    try:
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            importlib.reload(config_module)
    finally:
        # Explicitly restore TESTING=true (not delenv) before the cleanup
        # reload: conftest.py's os.environ.setdefault("TESTING", "true")
        # only runs once, at collection time - deleting it here would leave
        # TESTING *and* JWT_SECRET both unset for the reload below, which
        # would itself hit the exact RuntimeError this test is proving,
        # masking the real test result.
        monkeypatch.setenv("TESTING", "true")
        importlib.reload(config_module)  # restore real process env for other tests


def test_app_startup_rejects_default_placeholder_jwt_secret_outside_testing_mode(monkeypatch):
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setenv("JWT_SECRET", "change-this-secret")

    try:
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            importlib.reload(config_module)
    finally:
        monkeypatch.setenv("TESTING", "true")  # see note above
        monkeypatch.delenv("JWT_SECRET", raising=False)
        importlib.reload(config_module)  # restore real process env for other tests


def test_app_startup_accepts_a_real_jwt_secret_outside_testing_mode(monkeypatch):
    """Regression guard: the two rejection tests above must never pass by
    accident because a real secret stopped working too."""
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setenv("JWT_SECRET", "a-real-non-default-secret-value-1234567890")

    try:
        reloaded = importlib.reload(config_module)
        assert reloaded.JWT_SECRET == "a-real-non-default-secret-value-1234567890"
    finally:
        monkeypatch.setenv("TESTING", "true")  # see note above
        monkeypatch.delenv("JWT_SECRET", raising=False)
        importlib.reload(config_module)  # restore real process env for other tests