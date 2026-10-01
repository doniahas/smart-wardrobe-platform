"""Shared rate limiting: proves the Redis-backed rate-
limit design core/limiter.py now uses in normal (non-TESTING) runtime,
without needing a real Redis process or touching the app's own shared
`limiter` singleton (which conftest.py's autouse fixtures already reset
between every test and which stays on the unchanged in-memory backend
under TESTING - see core/limiter.py's own comment for why).

Each test here builds its own, fully independent Limiter/FastAPI app
pair(s), mirroring exactly how core/limiter.py constructs the real one
(same key_func shape, same key_style, same fallback/swallow_errors
options), so what's proven here is a direct stand-in for the real
configuration - not a different, hypothetical setup.

Two independent techniques are used, verified directly before being
committed to this file:

1. Scenarios A/B/D ("shared Redis, multiple replicas") use `fakeredis`
   (the `[lua]` extra specifically - `limits`' RedisStorage implements its
   rate-limit operations as server-side Lua scripts via EVALSHA, and plain
   fakeredis without that extra was directly confirmed to raise
   "unknown command 'evalsha'" against them) as an in-memory stand-in for
   a real Redis server. `limits.storage.redis.RedisStorage` always builds
   its client via the module-level `redis.from_url(uri)` - monkeypatching
   that one function (not any private/undocumented `limits`/slowapi
   internal) to return a `fakeredis.FakeStrictRedis` bound to one shared
   `fakeredis.FakeServer()` makes any number of independently-constructed
   `Limiter(storage_uri="redis://...")` instances - simulating separate
   web replicas - transparently share one counter, exactly like pointing
   them at the same real Redis would.
2. Scenario C ("Redis unavailable") uses a `storage_uri` pointing at a
   real, genuinely-nothing-listening TCP endpoint (`redis://127.0.0.1:1/0`)
   - no mocking at all - so the exact real connection-error path Redis
   being down in production would trigger is what's actually exercised.
"""
import redis
import fakeredis
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware


def _make_limiter(*, storage_uri: str, key_func) -> Limiter:
    """Mirrors core/limiter.py's real, non-TESTING construction exactly -
    same key_style, same fallback/swallow_errors options - so what these
    tests prove is a direct stand-in for the production configuration."""
    return Limiter(
        key_func=key_func,
        key_style="endpoint",
        storage_uri=storage_uri,
        in_memory_fallback_enabled=True,
        in_memory_fallback=["2/minute"],
        swallow_errors=True,
    )


def _make_app(limiter: Limiter, *, route_limit: str) -> TestClient:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/ping")
    @limiter.limit(route_limit)
    async def ping(request: Request):
        return {"ok": True}

    return TestClient(app)


@pytest.fixture()
def shared_fake_redis(monkeypatch):
    """One fakeredis "server" shared by every Limiter constructed while
    this fixture is active - the in-memory stand-in for "one real Redis
    instance reachable by every web replica". monkeypatch.setattr reverts
    this automatically after the test, so it can never leak into another
    test (including the app's own, separately-constructed `limiter`
    singleton used by every other test file)."""
    server = fakeredis.FakeServer()

    def fake_from_url(url, **kwargs):
        return fakeredis.FakeStrictRedis(server=server)

    monkeypatch.setattr(redis, "from_url", fake_from_url)
    return server


class TestSharedCounterAcrossReplicas:
    """Scenario A/D: two independently-constructed Limiter/app instances -
    standing in for two separate web replicas - sharing one Redis-
    compatible backend must enforce ONE combined counter per client key,
    not one independent counter each."""

    def test_two_independent_limiter_instances_share_one_counter(self, shared_fake_redis):
        replica_1 = _make_app(
            _make_limiter(storage_uri="redis://fake:6379/0", key_func=lambda request: "1.2.3.4"),
            route_limit="3/minute",
        )
        replica_2 = _make_app(
            _make_limiter(storage_uri="redis://fake:6379/0", key_func=lambda request: "1.2.3.4"),
            route_limit="3/minute",
        )

        # Alternate requests across the two "replicas" - a real attacker
        # distributing requests exactly like this is the scenario this
        # phase exists to close.
        codes = [
            replica_1.get("/ping").status_code,
            replica_2.get("/ping").status_code,
            replica_1.get("/ping").status_code,
            replica_2.get("/ping").status_code,  # 4th hit against a 3/minute limit
        ]

        assert codes == [200, 200, 200, 429]

    def test_shared_counter_holds_regardless_of_which_replica_is_hit_first(self, shared_fake_redis):
        """Same guarantee, exercised in the opposite order, to rule out an
        implementation that only happens to work for one specific
        request-arrival order."""
        replica_1 = _make_app(
            _make_limiter(storage_uri="redis://fake:6379/0", key_func=lambda request: "5.6.7.8"),
            route_limit="2/minute",
        )
        replica_2 = _make_app(
            _make_limiter(storage_uri="redis://fake:6379/0", key_func=lambda request: "5.6.7.8"),
            route_limit="2/minute",
        )

        assert replica_2.get("/ping").status_code == 200
        assert replica_1.get("/ping").status_code == 200
        assert replica_2.get("/ping").status_code == 429


class TestDistinctClientKeysStayIsolated:
    """Scenario B/H: sharing one Redis-compatible backend must never merge
    counters across different client keys - only requests from the SAME
    key should ever compete for the SAME ceiling."""

    def test_different_client_ips_do_not_share_a_bucket(self, shared_fake_redis):
        replica_a = _make_app(
            _make_limiter(storage_uri="redis://fake:6379/0", key_func=lambda request: "10.0.0.1"),
            route_limit="1/minute",
        )
        replica_b = _make_app(
            _make_limiter(storage_uri="redis://fake:6379/0", key_func=lambda request: "10.0.0.2"),
            route_limit="1/minute",
        )

        # Each key gets its own full allowance, even though both replicas
        # share the same underlying storage.
        assert replica_a.get("/ping").status_code == 200
        assert replica_b.get("/ping").status_code == 200
        # And each is now independently exhausted - one client's usage
        # never borrows from or blocks the other's.
        assert replica_a.get("/ping").status_code == 429
        assert replica_b.get("/ping").status_code == 429

    def test_same_replica_two_keys_stay_independent(self, shared_fake_redis):
        """The isolation guarantee also holds within a single 'replica' -
        not just across two - confirming key isolation is a property of
        the storage/key design, not an artifact of using two separate
        Limiter objects."""
        limiter = _make_limiter(storage_uri="redis://fake:6379/0", key_func=lambda request: request.headers["x-key"])
        app = FastAPI()
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.add_middleware(SlowAPIMiddleware)

        @app.get("/ping")
        @limiter.limit("1/minute")
        async def ping(request: Request):
            return {"ok": True}

        client = TestClient(app)
        assert client.get("/ping", headers={"x-key": "alice"}).status_code == 200
        assert client.get("/ping", headers={"x-key": "bob"}).status_code == 200
        assert client.get("/ping", headers={"x-key": "alice"}).status_code == 429
        assert client.get("/ping", headers={"x-key": "bob"}).status_code == 429


class TestRedisUnavailableFallback:
    """Scenario C: a genuinely unreachable storage_uri (real TCP endpoint,
    nothing listening - no mocking) must never surface as a 500, and must
    still enforce SOME bound (the configured in_memory_fallback) rather
    than silently disabling rate limiting altogether."""

    def test_unreachable_redis_falls_back_to_in_memory_limiting_without_500s(self):
        limiter = _make_limiter(
            storage_uri="redis://127.0.0.1:1/0",  # nothing listens here
            key_func=lambda request: "fixed-client",
        )
        client = _make_app(limiter, route_limit="100/minute")

        # First request: the real Redis connection attempt fails; slowapi
        # catches it, flips to the in-memory fallback storage, and this
        # request is still served normally (never a 500).
        first = client.get("/ping")
        assert first.status_code == 200
        assert limiter._storage_dead is True

        # Second request: still within the fallback's own 2/minute ceiling
        # (see _make_limiter's in_memory_fallback=["2/minute"]).
        second = client.get("/ping")
        assert second.status_code == 200

        # Third request: the fallback ceiling itself is genuinely
        # enforced - this must be a 429 (rate limiting still works, just
        # against a smaller, process-local ceiling), never a 500 and never
        # silently unlimited.
        third = client.get("/ping")
        assert third.status_code == 429

    def test_unreachable_redis_never_produces_a_500(self):
        """A stricter, explicit negative assertion: whatever happens,
        neither of the two possible non-2xx outcomes (429 from the
        fallback limit, or a clean pass-through) is ever a 500 - the one
        outcome this phase's design must guarantee is impossible."""
        limiter = _make_limiter(
            storage_uri="redis://127.0.0.1:1/0",
            key_func=lambda request: "fixed-client-2",
        )
        client = _make_app(limiter, route_limit="100/minute")

        statuses = {client.get("/ping").status_code for _ in range(5)}
        assert 500 not in statuses
        assert statuses <= {200, 429}
