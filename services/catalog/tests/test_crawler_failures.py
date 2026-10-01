import asyncio

from app.models.product import ClothingType, CrawlRequest, CrawlResponse
from app.services.crawler import Crawler
from tests.conftest import ScriptedAdapter, SequencedAdapter, make_product


async def _fast_sleep(seconds: float) -> None:
    return None


def _crawler(adapters, http_client, **overrides):
    kwargs = dict(
        adapters=adapters,
        client=http_client,
        max_concurrency=5,
        max_attempts=2,
        retry_base_delay_seconds=0.0,
        sleep=_fast_sleep,
    )
    kwargs.update(overrides)
    return Crawler(**kwargs)


# --- 5. one failing adapter does not cancel successful adapters ---

def test_one_failing_adapter_does_not_cancel_successful_adapters(http_client):
    good = ScriptedAdapter("good_source", products=[make_product("GOOD-1")])
    bad = ScriptedAdapter("bad_source", exception=ValueError("boom"))
    crawler = _crawler([good, bad], http_client)

    async def run():
        return await crawler.crawl(CrawlRequest(clothing_type=ClothingType.TOP))

    response = asyncio.run(run())

    assert [product.product_name for product in response.products] == ["GOOD-1"]
    statuses = {status.source: status for status in response.source_status}
    assert statuses["good_source"].ok is True
    assert statuses["bad_source"].ok is False
    assert statuses["bad_source"].detail == "parse_error"


# --- 6. global max_results is respected across multiple adapters ---

def test_global_max_results_respected_across_adapters(http_client):
    adapter_a = ScriptedAdapter("a", products=[make_product("A-1"), make_product("A-2")])
    adapter_b = ScriptedAdapter("b", products=[make_product("B-1"), make_product("B-2")])
    crawler = _crawler([adapter_a, adapter_b], http_client)

    async def run():
        return await crawler.crawl(
            CrawlRequest(clothing_type=ClothingType.TOP, max_results=3)
        )

    response = asyncio.run(run())

    assert len(response.products) == 3


# --- 7. sources=None selects all sources ---

def test_sources_none_selects_all_sources(http_client):
    adapter_a = ScriptedAdapter("a", products=[make_product("A-1")])
    adapter_b = ScriptedAdapter("b", products=[make_product("B-1")])
    crawler = _crawler([adapter_a, adapter_b], http_client)

    async def run():
        return await crawler.crawl(CrawlRequest(clothing_type=ClothingType.TOP, sources=None))

    response = asyncio.run(run())

    assert {product.product_name for product in response.products} == {"A-1", "B-1"}
    assert {status.source for status in response.source_status} == {"a", "b"}


# --- 8. selected known sources work ---

def test_selected_known_source_only_runs_that_source(http_client):
    adapter_a = ScriptedAdapter("a", products=[make_product("A-1")])
    adapter_b = ScriptedAdapter("b", products=[make_product("B-1")])
    crawler = _crawler([adapter_a, adapter_b], http_client)

    async def run():
        return await crawler.crawl(
            CrawlRequest(clothing_type=ClothingType.TOP, sources=["b"])
        )

    response = asyncio.run(run())

    assert [product.product_name for product in response.products] == ["B-1"]
    assert [status.source for status in response.source_status] == ["b"]
    assert adapter_a.call_count == 0
    assert adapter_b.call_count == 1


# --- 9. unknown-only sources return empty products and empty statuses ---

def test_unknown_only_sources_return_empty_products_and_statuses(http_client):
    adapter_a = ScriptedAdapter("a", products=[make_product("A-1")])
    crawler = _crawler([adapter_a], http_client)

    async def run():
        return await crawler.crawl(
            CrawlRequest(clothing_type=ClothingType.TOP, sources=["nonexistent"])
        )

    response = asyncio.run(run())

    assert response == CrawlResponse(products=[], source_status=[])
    assert adapter_a.call_count == 0
    # the never-run adapter still reports last_status=None, unaffected
    sources = crawler.list_sources()
    assert sources[0].last_status is None


# --- 10 & 11. consecutive failure counts increase, and a later success resets to zero ---

def test_consecutive_failure_count_increases_across_calls(http_client):
    adapter = SequencedAdapter("flaky", outcomes=[ValueError("boom"), ValueError("boom again")])
    crawler = _crawler([adapter], http_client)

    async def run():
        first = await crawler.crawl(CrawlRequest(clothing_type=ClothingType.TOP))
        second = await crawler.crawl(CrawlRequest(clothing_type=ClothingType.TOP))
        return first, second

    first, second = asyncio.run(run())

    assert first.source_status[0].consecutive_failures == 1
    assert second.source_status[0].consecutive_failures == 2


def test_later_success_resets_consecutive_failures_to_zero(http_client):
    adapter = SequencedAdapter(
        "flaky", outcomes=[ValueError("boom"), [make_product("RECOVERED-1")]]
    )
    crawler = _crawler([adapter], http_client)

    async def run():
        first = await crawler.crawl(CrawlRequest(clothing_type=ClothingType.TOP))
        second = await crawler.crawl(CrawlRequest(clothing_type=ClothingType.TOP))
        return first, second

    first, second = asyncio.run(run())

    assert first.source_status[0].ok is False
    assert first.source_status[0].consecutive_failures == 1
    assert second.source_status[0].ok is True
    assert second.source_status[0].consecutive_failures == 0


# --- 12. returned products/statuses are isolated from mutable internal state ---

def test_returned_products_and_statuses_are_isolated_from_internal_state(http_client):
    adapter = ScriptedAdapter("a", products=[make_product("A-1")])
    crawler = _crawler([adapter], http_client)

    async def run():
        return await crawler.crawl(CrawlRequest(clothing_type=ClothingType.TOP))

    response = asyncio.run(run())
    response.products[0].product_name = "MUTATED"
    response.source_status[0].detail = "mutated"

    sources = crawler.list_sources()
    assert sources[0].last_status.detail is None  # unaffected by the mutation above

    async def run_again():
        return await crawler.crawl(CrawlRequest(clothing_type=ClothingType.TOP))

    second_response = asyncio.run(run_again())
    assert second_response.products[0].product_name == "A-1"