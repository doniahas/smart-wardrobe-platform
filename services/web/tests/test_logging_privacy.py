"""Hardening: regression tests proving that sensitive values never
appear in captured application logs.

A review found the implementation already structurally safe by
inspection (no leak found). This file exists purely to catch a future
*regression* (e.g.
someone later adding a debug log that interpolates a password, token, or
message body) - it does not fix anything, because nothing was broken.

Reuses the exact `caplog` pattern already established in
test_recommendations_route.py::test_fallback_failure_log_does_not_expose_wardrobe_contents
- capture at a permissive log level, then assert the sentinel value is
absent from both the per-record message text and the fully-formatted
`caplog.text` (which also covers `logger.exception`'s traceback/exc_info
output, not just the bare log message).
"""
import logging

import pytest

_PASSWORD_SENTINEL = "S3ntinel-P@ssw0rd-9f8e7d6c5b4a"
_DM_BODY_SENTINEL = "SENTINEL-DM-BODY-3f9a8b7c6d5e4f1a2b3c-do-not-log-me"


def _assert_sentinel_absent(caplog, sentinel: str) -> None:
    assert sentinel not in caplog.text
    for record in caplog.records:
        assert sentinel not in record.getMessage()


def test_password_never_appears_in_logs_during_register_and_login(client, caplog):
    with caplog.at_level(logging.DEBUG):
        register_resp = client.post(
            "/auth/register",
            json={
                "username": "logprivacypwuser",
                "email": "logprivacypwuser@example.com",
                "password": _PASSWORD_SENTINEL,
            },
        )
        assert register_resp.status_code == 201

        login_resp = client.post(
            "/auth/login",
            json={"username": "logprivacypwuser", "password": _PASSWORD_SENTINEL},
        )
        assert login_resp.status_code == 200

    _assert_sentinel_absent(caplog, _PASSWORD_SENTINEL)


def test_wrong_password_attempt_never_logs_the_attempted_password(client, caplog):
    """A failed login is the case most tempting to "helpfully" log for
    debugging - must not leak the attempted password even on the 401 path."""
    client.post(
        "/auth/register",
        json={
            "username": "logprivacywronguser",
            "email": "logprivacywronguser@example.com",
            "password": "TheRealPassword123!",
        },
    )

    with caplog.at_level(logging.DEBUG):
        resp = client.post(
            "/auth/login",
            json={"username": "logprivacywronguser", "password": _PASSWORD_SENTINEL},
        )
        assert resp.status_code == 401

    _assert_sentinel_absent(caplog, _PASSWORD_SENTINEL)


def test_jwt_token_never_appears_in_logs_after_login_and_authenticated_call(client, caplog):
    """Uses the real, freshly-issued token (not a hardcoded fake) as the
    sentinel - the most faithful proof that this exact secret value is
    never logged, across both the login response and a subsequent
    authenticated request that carries it as a Bearer header."""
    client.post(
        "/auth/register",
        json={
            "username": "logprivacyjwtuser",
            "email": "logprivacyjwtuser@example.com",
            "password": "AnotherRealPassword123!",
        },
    )

    with caplog.at_level(logging.DEBUG):
        login_resp = client.post(
            "/auth/login",
            json={"username": "logprivacyjwtuser", "password": "AnotherRealPassword123!"},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["access_token"]
        assert token  # sanity: sentinel must be non-empty to be a real test

        me_resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me_resp.status_code == 200

    _assert_sentinel_absent(caplog, token)


@pytest.mark.asyncio
async def test_dm_body_never_appears_in_logs_on_normal_send(client, make_user, caplog):
    sender = await make_user("logprivacydmsender")
    recipient = await make_user("logprivacydmrecipient")

    with caplog.at_level(logging.DEBUG):
        resp = client.post(
            f"/messages/{recipient['user_id']}",
            data={"body": _DM_BODY_SENTINEL},
            headers=sender["headers"],
        )
        assert resp.status_code == 201

    _assert_sentinel_absent(caplog, _DM_BODY_SENTINEL)


@pytest.mark.asyncio
async def test_dm_body_never_appears_in_logs_on_notification_failure_path(
    client, make_user, monkeypatch, caplog
):
    """Forces routers/messages.py's `except Exception:
    logger.exception("send_message: unexpected error creating
    notification")` branch - the one place in the send-message flow that
    deliberately logs on failure - and proves even that log line (message
    *and* formatted traceback) never contains the DM body. The message
    must still be sent successfully (notification failure is best-effort
    and non-fatal by design)."""
    sender = await make_user("logprivacydmfailsender")
    recipient = await make_user("logprivacydmfailrecipient")

    async def _fake_create_notification(*args, **kwargs):
        raise RuntimeError("simulated notification failure")

    monkeypatch.setattr(
        "app.routers.messages.create_notification", _fake_create_notification
    )

    with caplog.at_level(logging.DEBUG):
        resp = client.post(
            f"/messages/{recipient['user_id']}",
            data={"body": _DM_BODY_SENTINEL},
            headers=sender["headers"],
        )
        assert resp.status_code == 201  # send succeeds despite the notification failure

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "send_message: unexpected error creating notification" in log_text
    _assert_sentinel_absent(caplog, _DM_BODY_SENTINEL)
