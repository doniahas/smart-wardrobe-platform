"""Cross-replica delivery: proves services/redis_client.py's
Pub/Sub mechanics directly - shared-channel fan-out to multiple
independent 'replica' delivery targets, exactly-once delivery, key
isolation, Mongo-write independence, and Redis-failure/reconnect
behavior - without needing a real Redis process (fakeredis[lua] stands
in, exactly like test_rate_limiting_shared_storage.py's shared-server
technique) or a real multi-container deployment.

Each simulated "replica" here is a small, self-contained stand-in for one
web process's own services/live.py-shaped local socket registry (a dict
of user_id -> sockets, plus a delivery function with the exact same shape
as live.py::_deliver_locally) - not the real, singleton live.py module
itself, which cannot have two independent instances within one Python
process. What's under test is the REAL services/redis_client.py module's
publish/subscribe/fan-out code, wired to these lightweight stand-ins via
start_live_subscriber's on_event callback parameter, exactly as main.py
wires it to the real live.py in production - and, for scenario E, the
REAL services/notifications.py::create_notification function, to prove
the Mongo-write guarantee end to end rather than only in isolation.

Every test uses redis_client.reset() (via the shared_fake_redis fixture)
between constructing each simulated replica specifically to clear
start_live_subscriber's own single-subscriber-per-process bookkeeping -
each simulated replica genuinely represents a different process in
production, so resetting that bookkeeping between them is the correct,
documented way to model that in one test process (see
redis_client.reset()'s own docstring). Within a single simulated replica,
that same guard is exercised directly (test_starting_subscriber_twice...).
"""
import asyncio

import fakeredis.aioredis as fakeredis_aioredis
import pytest
import redis.asyncio as redis_asyncio
from bson import ObjectId

from app.services import redis_client
from app.services.notifications import create_notification


class _FakeSocket:
    """Stands in for a WebSocket connection: records every payload it was
    sent, in order."""

    def __init__(self):
        self.received: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.received.append(payload)


class _FakeReplica:
    """One simulated web replica's own local socket registry + delivery
    function - the same shape as live.py's _connections/_deliver_locally,
    but independently instantiable so several can coexist in one test
    process (live.py's own registry is a true module-level singleton and
    cannot be duplicated this way - see this file's module docstring)."""

    def __init__(self):
        self.connections: dict[str, list[_FakeSocket]] = {}

    def register(self, user_id: str, socket: _FakeSocket) -> None:
        self.connections.setdefault(user_id, []).append(socket)

    async def deliver(self, user_id: str, payload: dict) -> None:
        for socket in self.connections.get(user_id, []):
            await socket.send_json(payload)


@pytest.fixture()
def shared_fake_redis(monkeypatch):
    """One fakeredis "server" shared by every redis_client-backed replica
    constructed while this fixture is active. redis_client.py always
    builds its clients via the module-level redis.asyncio.from_url(uri) -
    monkeypatching that one function (never a private/undocumented
    internal) makes any number of independently-started subscriber tasks
    - simulating separate web replicas - share one Redis Pub/Sub channel,
    exactly like pointing them at the same real Redis would. Reverted
    automatically by monkeypatch after the test."""
    server = fakeredis_aioredis.FakeServer()

    def _fake_from_url(url, **kwargs):
        return fakeredis_aioredis.FakeRedis(server=server)

    monkeypatch.setattr(redis_asyncio, "from_url", _fake_from_url)
    redis_client.reset()
    yield server
    redis_client.reset()


async def _wait_until(condition, *, timeout_s: float = 2.0, poll_s: float = 0.01) -> None:
    elapsed = 0.0
    while not condition() and elapsed < timeout_s:
        await asyncio.sleep(poll_s)
        elapsed += poll_s
    assert condition(), "condition not met before timeout"


async def _start_ready_subscriber(on_event) -> tuple[asyncio.Task, asyncio.Event]:
    """start_live_subscriber, but deterministically awaited until its
    subscription has actually taken effect before returning.

    Without this, there is a real race between "start_live_subscriber
    returned" and "the subscription has actually been registered on the
    shared channel" - asyncio.create_task only schedules the subscriber
    coroutine, it does not run it, so a publish immediately afterward can
    genuinely reach the channel before the subscriber has subscribed to
    it, and (correctly, matching real Redis Pub/Sub semantics) be
    delivered to nobody. Confirmed directly while writing these tests -
    every test below that publishes immediately after starting a
    subscriber, without this helper, was flaky/failing for exactly this
    reason. ready_event (see redis_client._subscriber_loop) is the
    deterministic fix - no arbitrary sleep/guesswork involved.
    """
    ready_event = asyncio.Event()
    task, stop_event = redis_client.start_live_subscriber(on_event, ready_event=ready_event)
    await asyncio.wait_for(ready_event.wait(), timeout=2.0)
    return task, stop_event


# --- A/B: cross-replica delivery for the two real notification-shaped events ---


@pytest.mark.asyncio
async def test_replica_2_publish_reaches_user_socket_owned_by_replica_1(shared_fake_redis):
    """Scenario A: a DM-notification-shaped event, published by whichever
    replica handled the triggering request, reaches a socket the
    *receiving* replica owns."""
    replica1 = _FakeReplica()
    socket = _FakeSocket()
    replica1.register("user-a", socket)

    task, stop_event = await _start_ready_subscriber(replica1.deliver)
    try:
        payload = {"event": "notification", "notification": {"type": "message"}}
        await redis_client.publish_live_event("user-a", payload)

        await _wait_until(lambda: len(socket.received) == 1)
        assert socket.received == [payload]
    finally:
        await redis_client.stop_live_subscriber(task, stop_event)


@pytest.mark.asyncio
async def test_replica_2_publish_like_dislike_reaches_user_socket_owned_by_replica_1(
    shared_fake_redis,
):
    """Scenario B: same mechanism, a like/dislike-shaped event instead -
    proving delivery isn't special-cased to one notification type."""
    replica1 = _FakeReplica()
    socket = _FakeSocket()
    replica1.register("user-a", socket)

    task, stop_event = await _start_ready_subscriber(replica1.deliver)
    try:
        payload = {"event": "notification", "notification": {"type": "like_post"}}
        await redis_client.publish_live_event("user-a", payload)

        await _wait_until(lambda: len(socket.received) == 1)
        assert socket.received[0]["notification"]["type"] == "like_post"
    finally:
        await redis_client.stop_live_subscriber(task, stop_event)


# --- C/D: multi-socket fan-out, same replica and across replicas ---


@pytest.mark.asyncio
async def test_two_sockets_same_replica_each_receive_exactly_once(shared_fake_redis):
    """Scenario C: two tabs/devices for one user on the SAME replica -
    both receive the event, each exactly once."""
    replica1 = _FakeReplica()
    socket1, socket2 = _FakeSocket(), _FakeSocket()
    replica1.register("user-a", socket1)
    replica1.register("user-a", socket2)

    task, stop_event = await _start_ready_subscriber(replica1.deliver)
    try:
        await redis_client.publish_live_event("user-a", {"n": 1})

        await _wait_until(lambda: len(socket1.received) == 1 and len(socket2.received) == 1)
        assert socket1.received == [{"n": 1}]
        assert socket2.received == [{"n": 1}]
    finally:
        await redis_client.stop_live_subscriber(task, stop_event)


@pytest.mark.asyncio
async def test_sockets_across_two_replicas_each_receive_exactly_once(shared_fake_redis):
    """Scenario D: sockets for the same user spread across two simulated
    replicas - every socket receives the event exactly once; no socket
    receives a duplicate."""
    replica1 = _FakeReplica()
    replica2 = _FakeReplica()
    socket1, socket2 = _FakeSocket(), _FakeSocket()
    replica1.register("user-a", socket1)
    replica2.register("user-a", socket2)

    task1, stop1 = await _start_ready_subscriber(replica1.deliver)
    # Clears only start_live_subscriber's single-subscriber bookkeeping -
    # task1 keeps running unaffected (asyncio.create_task already
    # scheduled it independently of this bookkeeping) - modeling "replica
    # 2 is a distinct real process", exactly as redis_client.reset()'s own
    # docstring describes.
    redis_client.reset()
    task2, stop2 = await _start_ready_subscriber(replica2.deliver)
    try:
        await redis_client.publish_live_event("user-a", {"n": 1})

        await _wait_until(lambda: len(socket1.received) == 1 and len(socket2.received) == 1)
        assert socket1.received == [{"n": 1}]
        assert socket2.received == [{"n": 1}]
    finally:
        await redis_client.stop_live_subscriber(task1, stop1)
        await redis_client.stop_live_subscriber(task2, stop2)


@pytest.mark.asyncio
async def test_event_never_delivered_to_a_different_users_socket(shared_fake_redis):
    """Security-relevant: subscriber delivery must only ever reach sockets
    stored under the event's exact recipient_id - never a broadcast."""
    replica = _FakeReplica()
    socket_a, socket_b = _FakeSocket(), _FakeSocket()
    replica.register("user-a", socket_a)
    replica.register("user-b", socket_b)

    task, stop_event = await _start_ready_subscriber(replica.deliver)
    try:
        await redis_client.publish_live_event("user-a", {"n": 1})
        await _wait_until(lambda: len(socket_a.received) == 1)

        await asyncio.sleep(0.05)  # give a wrongly-delivered event a chance to arrive
        assert socket_b.received == []
    finally:
        await redis_client.stop_live_subscriber(task, stop_event)


# --- E: Mongo write count is independent of subscriber/replica count ---


@pytest.mark.asyncio
async def test_create_notification_writes_mongo_exactly_once_with_multiple_subscribers(
    shared_fake_redis, db
):
    """Scenario E: uses the REAL create_notification (not a stand-in),
    with two live subscriber replicas actively running, to prove the
    Mongo write count is completely independent of how many replicas are
    subscribed to the channel it publishes on."""
    replica1 = _FakeReplica()
    replica2 = _FakeReplica()
    task1, stop1 = await _start_ready_subscriber(replica1.deliver)
    redis_client.reset()
    task2, stop2 = await _start_ready_subscriber(replica2.deliver)
    try:
        await create_notification(
            db,
            recipient_id="user-a",
            actor_id="user-b",
            actor_username="userb",
            notif_type="message",
            target_type="message",
            target_id=ObjectId(),
        )

        docs = [doc async for doc in db.notifications.find({"recipient_id": "user-a"})]
        assert len(docs) == 1
    finally:
        await redis_client.stop_live_subscriber(task1, stop1)
        await redis_client.stop_live_subscriber(task2, stop2)


# --- F: Redis publish failure must not affect the Mongo write or the caller ---


@pytest.mark.asyncio
async def test_publish_failure_does_not_prevent_or_duplicate_mongo_write(monkeypatch, db):
    """Scenario F (unit level): a genuinely failing publish path must
    never prevent create_notification's Mongo write, and must never
    propagate as an exception to its caller."""

    class _BrokenClient:
        async def publish(self, *args, **kwargs):
            raise ConnectionError("simulated Redis outage")

        async def aclose(self):
            pass

    monkeypatch.setattr(redis_asyncio, "from_url", lambda *a, **kw: _BrokenClient())

    await create_notification(
        db,
        recipient_id="user-a",
        actor_id="user-b",
        actor_username="userb",
        notif_type="message",
        target_type="message",
        target_id=ObjectId(),
    )

    docs = [doc async for doc in db.notifications.find({"recipient_id": "user-a"})]
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_unreachable_redis_via_real_route_still_returns_success_and_persists(
    client, make_user, monkeypatch
):
    """Scenario F (route level): the same guarantee, exercised through a
    real HTTP request rather than calling create_notification directly -
    proves the whole request/response cycle survives a live-broker
    failure, not just the function in isolation."""
    sender = await make_user("phase2failsender")
    recipient = await make_user("phase2failrecipient")

    class _BrokenClient:
        async def publish(self, *args, **kwargs):
            raise ConnectionError("simulated Redis outage")

        async def aclose(self):
            pass

    monkeypatch.setattr(redis_asyncio, "from_url", lambda *a, **kw: _BrokenClient())

    resp = client.post(
        f"/messages/{recipient['user_id']}", data={"body": "hi"}, headers=sender["headers"]
    )
    assert resp.status_code == 201  # never a 500 solely because the live publish failed

    notifs = client.get("/notifications", headers=recipient["headers"]).json()["items"]
    assert len(notifs) == 1
    assert notifs[0]["type"] == "message"


# --- G: subscriber survives a transient connection failure and reconnects ---


@pytest.mark.asyncio
async def test_subscriber_recovers_after_transient_connection_failure(
    shared_fake_redis, monkeypatch
):
    """Scenario G: a failure on the subscriber's own connection attempt
    must not permanently kill the subscriber task - it must retry (after
    a short, test-scoped backoff) and successfully deliver once the
    'connection' recovers."""
    monkeypatch.setattr(redis_client, "_RECONNECT_BACKOFF_SECONDS", (0.01, 0.01, 0.01))

    replica = _FakeReplica()
    socket = _FakeSocket()
    replica.register("user-a", socket)

    real_get_client = redis_client._get_client
    attempt_count = {"n": 0}

    class _FlakyClient:
        def __init__(self, real_client):
            self._real = real_client

        def pubsub(self):
            attempt_count["n"] += 1
            if attempt_count["n"] == 1:
                raise ConnectionError("simulated transient failure")
            return self._real.pubsub()

    def _flaky_get_client():
        return _FlakyClient(real_get_client())

    monkeypatch.setattr(redis_client, "_get_client", _flaky_get_client)

    # _start_ready_subscriber's ready_event only fires on a SUCCESSFUL
    # subscribe, so awaiting it here already waits out the first,
    # deliberately-failing attempt plus its (test-shortened) backoff.
    task, stop_event = await _start_ready_subscriber(replica.deliver)
    try:
        assert attempt_count["n"] >= 2  # failed once, then actually retried
        assert not task.done()  # survived the failure, did not crash

        await redis_client.publish_live_event("user-a", {"n": 1})
        await _wait_until(lambda: len(socket.received) == 1)
        assert socket.received == [{"n": 1}]
    finally:
        await redis_client.stop_live_subscriber(task, stop_event)


@pytest.mark.asyncio
async def test_malformed_published_message_is_dropped_not_fatal(shared_fake_redis):
    """Defensive coverage alongside scenario G: a malformed payload on the
    shared channel (never produced by this codebase's own publisher, but
    not something the subscriber should ever trust blindly) must be
    dropped, not crash the subscriber task."""
    replica = _FakeReplica()
    socket = _FakeSocket()
    replica.register("user-a", socket)

    task, stop_event = await _start_ready_subscriber(replica.deliver)
    try:
        raw_client = redis_client._get_client()
        await raw_client.publish(redis_client.LIVE_EVENTS_CHANNEL, "not valid json")
        await asyncio.sleep(0.05)
        assert not task.done()

        await redis_client.publish_live_event("user-a", {"n": 1})
        await _wait_until(lambda: len(socket.received) == 1)
    finally:
        await redis_client.stop_live_subscriber(task, stop_event)


# --- H: starting a subscriber twice in one process never double-delivers ---


@pytest.mark.asyncio
async def test_starting_subscriber_twice_without_stopping_raises_and_never_duplicates(
    shared_fake_redis,
):
    """Scenario H: calling start_live_subscriber a second time in the same
    process, without stopping the first, must raise rather than silently
    create a second, independently-subscribed task - which would
    otherwise deliver every event twice."""
    replica = _FakeReplica()
    socket = _FakeSocket()
    replica.register("user-a", socket)

    task, stop_event = await _start_ready_subscriber(replica.deliver)
    try:
        with pytest.raises(RuntimeError):
            redis_client.start_live_subscriber(replica.deliver)

        await redis_client.publish_live_event("user-a", {"n": 1})
        await _wait_until(lambda: len(socket.received) >= 1)
        await asyncio.sleep(0.05)  # give a hypothetical second delivery a chance to arrive
        assert socket.received == [{"n": 1}]
    finally:
        await redis_client.stop_live_subscriber(task, stop_event)
