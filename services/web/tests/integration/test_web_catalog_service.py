"""Real web -> catalog service integration test.

Same pattern as test_web_recommendation_service.py and
test_web_llm_service.py: a genuine `catalog` subprocess (the same
`uvicorn app.main:app` invocation services/catalog/Dockerfile uses),
reached over a real TCP socket, with only the destination URL
(CATALOG_SERVICE_URL) redirected.

Unlike the other two boundary tests, this one calls
catalog_gateway.fetch_catalog_products() directly rather than going
through GET /recommendations/catalog-suggestions. That route never
exposes a `sources` filter to *callers* (the route deliberately
hardcodes `sources=REAL_STORE_SOURCES` internally rather than leaving
`sources` unset), so driving mock_demo through it here isn't possible at
all any more: the real catalog service's real Crawler would only ever
receive the four real Shopify-store names, each of which makes a genuine
outbound HTTP request to a real e-commerce domain. That would violate this
suite's "no external services" rule (and be non-deterministic
besides). Calling fetch_catalog_products() directly with
`sources=["mock_demo"]` is not a test-only shortcut - it is the same
existing, already-shipped filtering parameter services/
chat_catalog_grounding.py itself uses in production to restrict a crawl to
specific stores - so this still exercises the real, unmodified HTTP call
in catalog_gateway.py end to end (real request serialization, real
response deserialization, real product validation), just against the one
registered source that's documented as "makes no network calls"
(services/catalog/app/services/adapters/mock_adapter.py).
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import app.services.catalog_gateway as catalog_gateway
from app.services.catalog_gateway import CatalogGatewayFailure, fetch_catalog_products

# services/web/tests/integration/test_X.py -> parents[3] is "services"
_CATALOG_SERVICE_DIR = Path(__file__).resolve().parents[3] / "catalog"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def real_catalog_service(monkeypatch):
    """Start the real catalog service as a genuine subprocess on a free
    localhost port, wait for it to actually report healthy, then point
    web's CATALOG_SERVICE_URL at it."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(_CATALOG_SERVICE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready = False
        for _ in range(50):
            try:
                if httpx.get(f"{base_url}/health", timeout=0.5).status_code == 200:
                    ready = True
                    break
            except httpx.RequestError:
                pass
            time.sleep(0.2)
        if not ready:
            raise RuntimeError("real catalog service did not become healthy within 10s")

        monkeypatch.setattr(catalog_gateway, "CATALOG_SERVICE_URL", base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_fetch_catalog_products_reaches_a_real_catalog_service(real_catalog_service):
    """No mock anywhere on the web side of this call: the request genuinely
    leaves the process, is served by a real, independently running catalog
    FastAPI instance's real Crawler/adapter machinery, and comes back
    through catalog_gateway's real response validation. Asserts on the
    mock source's known fixture product names (services/catalog/app/
    services/mock_source.py) - proof this is the real round trip, not a
    coincidence: a mocked/fake response could not know these exact names."""
    result = await fetch_catalog_products("top", max_results=5, sources=["mock_demo"])

    assert not isinstance(result, CatalogGatewayFailure)
    assert len(result.products) >= 1
    assert all(p.source == "mock_demo" for p in result.products)
    assert any("Cotton T-Shirt" in p.product_name for p in result.products)
