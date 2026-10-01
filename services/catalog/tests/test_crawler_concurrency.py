import asyncio

from app.models.product import ClothingType, CrawlRequest
from app.services.crawler import Crawler
from tests.conftest import ConcurrencyCounter, TrackedAdapter


async def _fast_sleep(seconds: float) -> None:
    """Used only so a test-configured retry delay (if any) never waits in
    real time; these tests don't expect retries to fire at all.
    """
    return None


# --- 1 & 2. semaphore bounds concurrency, and adapters actually overlap when max_concurrency > 1 ---

def test_semaphore_bounds_and_allows_overlap_when_max_concurrency_above_one(http_client):
    counter = ConcurrencyCounter()
    adapters = [TrackedAdapter(f"adapter_{i}", counter) for i in range(4)]
    crawler = Crawler(
        adapters=adapters,
        client=http_client,
        max_concurrency=2,
        max_attempts=1,
        retry_base_delay_seconds=0.0,
        sleep=_fast_sleep,
    )

    async def run():
        return await crawler.crawl(CrawlRequest(clothing_type=ClothingType.TOP))

    asyncio.run(run())

    assert counter.peak <= 2
    assert counter.peak > 1  # genuine overlap actually happened, not accidental serialization


# --- 3. max_concurrency=1 serializes adapters ---

def test_max_concurrency_one_serializes_adapters(http_client):
    counter = ConcurrencyCounter()
    adapters = [TrackedAdapter(f"adapter_{i}", counter) for i in range(4)]
    crawler = Crawler(
        adapters=adapters,
        client=http_client,
        max_concurrency=1,
        max_attempts=1,
        retry_base_delay_seconds=0.0,
        sleep=_fast_sleep,
    )

    async def run():
        return await crawler.crawl(CrawlRequest(clothing_type=ClothingType.TOP))

    asyncio.run(run())

    assert counter.peak == 1


# --- 4. results remain in adapter-registration order even when a later adapter finishes first ---

def test_results_remain_in_registration_order_regardless_of_completion_order(http_client):
    counter = ConcurrencyCounter()
    # adapter_a (registered first) yields more times than adapter_b
    # (registered second), so adapter_b's task actually completes first.
    adapter_a = TrackedAdapter("adapter_a", counter, extra_yields=3)
    adapter_b = TrackedAdapter("adapter_b", counter, extra_yields=0)
    crawler = Crawler(
        adapters=[adapter_a, adapter_b],
        client=http_client,
        max_concurrency=2,
        max_attempts=1,
        retry_base_delay_seconds=0.0,
        sleep=_fast_sleep,
    )

    async def run():
        return await crawler.crawl(CrawlRequest(clothing_type=ClothingType.TOP))

    response = asyncio.run(run())

    assert [product.product_name for product in response.products] == [
        "adapter_a-1",
        "adapter_b-1",
    ]
    assert [status.source for status in response.source_status] == ["adapter_a", "adapter_b"]