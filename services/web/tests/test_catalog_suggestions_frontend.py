"""Season wired into the catalog-suggestions frontend call (catalog-
grounding-season, frontend stage).

Lightweight content assertions against the exact static file FastAPI
serves under `/ui/app.js` - same style as test_frontend_ui_redesign.py,
not a JS test run (this project has no JS test runner - see
docs/testing.md's "Known gaps"). These lock in the literal source shape a
future edit could silently regress; a genuine runtime trace of the same
logic (executed with Node, not committed) additionally confirmed the
actual behavior (URL built correctly per season, label chosen correctly
per seasonVerified) during development of this change.
"""


def _fetch_js(client) -> str:
    resp = client.get("/ui/app.js")
    assert resp.status_code == 200
    return resp.text


def test_get_recommendation_passes_the_users_own_season_to_catalog_suggestions(client):
    """The season sent to loadCatalogSuggestions must come from the same
    `season` variable read from #r-season and sent in the recommendation
    request body - never hardcoded, never derived from the returned
    items."""
    js = _fetch_js(client)
    assert "const season   = document.getElementById('r-season').value;" in js
    assert "loadCatalogSuggestions(d.missing_types, season);" in js


def test_fetch_catalog_suggestions_appends_season_param_only_when_given(client):
    js = _fetch_js(client)
    assert "async function fetchCatalogSuggestions(clothingTypes, season)" in js
    assert "const seasonParam = season ? `&season=${encodeURIComponent(season)}` : '';" in js
    assert "${seasonParam}" in js


def test_season_is_forwarded_verbatim_never_mapped_client_side(client):
    """spring/autumn must be sent exactly as selected - the spring->summer/
    autumn->winter fallback is documented as backend-owned
    (catalog_gateway.py::SEASON_MATCH_FALLBACK); the frontend must not
    duplicate that mapping in JavaScript. `encodeURIComponent(season)`
    passes the raw <select> value straight through - there is no lookup/
    replace function applied to it anywhere before it's sent, and no
    season-remapping table/object exists in this file at all."""
    js = _fetch_js(client)
    assert "encodeURIComponent(season)" in js
    assert "mapSeason" not in js
    assert "seasonFallback" not in js
    assert "SEASON_MATCH_FALLBACK" not in js  # that's a backend-only symbol


def test_no_season_selected_yields_empty_string_not_omitted_variable(client):
    """#r-season's own value when nothing is selected is '' (a real HTML
    <select> with a blank default option) - the existing `if (season) ...`
    truthy-check pattern already used for the recommendation request body
    itself is what keeps an unselected season from ever appending
    &season= to the catalog-suggestions call."""
    js = _fetch_js(client)
    assert "if (season)   body.season   = season;" in js


def test_shop_suggestions_label_uses_seasonal_wording_only_when_verified(client):
    js = _fetch_js(client)
    assert "function shopSuggestionsLabel(entry)" in js
    assert "if (entry.seasonRequested && entry.seasonVerified) {" in js
    assert "const capitalized = season.charAt(0).toUpperCase() + season.slice(1);" in js
    assert "return `${esc(capitalized)} ${esc(entry.type)}`;" in js


def test_shop_suggestions_label_marks_unverified_fallback_honestly(client):
    """A season that was requested but not verified must keep the
    existing generic 'Shop for a <type>' wording, plus a small, honest
    unverified indicator - never the seasonal wording."""
    js = _fetch_js(client)
    assert "(season not verified)" in js
    assert "return `Shop for a ${esc(entry.type)}${unverifiedHint}`;" in js


def test_catalog_suggestions_html_still_uses_shop_suggestions_label(client):
    js = _fetch_js(client)
    assert '<p class="shop-suggestions-label">${shopSuggestionsLabel(r)}</p>' in js


def test_existing_product_card_rendering_is_unchanged(client):
    """Real product fields (name/price/image/URL) must still be rendered
    directly from the trusted catalog response - this frontend change must
    not have touched that rendering path at all."""
    js = _fetch_js(client)
    assert "function renderCatalogProductCard(product)" in js
    assert "product.source_url" in js
    assert "product.product_name" in js
    assert "product.price" in js
    assert "product.image_url" in js


def test_no_products_still_renders_nothing(client):
    """Regression guard: catalogSuggestionsHtml's early-return for a
    result set with no products at all must be unchanged."""
    js = _fetch_js(client)
    assert "if (withProducts.length === 0) return '';" in js


def test_outfit_builder_catalog_call_is_unaffected(client):
    """outfit-builder.js's own call must keep requesting exactly the
    pre-existing clothing-type-only shape (a single argument, no season) -
    it has no recommendation-request season to offer and must not be
    changed by this feature."""
    resp = client.get("/ui/outfit-builder.js")
    assert resp.status_code == 200
    js = resp.text
    assert "const results = await fetchCatalogSuggestions(missingTypes);" in js
