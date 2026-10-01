"""Async catalog crawler/orchestrator.

Owns everything adapters don't: concurrency bounding, retry/backoff around
each adapter call, result aggregation in stable registration order, global
max_results enforcement, and in-memory per-process SourceStatus tracking.
Adapters (app/services/adapters/*) only know how to fetch+normalize; they
never see retries, concurrency, or SourceStatus. No MongoDB or filesystem
persistence - status lives only in this instance's memory.
"""

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Sequence

import httpx

from app.models.product import (
    ClothingType,
    CrawlRequest,
    CrawlResponse,
    NormalizedProduct,
    SourceInfo,
    SourceStatus,
)
from app.services.adapters.base import SourceAdapter
from app.services.retry import RetryOutcome, run_with_retry


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Crawler:
    """Coordinates a fixed, ordered set of SourceAdapters."""

    def __init__(
        self,
        adapters: Sequence[SourceAdapter],
        client: httpx.AsyncClient,
        *,
        max_concurrency: int,
        max_attempts: int,
        retry_base_delay_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Optional[Callable[[float], float]] = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if not adapters:
            raise ValueError("Crawler requires at least one registered adapter")

        self._adapters: tuple[SourceAdapter, ...] = tuple(adapters)
        self._adapter_by_name = {adapter.name: adapter for adapter in self._adapters}
        if len(self._adapter_by_name) != len(self._adapters):
            raise ValueError("adapter names must be unique")

        self._client = client
        self._max_attempts = max_attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._sleep = sleep
        self._jitter = jitter
        self._now = now
        self._semaphore = asyncio.Semaphore(max_concurrency)
        # In-memory, per-process only - never persisted.
        self._status: dict[str, SourceStatus] = {}

    def _select_adapters(self, sources: Optional[list[str]]) -> list[SourceAdapter]:
        if sources is None:
            return list(self._adapters)
        selected_names = {name for name in sources if name in self._adapter_by_name}
        return [adapter for adapter in self._adapters if adapter.name in selected_names]

    async def _run_one(
        self,
        adapter: SourceAdapter,
        clothing_type: ClothingType,
        max_results: int,
    ) -> RetryOutcome:
        async def call() -> list[NormalizedProduct]:
            async with self._semaphore:
                return await adapter.fetch(clothing_type, max_results, self._client)

        retry_kwargs = {}
        if self._jitter is not None:
            retry_kwargs["jitter"] = self._jitter
        return await run_with_retry(
            call,
            max_attempts=self._max_attempts,
            base_delay_seconds=self._retry_base_delay_seconds,
            sleep=self._sleep,
            **retry_kwargs,
        )

    def _record_status(self, source: str, outcome: RetryOutcome) -> SourceStatus:
        previous = self._status.get(source)
        previous_failures = previous.consecutive_failures if previous else 0
        if outcome.reason is None:
            status = SourceStatus(
                source=source,
                ok=True,
                last_checked=self._now(),
                consecutive_failures=0,
                detail=None,
            )
        else:
            status = SourceStatus(
                source=source,
                ok=False,
                last_checked=self._now(),
                consecutive_failures=previous_failures + 1,
                detail=outcome.reason.value,
            )
        self._status[source] = status
        return status

    async def crawl(self, request: CrawlRequest) -> CrawlResponse:
        """Run selected adapters concurrently and return an aggregated,
        deterministically-ordered, globally-limited CrawlResponse. One
        adapter's failure never cancels or blocks the others, and this
        method itself never raises for an adapter-level failure - failures
        surface only as a SourceStatus entry with ok=False.
        """
        selected = self._select_adapters(request.sources)
        if not selected:
            return CrawlResponse(products=[], source_status=[])

        outcomes = await asyncio.gather(
            *(
                self._run_one(adapter, request.clothing_type, request.max_results)
                for adapter in selected
            )
        )

        products: list[NormalizedProduct] = []
        statuses: list[SourceStatus] = []
        # zip() preserves `selected`'s registration order regardless of
        # which underlying task actually finished first - asyncio.gather
        # always returns results in call order, not completion order.
        for adapter, outcome in zip(selected, outcomes):
            status = self._record_status(adapter.name, outcome)
            statuses.append(status)
            if outcome.value is not None:
                products.extend(outcome.value)

        limited = [product.model_copy(deep=True) for product in products[: request.max_results]]
        return CrawlResponse(
            products=limited,
            source_status=[status.model_copy(deep=True) for status in statuses],
        )

    def list_sources(self) -> list[SourceInfo]:
        """Every registered source in registration order, with its
        last-known status (None if it has never been run). Returns fresh
        SourceInfo/SourceStatus objects - never references into this
        instance's internal, mutable status dict.
        """
        return [
            SourceInfo(
                source=adapter.name,
                last_status=(
                    self._status[adapter.name].model_copy(deep=True)
                    if adapter.name in self._status
                    else None
                ),
            )
            for adapter in self._adapters
        ]