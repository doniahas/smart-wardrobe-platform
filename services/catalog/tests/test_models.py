from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.product import CrawlRequest, CrawlResponse, NormalizedProduct, SourceStatus


def _valid_product_kwargs(**overrides):
    base = dict(
        source="mock_demo",
        source_url="https://example.com/product/1",
        source_product_id="SKU-1",
        product_name="Blue T-Shirt",
        clothing_type="top",
        color="blue",
        material="cotton",
        price=Decimal("19.99"),
        currency="USD",
        image_url="https://example.com/images/1.jpg",
        availability="in_stock",
        fetched_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return base


# --- 1. valid NormalizedProduct is accepted ---

def test_valid_normalized_product_is_accepted():
    product = NormalizedProduct(**_valid_product_kwargs())
    assert product.source == "mock_demo"
    assert product.clothing_type == "top"
    assert product.price == Decimal("19.99")
    assert product.currency == "USD"


# --- 2. invalid clothing_type is rejected ---

def test_invalid_clothing_type_is_rejected():
    with pytest.raises(ValidationError):
        NormalizedProduct(**_valid_product_kwargs(clothing_type="hat"))


# --- 3. negative price is rejected ---

def test_negative_price_is_rejected():
    with pytest.raises(ValidationError):
        NormalizedProduct(**_valid_product_kwargs(price=Decimal("-1.00")))


def test_price_is_decimal_not_float():
    product = NormalizedProduct(**_valid_product_kwargs(price=Decimal("9.50")))
    assert isinstance(product.price, Decimal)


# --- 4. invalid currency values are rejected (lowercase, wrong length) ---

@pytest.mark.parametrize("bad_currency", ["usd", "US", "USDD", "12A"])
def test_invalid_currency_format_is_rejected(bad_currency):
    with pytest.raises(ValidationError):
        NormalizedProduct(**_valid_product_kwargs(currency=bad_currency))


# --- 5. invalid/non-HTTP(S) URLs are rejected for source_url and image_url ---

@pytest.mark.parametrize("bad_url", ["not-a-url", "ftp://example.com/product/1"])
def test_invalid_or_non_http_source_url_is_rejected(bad_url):
    with pytest.raises(ValidationError):
        NormalizedProduct(**_valid_product_kwargs(source_url=bad_url))


@pytest.mark.parametrize("bad_url", ["not-a-url", "ftp://example.com/image.jpg"])
def test_invalid_or_non_http_image_url_is_rejected(bad_url):
    with pytest.raises(ValidationError):
        NormalizedProduct(**_valid_product_kwargs(image_url=bad_url))


# --- 6. naive fetched_at / last_checked datetimes are rejected ---

def test_naive_fetched_at_is_rejected():
    with pytest.raises(ValidationError):
        NormalizedProduct(**_valid_product_kwargs(fetched_at=datetime.now()))


def test_naive_last_checked_is_rejected():
    with pytest.raises(ValidationError):
        SourceStatus(
            source="mock_demo",
            ok=True,
            last_checked=datetime.now(),
            consecutive_failures=0,
        )


# --- 7. CrawlRequest.max_results below 1 and above 100 are rejected ---

@pytest.mark.parametrize("bad_value", [0, -5, 101])
def test_max_results_out_of_range_is_rejected(bad_value):
    with pytest.raises(ValidationError):
        CrawlRequest(clothing_type="top", max_results=bad_value)


def test_max_results_default_is_20():
    request = CrawlRequest(clothing_type="top")
    assert request.max_results == 20


# --- 8. negative SourceStatus.consecutive_failures is rejected ---

def test_negative_consecutive_failures_is_rejected():
    with pytest.raises(ValidationError):
        SourceStatus(
            source="mock_demo",
            ok=False,
            last_checked=datetime.now(timezone.utc),
            consecutive_failures=-1,
        )


# --- 9. valid CrawlResponse serializes correctly ---

def test_crawl_response_serializes_correctly():
    product = NormalizedProduct(**_valid_product_kwargs())
    status = SourceStatus(
        source="mock_demo",
        ok=True,
        last_checked=datetime.now(timezone.utc),
        consecutive_failures=0,
        detail=None,
    )
    response = CrawlResponse(products=[product], source_status=[status])

    dumped = response.model_dump(mode="json")

    assert dumped["products"][0]["source"] == "mock_demo"
    assert dumped["products"][0]["source_url"] == "https://example.com/product/1"
    assert dumped["products"][0]["price"] == "19.99"
    assert dumped["products"][0]["availability"] == "in_stock"
    assert dumped["source_status"][0]["source"] == "mock_demo"
    assert dumped["source_status"][0]["ok"] is True
    assert dumped["source_status"][0]["consecutive_failures"] == 0


# --- 10. blank/whitespace-only required and optional strings are rejected ---

def test_blank_source_is_rejected():
    with pytest.raises(ValidationError):
        NormalizedProduct(**_valid_product_kwargs(source="   "))


def test_blank_product_name_is_rejected():
    with pytest.raises(ValidationError):
        NormalizedProduct(**_valid_product_kwargs(product_name="   "))


def test_blank_source_status_source_is_rejected():
    with pytest.raises(ValidationError):
        SourceStatus(
            source="   ",
            ok=True,
            last_checked=datetime.now(timezone.utc),
            consecutive_failures=0,
        )


def test_required_strings_are_stripped():
    product = NormalizedProduct(
        **_valid_product_kwargs(source="  mock_demo  ", product_name="  Blue T-Shirt  ")
    )
    assert product.source == "mock_demo"
    assert product.product_name == "Blue T-Shirt"


def test_blank_optional_string_field_is_rejected():
    with pytest.raises(ValidationError):
        NormalizedProduct(**_valid_product_kwargs(color="   "))


def test_blank_sources_entry_is_rejected():
    with pytest.raises(ValidationError):
        CrawlRequest(clothing_type="top", sources=["mock_demo", "   "])