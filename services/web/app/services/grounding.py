"""Verification/grounding layer for recommendation results.

Recommendation candidates (item ids chosen by the recommendation service)
are never trusted directly. Before anything is returned to a client, every
candidate id must be re-verified against MongoDB: it has to exist and
belong to the authenticated user. The verified MongoDB document - not
whatever the recommendation service echoed back - is the only source of
truth for an item's attributes.

This module's only responsibility is verification and reporting the
result (grounded item vs. failure reason). It intentionally does not
decide what a caller should do with a rejected candidate - no fallback
selection, no recommendation logic - so that verification and
recommendation stay decoupled. Fallback selection is a separate concern
(services/fallback.py), built on top of this module's output.
"""
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Union

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, ConfigDict

from app.models import WardrobeItemPublic
from app.services.image_storage import sign_wardrobe_image_url

logger = logging.getLogger(__name__)

# Every field on WardrobeItemPublic below is copied straight from the
# wardrobe document matched by _id + user_id, so once that match succeeds
# all of them are verified by definition. (Unlike a hypothetical external
# product source, wardrobe grounding has no "partially verified" case.)
WARDROBE_VERIFIED_FIELDS = [
    "name", "clothing_type", "color", "season", "style", "occasion",
    "image_url", "tags",
]


class VerificationFailureReason(str, Enum):
    INVALID_ID = "invalid_id"
    NOT_FOUND = "not_found"
    NOT_OWNED = "not_owned"


class VerificationFailure(BaseModel):
    """A recommendation candidate that could not be grounded."""
    requested_id: str
    reason: VerificationFailureReason


class GroundedWardrobeItem(BaseModel):
    """A recommendation candidate re-verified against MongoDB.

    `verified_fields` is an exhaustive allowlist, not a suggestion -
    every field on `item` was copied from the verified document and
    nothing else. extra="forbid" ensures no caller can silently attach an
    extra attribute (e.g. one derived from a memory note or a preference)
    to what is supposed to be a fully-verified wardrobe fact.
    """
    model_config = ConfigDict(extra="forbid")

    item: WardrobeItemPublic
    source_type: str = "wardrobe"
    source_id: str
    verified_at: datetime
    verified_fields: list[str] = WARDROBE_VERIFIED_FIELDS


def _doc_to_public(doc: dict) -> WardrobeItemPublic:
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


def _log_failure(user_id: str, item_id: str, reason: VerificationFailureReason) -> None:
    # Never log the full document or another user's data - just enough to
    # trace that a candidate was rejected, and why.
    logger.warning(
        "recommendation grounding rejected candidate: user=%s item=%s reason=%s",
        user_id, item_id, reason.value,
    )


async def ground_wardrobe_item(
    item_id: str, user_id: str, db
) -> Union[GroundedWardrobeItem, VerificationFailure]:
    """Re-verify a single candidate item id against MongoDB for this user.

    Never raises for a malformed/missing/foreign id - callers are expected
    to treat a VerificationFailure as "this candidate is not usable" and
    decide what to do next (skip it, look for a replacement, etc.) rather
    than have the whole request fail.
    """
    try:
        oid = ObjectId(item_id)
    except (InvalidId, TypeError):
        _log_failure(user_id, str(item_id), VerificationFailureReason.INVALID_ID)
        return VerificationFailure(
            requested_id=str(item_id), reason=VerificationFailureReason.INVALID_ID
        )

    doc = await db.wardrobe.find_one({"_id": oid, "user_id": user_id})
    if doc is None:
        # Distinguish "belongs to someone else" from "doesn't exist at all"
        # for logging/testing purposes, without ever returning the other
        # user's document contents.
        foreign_doc = await db.wardrobe.find_one({"_id": oid}, {"_id": 1})
        reason = (
            VerificationFailureReason.NOT_OWNED
            if foreign_doc is not None
            else VerificationFailureReason.NOT_FOUND
        )
        _log_failure(user_id, item_id, reason)
        return VerificationFailure(requested_id=item_id, reason=reason)

    return GroundedWardrobeItem(
        item=_doc_to_public(doc),
        source_id=str(doc["_id"]),
        verified_at=datetime.now(timezone.utc),
    )


async def ground_recommended_items(
    candidate_ids: list[str], user_id: str, db
) -> tuple[list[GroundedWardrobeItem], list[VerificationFailure]]:
    """Verify a list of recommendation candidate ids for one user.

    Preserves the recommendation service's ordering among items that pass
    verification. Every rejected candidate comes back as a
    VerificationFailure instead of being silently dropped, so the caller
    (the recommendation route) can decide what to do next. This function
    only verifies - it does not pick replacements for rejected candidates.
    """
    grounded: list[GroundedWardrobeItem] = []
    failures: list[VerificationFailure] = []
    for candidate_id in candidate_ids:
        result = await ground_wardrobe_item(candidate_id, user_id, db)
        if isinstance(result, GroundedWardrobeItem):
            grounded.append(result)
        else:
            failures.append(result)
    return grounded, failures
