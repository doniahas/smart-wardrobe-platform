"""Shared test doubles for the catalog tests.

These are test-only SourceAdapter implementations - never imported by
production code. They exist so concurrency, ordering, and failure/status
behavior can be tested deterministically: no real network calls, no real
(nonzero) sleeps, and no dependence on which async task happens to finish
first.
"""

from datetime import datetime, timezone

import asyncio

import httpx
import pytest

from app.models.product import ClothingType, NormalizedProduct


def make_product(name: str, clothing_type: ClothingType = ClothingType.TOP) -> NormalizedProduct:
    """Build a minimal, fully-valid NormalizedProduct for test fixtures."""
    return NormalizedProduct(
        source="test_source",
        source_url=f"https://example.com/test/{name}",
        product_name=name,
        clothing_type=clothing_type,
        availability="in_stock",
        fetched_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def http_client():
    """A real but never-connected AsyncClient - fine for tests, since none
    of the fake adapters below ever actually call it.
    """
    return httpx.AsyncClient()


class ConcurrencyCounter:
    """Tracks the current and peak number of simultaneously active fetches."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    def enter(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)

    def exit(self) -> None:
        self.active -= 1


class TrackedAdapter:
    """Test-only adapter that records concurrent overlap via a shared
    ConcurrencyCounter, yielding with `await asyncio.sleep(0)` - a
    cooperative yield, not a real (nonzero) wait - so multiple tasks can
    genuinely interleave without any wall-clock timing dependency.
    """

    def __init__(self, name: str, counter: ConcurrencyCounter, extra_yields: int = 0):
        self.name = name
        self._counter = counter
        self._extra_yields = extra_yields

    async def fetch(self, clothing_type, max_results, client):
        self._counter.enter()
        try:
            for _ in range(self._extra_yields + 1):
                await asyncio.sleep(0)
            return [make_product(f"{self.name}-1", clothing_type)]
        finally:
            self._counter.exit()


class ScriptedAdapter:
    """Test-only adapter that always either returns a fixed product list or
    always raises a fixed exception - for single-outcome-per-test
    scenarios (success/failure containment, source selection).
    """

    def __init__(self, name: str, *, products=None, exception: Exception | None = None):
        self.name = name
        self._products = list(products) if products is not None else []
        self._exception = exception
        self.call_count = 0

    async def fetch(self, clothing_type, max_results, client):
        self.call_count += 1
        if self._exception is not None:
            raise self._exception
        return list(self._products)


class SequencedAdapter:
    """Test-only adapter that pops one pre-scripted outcome (a product list
    or an exception instance) per fetch() call - lets a test script a
    failure-then-recovery sequence for the *same* adapter across multiple
    Crawler.crawl() calls.
    """

    def __init__(self, name: str, outcomes: list):
        self.name = name
        self._outcomes = list(outcomes)
        self.call_count = 0

    async def fetch(self, clothing_type, max_results, client):
        self.call_count += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return list(outcome)