"""Dedicated Redis Pub/Sub client
lifecycle for cross-replica live-event delivery (services/live.py).

Deliberately generic and standalone - this module knows nothing about
notifications, users, or _connections; it only ever publishes/subscribes
raw (recipient_id, payload) envelopes on one shared channel and invokes
whatever callback the caller wires in (see start_live_subscriber). This
keeps the dependency direction one-way (live.py imports this module to
publish; this module never imports live.py - no circular import), and
keeps this fully separate from core/limiter.py's own, independently-
constructed Redis client for rate-limit storage: both read the same
REDIS_URL, but by design the two subsystems must
never share one Python client object - different lifecycles (slowapi/
limits owns its storage client entirely and manages it internally; this
module's client is the only thing that ever touches this specific
instance), different libraries (`limits` uses the synchronous `redis`
client under the hood; this module uses `redis.asyncio` directly since a
long-lived Pub/Sub subscriber needs a real async listen loop, not a
polling wrapper around a sync client).

Reads REDIS_URL via app.core.config, exactly like every other internal
service URL this project already uses (RECOMMENDATION_SERVICE_URL,
LLM_SERVICE_URL, CATALOG_SERVICE_URL).
"""
import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

import redis.asyncio as redis_asyncio

from app.core.config import REDIS_URL

logger = logging.getLogger(__name__)

# One shared channel for every live event, regardless of recipient.
# One shared channel (not per-user channels) is the right-sized
# design at this project's scale: every replica already has to inspect
# its own local socket registry per event regardless of channel design,
# so per-user channels would only reduce which messages a replica has to
# evaluate, not what it has to do - and per-user channels would add real
# subscribe/unsubscribe churn (and a genuine race window between a socket
# connecting and its channel subscription completing) that this design
# avoids entirely.
LIVE_EVENTS_CHANNEL = "sw:live-notifications"

# Bounded, capped backoff between reconnect attempts after a lost/failed
# Redis connection - same intent as chat_worker.py's own error-isolation
# loops (survive an error and keep going, never crash the task),
# deliberately not imported from there to keep this module fully
# standalone from the chat subsystem.
_RECONNECT_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

OnLiveEvent = Callable[[str, dict], Awaitable[None]]

_client: Optional["redis_asyncio.Redis"] = None
_subscriber_task: Optional[asyncio.Task] = None


def _get_client() -> "redis_asyncio.Redis":
    """Lazily construct (once per process) the single Redis client this
    module uses for both publishing and subscribing.

    Construction itself never connects - redis.asyncio.from_url returns a
    client backed by a lazy connection pool (the same property already
    relied on for core/limiter.py's storage client; see that module's own
    comment). The real connection attempt only happens
    on the first actual command (a publish, or the subscriber's own
    pubsub().subscribe(...)) - so this never blocks or fails app startup
    even if Redis is completely unreachable at that moment.
    """
    global _client
    if _client is None:
        _client = redis_asyncio.from_url(REDIS_URL)
    return _client


async def publish_live_event(recipient_id: str, payload: dict) -> None:
    """Best-effort publish of one live event to the shared channel.

    Never raises - services/live.py::send_to_user calls this only after
    the caller (services/notifications.py::create_notification) has
    already durably persisted the Mongo notification document; a publish
    failure here must never affect that fact, undo it, or surface as an
    error to the HTTP request that triggered it. A failure is logged and
    swallowed, exactly mirroring send_to_user's own pre-existing "never
    raises" contract - Redis Pub/Sub is best-effort by design, Mongo stays
    the only durable store.

    Deliberately constructs its own short-lived client per call rather
    than reusing _get_client()'s cached subscriber client: a publish is a
    rare, cheap, one-shot command (unlike the subscriber's single
    long-lived connection, which benefits from being kept alive), and
    keeping the two fully independent avoids any assumption that both
    ever run on the same asyncio event loop - true for a real single-
    process uvicorn deployment, but not true in this project's own test
    suite, where a synchronous TestClient call can run ASGI code on a
    short-lived event loop distinct from the one driving the background
    subscriber (see the fixture in tests/conftest.py for exactly this).
    """
    message = json.dumps({"recipient_id": recipient_id, "payload": payload})
    client = None
    try:
        client = redis_asyncio.from_url(REDIS_URL)
        await client.publish(LIVE_EVENTS_CHANNEL, message)
    except Exception:
        logger.warning(
            "redis_client: failed to publish a live event (Redis unavailable?)",
            exc_info=True,
        )
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass


async def _subscriber_loop(
    on_event: OnLiveEvent,
    stop_event: asyncio.Event,
    ready_event: Optional[asyncio.Event] = None,
) -> None:
    """The subscriber's actual run loop - not part of the public API
    (start_live_subscriber is), but composable/directly usable by tests
    that need to simulate more than one independent subscriber within a
    single process (see test_live_broker_redis.py) - real deployments
    only ever run one of these per process, via start_live_subscriber.

    Survives any connection failure (initial subscribe or a later drop
    mid-listen) by logging it and retrying after a bounded backoff,
    exactly like chat_worker.py's own worker/reaper loops survive an
    unexpected error rather than dying. A bad on_event callback (e.g. one
    socket's send raising) is caught per-message so it can never take
    down delivery for every other event.

    ready_event, if given, is set every time a (re)subscribe actually
    succeeds - a deterministic "this subscriber can now actually receive
    messages" signal. Production never needs this (there is nothing to
    synchronize against); tests use it to avoid a real, otherwise-present
    race between "start_live_subscriber returned" and "the subscription
    has actually taken effect on the shared channel" - publishing before
    a subscriber has finished subscribing would, correctly, deliver to
    nobody, exactly like a real Redis Pub/Sub would.
    """
    backoff_index = 0
    while not stop_event.is_set():
        pubsub = None
        try:
            client = _get_client()
            pubsub = client.pubsub()
            await pubsub.subscribe(LIVE_EVENTS_CHANNEL)
            backoff_index = 0  # a successful (re)subscribe resets the backoff
            if ready_event is not None:
                ready_event.set()
            async for message in pubsub.listen():
                if stop_event.is_set():
                    break
                if message.get("type") != "message":
                    continue
                try:
                    envelope = json.loads(message["data"])
                    recipient_id = envelope["recipient_id"]
                    event_payload = envelope["payload"]
                except (ValueError, KeyError, TypeError):
                    logger.warning("redis_client: dropped a malformed live event")
                    continue
                try:
                    await on_event(recipient_id, event_payload)
                except Exception:
                    logger.exception(
                        "redis_client: on_event callback raised while delivering "
                        "a live event"
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            if ready_event is not None:
                ready_event.clear()
            logger.warning(
                "redis_client: live-event subscriber lost its Redis connection; "
                "will retry with backoff",
                exc_info=True,
            )
        finally:
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception:
                    pass

        if stop_event.is_set():
            break
        delay = _RECONNECT_BACKOFF_SECONDS[min(backoff_index, len(_RECONNECT_BACKOFF_SECONDS) - 1)]
        backoff_index += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


def start_live_subscriber(
    on_event: OnLiveEvent,
    ready_event: Optional[asyncio.Event] = None,
) -> tuple[asyncio.Task, asyncio.Event]:
    """Start the single, process-wide live-event subscriber task.

    Guards against accidentally starting a second one in the same process
    without stopping the first: Redis Pub/Sub delivers once per
    subscribed connection, so two independent subscriptions in one
    process would each independently receive and act on every message -
    a genuine duplicate-delivery bug, not a redundant safety net. Raises
    RuntimeError if a subscriber is already tracked as running; callers
    that genuinely need more than one independent subscriber inside a
    single process (only ever true in tests simulating several replicas -
    see test_live_broker_redis.py) must call reset() first to clear this
    bookkeeping between simulated "replicas".

    Never blocks on Redis being reachable - asyncio.create_task schedules
    the coroutine without awaiting it, so a slow/unreachable Redis at
    startup cannot delay or fail normal web startup; the subscriber loop
    itself handles a failed initial connection via the same retry/backoff
    path as any later disconnect.

    ready_event is optional and unused in production (main.py's real
    call site never passes one) - see _subscriber_loop's own docstring
    for what it's for and why only tests need it.
    """
    global _subscriber_task
    if _subscriber_task is not None and not _subscriber_task.done():
        raise RuntimeError(
            "start_live_subscriber called while a subscriber task is already "
            "running in this process - call stop_live_subscriber first."
        )
    stop_event = asyncio.Event()
    task = asyncio.create_task(_subscriber_loop(on_event, stop_event, ready_event))
    _subscriber_task = task
    return task, stop_event


async def stop_live_subscriber(task: asyncio.Task, stop_event: asyncio.Event) -> None:
    """Signal and cleanly stop the subscriber task started by
    start_live_subscriber.

    Unlike chat_worker.py's worker loops (which cooperatively check
    stop_event between polls), this loop spends most of its time blocked
    inside pubsub.listen()'s async iteration with no natural checkpoint to
    notice stop_event - cancelling the task directly is the correct way
    to interrupt that wait. There is no "let the current job finish
    first" concern here the way there is for a chat job: delivering one
    already-in-flight message is effectively instantaneous, so an
    immediate cancel is safe and never drops a message that was already
    handed to on_event.
    """
    global _subscriber_task
    stop_event.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    if _subscriber_task is task:
        _subscriber_task = None


def reset() -> None:
    """Test-only: clears this module's cached client AND its subscriber-
    tracking bookkeeping.

    Two distinct uses: (1) forces the next _get_client() call to
    reconstruct the client fresh - e.g. after a test monkeypatches
    redis.asyncio.from_url to point at a new fakeredis server; (2) lets a
    test simulate "a fresh process" between constructing several
    independent simulated replicas' subscriber tasks in test_live_broker_
    redis.py, when real production code only ever calls
    start_live_subscriber once per real process. Does NOT stop any task
    still running - a caller holding a (task, stop_event) handle must
    still stop it via stop_live_subscriber; this only clears bookkeeping,
    it never cancels anything.
    """
    global _client, _subscriber_task
    _client = None
    _subscriber_task = None
