"""Dedicated coverage for
GET /health and GET /health/full (routers/health.py) - previously
untested by any file in this suite.

full_health_check() calls `get_db()` directly (a plain function call, not
a `db=Depends(get_db)` parameter), so `app.dependency_overrides` - which
only intercepts FastAPI-DI-resolved dependencies - does not affect it.
These tests instead monkeypatch `app.routers.health.get_db` directly
(the exact name bound in that module's own namespace by its
`from app.core.database import get_db` import), the same idiom already
used elsewhere in this suite to patch one specific call site (e.g.
`app.services.chat_worker.call_llm`) rather than the whole shared module
function.

These tests exist to (1) lock in already-correct, pre-existing behavior
(health.py's own local try/except around the Mongo ping already reports
an outage without raising - confirmed by live testing against a real
stopped Mongo container) and (2) prove
the global ConnectionFailure -> 503 handler (registered in
main.py) does NOT hijack this route: health.py's own, more specific
try/except catches the simulated connectivity exception first, so
GET /health/full must keep returning 200 with a descriptive `mongo` field
- never a 503 - even when the injected failure is the exact same
exception class the global handler exists to convert into one elsewhere.
"""
import httpx
import pymongo.errors
import pytest


def test_health_returns_ok(client):
    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "smartwardrobe-web"}


@pytest.mark.asyncio
async def test_health_full_reports_ok_when_everything_is_reachable(client, db, monkeypatch):
    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, json={"status": "ok"})

    # full_health_check() calls get_db() as a plain function, not via
    # Depends(get_db) - app.dependency_overrides (set by the `client`
    # fixture) has no effect on it. Point this module's own get_db
    # reference at the working fixture db directly, the same idiom used
    # for the outage variants below.
    monkeypatch.setattr("app.routers.health.get_db", lambda: db)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    resp = client.get("/health/full")

    assert resp.status_code == 200
    body = resp.json()
    assert body["web"] == "ok"
    assert body["mongo"] == "ok"
    assert body["recommendation"] == "ok"


@pytest.mark.asyncio
async def test_health_full_reports_mongo_outage_without_raising_or_503(client, db, monkeypatch):
    """The exact scenario live testing exercised against a
    real stopped Mongo container: GET /health/full must keep responding
    200 and describe the outage in its own `mongo` field - never a bare
    500, and (regression guard) never a 503 either, since this route
    is deliberately designed to report a degraded dependency, not to fail
    itself."""

    class _RaisingDB:
        async def command(self, *_args, **_kwargs):
            raise pymongo.errors.ServerSelectionTimeoutError("no servers found")

    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr("app.routers.health.get_db", lambda: _RaisingDB())
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    resp = client.get("/health/full")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["web"] == "ok"
    assert body["mongo"] == "error: ServerSelectionTimeoutError"
    assert body["recommendation"] == "ok"


@pytest.mark.asyncio
async def test_health_full_reports_recommendation_outage(client, db, monkeypatch):
    async def fake_get(self, url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("app.routers.health.get_db", lambda: db)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    resp = client.get("/health/full")

    assert resp.status_code == 200
    body = resp.json()
    assert body["mongo"] == "ok"
    assert body["recommendation"] == "error: ConnectError"


@pytest.mark.asyncio
async def test_health_full_recovers_once_mongo_is_reachable_again(client, db, monkeypatch):
    """Mirrors the live recovery check: once the same process's
    view of the database is healthy again, /health/full reports "ok"
    again with no restart - the route re-checks fresh on every call, it
    never latches a stale outage status."""

    class _RaisingDB:
        async def command(self, *_args, **_kwargs):
            raise pymongo.errors.ServerSelectionTimeoutError("no servers found")

    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    monkeypatch.setattr("app.routers.health.get_db", lambda: _RaisingDB())
    during_outage = client.get("/health/full")
    assert during_outage.json()["mongo"] == "error: ServerSelectionTimeoutError"

    monkeypatch.setattr("app.routers.health.get_db", lambda: db)
    after_recovery = client.get("/health/full")
    assert after_recovery.json()["mongo"] == "ok"
