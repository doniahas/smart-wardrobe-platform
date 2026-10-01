"""Unit/route tests for the CATALOG_ENABLED_SOURCES startup
allowlist (services/catalog/app/core/config.py,
app/main.py::_select_registered_adapters).

Purpose: prove the config-parsing + adapter-filtering logic in isolation,
so the E2E test (tests/system/test_e2e_catalog_flow.py) only needs to
prove the real end-to-end wiring works, not re-prove this unit-level
behavior - the same "don't duplicate coverage already proven elsewhere"
principle applied to config parsing.
"""
import pytest
from fastapi.testclient import TestClient

import app.core.config as config_module
import app.main as main_module
from app.main import _select_registered_adapters, app
from app.services.adapters.mock_adapter import MockSourceAdapter
from app.services.adapters.shopify_adapter import ShopifyAdapter
from app.services.adapters.shopify_stores import SHOPIFY_STORES


def _all_real_adapters():
    """The exact same construction app/main.py's lifespan() uses."""
    return [MockSourceAdapter()] + [ShopifyAdapter(store) for store in SHOPIFY_STORES]


# --- config.py: _read_optional_source_list parsing ---


def test_unset_env_var_parses_to_none(monkeypatch):
    monkeypatch.delenv("CATALOG_ENABLED_SOURCES", raising=False)
    assert config_module._read_optional_source_list("CATALOG_ENABLED_SOURCES") is None


def test_blank_env_var_parses_to_none(monkeypatch):
    monkeypatch.setenv("CATALOG_ENABLED_SOURCES", "   ")
    assert config_module._read_optional_source_list("CATALOG_ENABLED_SOURCES") is None


def test_single_source_parses_to_one_item_list(monkeypatch):
    monkeypatch.setenv("CATALOG_ENABLED_SOURCES", "mock_demo")
    assert config_module._read_optional_source_list("CATALOG_ENABLED_SOURCES") == ["mock_demo"]


def test_comma_separated_sources_are_split_and_stripped(monkeypatch):
    monkeypatch.setenv("CATALOG_ENABLED_SOURCES", " mock_demo , shopify_ata ,shopify_shtaim")
    assert config_module._read_optional_source_list("CATALOG_ENABLED_SOURCES") == [
        "mock_demo", "shopify_ata", "shopify_shtaim",
    ]


def test_empty_entries_between_commas_are_dropped(monkeypatch):
    monkeypatch.setenv("CATALOG_ENABLED_SOURCES", "mock_demo,,shopify_ata,")
    assert config_module._read_optional_source_list("CATALOG_ENABLED_SOURCES") == [
        "mock_demo", "shopify_ata",
    ]


# --- main.py: _select_registered_adapters ---


def test_none_returns_all_adapters_unchanged(monkeypatch):
    """The default (unset) case - proves zero behavior change from before
    this feature existed. Pinned explicitly via monkeypatch (rather than
    relying on the ambient environment truly having the var unset) so this
    test's meaning doesn't depend on how/where it happens to run."""
    monkeypatch.setattr(main_module, "CATALOG_ENABLED_SOURCES", None)
    all_adapters = _all_real_adapters()
    result = _select_registered_adapters(all_adapters)
    assert result == all_adapters
    assert [a.name for a in result] == [
        "mock_demo", "shopify_shtaim", "shopify_pipa_fashion",
        "shopify_kahiko_swimwear", "shopify_ata",
    ]


def test_single_enabled_source_selects_only_that_adapter(monkeypatch):
    monkeypatch.setattr(main_module, "CATALOG_ENABLED_SOURCES", ["mock_demo"])
    all_adapters = _all_real_adapters()
    result = _select_registered_adapters(all_adapters)
    assert [a.name for a in result] == ["mock_demo"]


def test_selection_preserves_original_registration_order_not_env_var_order(monkeypatch):
    """The allowlist is written in reverse-of-canonical order; the
    resulting adapter list must still come out in all_adapters' own order
    (mock_demo before shopify_ata), never the order the env var listed
    them in - GET /sources' ordering guarantee must hold regardless."""
    monkeypatch.setattr(main_module, "CATALOG_ENABLED_SOURCES", ["shopify_ata", "mock_demo"])
    all_adapters = _all_real_adapters()
    result = _select_registered_adapters(all_adapters)
    assert [a.name for a in result] == ["mock_demo", "shopify_ata"]


def test_unknown_source_name_raises_value_error(monkeypatch):
    monkeypatch.setattr(main_module, "CATALOG_ENABLED_SOURCES", ["not_a_real_source"])
    with pytest.raises(ValueError, match="unknown source"):
        _select_registered_adapters(_all_real_adapters())


def test_mix_of_known_and_unknown_source_names_raises_value_error(monkeypatch):
    monkeypatch.setattr(main_module, "CATALOG_ENABLED_SOURCES", ["mock_demo", "not_a_real_source"])
    with pytest.raises(ValueError, match="not_a_real_source"):
        _select_registered_adapters(_all_real_adapters())


def test_empty_selection_result_raises_value_error(monkeypatch):
    """Can't actually happen via _read_optional_source_list (which never
    returns an empty list - see the parsing tests above), but
    _select_registered_adapters must still refuse to silently produce a
    zero-adapter Crawler if it were ever called with one directly."""
    monkeypatch.setattr(main_module, "CATALOG_ENABLED_SOURCES", [])
    with pytest.raises(ValueError, match="matched zero registered sources"):
        _select_registered_adapters(_all_real_adapters())


# --- Full route-level regression: default (unset) behavior is unchanged ---


def test_default_lifespan_still_registers_all_five_sources():
    """Route-level regression proof that this feature is additive: with
    CATALOG_ENABLED_SOURCES left unset (main_module's own default, matching
    every real deployment), GET /sources still lists all five adapters in
    the same order as before this feature existed."""
    with TestClient(app) as client:
        response = client.get("/sources")
        assert response.status_code == 200
        names = [entry["source"] for entry in response.json()["sources"]]
        assert names == [
            "mock_demo", "shopify_shtaim", "shopify_pipa_fashion",
            "shopify_kahiko_swimwear", "shopify_ata",
        ]


# --- Full route-level proof: setting the allowlist actually restricts the running app ---


def test_enabled_sources_restricts_the_running_app_end_to_end(monkeypatch):
    """Route-level proof (not just the helper function in isolation) that
    setting CATALOG_ENABLED_SOURCES before the app's lifespan runs actually
    results in a Crawler with only that one adapter registered - GET
    /sources and POST /crawl (with sources left unset, exactly like the
    real public web route sends it) both only ever see mock_demo."""
    monkeypatch.setattr(main_module, "CATALOG_ENABLED_SOURCES", ["mock_demo"])
    with TestClient(app) as client:
        sources_response = client.get("/sources")
        assert sources_response.status_code == 200
        names = [entry["source"] for entry in sources_response.json()["sources"]]
        assert names == ["mock_demo"]

        # sources intentionally left unset in the request body, exactly as
        # GET /recommendations/catalog-suggestions sends it in production
        # (see services/web/app/services/catalog_gateway.py) - proves the
        # real dispatch never reaches a real Shopify adapter even without
        # any per-request filter, because it was never registered at all.
        crawl_response = client.post("/crawl", json={"clothing_type": "top"})
        assert crawl_response.status_code == 200
        body = crawl_response.json()
        assert len(body["products"]) >= 1
        assert all(product["source"] == "mock_demo" for product in body["products"])
        assert len(body["source_status"]) == 1
        assert body["source_status"][0]["source"] == "mock_demo"
