"""Real-MongoDB integration tests for the four behaviors that
test_live_mongo_smoke.py's raw-collection inserts don't reach.

test_live_mongo_smoke.py already proves, against a real server, that the
declared unique indexes themselves reject a duplicate insert. What it
doesn't touch is the *router* layer built on top of that: does a real
DuplicateKeyError raised by a real MongoDB server actually get caught and
turned into the right HTTP response? mongomock never raises
DuplicateKeyError with full server fidelity, and no existing test (mocked
or live) ever sends two real HTTP requests through app.main.app and a real
database to find out. Same gap for the wardrobe-delete cascade (never
exercised end-to-end against any backend) and for claim_next_job's
atomicity (proven under mongomock + asyncio interleaving in
test_chat_concurrency.py, but never under genuine concurrent network
round-trips to a real server).

Reuses the exact dependency-override pattern services/web/tests/conftest.py
uses for its mongomock `client` fixture (app.dependency_overrides[get_db]),
pointed at the real `live_db` fixture from tests/system/conftest.py
instead - the only thing that changes between a mocked-DB test and this
real-DB test is which database the override returns.

Uses httpx.AsyncClient(transport=ASGITransport(...)) rather than
starlette's synchronous TestClient: TestClient runs the ASGI app through
its own anyio worker thread/event loop, and a real AsyncIOMotorClient
(unlike mongomock's fake) is bound to the event loop it was created on -
mixing the two raised "Future attached to a different loop" errors in
practice. AsyncClient against the same ASGI app, awaited from this same
async test's event loop, sidesteps that entirely.
"""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from bson import ObjectId

from app.auth import create_access_token, hash_password
from app.core.database import get_db
from app.main import app
from app.services.chat_queue import claim_next_job


@pytest.fixture
async def real_client(live_db):
    app.dependency_overrides[get_db] = lambda: live_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _register_payload(username: str) -> dict:
    return {"username": username, "email": f"{username}@example.com", "password": "password123"}


async def _make_user(live_db, username: str) -> dict:
    result = await live_db.users.insert_one(
        {
            "username": username,
            "email": f"{username}@example.com",
            "hashed_password": hash_password("password123"),
        }
    )
    token = create_access_token({"sub": username})
    return {"user_id": str(result.inserted_id), "headers": {"Authorization": f"Bearer {token}"}}


@pytest.mark.asyncio
async def test_duplicate_registration_via_api_returns_409_against_real_mongo(real_client):
    """The full router path: real MongoDB raises DuplicateKeyError on the
    second insert, and routers/auth.py::register actually catches it and
    returns 409 - not just that the index itself rejects the insert."""
    payload = _register_payload("m20dupuser")

    first = await real_client.post("/auth/register", json=payload)
    assert first.status_code == 201

    second = await real_client.post("/auth/register", json=payload)
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_duplicate_feedback_via_api_returns_409_against_real_mongo(real_client, live_db):
    """Same gap as above for routers/feedback.py's DuplicateKeyError ->
    409 handling on the (user_id, outfit_id) compound unique index."""
    user = await _make_user(live_db, "m20dupfeedbackuser")
    item_id = (await live_db.wardrobe.insert_one({"user_id": user["user_id"], "name": "Item"})).inserted_id
    outfit_id = str(
        (
            await live_db.outfits.insert_one(
                {"user_id": user["user_id"], "item_ids": [str(item_id)]}
            )
        ).inserted_id
    )

    body = {"outfit_id": outfit_id, "rating": 5}
    first = await real_client.post("/feedback", json=body, headers=user["headers"])
    assert first.status_code == 201

    second = await real_client.post("/feedback", json=body, headers=user["headers"])
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_wardrobe_delete_cascades_into_outfits_and_feedback_against_real_mongo(
    real_client, live_db
):
    """No existing test (mocked or live) drives DELETE /wardrobe/{id}
    end-to-end: pulling the deleted item out of every outfit's item_ids,
    deleting any outfit left with zero items, and deleting that outfit's
    feedback. Only the sibling behavior - deleting an *outfit* directly
    cascades its feedback - is covered (test_outfits_route.py). This is the
    one item of the four with no prior coverage at any backend."""
    user = await _make_user(live_db, "m20cascadeuser")
    item_id = str(
        (await live_db.wardrobe.insert_one({"user_id": user["user_id"], "name": "Only Item"})).inserted_id
    )
    outfit_id = str(
        (
            await live_db.outfits.insert_one(
                {"user_id": user["user_id"], "item_ids": [item_id]}
            )
        ).inserted_id
    )
    await live_db.feedback.insert_one(
        {"user_id": user["user_id"], "outfit_id": outfit_id, "rating": 4}
    )

    resp = await real_client.delete(f"/wardrobe/{item_id}", headers=user["headers"])

    assert resp.status_code == 204
    assert await live_db.wardrobe.count_documents({"_id": ObjectId(item_id)}) == 0
    assert await live_db.outfits.count_documents({"_id": ObjectId(outfit_id)}) == 0
    assert await live_db.feedback.count_documents({"outfit_id": outfit_id}) == 0


@pytest.mark.asyncio
async def test_claim_next_job_never_double_claims_under_real_concurrent_access(live_db):
    """test_chat_concurrency.py proves claim_next_job's atomicity under
    mongomock with asyncio-interleaved workers in one process - a real,
    meaningful test, but mongomock's find_one_and_update is an in-memory
    operation with no real server-side concurrency control behind it. This
    drives the exact same function with real overlapping network round-trips
    to an actual MongoDB server, which is the only way a regression that
    e.g. split the atomic find_one_and_update into a separate find + update
    would ever actually be caught."""
    job_ids = []
    for i in range(15):
        result = await live_db.chat_jobs.insert_one(
            {
                "user_id": f"user-{i}",
                "prompt": f"job-{i}",
                "status": "pending",
                "priority": 0,
                "attempts": 0,
                "created_at": datetime.now(timezone.utc),
            }
        )
        job_ids.append(result.inserted_id)

    results = await asyncio.gather(*(claim_next_job(live_db) for _ in range(30)))
    claimed = [r for r in results if r is not None]

    claimed_ids = [c["_id"] for c in claimed]
    assert len(claimed_ids) == 15
    assert len(set(claimed_ids)) == 15
    assert set(claimed_ids) == set(job_ids)

    docs = await live_db.chat_jobs.find({}).to_list(length=None)
    assert all(d["status"] == "processing" for d in docs)
    assert all(d["attempts"] == 1 for d in docs)
