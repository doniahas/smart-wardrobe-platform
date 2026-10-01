import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.auth import get_current_user
from app.core.config import (
    CATALOG_SUGGESTIONS_MAX_RESULTS,
    RATE_LIMIT_CATALOG,
    RATE_LIMIT_RECOMMENDATION,
    RECOMMENDATION_SERVICE_URL,
)
from app.core.database import get_db
from app.core.limiter import limiter
from app.models import (
    CatalogSuggestionsResponse,
    ClothingType,
    OutfitRecommendation,
    RecommendationRequest,
    Season,
    WardrobeItemPublic,
    _as_list,
)
from app.services.catalog_gateway import (
    REAL_STORE_SOURCES,
    CatalogGatewayFailure,
    fetch_catalog_products,
    real_store_products_only,
    season_matches,
)
from app.services.fallback import find_verified_replacements
from app.services.grounding import ground_recommended_items
from app.services.personalization import apply_memory_preferences, build_user_preferences

router = APIRouter(prefix="/recommendations", tags=["recommendations"])
logger = logging.getLogger(__name__)


def _doc_to_clothing_item(doc: dict) -> dict:
    """Map a MongoDB wardrobe document to the rec service ClothingItem shape.

    style/occasion go through _as_list: the rec service's ClothingItem
    always expects a list (a garment may fit more than one of each), but a
    document from before that change still stores a bare scalar - _as_list
    turns that into a one-element list so it validates the same as any
    other item's, without touching the stored document itself.
    """
    return {
        "id": str(doc["_id"]),
        "type": doc["clothing_type"],
        "color": doc.get("color"),
        "season": doc.get("season"),
        "style": _as_list(doc.get("style")),
        "occasion": _as_list(doc.get("occasion")),
    }


def _safe_json_object(resp: httpx.Response) -> dict | None:
    """Parse the recommendation service's response body defensively.

    The recommendation service is our own internal service,
    but its response is still not trusted blindly - a non-JSON body or an
    unexpected top-level shape (not an object) must never crash the
    request. Returns None for either case so callers can fall back to a
    controlled response.
    """
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _extract_candidate_ids(raw_selected, user_id: str) -> list[str]:
    """Pull candidate item ids out of the recommendation service's
    (untrusted) selected_items list, skipping anything malformed instead
    of raising. Only a count of skipped entries is logged - never their
    content, which is unverified and may not even be well-formed data.
    """
    if not isinstance(raw_selected, list):
        logger.warning(
            "recommendation service returned non-list selected_items: user=%s", user_id
        )
        return []

    candidate_ids: list[str] = []
    malformed_count = 0
    for entry in raw_selected:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            candidate_ids.append(entry["id"])
        else:
            malformed_count += 1
    if malformed_count:
        logger.warning(
            "recommendation service returned malformed candidate entries: user=%s malformed=%d",
            user_id, malformed_count,
        )
    return candidate_ids


def _compute_missing_types(selected_items: list) -> list[str]:
    """Report what's missing from the *final, post-verification* outfit.

    A complete outfit is a suitable dress, or a suitable top+bottom pair -
    mirroring the recommendation service's own mutually-exclusive core
    structure (rule_based.py). Computed from `selected_items` (after
    grounding + fallback), not from the recommendation service's original
    pick, so a candidate that only failed verification after the fact
    (deleted mid-request, no verified replacement found) is correctly
    reported as missing even though the recommendation service thought it
    had a complete outfit.
    """
    present_types = {item.clothing_type for item in selected_items}
    if "dress" in present_types or ("top" in present_types and "bottom" in present_types):
        return []
    missing = []
    if "top" not in present_types:
        missing.append("top")
    if "bottom" not in present_types:
        missing.append("bottom")
    return missing


def _build_final_explanation(selected_items: list[WardrobeItemPublic]) -> str:
    """Build the only explanation ever returned to the client.

    Built exclusively from `selected_items` (final, post-grounding/fallback
    WardrobeItemPublic objects - see grounding.py/fallback.py). Never
    touches the recommendation service's own `explanation` string or any
    attribute it echoed back: that text was written before verification ran
    and can describe items that were since rejected, replaced, or deleted.

    Describes only what was selected - never what's missing. Missing
    clothing categories are reported exclusively via the `missing_types`
    field, which the client renders in its own dedicated UI element; stating
    them here too would duplicate that warning inside the explanation text.
    """
    if not selected_items:
        return "No suitable items were selected from your wardrobe."
    names = [item.name for item in selected_items]
    if len(names) == 1:
        return f"Selected {names[0]} from your wardrobe."
    return f"Selected {', '.join(names[:-1])} and {names[-1]} from your wardrobe."


@router.post("", response_model=OutfitRecommendation)
@limiter.limit(RATE_LIMIT_RECOMMENDATION)
async def get_recommendation(
    request: Request,
    body: RecommendationRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])

    wardrobe_docs = await db.wardrobe.find({"user_id": user_id}).to_list(length=None)
    if not wardrobe_docs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Your wardrobe is empty. Add items before requesting recommendations.",
        )

    wardrobe_by_id = {str(doc["_id"]): doc for doc in wardrobe_docs}

    outfit_docs = await db.outfits.find({"user_id": user_id}).to_list(length=None)
    feedback_docs = await db.feedback.find({"user_id": user_id}).to_list(length=None)
    feedback_by_outfit = {doc["outfit_id"]: doc for doc in feedback_docs}
    preferences = build_user_preferences(outfit_docs, feedback_by_outfit, wardrobe_by_id)

    memory_doc = await db.memory.find_one({"user_id": user_id})
    preferences = apply_memory_preferences(preferences, memory_doc)

    payload = {
        "occasion": body.occasion.value if body.occasion else None,
        "season": body.season.value if body.season else None,
        "style": body.style.value if body.style else None,
        "wardrobe_items": [_doc_to_clothing_item(doc) for doc in wardrobe_docs],
        "preferences": preferences,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{RECOMMENDATION_SERVICE_URL}/recommend", json=payload)
    except httpx.RequestError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recommendation service is unavailable.",
        )

    resp_body = _safe_json_object(resp)

    if resp.status_code == 400:
        detail = (resp_body or {}).get("detail", "Bad request to recommendation service.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    if resp.status_code == 404:
        detail = (resp_body or {}).get("detail", "No suitable items found for the given filters.")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    if resp.status_code != 200:
        # Diagnostic: this branch previously logged nothing, so a real
        # occurrence left no server-side trace of what actually went wrong.
        # Status code + internal user_id only - never the response body
        # (could echo back request data) and never wardrobe/prompt/token
        # content, matching this codebase's existing logging-privacy
        # convention (see services/web/tests/test_logging_privacy.py).
        logger.warning(
            "recommendation service returned unexpected status: user=%s status=%d",
            user_id, resp.status_code,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail="Recommendation service returned an unexpected error.")

    if resp_body is None:
        logger.warning(
            "recommendation service returned a non-JSON/malformed response body: user=%s", user_id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Recommendation service returned an unexpected response.",
        )

    rec = resp_body

    # Only the id is taken from the recommendation service's response - any
    # other attributes it echoed back (type/color/season/...) are discarded
    # and never trusted as fact. Every id is re-verified against MongoDB
    # (existence + ownership) at response time via the grounding layer,
    # rather than relying on the wardrobe_by_id snapshot fetched before the
    # recommend call.
    candidate_ids = _extract_candidate_ids(rec.get("selected_items", []), user_id)
    grounded, failures = await ground_recommended_items(candidate_ids, user_id, db)

    logger.info(
        "recommendation grounding: user=%s candidates=%d grounded=%d failed=%d",
        user_id, len(candidate_ids), len(grounded), len(failures),
    )

    replacements_by_failed_id: dict = {}
    if failures:
        # The recommendation service is not authoritative for
        # wardrobe attributes, including for failed candidates - nothing it
        # echoed about them (e.g. a claimed "type") is used here. Fallback
        # searches this user's full wardrobe against the verified request
        # criteria and preferences only.
        used_ids = {g.source_id for g in grounded}
        used_types = {str(g.item.clothing_type).lower() for g in grounded}
        replacements_by_failed_id, _unresolved = await find_verified_replacements(
            failures,
            used_ids,
            used_types,
            user_id,
            db,
            payload["occasion"],
            payload["season"],
            payload["style"],
            preferences,
        )

    # Rebuild the final list in the recommendation service's original
    # candidate order: a grounded candidate keeps its place, a failed
    # candidate is swapped for its verified replacement if one was found,
    # or omitted entirely if not - never fabricated. Internal verification
    # failures are never surfaced to the client; a partial list is a
    # controlled, valid response.
    grounded_by_id = {g.source_id: g for g in grounded}
    selected_items = []
    for candidate_id in candidate_ids:
        if candidate_id in grounded_by_id:
            selected_items.append(grounded_by_id[candidate_id].item)
        elif candidate_id in replacements_by_failed_id:
            selected_items.append(replacements_by_failed_id[candidate_id].item)

    missing_types = _compute_missing_types(selected_items)

    return OutfitRecommendation(
        items=selected_items,
        explanation=_build_final_explanation(selected_items),
        missing_types=missing_types,
    )


# --- Catalog (crawler) product suggestions ---
#
# A deliberately separate, additive endpoint rather than a new field on
# POST /recommendations' response - see the CatalogSuggestionsResponse
# docstring in app/models.py for why. The typical client flow: call
# POST /recommendations first; if its missing_types is non-empty, call this
# endpoint once per missing clothing_type to offer real store products for
# what the wardrobe doesn't have. Nothing here reads or writes wardrobe/
# outfit/feedback/memory data, and a catalog failure of any kind (service
# down, timeout, malformed response) degrades to a 200 with
# catalog_available=False - it never raises and never affects any other
# endpoint's behavior.
#
# Real-store scoping: this is a real,
# frontend-wired, authenticated user path that presents products as real,
# purchasable "shop for a missing item" links (services/web/app/static/
# app.js::renderCatalogProductCard) - it must never be able to surface the
# catalog service's mock_demo fixture data (fabricated example.com URLs)
# to a real user. Two independent layers enforce this, mirroring
# chat_catalog_grounding.py's existing, already-correct pattern for the
# same problem: (1) the request to fetch_catalog_products is scoped to
# REAL_STORE_SOURCES only, so the catalog service never even runs the
# mock_demo adapter for this call; (2) real_store_products_only() filters
# the response defensively, so a misconfigured catalog deployment or a
# future regression still can't let a non-real-store product through.
# mock_demo itself is untouched and stays fully available - it's still the
# catalog service's default/first-registered source and still exactly
# what disposable test/E2E environments intentionally restrict
# CATALOG_ENABLED_SOURCES to (see services/catalog/app/core/config.py) -
# only this real-user-facing route now refuses to ask for or accept it.


@router.get("/catalog-suggestions", response_model=CatalogSuggestionsResponse)
@limiter.limit(RATE_LIMIT_CATALOG)
async def get_catalog_suggestions(
    request: Request,
    clothing_type: ClothingType,
    season: Season | None = Query(default=None),
    max_results: int = Query(default=5, ge=1, le=CATALOG_SUGGESTIONS_MAX_RESULTS),
    current_user: dict = Depends(get_current_user),
):
    user_id = str(current_user["_id"])

    result = await fetch_catalog_products(
        clothing_type.value, max_results, sources=list(REAL_STORE_SOURCES)
    )

    if isinstance(result, CatalogGatewayFailure):
        logger.info(
            "catalog suggestions unavailable: user=%s clothing_type=%s reason=%s",
            user_id, clothing_type.value, result.reason.value,
        )
        return CatalogSuggestionsResponse(
            clothing_type=clothing_type, products=[], catalog_available=False,
            season_requested=season,
        )

    products = real_store_products_only(result.products)

    # Issue 2 (catalog-grounding-season): clothing_type is already verified
    # (fetch_catalog_products/the crawler only ever returns matching-type
    # products). When a season was also requested, prefer the subset with
    # real, trusted season evidence (season_matches - direct match, or the
    # documented spring->summer/autumn->winter fallback). If NONE of the
    # type-matching products have verified season evidence, fall back to
    # the full, unfiltered clothing-type-only list rather than returning
    # nothing - but season_verified stays False for that fallback, so the
    # caller can never mistake a type-only result for a confirmed seasonal
    # match. Products are never mixed: either every returned product is a
    # verified seasonal match, or none of them are.
    season_verified = False
    if season is not None:
        seasonal_matches = [p for p in products if season_matches(p.season, season)]
        if seasonal_matches:
            products = seasonal_matches
            season_verified = True
        # else: no verified seasonal match exists - fall back to the full
        # clothing-type-only `products` list, unverified for season.

    return CatalogSuggestionsResponse(
        clothing_type=clothing_type,
        products=products,
        season_requested=season,
        season_verified=season_verified,
        catalog_available=True,
    )
