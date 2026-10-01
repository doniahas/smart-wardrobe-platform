import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.product import ClothingType, CrawlResponse, SourcesResponse
from app.services.crawler import Crawler
from tests.conftest import ScriptedAdapter


@pytest.fixture
def client():
    """A fresh TestClient per test, with the FastAPI lifespan actually run:
    startup creates app.state.http_client/app.state.crawler, and shutdown
    closes the shared AsyncClient. Every test therefore starts from a
    clean, unshared crawler with empty source-status state - no leakage
    between tests.
    """
    with TestClient(app) as test_client:
        yield test_client


# --- 1. existing GET /health remains unchanged ---

def test_health_remains_unchanged(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "smartwardrobe-catalog"}


# --- 2. existing POST /catalog/search still works (full regression suite lives in test_catalog_search_route.py) ---

def test_existing_catalog_search_still_works(client):
    response = client.post("/catalog/search", json={"clothing_type": "top"})

    assert response.status_code == 200
    CrawlResponse(**response.json())


# --- 3. POST /crawl with mock_demo returns 200 and a valid CrawlResponse ---

def test_crawl_with_mock_demo_returns_valid_crawl_response(client):
    # sources pinned to mock_demo: this test asserts an exact deterministic
    # product count/order, which only mock_demo guarantees - leaving
    # sources unset would also fan out to the real Shopify sources.
    response = client.post(
        "/crawl", json={"clothing_type": "dress", "sources": ["mock_demo"]}
    )

    assert response.status_code == 200
    body = CrawlResponse(**response.json())
    assert len(body.products) == 2
    assert all(product.clothing_type == ClothingType.DRESS for product in body.products)
    assert body.source_status[0].source == "mock_demo"
    assert body.source_status[0].ok is True


# --- 4. POST /crawl respects clothing_type and max_results ---

def test_crawl_respects_clothing_type_and_max_results(client):
    # sources pinned to mock_demo - see the comment in test 3 above.
    response = client.post(
        "/crawl",
        json={"clothing_type": "shoes", "max_results": 1, "sources": ["mock_demo"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["products"]) == 1
    assert body["products"][0]["clothing_type"] == "shoes"


# --- 5. unknown source returns 200 with empty lists ---

def test_crawl_unknown_source_returns_empty_lists(client):
    response = client.post(
        "/crawl",
        json={"clothing_type": "top", "sources": ["real_store_that_does_not_exist"]},
    )

    assert response.status_code == 200
    assert response.json() == {"products": [], "source_status": []}


# --- 6. GET /sources includes mock_demo ---

def test_sources_includes_mock_demo(client):
    response = client.get("/sources")

    assert response.status_code == 200
    body = SourcesResponse(**response.json())
    assert any(info.source == "mock_demo" for info in body.sources)


# --- 7 & 8. mock_demo has last_status=null before the first crawl, then ok=True after a successful crawl ---

def test_sources_status_is_null_before_first_crawl_then_ok_after(client):
    before = client.get("/sources")
    assert before.status_code == 200
    before_info = SourcesResponse(**before.json()).sources
    mock_before = next(info for info in before_info if info.source == "mock_demo")
    assert mock_before.last_status is None

    # sources pinned to mock_demo - see the comment in test 3 above.
    crawl_response = client.post(
        "/crawl", json={"clothing_type": "top", "sources": ["mock_demo"]}
    )
    assert crawl_response.status_code == 200

    after = client.get("/sources")
    after_info = SourcesResponse(**after.json()).sources
    mock_after = next(info for info in after_info if info.source == "mock_demo")
    assert mock_after.last_status is not None
    assert mock_after.last_status.ok is True


# --- 9. a test-injected failing adapter produces a failed status, not HTTP 500 ---

def test_injected_failing_adapter_produces_failed_status_not_500(client):
    # Narrowly justified: swap the real Crawler for one built with a
    # controlled failing adapter, to prove the /crawl route degrades
    # gracefully (200 + a failed SourceStatus) instead of ever returning
    # 500 for an adapter-level failure. This only reaches app.state, a
    # plain, intentionally-public attribute main.py sets up for exactly
    # this purpose - not a private implementation detail. The `client`
    # fixture's lifespan already ran, so app.state.http_client exists to
    # reuse here.
    failing_adapter = ScriptedAdapter("failing_source", exception=ValueError("boom"))
    client.app.state.crawler = Crawler(
        adapters=[failing_adapter],
        client=client.app.state.http_client,
        max_concurrency=1,
        max_attempts=1,
        retry_base_delay_seconds=0.0,
    )

    response = client.post(
        "/crawl", json={"clothing_type": "top", "sources": ["failing_source"]}
    )

    assert response.status_code == 200
    body = CrawlResponse(**response.json())
    assert body.products == []
    assert body.source_status[0].source == "failing_source"
    assert body.source_status[0].ok is False
    assert body.source_status[0].detail == "parse_error"


# --- 10. invalid CrawlRequest bodies still return 422 ---

def test_crawl_invalid_body_returns_422(client):
    missing_type = client.post("/crawl", json={})
    assert missing_type.status_code == 422

    bad_type = client.post("/crawl", json={"clothing_type": "hat"})
    assert bad_type.status_code == 422

    bad_max_results = client.post("/crawl", json={"clothing_type": "top", "max_results": 0})
    assert bad_max_results.status_code == 422


# --- 11. the shared AsyncClient is closed when application lifespan ends ---

def test_shared_http_client_is_closed_on_lifespan_shutdown():
    # Deliberately NOT using the `client` fixture: this test's entire
    # purpose is to observe both sides of the lifespan boundary (open,
    # then closed) within one test body, which the shared fixture
    # abstracts away by hiding the `with` block's exit.
    with TestClient(app) as isolated_client:
        http_client = isolated_client.app.state.http_client
        assert http_client.is_closed is False

    assert http_client.is_closed is True