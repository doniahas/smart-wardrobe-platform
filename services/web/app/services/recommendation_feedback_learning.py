"""Turns one recommendation-feedback event into memory
updates, reusing merge_chat_memory_candidate unchanged - see that
function's own docstring, which names this exact reuse case.

Two-tier trust model. There is no LLM here, so unlike the chat-driven
learning (services/memory_extraction.py::CHAT_MEMORY_CONFIDENCE_THRESHOLD),
there is no numeric confidence score to gate on:

  - Explicit (reason_scope names one item's color/style/clothing_type): a
    single event is enough, isolated to exactly that one attribute -
    matching chat-driven learning's own bar, where one explicit chat statement is
    already sufficient with no repetition required.
  - Implicit (reason_scope is combination or absent): never contributes
    anything by itself, no matter how many times the *same* outfit is
    disliked - "I disliked this pairing" says nothing about any single
    attribute. Instead, every new implicit event checks whether one
    attribute value has now recurred across
    RECOMMENDATION_FEEDBACK_REPETITION_THRESHOLD distinct implicit
    events - real evidence from genuinely different outfits, not one
    outfit's every attribute blamed at once (the bug this module
    exists to avoid reintroducing - see update_memory_from_feedback for
    the existing, unrelated flow that already has it).

Explicit events are never counted toward the implicit repetition tally,
including for the one attribute they themselves named: an explicit
callout on one attribute must never function as ambient implicit
evidence for the outfit's other, unrelated attributes.

occasion_season_mismatch is recorded by the caller (see routers/
recommendation_feedback.py) but never merged into memory here -
UserMemoryPublic has no "avoid this occasion" field, and adding one is
out of scope for this module.

Deliberately does not wrap merge_chat_memory_candidate's own conflict
behavior: if an attribute value is already on the opposite liked/disliked
list from a different signal source (e.g. chat), that function's existing
skip-rather-than-move behavior applies completely unchanged here too.
"""
from datetime import datetime, timezone

from app.models import ChatMemoryCandidate, ClothingType, RecommendationFeedbackReasonScope, Style
from app.services.personalization import merge_chat_memory_candidate
from app.services.recommendation_feedback import (
    count_matching_implicit_events,
    distinct_component_attribute_values,
)

RECOMMENDATION_FEEDBACK_REPETITION_THRESHOLD = 3

_EXPLICIT_ITEM_SCOPES = {
    RecommendationFeedbackReasonScope.COLOR.value,
    RecommendationFeedbackReasonScope.STYLE.value,
    RecommendationFeedbackReasonScope.CLOTHING_TYPE.value,
}
_IMPLICIT_SCOPES = {None, RecommendationFeedbackReasonScope.COMBINATION.value}


def _apply_attribute(fields: dict[str, set[str]], attr_name: str, attr_value: str, liked: bool) -> None:
    """Map one (attribute, value, liked) contribution onto the mutable
    candidate-fields accumulator, matching update_memory_from_feedback's
    existing field mapping exactly - including its asymmetry: color has
    both a liked and a disliked list, style is preferred-only, and
    clothing_type is disliked-only. A style dislike or a liked clothing
    type has no corresponding memory field and is a deliberate no-op, not
    an error."""
    if attr_name == "color":
        key = "liked_colors" if liked else "disliked_colors"
        fields.setdefault(key, set()).add(attr_value)
    elif attr_name == "style" and liked:
        fields.setdefault("preferred_styles", set()).add(attr_value)
    elif attr_name == "clothing_type" and not liked:
        fields.setdefault("disliked_item_types", set()).add(attr_value)


async def learn_memory_from_recommendation_feedback(db, user_id: str, feedback_doc: dict) -> bool:
    """Run the recommendation-feedback learning pipeline for one just-
    inserted event (the raw dict as stored in db.recommendation_feedback,
    including its now-assigned _id).

    Returns True only when the user's memory document was actually
    updated. Returns False for every expected non-update outcome (no
    attribute reached the required trust level, or the resulting
    candidate had nothing merge_chat_memory_candidate could contribute)
    without ever raising for any of them - mirroring
    chat_memory_learning.py::learn_memory_from_chat's exact contract. A
    genuinely unexpected error (e.g. a real MongoDB failure) is not
    swallowed here either, for the same reason that module gives.
    """
    liked = feedback_doc["liked"]
    components = feedback_doc["components"]
    reason_scope = feedback_doc.get("reason_scope")
    reason_index = feedback_doc.get("reason_component_index")

    candidate_fields: dict[str, set[str]] = {}

    if reason_scope in _EXPLICIT_ITEM_SCOPES:
        component = components[reason_index]
        _apply_attribute(candidate_fields, reason_scope, component[reason_scope], liked)
    elif reason_scope in _IMPLICIT_SCOPES:
        for attr_name, values in distinct_component_attribute_values(components).items():
            for value in values:
                count = await count_matching_implicit_events(db, user_id, liked, attr_name, value)
                if count >= RECOMMENDATION_FEEDBACK_REPETITION_THRESHOLD:
                    _apply_attribute(candidate_fields, attr_name, value, liked)
    # else: occasion_season_mismatch - no memory contribution, by design.

    if not candidate_fields:
        return False

    candidate = ChatMemoryCandidate(
        preferred_styles=[Style(v) for v in candidate_fields.get("preferred_styles", [])],
        liked_colors=list(candidate_fields.get("liked_colors", [])),
        disliked_colors=list(candidate_fields.get("disliked_colors", [])),
        disliked_item_types=[ClothingType(v) for v in candidate_fields.get("disliked_item_types", [])],
    )

    memory_doc = await db.memory.find_one({"user_id": user_id})
    updates = merge_chat_memory_candidate(memory_doc, candidate)
    if not updates:
        return False

    updates["updated_at"] = datetime.now(timezone.utc)
    await db.memory.update_one(
        {"user_id": user_id},
        {"$set": updates, "$setOnInsert": {"user_id": user_id}},
        upsert=True,
    )
    return True
