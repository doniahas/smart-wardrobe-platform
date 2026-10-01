"""Season enrichment (trusted Shopify collection membership).

Mirrors test_shopify_adapter.py's MockTransport conventions - no test here
performs a real network call. Covers ShopifyAdapter._fetch_season_by_id /
_parse_product's season assignment, in isolation from any specific store's
real (reviewed) shopify_stores.py config, using a throwaway
ShopifyStoreConfig with an explicit season_collections mapping instead.
"""

import asyncio
from datetime import datetime, timezone
from types import MappingProxyType

import httpx
import pytest

from app.models.product import ClothingType
from app.services.adapters.shopify_adapter import ShopifyAdapter
from app.services.adapters.shopify_stores import (
    ATA,
    KAHIKO_SWIMWEAR,
    PIPA_FASHION,
    SHTAIM,
    ShopifyStoreConfig,
)

_FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return _FIXED_NOW


def _product(id_, title="Some Top", product_type="Tops"):
    return {
        "id": id_,
        "handle": f"handle-{id_}",
        "title": title,
        "product_type": product_type,
        "variants": [{"price": "10.00", "available": True}],
        "images": [],
    }


def _products_payload(*ids):
    return {"products": [_product(i) for i in ids]}


def _handler(*, products=None, products_status=200, collections=None, collection_status=None):
    """`collections` maps handle -> payload dict (or an httpx.Response to
    return verbatim, for failure-injection). `collection_status` overrides
    the status code for every collection response not itself an
    httpx.Response. Any request outside /products.json, /cart.js, or a
    configured /collections/<handle>/products.json is a test-authoring
    mistake.
    """
    collections = collections or {}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cart.js":
            return httpx.Response(200, json={"currency": "ILS"})
        if path == "/products.json":
            if products is None:
                return httpx.Response(products_status)
            return httpx.Response(products_status, json=products)
        if path.startswith("/collections/") and path.endswith("/products.json"):
            handle_name = path[len("/collections/"):-len("/products.json")]
            # Handles are percent-decoded by httpx's URL parsing already.
            entry = collections.get(handle_name)
            if entry is None:
                raise AssertionError(f"unexpected collection handle requested: {handle_name!r}")
            if isinstance(entry, httpx.Response):
                return entry
            status = collection_status or 200
            return httpx.Response(status, json=entry)
        raise AssertionError(f"unexpected request path: {path}")

    return handle


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fetch(adapter, clothing_type, max_results, client):
    async def run():
        return await adapter.fetch(clothing_type, max_results, client)

    return asyncio.run(run())


def _store(season_collections: dict) -> ShopifyStoreConfig:
    return ShopifyStoreConfig(
        source_name="shopify_season_test_store",
        display_name="Season Test Store",
        base_url="https://season-test.example.com",
        season_collections=MappingProxyType(
            {season: tuple(handles) for season, handles in season_collections.items()}
        ),
    )


# --- basic classification --------------------------------------------------


def test_product_in_winter_collection_gets_season_winter():
    store = _store({"winter": ("winter-sale",)})
    handler = _handler(
        products=_products_payload(1, 2),
        collections={"winter-sale": _products_payload(1)},
    )
    client = _client(handler)
    adapter = ShopifyAdapter(store, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    by_id = {p.source_product_id: p.season for p in products}
    assert by_id["1"] == "winter"
    assert by_id["2"] == "unknown"


def test_product_in_summer_collection_gets_season_summer():
    store = _store({"summer": ("summer-picks",)})
    handler = _handler(
        products=_products_payload(1),
        collections={"summer-picks": _products_payload(1)},
    )
    client = _client(handler)
    adapter = ShopifyAdapter(store, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    assert products[0].season == "summer"


def test_product_with_no_configured_season_collections_is_unknown():
    store = _store({})
    handler = _handler(products=_products_payload(1))
    client = _client(handler)
    adapter = ShopifyAdapter(store, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    assert products[0].season == "unknown"


def test_store_with_no_season_collections_makes_no_collection_requests():
    """A store with an empty season_collections config (e.g. real PIPA_FASHION/
    ATA) must never issue a single /collections/*/products.json request -
    only products.json + cart.js, exactly like before this feature existed."""
    store = _store({})
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/cart.js":
            return httpx.Response(200, json={"currency": "ILS"})
        if request.url.path == "/products.json":
            return httpx.Response(200, json=_products_payload(1))
        raise AssertionError(f"unexpected request path: {request.url.path}")

    client = _client(handle)
    adapter = ShopifyAdapter(store, now=_fixed_now)

    _fetch(adapter, ClothingType.TOP, 20, client)

    assert calls == ["/cart.js", "/products.json"] or sorted(calls) == ["/cart.js", "/products.json"]


def test_multiple_handles_for_one_season_are_unioned():
    store = _store({"winter": ("winter-a", "winter-b")})
    handler = _handler(
        products=_products_payload(1, 2, 3),
        collections={
            "winter-a": _products_payload(1),
            "winter-b": _products_payload(2),
        },
    )
    client = _client(handler)
    adapter = ShopifyAdapter(store, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    by_id = {p.source_product_id: p.season for p in products}
    assert by_id["1"] == "winter"
    assert by_id["2"] == "winter"
    assert by_id["3"] == "unknown"


def test_product_in_both_winter_and_summer_collections_is_ambiguous_unknown():
    """A product a merchant (mistakenly, or on purpose) put in both a
    winter and a summer collection is genuinely ambiguous - reported as
    unknown rather than arbitrarily picking one season and presenting it
    as confidently verified."""
    store = _store({"winter": ("winter-sale",), "summer": ("summer-picks",)})
    handler = _handler(
        products=_products_payload(1),
        collections={
            "winter-sale": _products_payload(1),
            "summer-picks": _products_payload(1),
        },
    )
    client = _client(handler)
    adapter = ShopifyAdapter(store, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    assert products[0].season == "unknown"


# --- failure isolation -------------------------------------------------


def test_one_collection_endpoint_failure_does_not_break_the_crawl_or_other_collections():
    store = _store({"winter": ("winter-sale",), "summer": ("summer-picks",)})
    handler = _handler(
        products=_products_payload(1, 2),
        collections={
            "winter-sale": httpx.Response(500, json={"error": "down"}),
            "summer-picks": _products_payload(2),
        },
    )
    client = _client(handler)
    adapter = ShopifyAdapter(store, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    by_id = {p.source_product_id: p.season for p in products}
    # Winter's collection endpoint failed -> no winter evidence at all
    # (product 1 stays unknown), but summer's succeeded independently.
    assert by_id["1"] == "unknown"
    assert by_id["2"] == "summer"


def test_malformed_collection_response_is_treated_as_no_evidence_not_a_crash():
    store = _store({"winter": ("winter-sale",)})
    handler = _handler(
        products=_products_payload(1),
        collections={"winter-sale": httpx.Response(200, text="not valid json")},
    )
    client = _client(handler)
    adapter = ShopifyAdapter(store, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    assert products[0].season == "unknown"


def test_collection_response_with_wrong_shape_is_treated_as_no_evidence():
    store = _store({"winter": ("winter-sale",)})
    handler = _handler(
        products=_products_payload(1),
        collections={"winter-sale": httpx.Response(200, json={"unexpected": "shape"})},
    )
    client = _client(handler)
    adapter = ShopifyAdapter(store, now=_fixed_now)

    products = _fetch(adapter, ClothingType.TOP, 20, client)

    assert products[0].season == "unknown"


def test_clothing_type_filtering_is_unaffected_by_season_enrichment():
    """Regression guard: adding season enrichment must not change which
    products are returned for a given clothing_type - only their season
    field."""
    store = _store({"winter": ("winter-sale",)})
    handler = _handler(
        products={
            "products": [
                _product(1, title="Warm Coat", product_type="Outerwear"),
                _product(2, title="Cotton Shirt", product_type="Tops"),
            ]
        },
        collections={"winter-sale": _products_payload(1)},
    )
    client = _client(handler)
    adapter = ShopifyAdapter(store, now=_fixed_now)

    outerwear = _fetch(adapter, ClothingType.OUTERWEAR, 20, client)
    tops = _fetch(adapter, ClothingType.TOP, 20, client)

    assert {p.source_product_id for p in outerwear} == {"1"}
    assert {p.source_product_id for p in tops} == {"2"}
    assert outerwear[0].season == "winter"
    assert tops[0].season == "unknown"


# --- ShopifyStoreConfig hash regression (review follow-up) -----------------
#
# Adding season_collections (a Mapping/MappingProxyType, unhashable) to the
# frozen ShopifyStoreConfig dataclass silently broke hash(store) for every
# real store config, including ones with an empty season_collections -
# frozen=True conventionally implies hashable, and nothing enforced that
# invariant. Fixed via field(hash=False) on season_collections (still
# participates in __eq__, just excluded from the generated __hash__). These
# tests lock that fix in for all four real, reviewed store configs.


def test_hash_works_for_every_real_store_config_including_empty_season_collections():
    for store in (SHTAIM, PIPA_FASHION, KAHIKO_SWIMWEAR, ATA):
        hash(store)  # must not raise


def test_store_configs_are_usable_in_a_set():
    assert len({SHTAIM, PIPA_FASHION, KAHIKO_SWIMWEAR, ATA}) == 4


def test_equality_still_distinguishes_configs_that_differ_only_in_season_collections():
    """hash(hash=False) excludes the field from __hash__ only - __eq__ must
    still treat two otherwise-identical configs with different
    season_collections as unequal, so this isn't silently ignored data."""
    import dataclasses

    variant = dataclasses.replace(SHTAIM, season_collections={})

    assert SHTAIM != variant
    assert hash(variant) == hash(SHTAIM)  # same hash is fine - not the same object
