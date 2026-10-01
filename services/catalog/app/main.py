from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.core.config import (
    CATALOG_ENABLED_SOURCES,
    CATALOG_MAX_ATTEMPTS,
    CATALOG_MAX_CONCURRENCY,
    CATALOG_RETRY_BASE_DELAY_SECONDS,
    SERVICE_NAME,
)
from app.models.product import CrawlRequest, CrawlResponse, SourcesResponse
from app.services.adapters.base import SourceAdapter
from app.services.adapters.mock_adapter import MockSourceAdapter
from app.services.adapters.shopify_adapter import ShopifyAdapter
from app.services.adapters.shopify_stores import SHOPIFY_STORES
from app.services.crawler import Crawler
from app.services.http_client import create_http_client
from app.services.mock_source import search


def _select_registered_adapters(all_adapters: list[SourceAdapter]) -> list[SourceAdapter]:
    """Filter `all_adapters` down to CATALOG_ENABLED_SOURCES if set,
    preserving `all_adapters`' own registration order (never the env var's
    list order - so GET /sources' ordering guarantee holds regardless of
    how the allowlist was written). See config.py's CATALOG_ENABLED_SOURCES
    docstring for why this exists and why it defaults to a no-op.

    Fails loudly at startup (ValueError), matching every other CATALOG_*
    setting's "invalid config fails at import/startup, not deep inside a
    request" convention, if the allowlist names something that isn't an
    actually-registered adapter, or ends up selecting nothing at all -
    either is a misconfiguration worth surfacing immediately, not a silent
    empty-catalog service.
    """
    if CATALOG_ENABLED_SOURCES is None:
        return all_adapters

    known_names = {adapter.name for adapter in all_adapters}
    unknown = [name for name in CATALOG_ENABLED_SOURCES if name not in known_names]
    if unknown:
        raise ValueError(
            f"CATALOG_ENABLED_SOURCES names unknown source(s): {unknown}. "
            f"Known sources: {sorted(known_names)}"
        )

    enabled = set(CATALOG_ENABLED_SOURCES)
    selected = [adapter for adapter in all_adapters if adapter.name in enabled]
    if not selected:
        raise ValueError("CATALOG_ENABLED_SOURCES matched zero registered sources")
    return selected


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = create_http_client()
    # Stable registration order: mock_demo first, then the four
    # allowlisted real Shopify stores in shopify_stores.py's order.
    all_adapters = [MockSourceAdapter()] + [
        ShopifyAdapter(store) for store in SHOPIFY_STORES
    ]
    adapters = _select_registered_adapters(all_adapters)
    crawler = Crawler(
        adapters=adapters,
        client=client,
        max_concurrency=CATALOG_MAX_CONCURRENCY,
        max_attempts=CATALOG_MAX_ATTEMPTS,
        retry_base_delay_seconds=CATALOG_RETRY_BASE_DELAY_SECONDS,
    )
    app.state.http_client = client
    app.state.crawler = crawler
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(
    title="SmartWardrobe Catalog Service",
    description=(
        "Internal stateless clothing-catalog service - deterministic mock "
        "catalog search, plus async crawl orchestration (bounded "
        "concurrency, retry/backoff, per-source status) over a "
        "SourceAdapter interface. Sources: mock_demo plus four "
        "allowlisted real Israeli Shopify stores."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": SERVICE_NAME}


@app.post("/catalog/search", response_model=CrawlResponse)
async def catalog_search(request: CrawlRequest) -> CrawlResponse:
    return search(request)


@app.post("/crawl", response_model=CrawlResponse)
async def crawl(request: CrawlRequest, http_request: Request) -> CrawlResponse:
    crawler: Crawler = http_request.app.state.crawler
    return await crawler.crawl(request)


@app.get("/sources", response_model=SourcesResponse)
async def list_sources(http_request: Request) -> SourcesResponse:
    crawler: Crawler = http_request.app.state.crawler
    return SourcesResponse(sources=crawler.list_sources())