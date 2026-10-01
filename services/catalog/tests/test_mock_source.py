import pytest

from app.models.product import ClothingType, CrawlRequest, CrawlResponse
from app.services.mock_source import MOCK_SOURCE_NAME, search


# --- 1. every supported clothing type returns matching, non-empty results ---

@pytest.mark.parametrize("clothing_type", list(ClothingType))
def test_search_returns_products_for_each_supported_clothing_type(clothing_type):
    result = search(CrawlRequest(clothing_type=clothing_type))

    assert len(result.products) == 2
    assert all(product.clothing_type == clothing_type for product in result.products)


# --- 2. max_results is respected ---

def test_search_respects_max_results():
    result = search(CrawlRequest(clothing_type=ClothingType.TOP, max_results=1))

    assert len(result.products) == 1
    assert result.products[0].product_name == "Classic Cotton T-Shirt"


# --- 3. explicit source filtering by name ---

def test_search_filters_by_requested_source_name():
    result = search(CrawlRequest(clothing_type=ClothingType.TOP, sources=["mock_demo"]))

    assert len(result.products) == 2
    assert result.source_status[0].source == MOCK_SOURCE_NAME


# --- 4. unknown/nonmatching source returns no products ---

def test_search_unknown_source_returns_no_products():
    result = search(
        CrawlRequest(clothing_type=ClothingType.TOP, sources=["real_store_that_does_not_exist"])
    )

    assert result.products == []
    assert result.source_status == []


# --- 5. deterministic ordering/content across repeated calls ---

def test_search_is_deterministic_across_calls():
    request = CrawlRequest(clothing_type=ClothingType.SHOES)

    first = search(request)
    second = search(request)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


# --- 6. empty result behavior is a normal value, never an exception ---

def test_search_empty_when_no_products_match():
    result = search(
        CrawlRequest(clothing_type=ClothingType.ACCESSORY, sources=["other_store"])
    )

    assert result == CrawlResponse(products=[], source_status=[])


# --- 7. return value is a genuine, fully-validated CrawlResponse ---

def test_search_result_validates_as_crawl_response():
    result = search(CrawlRequest(clothing_type=ClothingType.DRESS))

    assert isinstance(result, CrawlResponse)
    revalidated = CrawlResponse(**result.model_dump())
    assert revalidated == result


# --- 8. omitting sources behaves the same as explicitly requesting mock_demo ---

def test_search_default_sources_none_queries_mock_demo():
    implicit = search(CrawlRequest(clothing_type=ClothingType.BOTTOM))
    explicit = search(CrawlRequest(clothing_type=ClothingType.BOTTOM, sources=["mock_demo"]))

    assert implicit.model_dump(mode="json") == explicit.model_dump(mode="json")


# --- 9. mutating one response's products never affects a later search() call ---

def test_mutating_one_search_response_does_not_affect_later_searches():
    request = CrawlRequest(clothing_type=ClothingType.TOP)

    first = search(request)
    assert first.products[0].color == "white"

    first.products[0].color = "mutated"

    second = search(request)

    assert second.products[0].color == "white"