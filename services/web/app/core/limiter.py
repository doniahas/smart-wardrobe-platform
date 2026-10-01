from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import RATE_LIMIT_DEFAULT, REDIS_URL, TESTING

# Shared instance so both main.py (middleware/app wiring) and individual
# routers (per-route @limiter.limit(...) decorators) reference the same
# limiter without a circular import.
#
# key_style="endpoint": slowapi's default ("url") scopes each limit bucket by
# the literal request path, so a path-parameterized route (e.g. PUT/DELETE
# /wardrobe/{item_id}) gets a fresh, independent bucket per distinct id -
# defeating the limit entirely for anyone varying which resource they hit.
# "endpoint" scopes by the decorated view function instead, so all calls to
# the same route share one bucket regardless of which resource id is in the
# URL. Routes with no path parameters (auth, recommendations, wardrobe/
# feedback/outfit creation) already have a 1:1 url<->endpoint mapping, so
# this changes nothing for them.
#
# storage_uri (see docs/architecture.md's "Shared, replica-safe rate
# limiting" for the design): normal runtime points
# every web replica's rate-limit counters at the same shared Redis instance
# (REDIS_URL), so N replicas enforce one combined ceiling per client key
# instead of N independent ones. TESTING keeps storage_uri=None, which
# resolves to slowapi/limits' own unchanged "memory://" default - the
# entire existing test suite (test_rate_limiting.py, test_config.py, every
# other test exercising a rate-limited route) depends on that exact,
# deterministic in-process behavior and none of it needs a shared backend.
#
# Redis-failure behavior below uses only slowapi's own, already-supported
# Limiter constructor options - no custom exception-wrapper code was
# written, per this phase's design instruction to prefer the smallest
# supported mechanism:
#   - in_memory_fallback_enabled + in_memory_fallback: verified directly
#     (see the implementation report) that the FIRST storage error on any
#     request makes this Limiter transparently switch every subsequent
#     check to a private, per-process MemoryStorage enforcing the
#     RATE_LIMIT_DEFAULT ceiling instead, until slowapi's own periodic,
#     exponential-backoff health probe confirms the real storage is
#     reachable again - automatic recovery, no restart needed, and no
#     window where a request is entirely unprotected.
#   - swallow_errors: a backstop only, not the primary mechanism - if some
#     other, unexpected error still escapes the fallback path, the request
#     proceeds unlimited (logged) rather than failing with a 500. Verified
#     directly that even a completely unreachable storage_uri never
#     produces a 500: the worst case is graceful fallback-limit
#     enforcement (a 429 once that separate, smaller ceiling is hit), never
#     an unhandled exception.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT_DEFAULT],
    key_style="endpoint",
    storage_uri=None if TESTING else REDIS_URL,
    in_memory_fallback_enabled=True,
    in_memory_fallback=[RATE_LIMIT_DEFAULT],
    swallow_errors=True,
)
