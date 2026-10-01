"""Focused proof that get_current_user (and, by
extension, every route depending on it) rejects the three ways a JWT can be
invalid - expired, malformed, and signed with the wrong secret - each with a
plain 401, never a 200 or a 500.

Uses GET /auth/me as the target route: it's the smallest protected endpoint
(auth-only, no other preconditions), so a failure here isolates token
validation itself rather than any route-specific logic.
"""
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

from app.core.config import JWT_ALGORITHM, JWT_SECRET


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_expired_token_is_rejected(client, make_user):
    user = await make_user("jwtexpireduser")
    expired_token = jwt.encode(
        {"sub": "jwtexpireduser", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    resp = client.get("/auth/me", headers=_auth_headers(expired_token))

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Could not validate credentials"


def test_malformed_token_is_rejected(client):
    resp = client.get("/auth/me", headers=_auth_headers("this-is-not-a-jwt-at-all"))

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Could not validate credentials"


@pytest.mark.asyncio
async def test_wrong_signature_token_is_rejected(client, make_user):
    user = await make_user("jwtwrongsiguser")
    wrong_secret_token = jwt.encode(
        {
            "sub": "jwtwrongsiguser",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
        },
        "a-completely-different-secret",
        algorithm=JWT_ALGORITHM,
    )

    resp = client.get("/auth/me", headers=_auth_headers(wrong_secret_token))

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Could not validate credentials"


@pytest.mark.asyncio
async def test_valid_token_is_still_accepted(client, make_user):
    """Regression guard: the three rejection tests above must never pass by
    accident because valid tokens stopped working too."""
    user = await make_user("jwtvaliduser")

    resp = client.get("/auth/me", headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json()["username"] == "jwtvaliduser"
