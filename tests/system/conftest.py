"""Shared real-MongoDB fixture for tests/system.

Factored out of test_live_mongo_smoke.py so test_live_mongo_integration.py
can reuse the exact same real-database setup/teardown instead
of redefining it - both files only ever need one thing from a real server:
a clean, indexed database, dropped afterwards.

MONGO_URI is read lazily, inside the fixture itself, rather than at module
import time: this conftest.py is shared by the whole tests/system directory,
including test_e2e_wardrobe_flow.py, which talks to a real running
`web` container over HTTP and never touches MongoDB directly. Reading
MONGO_URI eagerly at collection time would make every test in this
directory - including the ones that don't need it - fail to collect
whenever MONGO_URI isn't set (e.g. a plain local `pytest` run without the
live-Mongo CI service container). Deferring it to fixture setup time keeps
test_live_mongo_smoke.py/test_live_mongo_integration.py's own behavior
identical (they still require MONGO_URI, just at first use instead of at
import), while letting MONGO_URI-independent tests collect and run freely.
"""
import os

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.database import _ensure_indexes

_DATABASE_NAME = "smartwardrobe_ci_live_mongo_smoke"


@pytest.fixture
async def live_db():
    mongo_uri = os.environ["MONGO_URI"]
    client = AsyncIOMotorClient(mongo_uri)
    db = client[_DATABASE_NAME]
    await db.command("ping")
    await _ensure_indexes(db)
    try:
        yield db
    finally:
        await client.drop_database(_DATABASE_NAME)
        client.close()
