"""Deterministic, network-free mock clothing catalog.

This is the catalog's deterministic, offline source (`mock_demo`),
registered alongside the real Shopify stores. It is explicitly not a real
store and never makes a network call - all data is a fixed, in-repo
fixture tuple built once at import time. Every product is constructed
through NormalizedProduct so the same validation rules defined in
app/models/product.py apply here without any weakening or duplication.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.models.product import (
    ClothingType,
    CrawlRequest,
    CrawlResponse,
    NormalizedProduct,
    SourceStatus,
)

MOCK_SOURCE_NAME = "mock_demo"

# Fixed, timezone-aware timestamps - never datetime.now() - so every
# response is byte-identical across calls, processes, and days.
_FIXED_FETCHED_AT = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_FIXED_LAST_CHECKED = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# Two fixtures per supported clothing type, in a fixed declaration order
# (top, bottom, dress, outerwear, shoes, accessory - matching
# ClothingType's own declaration order). This tuple's order is the sole
# source of truth for result ordering; nothing downstream re-sorts it.
# A tuple, not a list, since these fixtures are fixed and never mutated.
_RAW_FIXTURES: tuple[dict, ...] = (
    # --- top ---
    dict(
        source_product_id="MOCK-TOP-1",
        product_name="Classic Cotton T-Shirt",
        clothing_type=ClothingType.TOP,
        color="white",
        material="cotton",
        price=Decimal("19.99"),
        currency="USD",
        source_url="https://example.com/mock/products/top-1",
        image_url="https://example.com/mock/images/top-1.jpg",
        availability="in_stock",
    ),
    dict(
        source_product_id="MOCK-TOP-2",
        product_name="Striped Long-Sleeve Shirt",
        clothing_type=ClothingType.TOP,
        color="navy",
        material="cotton blend",
        price=Decimal("24.50"),
        currency="USD",
        source_url="https://example.com/mock/products/top-2",
        image_url="https://example.com/mock/images/top-2.jpg",
        availability="in_stock",
    ),
    # --- bottom ---
    dict(
        source_product_id="MOCK-BOTTOM-1",
        product_name="Slim Fit Chinos",
        clothing_type=ClothingType.BOTTOM,
        color="khaki",
        material="cotton twill",
        price=Decimal("39.99"),
        currency="USD",
        source_url="https://example.com/mock/products/bottom-1",
        image_url="https://example.com/mock/images/bottom-1.jpg",
        availability="in_stock",
    ),
    dict(
        source_product_id="MOCK-BOTTOM-2",
        product_name="Straight Leg Denim Jeans",
        clothing_type=ClothingType.BOTTOM,
        color="blue",
        material="denim",
        price=Decimal("45.00"),
        currency="USD",
        source_url="https://example.com/mock/products/bottom-2",
        image_url="https://example.com/mock/images/bottom-2.jpg",
        availability="out_of_stock",
    ),
    # --- dress ---
    dict(
        source_product_id="MOCK-DRESS-1",
        product_name="Floral Summer Dress",
        clothing_type=ClothingType.DRESS,
        color="floral print",
        material="polyester",
        price=Decimal("34.99"),
        currency="USD",
        source_url="https://example.com/mock/products/dress-1",
        image_url="https://example.com/mock/images/dress-1.jpg",
        availability="in_stock",
    ),
    dict(
        source_product_id="MOCK-DRESS-2",
        product_name="Little Black Dress",
        clothing_type=ClothingType.DRESS,
        color="black",
        material="polyester blend",
        price=Decimal("59.99"),
        currency="USD",
        source_url="https://example.com/mock/products/dress-2",
        image_url="https://example.com/mock/images/dress-2.jpg",
        availability="in_stock",
    ),
    # --- outerwear ---
    dict(
        source_product_id="MOCK-OUTERWEAR-1",
        product_name="Water-Resistant Rain Jacket",
        clothing_type=ClothingType.OUTERWEAR,
        color="yellow",
        material="nylon",
        price=Decimal("69.99"),
        currency="USD",
        source_url="https://example.com/mock/products/outerwear-1",
        image_url="https://example.com/mock/images/outerwear-1.jpg",
        availability="in_stock",
    ),
    dict(
        source_product_id="MOCK-OUTERWEAR-2",
        product_name="Wool Blend Peacoat",
        clothing_type=ClothingType.OUTERWEAR,
        color="charcoal",
        material="wool blend",
        price=Decimal("129.99"),
        currency="USD",
        source_url="https://example.com/mock/products/outerwear-2",
        image_url="https://example.com/mock/images/outerwear-2.jpg",
        availability="out_of_stock",
    ),
    # --- shoes ---
    dict(
        source_product_id="MOCK-SHOES-1",
        product_name="Canvas Low-Top Sneakers",
        clothing_type=ClothingType.SHOES,
        color="white",
        material="canvas",
        price=Decimal("29.99"),
        currency="USD",
        source_url="https://example.com/mock/products/shoes-1",
        image_url="https://example.com/mock/images/shoes-1.jpg",
        availability="in_stock",
    ),
    dict(
        source_product_id="MOCK-SHOES-2",
        product_name="Leather Ankle Boots",
        clothing_type=ClothingType.SHOES,
        color="brown",
        material="leather",
        price=Decimal("89.99"),
        currency="USD",
        source_url="https://example.com/mock/products/shoes-2",
        image_url="https://example.com/mock/images/shoes-2.jpg",
        availability="in_stock",
    ),
    # --- accessory ---
    dict(
        source_product_id="MOCK-ACCESSORY-1",
        product_name="Woven Leather Belt",
        clothing_type=ClothingType.ACCESSORY,
        color="brown",
        material="leather",
        price=Decimal("15.99"),
        currency="USD",
        source_url="https://example.com/mock/products/accessory-1",
        image_url="https://example.com/mock/images/accessory-1.jpg",
        availability="in_stock",
    ),
    dict(
        source_product_id="MOCK-ACCESSORY-2",
        product_name="Wool Beanie",
        clothing_type=ClothingType.ACCESSORY,
        color="grey",
        material="wool",
        price=Decimal("12.50"),
        currency="USD",
        source_url="https://example.com/mock/products/accessory-2",
        image_url="https://example.com/mock/images/accessory-2.jpg",
        availability="in_stock",
    ),
)

# Built once at import time, as an immutable tuple: any invalid fixture
# fails loudly on service startup (and on test collection), never silently
# at request time. Being a tuple (not a list) prevents accidental
# append/remove/reassignment of the module-level catalog itself; search()
# additionally hands out deep copies (see below) so a caller mutating a
# returned NormalizedProduct can never affect this tuple's contents or any
# later search() call.
_PRODUCTS: tuple[NormalizedProduct, ...] = tuple(
    NormalizedProduct(source=MOCK_SOURCE_NAME, fetched_at=_FIXED_FETCHED_AT, **raw)
    for raw in _RAW_FIXTURES
)


def search(request: CrawlRequest) -> CrawlResponse:
    """Query the mock catalog. Pure, synchronous, no I/O of any kind.

    Returns independent deep copies of the matched products, never
    references into the shared _PRODUCTS tuple, so mutating a product on
    one returned CrawlResponse can never influence a later search() call.
    """

    if request.sources is not None and MOCK_SOURCE_NAME not in request.sources:
        return CrawlResponse(products=[], source_status=[])

    matched = [
        product for product in _PRODUCTS if product.clothing_type == request.clothing_type
    ]
    limited = [
        product.model_copy(deep=True) for product in matched[: request.max_results]
    ]

    status = SourceStatus(
        source=MOCK_SOURCE_NAME,
        ok=True,
        last_checked=_FIXED_LAST_CHECKED,
        consecutive_failures=0,
        detail=None,
    )
    return CrawlResponse(products=limited, source_status=[status])