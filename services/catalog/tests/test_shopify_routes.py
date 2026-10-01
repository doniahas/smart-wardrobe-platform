"""Crawler + route integration tests for the four registered Shopify
sources, plus a final regression check that existing endpoints/behavior
were not weakened.

Shopify-source requests go through the real, production ShopifyAdapter and
Crawler classes (the same registration app/main.py performs), but with
app.state.crawler swapped for one built on an httpx.MockTransport-backed
client - the same technique test_crawl_routes.py already uses to test
failure containment without a live network call.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.product import ClothingType, CrawlResponse, SourcesResponse
from app.services.adapters.mock_adapter import MockSourceAdapter
from app.services.adapters.shopify_adapter import ShopifyAdapter
from app.services.adapters.shopify_stores import SHOPIFY_STORES
from app.services.crawler import Crawler

_STORE_HOSTS = {
    "shopify_shtaim": "shtaim.store",
    "shopify_pipa_fashion": "www.pipafashion.co.il",
    "shopify_kahiko_swimwear": "kahikoswimwear.co.il",
    "shopify_ata": "atawear.co.il",
}


def _top_product(label: str) -> dict:
    return {
        "id": 1,
        "handle": f"{label}-basic-top",
        "title": f"{label} Basic Top",
        "product_type": "Tops",
        "variants": [{"price": "99.00", "available": True}],
        "images": [],
    }


def _multi_store_handler(*, failing_hosts: frozenset[str] = frozenset(), malformed_hosts: frozenset[str] = frozenset()):
    def handle(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if path == "/cart.js":
            return httpx.Response(200, json={"currency": "ILS"})
        if path == "/products.json":
            if host in failing_hosts:
                return httpx.Response(503, json={"error": "boom"})
            if host in malformed_hosts:
                return httpx.Response(200, json={"not_products": "oops"})
            label = next((name for name, h in _STORE_HOSTS.items() if h == host), host)
            return httpx.Response(200, json={"products": [_top_product(label)]})
        raise AssertionError(f"unexpected request: {request.url}")

    return handle


def _swap_crawler(test_client: TestClient, handler) -> None:
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapters = [MockSourceAdapter()] + [ShopifyAdapter(store) for store in SHOPIFY_STORES]
    test_client.app.state.crawler = Crawler(
        adapters=adapters,
        client=mock_client,
        max_concurrency=5,
        max_attempts=1,
        retry_base_delay_seconds=0.0,
    )


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


# --- 52. GET /sources lists mock_demo + all four Shopify sources in stable order ---


def test_sources_lists_mock_demo_and_all_shopify_sources_in_stable_order(client):
    response = client.get("/sources")

    assert response.status_code == 200
    body = SourcesResponse(**response.json())
    assert [info.source for info in body.sources] == [
        "mock_demo",
        "shopify_shtaim",
        "shopify_pipa_fashion",
        "shopify_kahiko_swimwear",
        "shopify_ata",
    ]


# --- 53. before use, every Shopify source has last_status=null ---


def test_shopify_sources_have_null_status_before_first_use(client):
    response = client.get("/sources")

    body = SourcesResponse(**response.json())
    shopify_sources = [info for info in body.sources if info.source != "mock_demo"]
    assert len(shopify_sources) == 4
    assert all(info.last_status is None for info in shopify_sources)


# --- 54-57. /crawl can select each individual Shopify source ---


@pytest.mark.parametrize("source_name", list(_STORE_HOSTS))
def test_crawl_can_select_each_shopify_source(client, source_name):
    _swap_crawler(client, _multi_store_handler())

    response = client.post(
        "/crawl", json={"clothing_type": "top", "sources": [source_name]}
    )

    assert response.status_code == 200
    body = CrawlResponse(**response.json())
    assert len(body.products) == 1
    assert body.products[0].source == source_name
    assert body.source_status == [
        status for status in body.source_status if status.source == source_name
    ]
    assert body.source_status[0].ok is True


# --- 58. /crawl can select multiple Shopify sources ---


def test_crawl_can_select_multiple_shopify_sources(client):
    _swap_crawler(client, _multi_store_handler())

    response = client.post(
        "/crawl",
        json={
            "clothing_type": "top",
            "sources": ["shopify_shtaim", "shopify_ata"],
        },
    )

    assert response.status_code == 200
    body = CrawlResponse(**response.json())
    assert {product.source for product in body.products} == {"shopify_shtaim", "shopify_ata"}
    assert {status.source for status in body.source_status} == {"shopify_shtaim", "shopify_ata"}


# --- 59. unknown-only source still returns empty lists ---


def test_unknown_only_shopify_like_source_returns_empty_lists(client):
    _swap_crawler(client, _multi_store_handler())

    response = client.post(
        "/crawl",
        json={"clothing_type": "top", "sources": ["shopify_not_a_real_store"]},
    )

    assert response.status_code == 200
    assert response.json() == {"products": [], "source_status": []}


# --- 60, 63. one failed Shopify source does not cancel a successful one; failure detail is parse_error ---


def test_one_failed_shopify_source_does_not_cancel_a_successful_one(client):
    _swap_crawler(
        client,
        _multi_store_handler(malformed_hosts=frozenset({_STORE_HOSTS["shopify_shtaim"]})),
    )

    response = client.post(
        "/crawl",
        json={
            "clothing_type": "top",
            "sources": ["shopify_shtaim", "shopify_ata"],
        },
    )

    assert response.status_code == 200
    body = CrawlResponse(**response.json())
    statuses = {status.source: status for status in body.source_status}
    assert statuses["shopify_shtaim"].ok is False
    assert statuses["shopify_shtaim"].detail == "parse_error"  # 63
    assert statuses["shopify_ata"].ok is True
    assert [product.source for product in body.products] == ["shopify_ata"]


# --- 61. global max_results is still enforced across multiple Shopify sources ---


def test_global_max_results_enforced_across_shopify_sources(client):
    _swap_crawler(client, _multi_store_handler())

    response = client.post(
        "/crawl",
        json={
            "clothing_type": "top",
            "max_results": 2,
            "sources": list(_STORE_HOSTS),
        },
    )

    assert response.status_code == 200
    body = CrawlResponse(**response.json())
    assert len(body.products) == 2


# --- 62. a successful source receives ok=True status ---


def test_successful_shopify_source_receives_ok_true_status(client):
    _swap_crawler(client, _multi_store_handler())

    response = client.post(
        "/crawl", json={"clothing_type": "top", "sources": ["shopify_pipa_fashion"]}
    )

    body = CrawlResponse(**response.json())
    assert body.source_status[0].ok is True
    assert body.source_status[0].detail is None


# --- 64-66. existing endpoints/behavior remain green with Shopify sources registered ---


def test_health_still_green(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "smartwardrobe-catalog"}


def test_catalog_search_still_green(client):
    response = client.post("/catalog/search", json={"clothing_type": "top"})

    assert response.status_code == 200
    CrawlResponse(**response.json())


def test_mock_demo_crawl_still_green(client):
    response = client.post(
        "/crawl", json={"clothing_type": "top", "sources": ["mock_demo"]}
    )

    assert response.status_code == 200
    body = CrawlResponse(**response.json())
    assert body.source_status[0].source == "mock_demo"
    assert body.source_status[0].ok is True
    assert all(product.source == "mock_demo" for product in body.products)