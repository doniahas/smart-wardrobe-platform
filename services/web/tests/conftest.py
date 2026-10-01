import asyncio
import os
import threading
from datetime import datetime, timezone

import fakeredis.aioredis as fakeredis_aioredis
import pytest
import redis.asyncio as redis_asyncio
from bson import ObjectId
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

# Must be set before any `app.*` import below: app.core.config reads it at
# module load time to decide whether a real JWT_SECRET is required (see
# app/core/config.py) or a TESTING-only fallback is safe to use. Uses
# setdefault so a developer's own TESTING=false in their shell is never
# silently overridden.
os.environ.setdefault("TESTING", "true")

from app.auth import create_access_token, hash_password
from app.core.database import get_db
from app.core.limiter import limiter
from app.main import app
from app.services import live, redis_client


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Each test starts with a clean slowapi counter state.

    slowapi's default storage is in-memory and keyed by client address, which
    TestClient always reports as the same fixed address - without this reset,
    request counts would leak between tests and cause order-dependent 429s.
    """
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _reset_live_connections():
    """services/live.py's connection registry is a module-level, in-process
    singleton - without this reset, a WebSocket connection left registered
    by one test (e.g. one that doesn't cleanly disconnect) would leak into
    the next test and skew connection-limit/delivery assertions."""
    live.reset()
    yield
    live.reset()


@pytest.fixture()
def db():
    return AsyncMongoMockClient()["smartwardrobe_test"]


class _BackgroundSubscriber:
    """Test-only: runs services/redis_client.py's real subscriber loop on
    its own dedicated OS thread + event loop, rather than as a plain
    asyncio task on whatever loop the current test happens to be running
    on.

    This is necessary, not a stylistic choice: Starlette's TestClient,
    when never entered as a context manager (see the `client` fixture's
    own comment on why), dispatches every ordinary HTTP call through its
    own short-lived portal thread+loop, while a WebSocket session's
    `send_json`/`receive_json` are genuinely thread-blocking calls (not
    `await`ed, even from an `async def` test). If the subscriber instead
    ran as a bare `asyncio.create_task(...)` on the SAME loop as a test
    coroutine currently blocked inside one of those calls, it could never
    actually be scheduled to run - a real deadlock, reproduced directly
    while building this fixture (a live-delivery test hung indefinitely).
    Running it on its own thread/loop lets it make progress regardless of
    what the main test thread is currently blocked on, exactly as the
    real WebSocket connection's own portal thread already does.

    Deliberately test-only scaffolding - NOT something
    redis_client.py itself needs. A real uvicorn-driven deployment has
    exactly one event loop for the whole process and no synchronous
    cross-thread-blocking calls, so redis_client.start_live_subscriber's
    plain `asyncio.create_task` is correct and sufficient there; this
    class exists solely to bridge into this test suite's synchronous
    TestClient usage.
    """

    def __init__(self, on_event):
        self._on_event = on_event
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=5), "background subscriber thread failed to start"

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._stop_event = asyncio.Event()
        self._task = loop.create_task(redis_client._subscriber_loop(self._on_event, self._stop_event))
        self._ready.set()
        try:
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

    def stop(self) -> None:
        if self._loop is None:
            return

        def _request_stop() -> None:
            self._stop_event.set()
            self._task.cancel()

        self._loop.call_soon_threadsafe(_request_stop)
        self._thread.join(timeout=5)


@pytest.fixture()
def client(db, monkeypatch):
    # Note: intentionally not used as a context manager - entering it would
    # run the real app lifespan (connect_db against a real Mongo server),
    # which isn't available in tests. Routes only need get_db, overridden below.
    app.dependency_overrides[get_db] = lambda: db

    # Cross-replica delivery: services/live.py::send_to_user
    # delivers exclusively through Redis Pub/Sub (services/redis_client.py)
    # rather than a direct in-process call - main.py's real lifespan (which
    # starts one subscriber per real process) never runs here, since this
    # fixture deliberately never enters `app` as a context manager (see the
    # comment above), so without this, every existing live-delivery test
    # would hang waiting for an event nothing would ever deliver. A fresh
    # fakeredis "server" scoped to this one test stands in for Redis - no
    # real Redis process is needed to run this suite, and this in no way
    # weakens what those tests prove: the same production send_to_user ->
    # publish -> subscribe -> _deliver_locally path they always exercised
    # is what runs here, just backed by an in-memory fake instead of a real
    # Redis container. See _BackgroundSubscriber's own docstring for why
    # the subscriber must run on its own thread/loop, not a bare asyncio
    # task, in this specific test harness.
    server = fakeredis_aioredis.FakeServer()

    def _fake_from_url(url, **kwargs):
        return fakeredis_aioredis.FakeRedis(server=server)

    monkeypatch.setattr(redis_asyncio, "from_url", _fake_from_url)
    redis_client.reset()
    subscriber = _BackgroundSubscriber(live._deliver_locally)
    subscriber.start()

    test_client = TestClient(app)
    yield test_client

    subscriber.stop()
    redis_client.reset()
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def make_user(db):
    """Insert a user directly into the fake DB; return its id and auth header."""

    async def _make_user(username: str) -> dict:
        result = await db.users.insert_one(
            {
                "username": username,
                "email": f"{username}@example.com",
                "hashed_password": hash_password("password123"),
            }
        )
        token = create_access_token({"sub": username})
        return {
            "user_id": str(result.inserted_id),
            "headers": {"Authorization": f"Bearer {token}"},
        }

    return _make_user


@pytest.fixture()
def make_wardrobe_item(db):
    async def _make_item(user_id: str, **overrides) -> str:
        doc = {
            "user_id": user_id,
            "name": "Item",
            "clothing_type": "top",
            "color": "red",
            "season": "all",
            "style": "casual",
            "occasion": "casual",
            "image_url": None,
            "tags": [],
        }
        doc.update(overrides)
        result = await db.wardrobe.insert_one(doc)
        return str(result.inserted_id)

    return _make_item


@pytest.fixture()
def make_outfit(db):
    async def _make_outfit(user_id: str, item_ids: list[str], **overrides) -> str:
        doc = {
            "user_id": user_id,
            "item_ids": item_ids,
            "name": None,
            "occasion": None,
            "season": None,
            "style": None,
            "created_at": datetime.now(timezone.utc),
        }
        doc.update(overrides)
        result = await db.outfits.insert_one(doc)
        return str(result.inserted_id)

    return _make_outfit


@pytest.fixture()
def make_post(db):
    async def _make_post(author_id: str, **overrides) -> str:
        doc = {
            "author_id": author_id,
            "author_username": "someone",
            "is_anonymous": False,
            "title": "Title",
            "body": "Body",
            "created_at": datetime.now(timezone.utc),
        }
        doc.update(overrides)
        result = await db.posts.insert_one(doc)
        return str(result.inserted_id)

    return _make_post


@pytest.fixture()
def make_comment(db):
    async def _make_comment(post_id, author_id: str, **overrides) -> str:
        doc = {
            "post_id": ObjectId(post_id) if isinstance(post_id, str) else post_id,
            "author_id": author_id,
            "author_username": "someone",
            "body": "Comment body",
            "created_at": datetime.now(timezone.utc),
        }
        doc.update(overrides)
        result = await db.comments.insert_one(doc)
        return str(result.inserted_id)

    return _make_comment


@pytest.fixture()
def make_feedback(db):
    async def _make_feedback(user_id: str, outfit_id: str, rating: int, liked) -> None:
        now = datetime.now(timezone.utc)
        await db.feedback.insert_one(
            {
                "user_id": user_id,
                "outfit_id": outfit_id,
                "rating": rating,
                "liked": liked,
                "comment": None,
                "created_at": now,
                "updated_at": now,
            }
        )

    return _make_feedback