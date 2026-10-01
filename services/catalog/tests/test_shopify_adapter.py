"""ShopifyAdapter integration tests.

Every AsyncClient here is built with httpx.MockTransport - no test in this
file (or anywhere in this suite) performs a real network call. The adapter
itself never constructs its own AsyncClient; it only ever uses the one
passed into fetch(), exactly like every other SourceAdapter.
"""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.models.product import ClothingType, NormalizedProduct
from app.services.adapters.shopify_adapter import ShopifyAdapter
from app.services.adapters.shopify_stores import ShopifyStoreConfig
from app.services.retry import CrawlFailureReason, classify_exception

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "shopify"


def _load(name: str):
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


TEST_STORE = ShopifyStoreConfig(
    source_name="shopify_test_store",
    display_name="Test Store",
    base_url="https://test-store.example.com",
)

_FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return _FIXED_NOW


def _handler(
    *,
    cart=None,
    cart_status=200,
    cart_text=None,
    products=None,
    products_status=200,
    products_text=None,
):
    """Build a MockTransport handler that answers only /cart.js and
    /products.json - anything else is a test-authoring mistake.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cart.js":
            if cart_text is not None:
                return httpx.Response(
                    cart_status, text=cart_text, headers={"content-type": "application/json"}
                )
            if cart is None:
                return httpx.Response(cart_status)
            return httpx.Response(cart_status, json=cart)
        if path == "/products.json":
            if products_text is not None:
                return httpx.Response(
                    products_status,
                    text=products_text,
                    headers={"content-type": "application/json"},
                )
            if products is None:
                return httpx.Response(products_status)
            return httpx.Response(products_status, json=products)
        raise AssertionError(f"unexpected request path: {path}")

    return handle


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fetch(adapter, clothing_type, max_results, client):
    async def run():
        return await adapter.fetch(clothing_type, max_results, client)

    return asyncio.run(run())


# --- 33-36, 42. a valid response becomes validated, correctly-shaped products ---


def test_valid_response_becomes_normalized_products_with_correct_fields():
    handler = _handler(cart={"currency": "ILS"}, products=_load("products_valid.json"))
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    assert len(products) == 1
    product = products[0]
    assert isinstance(product, NormalizedProduct)
    assert product.source == "shopify_test_store"  # 34
    assert product.product_name == "Classic Cotton T-Shirt"  # 35
    assert str(product.source_url).startswith("https://test-store.example.com/products/")  # 36
    assert product.price == Decimal("49.90")  # lowest of the two available variants
    assert product.currency == "ILS"
    assert product.availability == "in_stock"
    assert product.fetched_at == _FIXED_NOW


def test_missing_image_does_not_discard_an_otherwise_valid_product():  # 42
    handler = _handler(cart={"currency": "ILS"}, products=_load("products_valid.json"))
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    products = _fetch(adapter, ClothingType.BOTTOM, 20, client)

    assert len(products) == 1
    assert products[0].image_url is None


# --- 37. source_product_id prefers id, falls back to handle ---


def test_source_product_id_prefers_id_falls_back_to_handle():
    products_payload = {
        "products": [
            {
                "id": 999,
                "handle": "has-id",
                "title": "Has Id Top",
                "product_type": "Tops",
                "variants": [{"price": "10.00", "available": True}],
                "images": [],
            },
            {
                "handle": "no-id-handle",
                "title": "No Id Top",
                "product_type": "Tops",
                "variants": [{"price": "10.00", "available": True}],
                "images": [],
            },
        ]
    }
    handler = _handler(cart={"currency": "ILS"}, products=products_payload)
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    by_name = {product.product_name: product.source_product_id for product in products}
    assert by_name["Has Id Top"] == "999"
    assert by_name["No Id Top"] == "no-id-handle"


# --- 38, 41. only available-variant products, invalid ones skipped without breaking others ---


def test_invalid_individual_products_are_skipped_without_breaking_valid_ones():
    handler = _handler(cart={"currency": "ILS"}, products=_load("products_edge_cases.json"))
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    products = _fetch(adapter, ClothingType.ACCESSORY, 20, client)

    assert len(products) == 1
    assert products[0].product_name == "Wool Beanie Hat"


# --- 39. clothing_type filtering works ---


def test_clothing_type_filtering_works():
    handler = _handler(cart={"currency": "ILS"}, products=_load("products_valid.json"))
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    top = _fetch(adapter, ClothingType.TOP, 20, client)
    dress = _fetch(adapter, ClothingType.DRESS, 20, client)

    assert {product.clothing_type for product in top} == {ClothingType.TOP}
    assert {product.clothing_type for product in dress} == {ClothingType.DRESS}


# --- 40. max_results is respected ---


def test_max_results_is_respected():
    products_payload = {
        "products": [
            {
                "id": i,
                "handle": f"top-{i}",
                "title": f"Top Shirt {i}",
                "product_type": "Tops",
                "variants": [{"price": "10.00", "available": True}],
                "images": [],
            }
            for i in range(5)
        ]
    }
    handler = _handler(cart={"currency": "ILS"}, products=products_payload)
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 2, client)

    assert len(products) == 2


# --- 13-15 (adapter-level currency fallback) ---


def test_valid_cart_currency_is_applied_to_products():
    handler = _handler(cart={"currency": "usd"}, products=_load("products_valid.json"))
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    assert products[0].currency == "USD"


def test_currency_falls_back_to_default_when_cart_js_fails():
    handler = _handler(cart_status=500, products=_load("products_valid.json"))
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    assert products[0].currency == "ILS"


def test_currency_falls_back_to_default_when_cart_currency_is_unusable():
    handler = _handler(cart={"currency": "???"}, products=_load("products_valid.json"))
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    assert products[0].currency == "ILS"


def test_currency_falls_back_to_default_when_cart_json_is_malformed():
    handler = _handler(cart_text="{not valid json", products=_load("products_valid.json"))
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    assert products[0].currency == "ILS"


# --- 43-44. whole-response failures raise, and classify as parse_error ---


def test_malformed_products_json_becomes_parse_error():
    handler = _handler(cart={"currency": "ILS"}, products_text="{not valid json")
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    with pytest.raises(Exception) as exc_info:
        _fetch(adapter, ClothingType.TOP, 20, client)

    assert classify_exception(exc_info.value) == CrawlFailureReason.PARSE_ERROR


def test_invalid_top_level_structure_becomes_parse_error():
    handler = _handler(cart={"currency": "ILS"}, products=_load("malformed_top_level.json"))
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    with pytest.raises(Exception) as exc_info:
        _fetch(adapter, ClothingType.TOP, 20, client)

    assert classify_exception(exc_info.value) == CrawlFailureReason.PARSE_ERROR


# --- 45-48. HTTP/timeout failures propagate for the crawler's retry system to classify ---


def test_timeout_propagates_for_retry_classification():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cart.js":
            return httpx.Response(200, json={"currency": "ILS"})
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    with pytest.raises(httpx.TimeoutException) as exc_info:
        _fetch(adapter, ClothingType.TOP, 20, client)

    assert classify_exception(exc_info.value) == CrawlFailureReason.SOURCE_TIMEOUT


def test_http_429_propagates_for_retry_classification():
    handler = _handler(cart={"currency": "ILS"}, products_status=429, products={"errors": "slow down"})
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        _fetch(adapter, ClothingType.TOP, 20, client)

    assert classify_exception(exc_info.value) == CrawlFailureReason.RATE_LIMITED


def test_http_5xx_propagates_for_retry_classification():
    handler = _handler(cart={"currency": "ILS"}, products_status=503, products={"errors": "down"})
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        _fetch(adapter, ClothingType.TOP, 20, client)

    assert classify_exception(exc_info.value) == CrawlFailureReason.SOURCE_UNAVAILABLE


def test_ordinary_http_4xx_is_not_retried():
    handler = _handler(cart={"currency": "ILS"}, products_status=404, products={"errors": "not found"})
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        _fetch(adapter, ClothingType.TOP, 20, client)

    assert classify_exception(exc_info.value) == CrawlFailureReason.SOURCE_CLIENT_ERROR


# --- 49. the adapter never constructs its own AsyncClient ---


def test_adapter_does_not_create_a_new_async_client(monkeypatch):
    handler = _handler(cart={"currency": "ILS"}, products=_load("products_valid.json"))
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    def guarded_init(self, *args, **kwargs):
        raise AssertionError("ShopifyAdapter must not construct its own AsyncClient")

    # The test's own `client` above is already constructed - guard against
    # *any further* AsyncClient construction happening during fetch().
    monkeypatch.setattr(httpx.AsyncClient, "__init__", guarded_init)
    try:
        products = _fetch(adapter, ClothingType.TOP, 20, client)
    finally:
        monkeypatch.undo()

    assert len(products) == 1


# --- 50. returned products are independent objects ---


def test_returned_products_are_independent_objects():
    handler = _handler(cart={"currency": "ILS"}, products=_load("products_valid.json"))
    client = _client(handler)
    adapter = ShopifyAdapter(TEST_STORE, now=_fixed_now)

    first = _fetch(adapter, ClothingType.TOP, 20, client)
    second = _fetch(adapter, ClothingType.TOP, 20, client)

    assert first[0] is not second[0]
    first[0].product_name = "MUTATED"
    assert second[0].product_name == "Classic Cotton T-Shirt"