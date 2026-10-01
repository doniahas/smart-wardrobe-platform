"""Gateway to the internal `catalog` service.

Mirrors services/llm_gateway.py's established idiom for calling an internal
service defensively: classify every failure into a stable, loggable reason,
parse the response body defensively, and never raise for an expected
failure (connection error, timeout, non-200, or a malformed/unexpected
body) - the caller always gets back either validated products or a
CatalogGatewayFailure, never an exception.

Unlike the *required* call to the `recommendation` service in
routers/recommendations.py (whose failure fails the whole request with a
503), a catalog failure must always degrade gracefully: the recommendation
flow this augments (wardrobe grounding + fallback + missing_types) must stay
100% functional whether or not `catalog` is reachable. See
routers/recommendations.py::get_catalog_suggestions for how this is wired.

Each candidate product from the catalog service is independently validated
through CatalogProductPublic - the same "verify or omit, never invent"
contract used for recommendations (see services/grounding.py) applies here too,
just against the catalog service's own already-normalized/validated output
rather than against MongoDB: a single malformed product is dropped without
affecting the rest of the response or raising.
"""
import logging
from enum import Enum

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import CATALOG_ENABLED, CATALOG_SERVICE_TIMEOUT_SECONDS, CATALOG_SERVICE_URL
from app.models import CatalogProductPublic, CatalogSeason, Season

logger = logging.getLogger(__name__)

# The single source of truth for the four allowlisted real Shopify
# stores' catalog `source_name`s - mirrors
# services/catalog/app/services/adapters/shopify_stores.py::
# SHOPIFY_STORES. Lives here (not in chat_catalog_grounding.py,
# which historically defined it) because every real-user-facing caller
# that talks to the catalog service - both chat grounding
# (chat_catalog_grounding.py) and recommendation catalog suggestions
# (routers/recommendations.py) - already imports this module, so this is
# the one place both can share without either importing the other.
# `catalog` is a separate Docker service (see that module's own
# product.py docstring) so this stays a plain duplicated string tuple
# rather than a cross-service import - the same established convention
# ClothingType already uses on the web side.
REAL_STORE_SOURCES: tuple[str, ...] = (
    "shopify_shtaim",
    "shopify_pipa_fashion",
    "shopify_kahiko_swimwear",
    "shopify_ata",
)


class CatalogGatewayFailureReason(str, Enum):
    """Stable, machine-readable failure classification - safe to log,
    unlike a raw exception message."""
    CATALOG_DISABLED = "catalog_disabled"
    CATALOG_UNAVAILABLE = "catalog_unavailable"
    CATALOG_INVALID_RESPONSE = "catalog_invalid_response"


class CatalogGatewayFailure(BaseModel):
    reason: CatalogGatewayFailureReason
    detail: str


class CatalogCrawlResult(BaseModel):
    products: list[CatalogProductPublic]


def _safe_json_object(resp: httpx.Response) -> dict | None:
    """Parse the catalog service's response body defensively - it's our own
    internal service, but its response is still not trusted blindly."""
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# The catalog service's NormalizedProduct (services/catalog/app/models/
# product.py) legitimately carries fields CatalogProductPublic doesn't
# expose to the client (source_product_id, fetched_at). CatalogProductPublic
# uses extra="forbid" so nothing *unexpected* can attach to what's shown to
# the client - so a real catalog product must be filtered down to exactly
# this allowlist before construction, rather than passed through as-is;
# passing the raw dict straight through would make every real product fail
# validation (and be silently dropped) purely because of fields we simply
# chose not to expose, not because anything was actually wrong with them.
_ALLOWED_PRODUCT_FIELDS = {
    "source", "source_url", "product_name", "clothing_type", "color",
    "material", "price", "currency", "image_url", "availability", "season",
}


def _validate_products(raw_products) -> list[CatalogProductPublic]:
    """Validate each candidate product independently. A single malformed
    entry (unexpected type, missing field, out-of-range value) is skipped
    rather than discarding the whole response or raising - only a count of
    skipped entries is logged, never their content."""
    if not isinstance(raw_products, list):
        logger.warning("catalog service returned non-list products")
        return []

    validated: list[CatalogProductPublic] = []
    skipped = 0
    for entry in raw_products:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        filtered = {key: entry[key] for key in _ALLOWED_PRODUCT_FIELDS if key in entry}
        try:
            validated.append(CatalogProductPublic(**filtered))
        except ValidationError:
            skipped += 1
    if skipped:
        logger.warning(
            "catalog service returned malformed product entries: skipped=%d", skipped
        )
    return validated


def real_store_products_only(
    products: list[CatalogProductPublic],
) -> list[CatalogProductPublic]:
    """Defense-in-depth filter for any real-user-facing path: drop a
    product whose `source` isn't one of the four allowlisted real
    stores (REAL_STORE_SOURCES), e.g. `mock_demo`.

    Callers that only ever want real-store products should already be
    requesting `sources=list(REAL_STORE_SOURCES)` from
    fetch_catalog_products (never desired for a real user answer) - this
    is a second, independent layer on the *response* side, so a
    misconfigured caller, a future regression, or an unexpected catalog
    response still can never let a fabricated mock/demo product reach a
    real user, mirroring this module's existing "verify or omit, never
    invent" discipline (see _validate_products above).
    """
    return [product for product in products if product.source in REAL_STORE_SOURCES]


# Issue 2 (catalog-grounding-season): a requested season this project has no
# dedicated trusted collection concept for maps onto the documented,
# deliberate equivalent below - never inferred per-store, and never for
# winter/summer themselves (which have no fallback: a winter request is
# never satisfied by a summer-classified product). Keep this mapping
# explicit and small - it is a project-level business rule, not a general
# "climate adjacency" heuristic.
SEASON_MATCH_FALLBACK: dict[Season, CatalogSeason] = {
    Season.SPRING: CatalogSeason.SUMMER,
    Season.AUTUMN: CatalogSeason.WINTER,
}


def season_matches(product_season: CatalogSeason, requested_season: Season) -> bool:
    """Whether a catalog product's trusted season classification actually
    satisfies a requested season - the single source of truth for "season-
    verified" used by routers/recommendations.py.

    CatalogSeason.ALL (an explicit "all-season" collection, were one ever
    configured) satisfies any requested season. CatalogSeason.UNKNOWN never
    satisfies anything - the whole point of this function is that a
    product with no trusted season evidence must never be presented as a
    verified seasonal match. A direct season match always satisfies its own
    request. Beyond that, only the documented SEASON_MATCH_FALLBACK
    equivalences apply (spring accepts a summer-classified product, autumn
    accepts a winter-classified one) - winter and summer requests have no
    fallback and only ever accept an exact (or ALL) match.
    """
    if product_season == CatalogSeason.ALL:
        return True
    if product_season.value == requested_season.value:
        return True
    return SEASON_MATCH_FALLBACK.get(requested_season) == product_season


async def fetch_catalog_products(
    clothing_type: str, max_results: int, sources: list[str] | None = None
) -> CatalogCrawlResult | CatalogGatewayFailure:
    """Call the catalog service's POST /crawl for one clothing_type.

    `sources` is forwarded as-is to the catalog service's own
    CrawlRequest.sources (services/catalog/app/models/product.py), which
    already supports filtering to a subset of registered adapters by name -
    None (the default) omits the field entirely, preserving the exact
    request body existing callers (routers/recommendations.py) have always
    sent. Added for chat grounding (services/chat_catalog_grounding.py)
    so a chat message naming a specific real store can be
    verified against that store only, and so chat grounding can exclude the
    mock_demo source (never desired for a real product-verification answer)
    by passing the four real store names explicitly instead of leaving
    sources unset.

    Returns validated products, or a CatalogGatewayFailure describing what
    went wrong. Never raises for an expected failure mode.
    """
    if not CATALOG_ENABLED:
        return CatalogGatewayFailure(
            reason=CatalogGatewayFailureReason.CATALOG_DISABLED,
            detail="Catalog integration is disabled.",
        )

    request_body = {"clothing_type": clothing_type, "max_results": max_results}
    if sources is not None:
        request_body["sources"] = sources

    try:
        async with httpx.AsyncClient(timeout=CATALOG_SERVICE_TIMEOUT_SECONDS) as client:
            resp = await client.post(f"{CATALOG_SERVICE_URL}/crawl", json=request_body)
    except httpx.RequestError:
        return CatalogGatewayFailure(
            reason=CatalogGatewayFailureReason.CATALOG_UNAVAILABLE,
            detail="Catalog service is unavailable.",
        )

    if resp.status_code != 200:
        logger.warning("catalog service returned an unexpected status: %d", resp.status_code)
        return CatalogGatewayFailure(
            reason=CatalogGatewayFailureReason.CATALOG_INVALID_RESPONSE,
            detail="Catalog service returned an unexpected error.",
        )

    resp_body = _safe_json_object(resp)
    if resp_body is None or "products" not in resp_body:
        logger.warning("catalog service returned a non-JSON/malformed response body")
        return CatalogGatewayFailure(
            reason=CatalogGatewayFailureReason.CATALOG_INVALID_RESPONSE,
            detail="Catalog service returned an unexpected response.",
        )

    return CatalogCrawlResult(products=_validate_products(resp_body.get("products")))
