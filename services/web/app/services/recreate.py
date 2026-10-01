"""Recreate with My Wardrobe.

Matches a post's sanitized SharedOutfitSnapshot (see models.py) against the
*requesting user's own* wardrobe - never the original author's. This module
never queries another user's wardrobe or outfits collection; it only reads
the snapshot already embedded on the post (sanitized once, at post-creation
time - see routers/posts.py) and the caller's own db.wardrobe documents,
scoped by user_id exactly like every other wardrobe query in the app. This
is what makes the same code path work identically whether the caller is the
post's original author or any other user.

Matching compares only the four structured attributes color/season/style/
occasion; clothing_type is a hard filter, not a scored attribute, since a
slot can only ever be filled by an item of the exact type it calls for.
This is intentionally not services/personalization.py's
score_candidate_for_request - that scorer encodes user *preference*
exclusions (disliked colors/types), answering "would this user want to wear
this" rather than "how closely does this match a specific already-chosen
garment". Recreate needs the latter: a plain, fully explainable similarity
score with no hidden exclusions.
"""
from enum import Enum

from app.models import RecreateMatchResult, SharedOutfitComponentSnapshot, SharedOutfitSnapshot, WardrobeItemPublic
from app.services.image_storage import sign_wardrobe_image_url

_SCORED_ATTRIBUTES = ("color", "season", "style", "occasion")


def _wardrobe_doc_to_public(doc: dict) -> WardrobeItemPublic:
    return WardrobeItemPublic(
        id=str(doc["_id"]),
        user_id=doc["user_id"],
        name=doc["name"],
        clothing_type=doc["clothing_type"],
        color=doc["color"],
        season=doc["season"],
        style=doc["style"],
        occasion=doc["occasion"],
        image_url=sign_wardrobe_image_url(doc.get("image_url")),
        tags=doc.get("tags", []),
    )


def _attr_value(value) -> str:
    """TEMPORARY COMPATIBILITY: `value` can now be a list (a wardrobe
    item's own style/occasion, read straight from its Mongo document -
    see doc.get(attr, "") below), while `component`'s corresponding
    attribute (a SharedOutfitComponentSnapshot field) stays scalar. Only
    the first element is compared, matching the same first-value stopgap
    already applied where the snapshot itself is built (see
    models.py::_first_or_scalar) - Recreate's matching is being
    simplified separately, so this is not new multi-value matching, just
    what keeps a str(list) from silently comparing unequal to everything."""
    if isinstance(value, list):
        value = value[0] if value else ""
    return (value.value if isinstance(value, Enum) else str(value)).lower()


def _score_candidate(doc: dict, component: SharedOutfitComponentSnapshot) -> tuple[float, list[str]]:
    """Fraction of scored attributes that match, plus which ones matched
    (for the explanation). A stored "all" season value matches any target
    season and vice versa - the same wildcard convention used everywhere
    else this enum appears."""
    matched: list[str] = []
    for attr in _SCORED_ATTRIBUTES:
        doc_value = _attr_value(doc.get(attr, ""))
        target_value = _attr_value(getattr(component, attr))
        if doc_value == target_value or doc_value == "all" or target_value == "all":
            matched.append(attr)
    return len(matched) / len(_SCORED_ATTRIBUTES), matched


def _build_explanation(matched: list[str]) -> str:
    if not matched:
        return "Same clothing type only - color, season, style, and occasion all differ."
    if len(matched) == len(_SCORED_ATTRIBUTES):
        return "Exact match on clothing type, color, season, style, and occasion."
    differing = [a for a in _SCORED_ATTRIBUTES if a not in matched]
    return f"Same clothing type and {', '.join(matched)} - {', '.join(differing)} differ."


async def match_snapshot_to_wardrobe(
    snapshot: SharedOutfitSnapshot, user_id: str, db
) -> list[RecreateMatchResult]:
    """Match each snapshot component against this user's own wardrobe.

    Fetches the caller's full wardrobe once (mirrors
    services/fallback.py::find_verified_replacements), sorted by _id for a
    deterministic, stable candidate order: when two candidates tie on
    score, the rule is "highest score, then first in this stable order" -
    reproducible across repeated calls regardless of MongoDB's internal
    storage order. Never reuses the same wardrobe item across two different
    components in one response.
    """
    all_docs = await db.wardrobe.find({"user_id": user_id}).sort("_id", 1).to_list(length=None)

    used_ids: set[str] = set()
    results: list[RecreateMatchResult] = []

    for component in snapshot.components:
        target_type = _attr_value(component.clothing_type)
        candidates = [
            doc for doc in all_docs
            if str(doc["_id"]) not in used_ids and _attr_value(doc.get("clothing_type", "")) == target_type
        ]

        best_doc = None
        best_score = -1.0
        best_matched: list[str] = []
        for doc in candidates:
            score, matched = _score_candidate(doc, component)
            if score > best_score:
                best_score = score
                best_doc = doc
                best_matched = matched

        if best_doc is None:
            results.append(RecreateMatchResult(
                component=component,
                matched_item=None,
                match_score=None,
                match_explanation=f"No {component.clothing_type.value} in your wardrobe.",
            ))
            continue

        used_ids.add(str(best_doc["_id"]))
        results.append(RecreateMatchResult(
            component=component,
            matched_item=_wardrobe_doc_to_public(best_doc),
            match_score=best_score,
            match_explanation=_build_explanation(best_matched),
        ))

    return results
