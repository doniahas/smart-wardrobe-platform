"""Stress testing: a genuinely time-delayed source,
proving the crawler's real timeout mechanism (httpx.Timeout on the shared
AsyncClient - see app/services/http_client.py::create_http_client) actually
fires from real elapsed wall-clock time.

Every existing "timeout" test in this suite (test_retry_backoff.py,
test_shopify_adapter.py) injects httpx.ReadTimeout/httpx.TimeoutException
directly - the operation raises instantly, proving the RETRY/CLASSIFICATION
logic downstream of a timeout, but never that httpx's own timeout
enforcement actually engages. httpx.MockTransport (used by every other
adapter test in this suite) does not enforce httpx.Timeout at all - a
MockTransport handler that sleeps past the configured timeout simply
returns late instead of timing out (verified directly against httpx before
writing this test). Proving the real mechanism therefore requires a real
transport: a tiny local asyncio TCP server that never writes a response,
so the client's own short, real httpx.Timeout is what ends the wait - not
an injected exception.

Kept CI-fast: a 0.3s client timeout against a server that never responds
bounds this test's real elapsed time to roughly 0.3-1s (fetch() makes up to
two requests - cart.js, then products.json - each independently timing out
against the same short timeout), with max_attempts=1 so no retry/backoff
sleep is added on top.
"""
import asyncio
import time

import httpx

from app.models.product import ClothingType
from app.services.adapters.shopify_adapter import ShopifyAdapter
from app.services.adapters.shopify_stores import ShopifyStoreConfig
from app.services.retry import CrawlFailureReason, run_with_retry

CLIENT_TIMEOUT_SECONDS = 0.3


async def _start_hanging_server() -> tuple[asyncio.base_events.Server, int, list]:
    """A raw TCP server on 127.0.0.1 that accepts a connection and never
    writes a response - the client's own configured httpx.Timeout is what
    ends the wait, not the server closing/refusing the connection.

    Returns the running handler task(s) too: `Server.wait_closed()` waits
    for every still-active connection handler to actually finish, and a
    handler deliberately sleeping for a long time would make cleanup hang
    right along with it - the caller must cancel these tasks before
    awaiting wait_closed() (see the test's own cleanup below)."""
    handler_tasks: list[asyncio.Task] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handler_tasks.append(asyncio.current_task())
        try:
            await reader.read(65536)
        except Exception:
            pass
        try:
            await asyncio.sleep(3600)  # effectively never respond
        except asyncio.CancelledError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, handler_tasks


def test_real_elapsed_time_timeout_is_classified_as_source_timeout():
    async def run():
        server, port, handler_tasks = await _start_hanging_server()
        client = httpx.AsyncClient(timeout=httpx.Timeout(CLIENT_TIMEOUT_SECONDS))
        try:
            config = ShopifyStoreConfig(
                source_name="slow_store",
                display_name="Slow Store",
                base_url=f"http://127.0.0.1:{port}",
            )
            adapter = ShopifyAdapter(config)

            t0 = time.monotonic()
            outcome = await run_with_retry(
                lambda: adapter.fetch(ClothingType.TOP, 10, client),
                max_attempts=1,
                base_delay_seconds=0.1,
            )
            elapsed = time.monotonic() - t0
            return outcome, elapsed
        finally:
            await client.aclose()
            server.close()
            # Cancel the still-sleeping connection handler(s) first -
            # wait_closed() would otherwise hang waiting for them to finish
            # on their own, which (by design) never happens quickly.
            for task in handler_tasks:
                task.cancel()
            await asyncio.gather(*handler_tasks, return_exceptions=True)
            await server.wait_closed()

    outcome, elapsed = asyncio.run(run())

    # Real elapsed wall-clock time actually passed - not the near-zero
    # elapsed time an injected exception would produce, and not an
    # unbounded/hung wait either.
    assert CLIENT_TIMEOUT_SECONDS <= elapsed < 5.0

    # The real httpx timeout is classified exactly the same way the
    # existing injected-exception tests already prove for
    # httpx.TimeoutException - this test's only new claim is that the
    # mechanism itself actually engages, not the classification logic.
    assert outcome.reason == CrawlFailureReason.SOURCE_TIMEOUT
    assert outcome.value is None
    assert outcome.attempts == 1
