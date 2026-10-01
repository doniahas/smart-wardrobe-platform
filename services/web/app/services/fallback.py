"""Verified fallback replacement selection.

When one or more recommendation candidates fail grounding (grounding.py),
this module searches the authenticated user's OWN wardrobe in MongoDB for
the best verified replacement - matching the original recommendation
intent (season/occasion/style/memory preferences) as closely as possible,
never duplicating an already-selected item, and never inventing an item.

The recommendation service is not authoritative for wardrobe attributes,
including for items it recommended that failed verification. A failed
candidate has, by definition, no trustworthy MongoDB record (that is
exactly why it failed) - so this module never uses anything the
recommendation service echoed about a failed candidate (its claimed
"type" included) to steer which replacement is searched for. Every
candidate considered here comes straight from a fresh query of this
user's own wardrobe, scored only against the authenticated request's own
criteria (season/occasion/style) and the already-verified preferences
profile.

This module is only responsible for *selecting* a replacement candidate;
it is not a second verification mechanism and does not duplicate
grounding.py's job. Every candidate this module picks is still
independently confirmed through grounding.ground_wardrobe_item() before
being returned, so grounding.py remains the sole authority on what counts
as verified.
"""
import logging

from app.services.grounding import (
    GroundedWardrobeItem,
    VerificationFailure,
    ground_wardrobe_item,
)
from app.services.personalization import score_candidate_for_request

logger = logging.getLogger(__name__)


async def find_verified_replacements(
    failures: list[VerificationFailure],
    already_used_ids: set[str],
    already_used_types: set[str],
    user_id: str,
    db,
    occasion: str | None,
    season: str | None,
    style: str | None,
    preferences: dict,
) -> tuple[dict[str, GroundedWardrobeItem], list[VerificationFailure]]:
    """Attempt to replace each failed candidate with a verified substitute.

    Candidates are drawn from this user's full wardrobe (trusted MongoDB
    data), never filtered by anything the recommendation service claimed
    about the failed item. `already_used_types` prevents a replacement
    from duplicating a clothing type already present among the
    successfully grounded items, mirroring the recommendation service's
    own "at most one item per type" contract - but the type used for that
    check always comes from each real wardrobe candidate's own verified
    `clothing_type` field, never from the failed candidate.

    Returns (replacements_by_failed_id, unresolved). unresolved holds the
    original VerificationFailure entries for which no suitable verified
    replacement existed, so the caller can leave those outfit slots empty
    rather than fabricate anything.
    """
    used_ids = set(already_used_ids)
    used_types = set(already_used_types)
    replacements: dict[str, GroundedWardrobeItem] = {}
    unresolved: list[VerificationFailure] = []

    # This user's full wardrobe, fetched once - independent of anything
    # any failed candidate claims about itself.
    all_candidates = await db.wardrobe.find({"user_id": user_id}).to_list(length=None)

    for failure in failures:
        best_doc = None
        best_score = None
        for doc in all_candidates:
            candidate_id = str(doc["_id"])
            if candidate_id in used_ids:
                continue
            candidate_type = str(doc.get("clothing_type", "")).lower()
            if candidate_type in used_types:
                continue
            score = score_candidate_for_request(doc, occasion, season, style, preferences)
            if score is None:
                continue
            if best_score is None or score > best_score:
                best_score = score
                best_doc = doc

        if best_doc is None:
            unresolved.append(failure)
            continue

        candidate_id = str(best_doc["_id"])
        verified = await ground_wardrobe_item(candidate_id, user_id, db)
        if not isinstance(verified, GroundedWardrobeItem):
            # Extremely unlikely (the item would have to vanish between the
            # query above and this re-check) - treat it like any other
            # verification failure rather than trusting the query result.
            unresolved.append(failure)
            continue

        replacements[failure.requested_id] = verified
        used_ids.add(candidate_id)
        used_types.add(str(best_doc.get("clothing_type", "")).lower())

    if failures:
        logger.info(
            "recommendation fallback: user=%s failed=%d replaced=%d unresolved=%d",
            user_id, len(failures), len(replacements), len(unresolved),
        )

    return replacements, unresolved
