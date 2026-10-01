import asyncio

import httpx

from app.services.retry import CrawlFailureReason, RetryableError, run_with_retry


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid/products")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("status error", request=request, response=response)


class _FakeClock:
    """Records every requested delay instead of actually waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)


def _zero_jitter(delay: float) -> float:
    return 0.0


def _fixed_jitter(amount: float):
    def _jitter(delay: float) -> float:
        return amount

    return _jitter


# --- 1. succeeds on first attempt with no sleep ---

def test_succeeds_on_first_attempt_with_no_sleep():
    clock = _FakeClock()

    async def always_ok():
        return "value"

    async def run():
        return await run_with_retry(
            always_ok, max_attempts=3, base_delay_seconds=0.5, sleep=clock.sleep
        )

    outcome = asyncio.run(run())

    assert outcome.value == "value"
    assert outcome.reason is None
    assert outcome.attempts == 1
    assert clock.delays == []


# --- 2. fails transiently, then succeeds ---

def test_fails_transiently_then_succeeds():
    clock = _FakeClock()
    calls = {"count": 0}

    async def flaky():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RetryableError(CrawlFailureReason.SOURCE_TIMEOUT)
        return "recovered"

    async def run():
        return await run_with_retry(
            flaky,
            max_attempts=3,
            base_delay_seconds=0.5,
            sleep=clock.sleep,
            jitter=_zero_jitter,
        )

    outcome = asyncio.run(run())

    assert outcome.value == "recovered"
    assert outcome.reason is None
    assert outcome.attempts == 2
    assert clock.delays == [0.5]


# --- 3. stops after exact max_attempts ---

def test_stops_after_exact_max_attempts():
    clock = _FakeClock()
    calls = {"count": 0}

    async def always_fails():
        calls["count"] += 1
        raise RetryableError(CrawlFailureReason.SOURCE_UNAVAILABLE)

    async def run():
        return await run_with_retry(
            always_fails,
            max_attempts=3,
            base_delay_seconds=0.1,
            sleep=clock.sleep,
            jitter=_zero_jitter,
        )

    outcome = asyncio.run(run())

    assert outcome.value is None
    assert outcome.reason == CrawlFailureReason.SOURCE_UNAVAILABLE
    assert outcome.attempts == 3
    assert calls["count"] == 3
    assert len(clock.delays) == 2  # sleeps only happen *between* attempts


# --- 4. uses exponential delays ---

def test_uses_exponential_delays():
    clock = _FakeClock()

    async def always_fails():
        raise RetryableError(CrawlFailureReason.SOURCE_UNAVAILABLE)

    async def run():
        return await run_with_retry(
            always_fails,
            max_attempts=4,
            base_delay_seconds=0.5,
            sleep=clock.sleep,
            jitter=_zero_jitter,
        )

    asyncio.run(run())

    assert clock.delays == [0.5, 1.0, 2.0]


# --- 5. injected jitter is applied deterministically ---

def test_injected_jitter_is_applied_deterministically():
    clock = _FakeClock()

    async def always_fails():
        raise RetryableError(CrawlFailureReason.RATE_LIMITED)

    async def run():
        return await run_with_retry(
            always_fails,
            max_attempts=2,
            base_delay_seconds=1.0,
            sleep=clock.sleep,
            jitter=_fixed_jitter(0.25),
        )

    asyncio.run(run())

    assert clock.delays == [1.25]


# --- 6. timeout is classified correctly ---

def test_timeout_is_classified_as_source_timeout():
    async def times_out():
        raise httpx.ReadTimeout("timed out")

    async def run():
        return await run_with_retry(times_out, max_attempts=1, base_delay_seconds=0.1)

    outcome = asyncio.run(run())

    assert outcome.reason == CrawlFailureReason.SOURCE_TIMEOUT
    assert outcome.attempts == 1


# --- 7. connection error is classified correctly ---

def test_connection_error_is_classified_as_source_unavailable():
    async def cannot_connect():
        raise httpx.ConnectError("connection refused")

    async def run():
        return await run_with_retry(cannot_connect, max_attempts=1, base_delay_seconds=0.1)

    outcome = asyncio.run(run())

    assert outcome.reason == CrawlFailureReason.SOURCE_UNAVAILABLE
    assert outcome.attempts == 1


# --- 8. HTTP 429 is retryable and classified as rate_limited ---

def test_http_429_is_retryable_and_classified_as_rate_limited():
    clock = _FakeClock()
    calls = {"count": 0}

    async def rate_limited():
        calls["count"] += 1
        raise _http_status_error(429)

    async def run():
        return await run_with_retry(
            rate_limited,
            max_attempts=2,
            base_delay_seconds=0.1,
            sleep=clock.sleep,
            jitter=_zero_jitter,
        )

    outcome = asyncio.run(run())

    assert outcome.reason == CrawlFailureReason.RATE_LIMITED
    assert calls["count"] == 2
    assert clock.delays == [0.1]


# --- 9. HTTP 5xx is retryable ---

def test_http_5xx_is_retryable():
    clock = _FakeClock()
    calls = {"count": 0}

    async def server_error():
        calls["count"] += 1
        raise _http_status_error(503)

    async def run():
        return await run_with_retry(
            server_error,
            max_attempts=2,
            base_delay_seconds=0.1,
            sleep=clock.sleep,
            jitter=_zero_jitter,
        )

    outcome = asyncio.run(run())

    assert outcome.reason == CrawlFailureReason.SOURCE_UNAVAILABLE
    assert calls["count"] == 2
    assert clock.delays == [0.1]


# --- 10. ordinary HTTP 4xx is not retried ---

def test_ordinary_http_4xx_is_not_retried():
    clock = _FakeClock()
    calls = {"count": 0}

    async def not_found():
        calls["count"] += 1
        raise _http_status_error(404)

    async def run():
        return await run_with_retry(
            not_found, max_attempts=3, base_delay_seconds=0.1, sleep=clock.sleep
        )

    outcome = asyncio.run(run())

    assert outcome.reason == CrawlFailureReason.SOURCE_CLIENT_ERROR
    assert calls["count"] == 1
    assert clock.delays == []


# --- 11. parse/validation failure is not retried by default ---

def test_parse_failure_is_not_retried():
    clock = _FakeClock()
    calls = {"count": 0}

    async def bad_data():
        calls["count"] += 1
        raise ValueError("could not parse product")

    async def run():
        return await run_with_retry(
            bad_data, max_attempts=3, base_delay_seconds=0.1, sleep=clock.sleep
        )

    outcome = asyncio.run(run())

    assert outcome.reason == CrawlFailureReason.PARSE_ERROR
    assert calls["count"] == 1
    assert clock.delays == []


# --- 12. unexpected failure is contained and classified ---

def test_unexpected_failure_is_contained_and_classified():
    clock = _FakeClock()
    calls = {"count": 0}

    async def surprise():
        calls["count"] += 1
        raise RuntimeError("boom")

    async def run():
        return await run_with_retry(
            surprise, max_attempts=3, base_delay_seconds=0.1, sleep=clock.sleep
        )

    outcome = asyncio.run(run())  # must not raise

    assert outcome.reason == CrawlFailureReason.UNEXPECTED_ERROR
    assert calls["count"] == 1
    assert clock.delays == []