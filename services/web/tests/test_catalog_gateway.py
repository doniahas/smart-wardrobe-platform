"""services/catalog_gateway.py - the defensive HTTP wrapper
around the internal `catalog` service. Mirrors test_llm_gateway.py's
patching idiom (monkeypatch httpx.AsyncClient.post)."""
import httpx
import pytest

from app.models import CatalogSeason, Season
from app.services.catalog_gateway import (
    CatalogCrawlResult,
    CatalogGatewayFailure,
    CatalogGatewayFailureReason,
    fetch_catalog_products,
    season_matches,
)


def _patch_post(monkeypatch, response: httpx.Response = None, exc=None):
    async def fake_post(self, url, json=None, **kwargs):
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _product(**overrides) -> dict:
    """Mirrors the catalog service's actual NormalizedProduct JSON shape
    exactly (services/catalog/app/models/product.py) - including
    source_product_id and fetched_at, which CatalogProductPublic does not
    expose but a real /crawl response always includes. Omitting these here
    would mask a real bug: see
    test_fetch_real_shaped_product_with_extra_fields_is_not_dropped below,
    a regression test for exactly that."""
    product = {
        "source": "mock_demo",
        "source_url": "https://example.com/mock/products/top-1",
        "source_product_id": "MOCK-TOP-1",
        "product_name": "Classic Cotton T-Shirt",
        "clothing_type": "top",
        "color": "white",
        "material": "cotton",
        "price": 19.99,
        "currency": "USD",
        "image_url": "https://example.com/mock/images/top-1.jpg",
        "availability": "in_stock",
        "fetched_at": "2025-01-01T00:00:00Z",
    }
    product.update(overrides)
    return product


@pytest.mark.asyncio
async def test_fetch_success_returns_validated_products(monkeypatch):
    _patch_post(
        monkeypatch,
        response=httpx.Response(
            200, json={"products": [_product()], "source_status": []}
        ),
    )

    result = await fetch_catalog_products("top", 5)

    assert isinstance(result, CatalogCrawlResult)
    assert len(result.products) == 1
    assert result.products[0].product_name == "Classic Cotton T-Shirt"
    assert result.products[0].clothing_type == "top"


@pytest.mark.asyncio
async def test_fetch_sends_clothing_type_and_max_results(monkeypatch):
    calls = []

    async def fake_post(self, url, json=None, **kwargs):
        calls.append((url, json))
        return httpx.Response(200, json={"products": [], "source_status": []})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await fetch_catalog_products("bottom", 7)

    assert calls[0][0].endswith("/crawl")
    assert calls[0][1] == {"clothing_type": "bottom", "max_results": 7}


@pytest.mark.asyncio
async def test_fetch_omits_sources_key_when_not_given(monkeypatch):
    """Chat grounding added an optional `sources` param - existing
    callers (routers/recommendations.py) that don't pass it must keep
    sending the exact same request body as before (see
    test_fetch_sends_clothing_type_and_max_results above)."""
    calls = []

    async def fake_post(self, url, json=None, **kwargs):
        calls.append(json)
        return httpx.Response(200, json={"products": []})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await fetch_catalog_products("top", 5)

    assert "sources" not in calls[0]


@pytest.mark.asyncio
async def test_fetch_forwards_sources_when_given(monkeypatch):
    calls = []

    async def fake_post(self, url, json=None, **kwargs):
        calls.append(json)
        return httpx.Response(200, json={"products": []})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await fetch_catalog_products("top", 5, sources=["shopify_ata"])

    assert calls[0] == {"clothing_type": "top", "max_results": 5, "sources": ["shopify_ata"]}


@pytest.mark.asyncio
async def test_fetch_unreachable_maps_to_catalog_unavailable(monkeypatch):
    _patch_post(monkeypatch, exc=httpx.ConnectError("connection refused"))

    result = await fetch_catalog_products("top", 5)

    assert isinstance(result, CatalogGatewayFailure)
    assert result.reason == CatalogGatewayFailureReason.CATALOG_UNAVAILABLE


@pytest.mark.asyncio
async def test_fetch_timeout_maps_to_catalog_unavailable(monkeypatch):
    _patch_post(monkeypatch, exc=httpx.TimeoutException("timed out"))

    result = await fetch_catalog_products("top", 5)

    assert isinstance(result, CatalogGatewayFailure)
    assert result.reason == CatalogGatewayFailureReason.CATALOG_UNAVAILABLE


@pytest.mark.asyncio
async def test_fetch_non_200_maps_to_catalog_invalid_response(monkeypatch):
    _patch_post(monkeypatch, response=httpx.Response(500, text="internal error"))

    result = await fetch_catalog_products("top", 5)

    assert isinstance(result, CatalogGatewayFailure)
    assert result.reason == CatalogGatewayFailureReason.CATALOG_INVALID_RESPONSE


@pytest.mark.asyncio
async def test_fetch_malformed_json_maps_to_catalog_invalid_response(monkeypatch):
    _patch_post(monkeypatch, response=httpx.Response(200, text="not json"))

    result = await fetch_catalog_products("top", 5)

    assert isinstance(result, CatalogGatewayFailure)
    assert result.reason == CatalogGatewayFailureReason.CATALOG_INVALID_RESPONSE


@pytest.mark.asyncio
async def test_fetch_missing_products_field_maps_to_catalog_invalid_response(monkeypatch):
    _patch_post(monkeypatch, response=httpx.Response(200, json={"source_status": []}))

    result = await fetch_catalog_products("top", 5)

    assert isinstance(result, CatalogGatewayFailure)
    assert result.reason == CatalogGatewayFailureReason.CATALOG_INVALID_RESPONSE


@pytest.mark.asyncio
async def test_fetch_products_not_a_list_degrades_to_empty(monkeypatch):
    _patch_post(
        monkeypatch,
        response=httpx.Response(200, json={"products": "oops-not-a-list"}),
    )

    result = await fetch_catalog_products("top", 5)

    assert isinstance(result, CatalogCrawlResult)
    assert result.products == []


@pytest.mark.asyncio
async def test_fetch_malformed_product_entries_are_skipped_not_crashed(monkeypatch):
    _patch_post(
        monkeypatch,
        response=httpx.Response(
            200,
            json={
                "products": [
                    "not-a-dict",
                    42,
                    {"source": "mock_demo"},  # missing required fields
                    _product(product_name="Valid Item"),
                ],
            },
        ),
    )

    result = await fetch_catalog_products("top", 5)

    assert isinstance(result, CatalogCrawlResult)
    assert len(result.products) == 1
    assert result.products[0].product_name == "Valid Item"


@pytest.mark.asyncio
async def test_fetch_disabled_returns_catalog_disabled_without_network_call(monkeypatch):
    import app.services.catalog_gateway as gateway

    monkeypatch.setattr(gateway, "CATALOG_ENABLED", False)

    called = False

    async def fake_post(self, url, json=None, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json={"products": []})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await fetch_catalog_products("top", 5)

    assert isinstance(result, CatalogGatewayFailure)
    assert result.reason == CatalogGatewayFailureReason.CATALOG_DISABLED
    assert called is False


@pytest.mark.asyncio
async def test_fetch_real_shaped_product_with_extra_fields_is_not_dropped(monkeypatch):
    """Regression test: a real catalog /crawl response's NormalizedProduct
    always includes source_product_id and fetched_at, which
    CatalogProductPublic (extra="forbid") does not expose. These must be
    filtered out before validation, not cause the whole product to be
    treated as malformed and silently dropped - discovered via a live
    Docker smoke test against the real catalog service during
    validation, where every real product was being skipped for exactly
    this reason before the _ALLOWED_PRODUCT_FIELDS filter was added."""
    _patch_post(
        monkeypatch,
        response=httpx.Response(
            200,
            json={
                "products": [_product()],
                "source_status": [
                    {"source": "mock_demo", "ok": True, "last_checked": "2025-01-01T00:00:00Z",
                     "consecutive_failures": 0, "detail": None}
                ],
            },
        ),
    )

    result = await fetch_catalog_products("top", 5)

    assert isinstance(result, CatalogCrawlResult)
    assert len(result.products) == 1
    assert result.products[0].product_name == "Classic Cotton T-Shirt"


@pytest.mark.asyncio
async def test_fetch_rejects_invalid_clothing_type_product_via_validation(monkeypatch):
    """A product whose clothing_type doesn't match the web-side ClothingType
    enum must be dropped, never surfaced as an untyped/invented category."""
    _patch_post(
        monkeypatch,
        response=httpx.Response(
            200,
            json={"products": [_product(clothing_type="not-a-real-type")]},
        ),
    )

    result = await fetch_catalog_products("top", 5)

    assert isinstance(result, CatalogCrawlResult)
    assert result.products == []


# --- Issue 2 (catalog-grounding-season) -------------------------------------


@pytest.mark.asyncio
async def test_fetch_forwards_the_season_field(monkeypatch):
    """`season` is now an allowlisted field - a product's trusted season
    classification must survive the same "verify, don't invent" filtering
    every other field already goes through."""
    _patch_post(
        monkeypatch,
        response=httpx.Response(200, json={"products": [_product(season="winter")]}),
    )

    result = await fetch_catalog_products("top", 5)

    assert result.products[0].season == CatalogSeason.WINTER


@pytest.mark.asyncio
async def test_fetch_product_with_no_season_defaults_to_unknown(monkeypatch):
    product = _product()
    product.pop("season", None)
    _patch_post(
        monkeypatch,
        response=httpx.Response(200, json={"products": [product]}),
    )

    result = await fetch_catalog_products("top", 5)

    assert result.products[0].season == CatalogSeason.UNKNOWN


# --- season_matches (pure function) ----------------------------------------


def test_season_matches_exact_winter():
    assert season_matches(CatalogSeason.WINTER, Season.WINTER) is True


def test_season_matches_exact_summer():
    assert season_matches(CatalogSeason.SUMMER, Season.SUMMER) is True


def test_season_matches_all_satisfies_any_request():
    assert season_matches(CatalogSeason.ALL, Season.WINTER) is True
    assert season_matches(CatalogSeason.ALL, Season.SUMMER) is True
    assert season_matches(CatalogSeason.ALL, Season.SPRING) is True
    assert season_matches(CatalogSeason.ALL, Season.AUTUMN) is True


def test_season_matches_unknown_never_satisfies_anything():
    assert season_matches(CatalogSeason.UNKNOWN, Season.WINTER) is False
    assert season_matches(CatalogSeason.UNKNOWN, Season.SUMMER) is False
    assert season_matches(CatalogSeason.UNKNOWN, Season.SPRING) is False
    assert season_matches(CatalogSeason.UNKNOWN, Season.AUTUMN) is False


def test_season_matches_winter_rejects_summer_product():
    assert season_matches(CatalogSeason.SUMMER, Season.WINTER) is False


def test_season_matches_summer_rejects_winter_product():
    assert season_matches(CatalogSeason.WINTER, Season.SUMMER) is False


def test_season_matches_spring_accepts_summer_fallback():
    assert season_matches(CatalogSeason.SUMMER, Season.SPRING) is True


def test_season_matches_spring_prefers_exact_spring_match():
    assert season_matches(CatalogSeason.SPRING, Season.SPRING) is True


def test_season_matches_autumn_accepts_winter_fallback():
    assert season_matches(CatalogSeason.WINTER, Season.AUTUMN) is True


def test_season_matches_autumn_prefers_exact_autumn_match():
    assert season_matches(CatalogSeason.AUTUMN, Season.AUTUMN) is True


def test_season_matches_winter_has_no_fallback_to_spring_or_autumn():
    """Winter/summer requests only accept an exact (or ALL) match - the
    fallback mapping only ever applies to spring/autumn requests."""
    assert season_matches(CatalogSeason.SPRING, Season.WINTER) is False
    assert season_matches(CatalogSeason.AUTUMN, Season.WINTER) is False
