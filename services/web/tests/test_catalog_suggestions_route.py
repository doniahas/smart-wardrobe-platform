"""GET /recommendations/catalog-suggestions.

This endpoint is strictly additive - it must require auth like every other
endpoint, degrade to a controlled 200 (never an error) whenever the catalog
service is unavailable/times out/returns something malformed, and must
never affect POST /recommendations' own behavior or response shape.

Real-store scoping: this route is a real,
frontend-wired "shop for a missing item" path, so it must forward only the
four real allowlisted stores to the catalog service and must never
surface a mock_demo product to the client, even if the (mocked, in these
tests) catalog backend returns one - see the "real-store scoping" and
"mock_demo exclusion" test groups below.
"""
import httpx
import pytest

from app.services import catalog_gateway
from app.services.catalog_gateway import REAL_STORE_SOURCES


def _patch_crawl(monkeypatch, response: httpx.Response = None, exc=None):
    async def fake_post(self, url, json=None, **kwargs):
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _product(**overrides) -> dict:
    """Mirrors the real catalog service's NormalizedProduct JSON shape,
    including fields (source_product_id, fetched_at) CatalogProductPublic
    doesn't expose - see test_catalog_gateway.py for why this matters.
    Defaults to a real supported store (not mock_demo) since that's what
    this route is expected to actually receive/return in production; tests
    that specifically need mock_demo behavior override `source` explicitly."""
    product = {
        "source": "shopify_ata",
        "source_url": "https://atawear.co.il/products/classic-cotton-t-shirt",
        "source_product_id": "ATA-TOP-1",
        "product_name": "Classic Cotton T-Shirt",
        "clothing_type": "top",
        "color": "white",
        "material": "cotton",
        "price": 19.99,
        "currency": "ILS",
        "image_url": "https://atawear.co.il/images/top-1.jpg",
        "availability": "in_stock",
        "fetched_at": "2025-01-01T00:00:00Z",
    }
    product.update(overrides)
    return product


def test_catalog_suggestions_requires_auth(client):
    resp = client.get("/recommendations/catalog-suggestions", params={"clothing_type": "top"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_catalog_suggestions_success_returns_products(client, make_user, monkeypatch):
    user = await make_user("shopper1")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(200, json={"products": [_product()], "source_status": []}),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["catalog_available"] is True
    assert body["clothing_type"] == "top"
    assert len(body["products"]) == 1
    assert body["products"][0]["product_name"] == "Classic Cotton T-Shirt"


@pytest.mark.asyncio
async def test_catalog_suggestions_service_down_degrades_to_controlled_response(
    client, make_user, monkeypatch
):
    user = await make_user("shopper2")
    _patch_crawl(monkeypatch, exc=httpx.ConnectError("connection refused"))

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["catalog_available"] is False
    assert body["products"] == []


@pytest.mark.asyncio
async def test_catalog_suggestions_malformed_response_degrades_to_controlled_response(
    client, make_user, monkeypatch
):
    user = await make_user("shopper3")
    _patch_crawl(monkeypatch, response=httpx.Response(200, text="not json"))

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["catalog_available"] is False
    assert body["products"] == []


@pytest.mark.asyncio
async def test_catalog_suggestions_disabled_degrades_to_controlled_response(
    client, make_user, monkeypatch
):
    user = await make_user("shopper4")
    monkeypatch.setattr(catalog_gateway, "CATALOG_ENABLED", False)

    called = False

    async def fake_post(self, url, json=None, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json={"products": []})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["catalog_available"] is False
    assert body["products"] == []
    assert called is False


@pytest.mark.asyncio
async def test_catalog_suggestions_rejects_invalid_clothing_type(client, make_user):
    user = await make_user("shopper5")
    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "not-a-real-type"},
        headers=user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_catalog_suggestions_max_results_out_of_range_is_rejected(client, make_user):
    user = await make_user("shopper6")
    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top", "max_results": 999},
        headers=user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_catalog_suggestions_forwards_requested_clothing_type_and_max_results(
    client, make_user, monkeypatch
):
    user = await make_user("shopper7")
    calls = []

    async def fake_post(self, url, json=None, **kwargs):
        calls.append(json)
        return httpx.Response(200, json={"products": []})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "bottom", "max_results": 3},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    # Real-store scoping: `sources` is now always forwarded, scoped to exactly the
    # four real allowlisted stores - never left unset (which would
    # let the catalog service run every registered adapter, including
    # mock_demo).
    assert calls[0] == {
        "clothing_type": "bottom",
        "max_results": 3,
        "sources": list(REAL_STORE_SOURCES),
    }


# --- Real-store scoping / mock_demo exclusion ------------------------------


@pytest.mark.asyncio
async def test_catalog_suggestions_never_forwards_mock_demo_as_a_source(
    client, make_user, monkeypatch
):
    """The request to the catalog service must never include mock_demo -
    only the four real stores, regardless of clothing_type/max_results."""
    user = await make_user("shopper9")
    calls = []

    async def fake_post(self, url, json=None, **kwargs):
        calls.append(json)
        return httpx.Response(200, json={"products": []})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert "mock_demo" not in calls[0]["sources"]
    assert set(calls[0]["sources"]) == {
        "shopify_shtaim", "shopify_pipa_fashion", "shopify_kahiko_swimwear", "shopify_ata",
    }


@pytest.mark.asyncio
async def test_catalog_suggestions_drops_a_mock_demo_product_even_if_returned(
    client, make_user, monkeypatch
):
    """Defense-in-depth: even if the catalog service (mocked here) ignores
    the sources filter and returns a mock_demo product anyway, this route
    must never let it reach the client - it must be silently dropped, not
    surfaced as a real, purchasable recommendation."""
    user = await make_user("shopper10")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(
            200,
            json={
                "products": [
                    _product(source="mock_demo", source_url="https://example.com/mock/products/top-1"),
                    _product(source="shopify_ata", product_name="Real ATA Shirt"),
                ],
                "source_status": [],
            },
        ),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["catalog_available"] is True
    sources_returned = {p["source"] for p in body["products"]}
    assert "mock_demo" not in sources_returned
    assert [p["product_name"] for p in body["products"]] == ["Real ATA Shirt"]


@pytest.mark.asyncio
async def test_catalog_suggestions_still_returns_real_store_products(
    client, make_user, monkeypatch
):
    """The fix must not throw out real products along with mock_demo - a
    genuine product from one of the four real stores must still come
    through unchanged."""
    user = await make_user("shopper11")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(
            200,
            json={"products": [_product(source="shopify_pipa_fashion")], "source_status": []},
        ),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["catalog_available"] is True
    assert len(body["products"]) == 1
    assert body["products"][0]["source"] == "shopify_pipa_fashion"
    assert body["products"][0]["product_name"] == "Classic Cotton T-Shirt"


# --- Issue 2 (catalog-grounding-season): season filtering/fallback --------


@pytest.mark.asyncio
async def test_winter_collection_product_is_returned_for_winter_request(client, make_user, monkeypatch):
    user = await make_user("winter-shopper")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(
            200,
            json={"products": [_product(season="winter")], "source_status": []},
        ),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top", "season": "winter"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["season_requested"] == "winter"
    assert body["season_verified"] is True
    assert len(body["products"]) == 1
    assert body["products"][0]["season"] == "winter"


@pytest.mark.asyncio
async def test_summer_collection_product_is_returned_for_summer_request(client, make_user, monkeypatch):
    user = await make_user("summer-shopper")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(
            200,
            json={"products": [_product(season="summer")], "source_status": []},
        ),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top", "season": "summer"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["season_verified"] is True
    assert body["products"][0]["season"] == "summer"


@pytest.mark.asyncio
async def test_mismatched_season_is_rejected_when_a_valid_seasonal_alternative_exists(
    client, make_user, monkeypatch
):
    """A summer-classified product must not be returned for a winter
    request when a genuinely winter-verified product is also available -
    the mismatched one is excluded, not merely deprioritized."""
    user = await make_user("winter-shopper2")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(
            200,
            json={
                "products": [
                    _product(source_product_id="SUMMER-1", product_name="Summer Tee", season="summer"),
                    _product(source_product_id="WINTER-1", product_name="Winter Sweater", season="winter"),
                ],
                "source_status": [],
            },
        ),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top", "season": "winter"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["season_verified"] is True
    names = [p["product_name"] for p in body["products"]]
    assert names == ["Winter Sweater"]


@pytest.mark.asyncio
async def test_spring_uses_explicit_spring_collection_when_available(client, make_user, monkeypatch):
    user = await make_user("spring-shopper")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(
            200,
            json={"products": [_product(season="spring")], "source_status": []},
        ),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top", "season": "spring"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["season_verified"] is True
    assert body["products"][0]["season"] == "spring"


@pytest.mark.asyncio
async def test_spring_falls_back_to_summer_mapping_when_no_spring_collection_exists(
    client, make_user, monkeypatch
):
    """No product is classified 'spring' at all (this project's stores have
    no trusted spring collection today), but a summer-classified product
    exists - the documented spring->summer equivalence accepts it as a
    verified match for a spring request."""
    user = await make_user("spring-shopper2")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(
            200,
            json={"products": [_product(season="summer")], "source_status": []},
        ),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top", "season": "spring"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["season_verified"] is True
    assert body["products"][0]["season"] == "summer"


@pytest.mark.asyncio
async def test_autumn_uses_explicit_autumn_collection_when_available(client, make_user, monkeypatch):
    user = await make_user("autumn-shopper")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(
            200,
            json={"products": [_product(season="autumn")], "source_status": []},
        ),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top", "season": "autumn"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["season_verified"] is True
    assert body["products"][0]["season"] == "autumn"


@pytest.mark.asyncio
async def test_autumn_falls_back_to_winter_mapping_when_no_autumn_collection_exists(
    client, make_user, monkeypatch
):
    user = await make_user("autumn-shopper2")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(
            200,
            json={"products": [_product(season="winter")], "source_status": []},
        ),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top", "season": "autumn"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["season_verified"] is True
    assert body["products"][0]["season"] == "winter"


@pytest.mark.asyncio
async def test_product_with_no_trusted_seasonal_collection_defaults_to_unknown(
    client, make_user, monkeypatch
):
    user = await make_user("unknown-season-shopper")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(200, json={"products": [_product()], "source_status": []}),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["products"][0]["season"] == "unknown"


@pytest.mark.asyncio
async def test_no_verified_seasonal_product_falls_back_to_type_only_result(
    client, make_user, monkeypatch
):
    """Only an unknown-season product is available for a winter request -
    there is no verified winter match, so the endpoint falls back to the
    unfiltered clothing-type-only result rather than returning nothing."""
    user = await make_user("no-season-evidence-shopper")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(200, json={"products": [_product()], "source_status": []}),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top", "season": "winter"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["products"]) == 1
    assert body["products"][0]["product_name"] == "Classic Cotton T-Shirt"


@pytest.mark.asyncio
async def test_type_only_fallback_result_is_marked_season_unverified(client, make_user, monkeypatch):
    """The fallback result from the previous scenario must never be
    presented as a confirmed seasonal match."""
    user = await make_user("no-season-evidence-shopper2")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(200, json={"products": [_product()], "source_status": []}),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top", "season": "winter"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["season_requested"] == "winter"
    assert body["season_verified"] is False


@pytest.mark.asyncio
async def test_no_season_requested_never_marks_result_as_season_verified(
    client, make_user, monkeypatch
):
    """Regression guard: when the caller doesn't ask for a season at all
    (existing/default behavior), season_verified must stay False - there
    was nothing to verify, and it must never look like a confirmed match."""
    user = await make_user("no-season-requested-shopper")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(
            200, json={"products": [_product(season="winter")], "source_status": []}
        ),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["season_requested"] is None
    assert body["season_verified"] is False
    assert len(body["products"]) == 1


@pytest.mark.asyncio
async def test_clothing_type_filtering_remains_unchanged_with_season_requested(
    client, make_user, monkeypatch
):
    """Season filtering is layered on top of, never a replacement for, the
    existing clothing_type contract - the request/response clothing_type
    is unaffected by adding a season filter."""
    user = await make_user("type-unchanged-shopper")
    _patch_crawl(
        monkeypatch,
        response=httpx.Response(
            200, json={"products": [_product(clothing_type="bottom", season="winter")], "source_status": []}
        ),
    )

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "bottom", "season": "winter"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["clothing_type"] == "bottom"
    assert body["products"][0]["clothing_type"] == "bottom"


@pytest.mark.asyncio
async def test_catalog_suggestions_does_not_touch_wardrobe_or_recommendation_flow(
    client, make_user, make_wardrobe_item, monkeypatch
):
    """Regression guard: calling this endpoint must never read/write
    wardrobe data or otherwise interfere with POST /recommendations."""
    user = await make_user("shopper8")
    item_id = await make_wardrobe_item(user["user_id"])

    _patch_crawl(monkeypatch, response=httpx.Response(200, json={"products": []}))
    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top"},
        headers=user["headers"],
    )
    assert resp.status_code == 200

    # The wardrobe item is untouched.
    list_resp = client.get("/wardrobe", headers=user["headers"])
    assert list_resp.status_code == 200
    ids = [item["id"] for item in list_resp.json()]
    assert item_id in ids
