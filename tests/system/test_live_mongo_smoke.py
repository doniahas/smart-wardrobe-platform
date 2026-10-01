"""Live-MongoDB smoke test - the one thing mongomock cannot prove
(see docs/testing.md's "Known gaps"): that a unique index declared by
app.core.database._ensure_indexes actually rejects a duplicate insert
against a *real* MongoDB server, not just that mongomock reports the
index as declared unique (test_indexes.py already covers that half).

Reuses the real, production _ensure_indexes function rather than
re-declaring index shapes here. Covers the two distinct unique-index
shapes that exist in the schema: a single field (users.username) and a
compound key (feedback's (user_id, outfit_id)) - not every unique index
in the schema, since both already share the same underlying MongoDB
enforcement mechanism and a third example would prove nothing new.

Only runs against a real MongoDB server via MONGO_URI (see
.github/workflows/ci.yml's live-mongo-smoke job's `mongo` service
container) - never part of the mongomock-based per-service unit suites.

The `live_db` fixture lives in conftest.py (shared with
test_live_mongo_integration.py) rather than here.
"""
import pytest
from pymongo.errors import DuplicateKeyError


@pytest.mark.asyncio
async def test_duplicate_username_rejected_by_real_mongo(live_db):
    await live_db.users.insert_one(
        {"username": "dup", "email": "a@example.com", "hashed_password": "x"}
    )
    with pytest.raises(DuplicateKeyError):
        await live_db.users.insert_one(
            {"username": "dup", "email": "b@example.com", "hashed_password": "x"}
        )


@pytest.mark.asyncio
async def test_duplicate_feedback_per_user_per_outfit_rejected_by_real_mongo(live_db):
    await live_db.feedback.insert_one({"user_id": "u1", "outfit_id": "o1", "rating": 5})
    with pytest.raises(DuplicateKeyError):
        await live_db.feedback.insert_one({"user_id": "u1", "outfit_id": "o1", "rating": 3})
