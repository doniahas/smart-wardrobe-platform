"""Graceful handling of a MongoDB connectivity outage.

Live-testing a stopped `mongo` container showed `POST /auth/login`
previously returned a bare, unhandled-looking `500 Internal Server
Error` instead of a controlled response - an unacceptable "it crashes"
outcome for a dependency outage. These tests reproduce that failure
class without a real Mongo container: `app.core.database.get_db` is
overridden (via the existing `client`/`db` fixture machinery already used
throughout this suite) with a stand-in whose collection methods raise the
exact `pymongo.errors` exception classes a real driver raises when the
server is unreachable/times out selecting a server - the same classes
Motor (this project's async driver) surfaces directly, since it does not
define its own exception hierarchy.

Two routes are exercised, in two different routers, to prove this is a
centralized fix (a global FastAPI exception handler covering every
DB-backed route through one registration) rather than a login-only patch:
  - POST /auth/login (routers/auth.py) - the exact route class seen
    failing during live testing.
  - GET /wardrobe/{item_id} (routers/wardrobe.py) - a different,
    authenticated, DB-backed route. The failure is injected into the
    `wardrobe` collection specifically while `users` (used by the
    get_current_user auth dependency every authenticated route shares)
    keeps working normally - proving the fix covers a failure inside a
    route's own body, not merely the one shared auth dependency every
    authenticated route already goes through.

A fifth test (test_duplicate_registration_still_returns_409...) proves
the fix is precise, not a blanket "any database error becomes 503":
routers/auth.py's own existing DuplicateKeyError -> 409 Conflict handling
(a real business-rule violation, not a connectivity failure) must be
completely unaffected.
"""
import pymongo.errors
import pytest


class _RaisingCollection:
    """A stand-in Mongo collection whose every method raises the given
    exception when awaited - simulates a collection-level operation
    hitting an unreachable/timed-out server."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def __getattr__(self, _name):
        async def _raise(*_args, **_kwargs):
            raise self._exc

        return _raise


class _RaisingDB:
    """A stand-in `db` object (as returned by app.core.database.get_db)
    whose every collection - and whose own `command()`, used by
    GET /health/full - raises the given exception."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def __getattr__(self, _name):
        return _RaisingCollection(self._exc)

    async def command(self, *_args, **_kwargs):
        raise self._exc


class _SelectivelyRaisingDB:
    """Proxies every collection to a real (working) db EXCEPT one named
    collection, which raises instead. Lets a test simulate a failure
    inside one specific route's own query while every other collection -
    including `users`, which the shared get_current_user auth dependency
    queries on every authenticated request - keeps working exactly as it
    would against a real, reachable MongoDB."""

    def __init__(self, real_db, failing_collection: str, exc: Exception):
        self._real_db = real_db
        self._failing_collection = failing_collection
        self._exc = exc

    def __getattr__(self, name):
        if name == self._failing_collection:
            return _RaisingCollection(self._exc)
        return getattr(self._real_db, name)


# The exact exception a real Mongo outage produced
# (confirmed directly against the installed pymongo==4.7.2: `mro()` is
# ServerSelectionTimeoutError -> AutoReconnect -> ConnectionFailure ->
# PyMongoError). Used for the login test.
_SERVER_SELECTION_TIMEOUT = pymongo.errors.ServerSelectionTimeoutError(
    "No servers found yet, Timeout: 0.5s, Topology Description: "
    "<TopologyDescription id: 000000000000000000000000, topology_type: Unknown, "
    "servers: [<ServerDescription ('mongo', 27017) server_type: Unknown, rtt: None>]>"
)
# A second, different ConnectionFailure subclass (a genuine mid-operation
# disconnect/reconnect-eligible failure, distinct from a
# ServerSelectionTimeoutError, which is a startup-of-operation failure) -
# used for the wardrobe test, to prove the fix isn't narrowly keyed to one
# single concrete exception class but to the whole transient-connectivity
# family.
_AUTO_RECONNECT = pymongo.errors.AutoReconnect("connection pool paused")


def _register_and_login(client, username: str) -> str:
    client.post(
        "/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": "Passw0rd!23"},
    )
    resp = client.post("/auth/login", json={"username": username, "password": "Passw0rd!23"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# --- A. login + Mongo connectivity failure -----------------------------


@pytest.mark.asyncio
async def test_login_returns_503_when_mongo_is_unreachable(client, db, monkeypatch):
    """Reproduces the exact route class live testing found returning a
    bare 500: POST /auth/login's own db.users.find_one() call raises a
    real pymongo connectivity exception."""
    # A real, normal user exists (so a *working* login would otherwise
    # succeed) - the failure must come purely from the simulated outage,
    # not from "user not found".
    await db.users.insert_one({
        "username": "outageuser", "email": "outageuser@example.com",
        "hashed_password": "irrelevant-not-reached",
    })

    from app.core.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: _RaisingDB(_SERVER_SELECTION_TIMEOUT)
    try:
        resp = client.post(
            "/auth/login", json={"username": "outageuser", "password": "whatever"}
        )
    finally:
        app.dependency_overrides[get_db] = lambda: db

    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": "Service temporarily unavailable. Please try again shortly."}


# --- B. a different DB-backed route -------------------------------------


@pytest.mark.asyncio
async def test_wardrobe_get_item_returns_503_when_mongo_is_unreachable(
    client, db, make_user, make_wardrobe_item, monkeypatch
):
    """A completely different router (wardrobe.py, not auth.py) hits the
    same controlled 503 - proving this is centralized protection, not a
    login-only patch. The `users` collection (queried by the shared
    get_current_user auth dependency) is left fully working; only
    `wardrobe` (queried inside get_item's own body) is made to fail, with
    a *different* ConnectionFailure subclass than the login test used."""
    user = await make_user("wardrobeoutageuser")
    item_id = await make_wardrobe_item(user["user_id"])

    from app.core.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: _SelectivelyRaisingDB(
        db, "wardrobe", _AUTO_RECONNECT
    )
    try:
        resp = client.get(f"/wardrobe/{item_id}", headers=user["headers"])
    finally:
        app.dependency_overrides[get_db] = lambda: db

    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": "Service temporarily unavailable. Please try again shortly."}


# --- C. normal login is unaffected --------------------------------------


@pytest.mark.asyncio
async def test_login_unaffected_when_mongo_is_reachable(client, db):
    token = _register_and_login(client, "normaloutagecheckuser")
    assert token


# --- D. normal DB-backed route is unaffected -----------------------------


@pytest.mark.asyncio
async def test_wardrobe_get_item_unaffected_when_mongo_is_reachable(
    client, make_user, make_wardrobe_item
):
    user = await make_user("normalwardrobeoutagecheckuser")
    item_id = await make_wardrobe_item(user["user_id"])

    resp = client.get(f"/wardrobe/{item_id}", headers=user["headers"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == item_id


# --- E. a non-connectivity database error keeps its existing semantics --


@pytest.mark.asyncio
async def test_duplicate_registration_still_returns_409_not_503(client, db):
    """routers/auth.py's own existing DuplicateKeyError -> 409 Conflict
    handling (a real business-rule violation: username/email already
    taken) must be completely untouched by the new connectivity-failure
    handling - a duplicate-key error is a normal, expected outcome of a
    reachable, working database enforcing its own unique indexes, not an
    availability failure, and must never be reported to the client as
    "temporarily unavailable".

    The `db` fixture is a bare mongomock client with no indexes created
    on it (unlike a real deployment, where app.core.database.connect_db()
    always creates them at startup - see core/database.py::_ensure_indexes)
    - the unique index is created explicitly here so this test exercises
    a real DuplicateKeyError, not merely two 201s in a row.
    """
    await db.users.create_index("username", unique=True)
    await db.users.create_index("email", unique=True)
    payload = {"username": "dupeoutagecheckuser", "email": "dupeoutagecheckuser@example.com",
               "password": "Passw0rd!23"}

    first = client.post("/auth/register", json=payload)
    assert first.status_code == 201, first.text

    second = client.post("/auth/register", json=payload)

    assert second.status_code == 409, second.text
    assert second.json() == {"detail": "Username or email already registered"}


# --- F. the 503 body never leaks internal details ------------------------


@pytest.mark.asyncio
async def test_503_body_never_leaks_driver_details_or_uri(client, db):
    """The safe, generic body must never contain the raw exception text
    (which - confirmed directly against pymongo==4.7.2 - includes the
    topology's host/port), a stack trace, or anything resembling a Mongo
    connection URI/credential."""
    await db.users.insert_one({
        "username": "leakcheckuser", "email": "leakcheckuser@example.com",
        "hashed_password": "irrelevant-not-reached",
    })

    from app.core.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: _RaisingDB(_SERVER_SELECTION_TIMEOUT)
    try:
        resp = client.post(
            "/auth/login", json={"username": "leakcheckuser", "password": "whatever"}
        )
    finally:
        app.dependency_overrides[get_db] = lambda: db

    assert resp.status_code == 503
    body_text = resp.text
    for forbidden in (
        "mongo", "Mongo", "27017", "Topology", "ServerSelectionTimeoutError",
        "AutoReconnect", "Traceback", "mongodb://", "password", "PASSWORD",
    ):
        assert forbidden not in body_text, f"leaked {forbidden!r} in response body: {body_text}"
