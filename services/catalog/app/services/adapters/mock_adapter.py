"""Deterministic mock source adapter.

Wraps app.services.mock_source's existing, tested query behavior in the
SourceAdapter shape the crawler expects. Reuses that module's public
search() function so the deterministic mock-catalog behavior is not
duplicated or reimplemented - this adapter is a thin translation layer
only, with no artificial delay added for testing purposes (concurrency
tests use their own dedicated fake adapters instead, see tests/conftest.py).
"""

import httpx

from app.models.product import ClothingType, CrawlRequest, NormalizedProduct
from app.services.mock_source import MOCK_SOURCE_NAME, search


class MockSourceAdapter:
    """The mock_demo source adapter. Makes no network calls."""

    name = MOCK_SOURCE_NAME

    async def fetch(
        self,
        clothing_type: ClothingType,
        max_results: int,
        client: httpx.AsyncClient,
    ) -> list[NormalizedProduct]:
        # `client` is accepted only to satisfy the SourceAdapter interface;
        # the mock source makes no network calls and never touches it.
        del client
        result = search(CrawlRequest(clothing_type=clothing_type, max_results=max_results))
        return result.products