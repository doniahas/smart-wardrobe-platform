"""Source adapter interface.

A SourceAdapter is a single external (or mock) product source. It knows how
to fetch raw products for one clothing type and normalize them into
NormalizedProduct instances. It does not know about retries, concurrency,
aggregation, or SourceStatus - that is the crawler/orchestrator's job (see
app/services/crawler.py). An adapter must never return CrawlResponse or
SourceStatus itself.
"""

from typing import Protocol

import httpx

from app.models.product import ClothingType, NormalizedProduct


class SourceAdapter(Protocol):
    """Structural interface every catalog source adapter must satisfy."""

    name: str

    async def fetch(
        self,
        clothing_type: ClothingType,
        max_results: int,
        client: httpx.AsyncClient,
    ) -> list[NormalizedProduct]:
        """Fetch and normalize up to max_results products of one type.

        `client` is the one shared AsyncClient for the whole app lifespan -
        implementations must reuse it rather than creating their own.
        Raises on failure (classified and retried by the crawler); never
        returns a partial/sentinel value to signal an error.
        """
        ...