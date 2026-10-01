from fastapi.testclient import TestClient

from app.main import app
from app.models.product import ClothingType, CrawlRequest, CrawlResponse
from app.services.mock_source import search

client = TestClient(app)


# --- 1 & 2. a valid request returns 200 and validates through CrawlResponse ---

def test_search_endpoint_returns_200_and_valid_crawl_response():
    response = client.post("/catalog/search", json={"clothing_type": "top"})

    assert response.status_code == 200
    CrawlResponse(**response.json())


# --- 3. results contain only the requested clothing_type ---

def test_search_endpoint_filters_by_clothing_type():
    response = client.post("/catalog/search", json={"clothing_type": "shoes"})

    body = response.json()
    assert len(body["products"]) > 0
    assert all(product["clothing_type"] == "shoes" for product in body["products"])


# --- 4. max_results is respected ---

def test_search_endpoint_respects_max_results():
    response = client.post(
        "/catalog/search", json={"clothing_type": "top", "max_results": 1}
    )

    assert response.status_code == 200
    assert len(response.json()["products"]) == 1


# --- 5. sources=["mock_demo"] returns products ---

def test_search_endpoint_known_source_returns_products():
    response = client.post(
        "/catalog/search", json={"clothing_type": "dress", "sources": ["mock_demo"]}
    )

    assert response.status_code == 200
    assert len(response.json()["products"]) == 2


# --- 6. an unknown source returns 200 with empty products and source_status ---

def test_search_endpoint_unknown_source_returns_empty_lists():
    response = client.post(
        "/catalog/search",
        json={"clothing_type": "dress", "sources": ["real_store_that_does_not_exist"]},
    )

    assert response.status_code == 200
    assert response.json() == {"products": [], "source_status": []}


# --- 7. missing clothing_type is a validation error ---

def test_search_endpoint_missing_clothing_type_is_422():
    response = client.post("/catalog/search", json={})

    assert response.status_code == 422


# --- 8. invalid clothing_type is a validation error ---

def test_search_endpoint_invalid_clothing_type_is_422():
    response = client.post("/catalog/search", json={"clothing_type": "hat"})

    assert response.status_code == 422


# --- 9 & 10. max_results out of range is a validation error ---

def test_search_endpoint_max_results_zero_is_422():
    response = client.post(
        "/catalog/search", json={"clothing_type": "top", "max_results": 0}
    )

    assert response.status_code == 422


def test_search_endpoint_max_results_101_is_422():
    response = client.post(
        "/catalog/search", json={"clothing_type": "top", "max_results": 101}
    )

    assert response.status_code == 422


# --- 11. endpoint JSON exactly matches a direct search() call ---

def test_search_endpoint_response_matches_direct_search_call():
    response = client.post(
        "/catalog/search", json={"clothing_type": "outerwear", "max_results": 5}
    )

    expected = search(CrawlRequest(clothing_type=ClothingType.OUTERWEAR, max_results=5))

    assert response.json() == expected.model_dump(mode="json")


# --- 12. repeated identical requests return identical JSON ---

def test_search_endpoint_repeated_requests_return_identical_json():
    payload = {"clothing_type": "accessory"}

    first = client.post("/catalog/search", json=payload)
    second = client.post("/catalog/search", json=payload)

    assert first.json() == second.json()