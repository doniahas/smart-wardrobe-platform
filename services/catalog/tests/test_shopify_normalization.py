"""Unit tests for the Shopify parsing/normalization helpers and the
category-classification logic - no network, no adapter, no FastAPI
involved. See test_shopify_adapter.py and test_shopify_routes.py for the
integrated behaviors.
"""

from decimal import Decimal

from app.models.product import ClothingType
from app.services.adapters.shopify_adapter import (
    available_variants,
    build_product_url,
    classify_clothing_type,
    first_image,
    lowest_price,
    select_currency,
    select_source_product_id,
)

# --- variant selection (1-2) --------------------------------------------------


def test_keeps_only_available_variants():
    variants = [
        {"id": 1, "price": "10.00", "available": True},
        {"id": 2, "price": "12.00", "available": False},
    ]
    assert [v["id"] for v in available_variants({"variants": variants})] == [1]


def test_ignores_unavailable_variants_entirely():
    variants = [{"id": 1, "price": "10.00", "available": False}]
    assert available_variants({"variants": variants}) == []


# --- price selection (3-6) ----------------------------------------------------


def test_chooses_lowest_available_valid_decimal_price():
    variants = [
        {"price": "59.90", "available": True},
        {"price": "49.90", "available": True},
        {"price": "39.90", "available": True},
    ]
    assert lowest_price(variants) == Decimal("39.90")


def test_ignores_invalid_or_missing_prices():
    variants = [
        {"price": "not-a-number", "available": True},
        {"price": None, "available": True},
        {"available": True},
        {"price": "25.00", "available": True},
    ]
    assert lowest_price(variants) == Decimal("25.00")


def test_rejects_negative_prices():
    variants = [{"price": "-5.00", "available": True}]
    assert lowest_price(variants) is None


def test_rejects_nan_and_infinite_prices():
    variants = [
        {"price": "NaN", "available": True},
        {"price": "Infinity", "available": True},
        {"price": "-Infinity", "available": True},
    ]
    assert lowest_price(variants) is None


def test_price_selection_ignores_negative_alongside_a_valid_price():
    variants = [
        {"price": "-5.00", "available": True},
        {"price": "15.00", "available": True},
    ]
    assert lowest_price(variants) == Decimal("15.00")


# --- image selection (7-9) ----------------------------------------------------


def test_extracts_image_from_dict_containing_src():
    product = {"images": [{"src": "https://cdn.example.com/a.jpg"}]}
    assert first_image(product) == "https://cdn.example.com/a.jpg"


def test_extracts_direct_string_image():
    product = {"images": ["https://cdn.example.com/b.jpg"]}
    assert first_image(product) == "https://cdn.example.com/b.jpg"


def test_allows_missing_image():
    assert first_image({"images": []}) is None
    assert first_image({}) is None


# --- product URL / id (10-12) -------------------------------------------------


def test_constructs_correctly_encoded_product_url():
    # Real Shopify handles use hyphens, never spaces (Shopify generates the
    # handle from the title by replacing spaces with hyphens).
    url = build_product_url("https://shtaim.store", "כובע-באקט")
    assert url == "https://shtaim.store/products/%D7%9B%D7%95%D7%91%D7%A2-%D7%91%D7%90%D7%A7%D7%98"


def test_prefers_shopify_product_id_as_source_product_id():
    product = {"id": 987654, "handle": "some-handle"}
    assert select_source_product_id(product) == "987654"


def test_falls_back_to_handle_when_id_missing():
    product = {"handle": "some-handle"}
    assert select_source_product_id(product) == "some-handle"


# --- currency selection (13-15) -----------------------------------------------


def test_uses_valid_cart_js_currency():
    assert select_currency("usd", "ILS") == "USD"


def test_falls_back_to_ils_when_cart_currency_missing():
    assert select_currency(None, "ILS") == "ILS"


def test_falls_back_to_ils_when_cart_currency_not_a_usable_code():
    assert select_currency("", "ILS") == "ILS"
    assert select_currency("dollars", "ILS") == "ILS"
    assert select_currency(123, "ILS") == "ILS"  # type: ignore[arg-type]


# --- category classification (16-32) -------------------------------------------


def test_english_top():
    assert classify_clothing_type("Tops", "Classic Cotton T-Shirt") == ClothingType.TOP


def test_hebrew_top():
    assert classify_clothing_type(None, "חולצה כחולה") == ClothingType.TOP


def test_english_bottom():
    assert classify_clothing_type("Trousers", "Slim Fit Chinos") == ClothingType.BOTTOM


def test_hebrew_bottom():
    assert classify_clothing_type(None, "מכנסון קיץ") == ClothingType.BOTTOM


def test_english_dress():
    assert classify_clothing_type("Dresses", "Little Black Dress") == ClothingType.DRESS


def test_hebrew_dress():
    assert classify_clothing_type(None, "שמלה שחורה") == ClothingType.DRESS


def test_bikini_top():
    assert classify_clothing_type(None, "Bikini Top Santorini") == ClothingType.TOP


def test_bikini_bottom():
    assert classify_clothing_type(None, "Bikini Bottom Chiba") == ClothingType.BOTTOM


def test_one_piece_swimsuit_maps_to_dress():
    assert classify_clothing_type(None, "One-Piece Swimsuit Tellos") == ClothingType.DRESS
    assert classify_clothing_type(None, "בגד ים שלם ירוק טורקיז") == ClothingType.DRESS


def test_accessory():
    assert classify_clothing_type("Bags", "Everyday Tote Bag") == ClothingType.ACCESSORY
    assert classify_clothing_type(None, "כובע טמבל") == ClothingType.ACCESSORY


def test_shoes():
    assert classify_clothing_type("Shoes", "Leather Ankle Boots") == ClothingType.SHOES
    assert classify_clothing_type(None, "סנדל קיץ") == ClothingType.SHOES


def test_outerwear():
    assert classify_clothing_type("Jackets", "Wool Blend Peacoat Jacket") == ClothingType.OUTERWEAR
    assert classify_clothing_type(None, "מעיל חורף") == ClothingType.OUTERWEAR


def test_missing_product_type_classified_from_title():
    assert classify_clothing_type(None, "Straight Leg Denim Jeans") == ClothingType.BOTTOM


def test_incorrect_product_type_corrected_from_title():
    # product_type says a top, title clearly says shoes - title wins.
    assert classify_clothing_type("Tops", "Leather Ankle Boots") == ClothingType.SHOES


def test_hebrew_title_overrides_incorrect_product_type_real_data_example():
    # Real-data example captured from an earlier crawl of Shtaim's catalog:
    # "חצאית כיווץ משבצות" is mis-tagged product_type="shirts".
    result = classify_clothing_type("shirts", "חצאית כיווץ משבצות")
    assert result == ClothingType.BOTTOM


def test_unknown_product_is_skipped():
    assert classify_clothing_type("Widgets", "Mystery Gadget XR200") is None


def test_ambiguous_product_is_not_forced_into_a_category():
    # Title contains both a TOP and a BOTTOM keyword - genuinely ambiguous.
    assert classify_clothing_type(None, "חולצה מכנס משולב") is None