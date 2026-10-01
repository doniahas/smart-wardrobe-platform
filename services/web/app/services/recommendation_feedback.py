"""Grounding and frozen-snapshot construction for
recommendation-level feedback, plus the repetition-counting query used by
services/recommendation_feedback_learning.py.

Mirrors routers/posts.py::_build_shared_outfit_snapshot's reasoning: what
gets stored is a one-time, immutable, sanitized copy of each item's
categorical attributes - never a live wardrobe item id - so later
analysis can never depend on an item that has since been edited or
deleted.
"""
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException, status

from app.models import SharedOutfitComponentSnapshot, _first_or_scalar

COUNTABLE_ATTRIBUTES = ("color", "style", "clothing_type")


async def build_recommendation_feedback_components(
    item_ids: list[str], user_id: str, db
) -> list[SharedOutfitComponentSnapshot]:
    """Verify ownership of every item id and freeze each into a categorical
    snapshot, in the same order as item_ids."""
    try:
        item_oids = [ObjectId(item_id) for item_id in item_ids]
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid item id")

    docs = await db.wardrobe.find({"_id": {"$in": item_oids}, "user_id": user_id}).to_list(length=None)
    if len(docs) != len(item_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more items do not exist or do not belong to you",
        )
    docs_by_id = {str(doc["_id"]): doc for doc in docs}

    # style/occasion: _first_or_scalar is a temporary compatibility shim -
    # see its docstring in models.py. This snapshot type stays scalar
    # while recommendation-feedback's own scoped feature is unaffected;
    # only the source item's style/occasion may now be a list.
    return [
        SharedOutfitComponentSnapshot(
            clothing_type=docs_by_id[item_id]["clothing_type"],
            color=docs_by_id[item_id]["color"],
            season=docs_by_id[item_id]["season"],
            style=_first_or_scalar(docs_by_id[item_id]["style"]),
            occasion=_first_or_scalar(docs_by_id[item_id]["occasion"]),
        )
        for item_id in item_ids
    ]


def _outfit_fingerprint(components: list[dict]) -> tuple:
    """A stable, order-independent identity for one event's full set of
    components - used to tell genuinely different outfits apart from the
    same outfit disliked/liked more than once."""
    return tuple(sorted(
        (c["clothing_type"], c["color"], c["season"], c["style"], c["occasion"])
        for c in components
    ))


async def count_matching_implicit_events(
    db, user_id: str, liked: bool, attr_name: str, attr_value: str
) -> int:
    """Count distinct *outfits* (not raw event documents) among this
    user's implicit (bare or combination-scope) recommendation-feedback
    events with this liked value, where at least one frozen component has
    {attr_name: attr_value}.

    Deliberately counts distinct outfits rather than events: disliking
    the exact same recommendation three times is one data point repeated,
    not three independent pieces of corroborating evidence, and must not
    be treated the same as three genuinely different outfits that happen
    to share one attribute. Explicit-reasoned events are excluded from
    this pool entirely; see recommendation_feedback_learning.py for why.
    """
    cursor = db.recommendation_feedback.find(
        {
            "user_id": user_id,
            "liked": liked,
            "reason_scope": {"$in": [None, "combination"]},
            f"components.{attr_name}": attr_value,
        },
        {"components": 1},
    )
    fingerprints = {_outfit_fingerprint(doc["components"]) async for doc in cursor}
    return len(fingerprints)


def distinct_component_attribute_values(components: list[dict]) -> dict[str, set[str]]:
    """Every distinct value present across a set of frozen components, for
    each of the three attributes implicit promotion can act on. Takes
    plain dicts (as stored in / read from db.recommendation_feedback),
    not model instances."""
    result: dict[str, set[str]] = {attr: set() for attr in COUNTABLE_ATTRIBUTES}
    for component in components:
        for attr_name in COUNTABLE_ATTRIBUTES:
            result[attr_name].add(component[attr_name])
    return result
