"""Dependency-free async retry/backoff helper.

Hand-rolled (matches this repo's existing convention of not depending on a
tenacity-style retry library - see services/web/app/services/llm_gateway.py
and ollama_client.py, which hand-roll the same "classify into a stable
enum, never raise for an expected failure" idiom): classifies every
failure into a small, stable CrawlFailureReason enum and retries only
failures that are plausibly transient, using exponential backoff with a
small amount of injectable jitter.
"""

import asyncio
import random
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Generic, Optional, TypeVar

import httpx
from pydantic import ValidationError as PydanticValidationError

T = TypeVar("T")


class CrawlFailureReason(str, Enum):
    SOURCE_TIMEOUT = "source_timeout"
    SOURCE_UNAVAILABLE = "source_unavailable"
    RATE_LIMITED = "rate_limited"
    SOURCE_CLIENT_ERROR = "source_client_error"
    PARSE_ERROR = "parse_error"
    UNEXPECTED_ERROR = "unexpected_error"


# Only failures plausibly transient (timeout, connection/network failure,
# 429, 5xx) are retried. Ordinary 4xx, parse/validation failures, and
# anything unclassified are never retried by default.
_RETRYABLE_REASONS = frozenset(
    {
        CrawlFailureReason.SOURCE_TIMEOUT,
        CrawlFailureReason.SOURCE_UNAVAILABLE,
        CrawlFailureReason.RATE_LIMITED,
    }
)


class RetryableError(Exception):
    """Raised by adapter-facing code to signal an already-classified failure.

    `reason` drives both retry eligibility and the detail stored on
    SourceStatus - never the raw exception message, so nothing
    exception-specific (which could contain secrets or internal detail) is
    ever exposed through the public API.
    """

    def __init__(self, reason: CrawlFailureReason):
        super().__init__(reason.value)
        self.reason = reason


@dataclass
class RetryOutcome(Generic[T]):
    """Result of run_with_retry(): exactly one of value/reason is set."""

    value: Optional[T]
    reason: Optional[CrawlFailureReason]
    attempts: int


def classify_exception(exc: BaseException) -> CrawlFailureReason:
    """Map a raised exception to a stable CrawlFailureReason.

    Only `.value` of the returned enum member may ever be surfaced through
    the public API (see SourceStatus.detail) - never str(exc) or exc.args,
    which could contain internal/secret detail.
    """
    if isinstance(exc, RetryableError):
        return exc.reason
    if isinstance(exc, httpx.TimeoutException):
        return CrawlFailureReason.SOURCE_TIMEOUT
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return CrawlFailureReason.RATE_LIMITED
        if status >= 500:
            return CrawlFailureReason.SOURCE_UNAVAILABLE
        return CrawlFailureReason.SOURCE_CLIENT_ERROR
    if isinstance(exc, httpx.RequestError):
        # Connection failures, DNS errors, protocol errors, etc.
        return CrawlFailureReason.SOURCE_UNAVAILABLE
    if isinstance(exc, (PydanticValidationError, ValueError)):
        return CrawlFailureReason.PARSE_ERROR
    return CrawlFailureReason.UNEXPECTED_ERROR


def _default_jitter(delay: float) -> float:
    """Small jitter: up to 10% of the un-jittered delay, added on top."""
    return delay * random.uniform(0.0, 0.1)


async def run_with_retry(
    func: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    base_delay_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[float], float] = _default_jitter,
) -> RetryOutcome[T]:
    """Call func() up to max_attempts times TOTAL, including the first call.

    Never raises - always returns a RetryOutcome with either a successful
    value or the final classified failure reason. Retries only failures
    classified as transient (see _RETRYABLE_REASONS), waiting
    base_delay_seconds * 2**(attempt-1) plus jitter(that delay) between
    attempts. Never retries indefinitely: bounded strictly by max_attempts.

    `sleep`/`jitter` are injectable so tests never wait in real time and
    stay fully deterministic.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(1, max_attempts + 1):
        try:
            value = await func()
            return RetryOutcome(value=value, reason=None, attempts=attempt)
        except Exception as exc:  # noqa: BLE001 - deliberately broad; classified below
            reason = classify_exception(exc)
            if reason not in _RETRYABLE_REASONS or attempt == max_attempts:
                return RetryOutcome(value=None, reason=reason, attempts=attempt)
            delay = base_delay_seconds * (2 ** (attempt - 1))
            await sleep(delay + jitter(delay))

    raise AssertionError("run_with_retry: unreachable - loop must always return")