from app.models import MEMORY_MAX_GENERIC_PREFERENCES, ChatMemoryCandidate, GenericPreference
from app.services.personalization import (
    build_user_preferences,
    merge_chat_memory_candidate,
    update_memory_from_feedback,
)


def _item(color=None, style=None, occasion=None, season=None, clothing_type=None):
    return {
        "color": color,
        "style": style,
        "occasion": occasion,
        "season": season,
        "clothing_type": clothing_type,
    }


def test_no_outfits_gives_empty_profile():
    """No saved outfits and no feedback -> every score dict is empty, so the
    recommendation call downstream behaves exactly as without personalization."""
    profile = build_user_preferences(outfits=[], feedback_by_outfit={}, wardrobe_by_id={})
    assert profile == {
        "color_scores": {},
        "style_scores": {},
        "occasion_scores": {},
        "season_scores": {},
        "type_scores": {},
    }


def test_saved_outfit_without_feedback_is_a_mild_positive_signal():
    wardrobe_by_id = {"item1": _item(color="Red", style="casual")}
    outfits = [{"_id": "outfit1", "item_ids": ["item1"]}]

    profile = build_user_preferences(outfits, feedback_by_outfit={}, wardrobe_by_id=wardrobe_by_id)

    assert profile["color_scores"]["red"] > 0
    assert profile["style_scores"]["casual"] > 0


def test_liked_high_rating_outfit_boosts_score_more_than_just_saving():
    wardrobe_by_id = {"item1": _item(color="red")}
    outfits = [{"_id": "outfit1", "item_ids": ["item1"]}]

    saved_only = build_user_preferences(outfits, feedback_by_outfit={}, wardrobe_by_id=wardrobe_by_id)
    liked_and_rated = build_user_preferences(
        outfits,
        feedback_by_outfit={"outfit1": {"rating": 5, "liked": True}},
        wardrobe_by_id=wardrobe_by_id,
    )

    assert liked_and_rated["color_scores"]["red"] > saved_only["color_scores"]["red"]


def test_disliked_low_rating_outfit_gives_negative_score():
    wardrobe_by_id = {"item1": _item(color="red", style="formal")}
    outfits = [{"_id": "outfit1", "item_ids": ["item1"]}]

    profile = build_user_preferences(
        outfits,
        feedback_by_outfit={"outfit1": {"rating": 1, "liked": False}},
        wardrobe_by_id=wardrobe_by_id,
    )

    assert profile["color_scores"]["red"] < 0
    assert profile["style_scores"]["formal"] < 0


def test_neutral_feedback_does_not_pollute_profile():
    """Rating 3 with no liked signal nets to zero weight - it shouldn't show
    up as either a like or a dislike."""
    wardrobe_by_id = {"item1": _item(color="red")}
    outfits = [{"_id": "outfit1", "item_ids": ["item1"]}]

    profile = build_user_preferences(
        outfits,
        feedback_by_outfit={"outfit1": {"rating": 3, "liked": None}},
        wardrobe_by_id=wardrobe_by_id,
    )

    assert "red" not in profile["color_scores"]


def test_scores_are_clamped_to_a_bounded_range():
    wardrobe_by_id = {"item1": _item(color="red")}
    outfits = [{"_id": f"outfit{i}", "item_ids": ["item1"]} for i in range(10)]
    feedback_by_outfit = {f"outfit{i}": {"rating": 5, "liked": True} for i in range(10)}

    profile = build_user_preferences(outfits, feedback_by_outfit, wardrobe_by_id)

    assert profile["color_scores"]["red"] <= 3.0


def test_outfit_referencing_missing_wardrobe_item_is_ignored_safely():
    """If an item_id isn't in wardrobe_by_id (e.g. belongs to another user
    or was deleted), it must not raise and must not contribute a score."""
    outfits = [{"_id": "outfit1", "item_ids": ["item-not-owned"]}]

    profile = build_user_preferences(outfits, feedback_by_outfit={}, wardrobe_by_id={})

    assert profile["color_scores"] == {}


# --- update_memory_from_feedback (learned long-term memory) ---


def test_positive_feedback_learns_styles_occasions_and_colors():
    items = [_item(color="Red", style="casual", occasion="work")]

    updates = update_memory_from_feedback(memory=None, outfit_items=items, rating=5, liked=True)

    assert updates["preferred_styles"] == ["casual"]
    assert updates["preferred_occasions"] == ["work"]
    assert updates["liked_colors"] == ["red"]
    assert updates["disliked_colors"] == []
    assert updates["disliked_item_types"] == []


def test_high_rating_alone_counts_as_positive_signal():
    """rating >= 4 should learn preferences even with no explicit `liked` flag."""
    items = [_item(color="blue", style="sport", occasion="sport")]

    updates = update_memory_from_feedback(memory=None, outfit_items=items, rating=4, liked=None)

    assert updates["preferred_styles"] == ["sport"]
    assert updates["preferred_occasions"] == ["sport"]
    assert updates["liked_colors"] == ["blue"]


def test_negative_feedback_learns_disliked_colors_and_item_types():
    items = [_item(color="Black", clothing_type="shoes")]

    updates = update_memory_from_feedback(memory=None, outfit_items=items, rating=1, liked=False)

    assert updates["disliked_colors"] == ["black"]
    assert updates["disliked_item_types"] == ["shoes"]
    assert updates["preferred_styles"] == []
    assert updates["liked_colors"] == []


def test_low_rating_alone_counts_as_negative_signal():
    """rating <= 2 should learn dislikes even with no explicit `liked` flag."""
    items = [_item(color="green", clothing_type="accessory")]

    updates = update_memory_from_feedback(memory=None, outfit_items=items, rating=2, liked=None)

    assert updates["disliked_colors"] == ["green"]
    assert updates["disliked_item_types"] == ["accessory"]


def test_neutral_feedback_returns_no_updates():
    """A middling rating with no like/dislike flag shouldn't touch memory at all."""
    items = [_item(color="red", style="casual")]

    updates = update_memory_from_feedback(memory=None, outfit_items=items, rating=3, liked=None)

    assert updates == {}


def test_duplicates_already_in_memory_are_not_added_again():
    memory = {"preferred_styles": ["casual"], "liked_colors": ["red"]}
    items = [_item(color="Red", style="casual")]

    updates = update_memory_from_feedback(memory=memory, outfit_items=items, rating=5, liked=True)

    assert updates["preferred_styles"] == ["casual"]
    assert updates["liked_colors"] == ["red"]


def test_duplicates_within_the_same_outfit_are_not_added_twice():
    items = [_item(color="red", style="casual"), _item(color="red", style="casual")]

    updates = update_memory_from_feedback(memory=None, outfit_items=items, rating=5, liked=True)

    assert updates["preferred_styles"] == ["casual"]
    assert updates["liked_colors"] == ["red"]


def test_color_already_disliked_is_not_added_to_liked_colors():
    memory = {"disliked_colors": ["red"]}
    items = [_item(color="Red", style="casual")]

    updates = update_memory_from_feedback(memory=memory, outfit_items=items, rating=5, liked=True)

    assert updates["liked_colors"] == []
    assert updates["disliked_colors"] == ["red"]


def test_color_already_liked_is_not_added_to_disliked_colors():
    memory = {"liked_colors": ["red"]}
    items = [_item(color="Red", clothing_type="shoes")]

    updates = update_memory_from_feedback(memory=memory, outfit_items=items, rating=1, liked=False)

    assert updates["disliked_colors"] == []
    assert updates["liked_colors"] == ["red"]


def test_updates_never_include_manual_only_memory_fields():
    """preferred_fit/communication_style/notes are never touched by learning,
    so the caller's `$set` must not mention them at all."""
    items = [_item(color="red", style="casual")]

    updates = update_memory_from_feedback(memory=None, outfit_items=items, rating=5, liked=True)

    assert "preferred_fit" not in updates
    assert "communication_style" not in updates


# --- merge_chat_memory_candidate (chat-driven memory learning) ---
#
# merge_chat_memory_candidate only merges an already-approved candidate - it
# has no should_save/confidence concept of its own (see
# services/memory_extraction.py::is_chat_memory_candidate_approved for that
# gate), so every candidate passed to it here is treated as approved.


def test_empty_chat_memory_candidate_returns_no_updates():
    existing = {"liked_colors": ["blue"]}
    assert merge_chat_memory_candidate(existing, ChatMemoryCandidate()) == {}


def test_chat_memory_candidate_additively_merges_into_existing_lists():
    existing = {
        "preferred_styles": ["formal"],
        "liked_colors": ["red"],
        "disliked_colors": ["yellow"],
        "disliked_item_types": ["shoes"],
        "preferred_occasions": ["work"],
    }
    candidate = ChatMemoryCandidate(
        preferred_styles=["casual"],
        liked_colors=["blue"],
        disliked_colors=["black"],
        disliked_item_types=["dress"],
        preferred_occasions=["university"],
    )

    updates = merge_chat_memory_candidate(existing, candidate)

    assert updates["preferred_styles"] == ["formal", "casual"]
    assert updates["liked_colors"] == ["red", "blue"]
    assert updates["disliked_colors"] == ["yellow", "black"]
    assert updates["disliked_item_types"] == ["shoes", "dress"]
    assert updates["preferred_occasions"] == ["work", "university"]


def test_chat_memory_candidate_merge_deduplicates_against_existing_values():
    existing = {"liked_colors": ["blue"], "preferred_styles": ["casual"]}
    candidate = ChatMemoryCandidate(liked_colors=["blue"], preferred_styles=["casual"])

    updates = merge_chat_memory_candidate(existing, candidate)

    assert updates["liked_colors"] == ["blue"]
    assert updates["preferred_styles"] == ["casual"]


def test_chat_memory_candidate_merge_moves_liked_color_off_existing_dislike_when_reversal_allowed():
    """Preference reversal: a later 'I like black now'
    statement (after previously disliking it) must actually update memory,
    not be silently skipped forever - see merge_chat_memory_candidate's
    docstring. Only true when allow_color_preference_reversal=True (the
    conversational-memory caller's own choice - see the next test for the
    unchanged default)."""
    existing = {"disliked_colors": ["black"]}
    candidate = ChatMemoryCandidate(liked_colors=["black", "green"])

    updates = merge_chat_memory_candidate(existing, candidate, allow_color_preference_reversal=True)

    assert updates["liked_colors"] == ["black", "green"]
    assert updates["disliked_colors"] == []


def test_chat_memory_candidate_merge_default_still_skips_conflicting_color():
    """The pre-existing, still-default behavior (no reversal flag passed) -
    locked in unchanged for callers like recommendation_feedback_learning.py
    that never opt into reversal (see
    test_recommendation_feedback_learning.py::
    test_conflict_with_existing_opposite_signal_is_skipped)."""
    existing = {"disliked_colors": ["black"]}
    candidate = ChatMemoryCandidate(liked_colors=["black", "green"])

    updates = merge_chat_memory_candidate(existing, candidate)

    assert updates["liked_colors"] == ["green"]
    assert updates["disliked_colors"] == ["black"]


def test_chat_memory_candidate_merge_moves_disliked_color_off_existing_like_when_reversal_allowed():
    existing = {"liked_colors": ["red"]}
    candidate = ChatMemoryCandidate(disliked_colors=["red", "orange"])

    updates = merge_chat_memory_candidate(existing, candidate, allow_color_preference_reversal=True)

    assert updates["disliked_colors"] == ["red", "orange"]
    assert updates["liked_colors"] == []


def test_chat_memory_candidate_merge_reversal_does_not_disturb_unrelated_colors():
    existing = {"liked_colors": ["blue"], "disliked_colors": ["black", "orange"]}
    candidate = ChatMemoryCandidate(liked_colors=["black"])

    updates = merge_chat_memory_candidate(existing, candidate, allow_color_preference_reversal=True)

    assert updates["liked_colors"] == ["blue", "black"]
    # "orange" was never mentioned by this candidate - stays disliked.
    assert updates["disliked_colors"] == ["orange"]


def test_chat_memory_candidate_updates_preferred_fit_when_explicit_and_different():
    existing = {"preferred_fit": "regular"}
    candidate = ChatMemoryCandidate(preferred_fit="oversized")

    updates = merge_chat_memory_candidate(existing, candidate)

    assert updates["preferred_fit"] == "oversized"


def test_chat_memory_candidate_does_not_update_preferred_fit_when_unchanged():
    existing = {"preferred_fit": "oversized"}
    candidate = ChatMemoryCandidate(preferred_fit="oversized")

    updates = merge_chat_memory_candidate(existing, candidate)

    assert "preferred_fit" not in updates


def test_chat_memory_candidate_updates_communication_style_when_explicit_and_different():
    existing = {"communication_style": "formal"}
    candidate = ChatMemoryCandidate(communication_style="concise")

    updates = merge_chat_memory_candidate(existing, candidate)

    assert updates["communication_style"] == "concise"


def test_chat_memory_candidate_does_not_update_communication_style_when_unchanged():
    existing = {"communication_style": "concise"}
    candidate = ChatMemoryCandidate(communication_style="concise")

    updates = merge_chat_memory_candidate(existing, candidate)

    assert "communication_style" not in updates


def test_chat_memory_candidate_merge_never_touches_notes():
    existing = {"notes": "prefers neutral tones", "liked_colors": ["blue"]}
    candidate = ChatMemoryCandidate(liked_colors=["green"])

    updates = merge_chat_memory_candidate(existing, candidate)

    assert "notes" not in updates


def test_chat_memory_candidate_merge_preserves_unrelated_existing_fields():
    """Only the fields this candidate actually touches are returned, so a
    $set with this dict can never overwrite preferred_fit, communication_style,
    or notes even though they already exist on the memory doc."""
    existing = {
        "liked_colors": ["blue"],
        "notes": "some notes",
        "communication_style": "friendly",
        "preferred_fit": "relaxed",
    }
    candidate = ChatMemoryCandidate(liked_colors=["green"])

    updates = merge_chat_memory_candidate(existing, candidate)

    assert set(updates.keys()) == {
        "preferred_styles",
        "preferred_occasions",
        "liked_colors",
        "disliked_colors",
        "disliked_item_types",
    }
    assert updates["liked_colors"] == ["blue", "green"]
    assert "notes" not in updates


# --- Long-term memory follow-up completion: additional_preferences merge
# (the generic durable-preference fallback) --------------------------------


def test_new_generic_preference_topic_is_appended():
    candidate = ChatMemoryCandidate(
        additional_preferences=[GenericPreference(topic="fabric", preference="prefers soft, breathable fabrics")]
    )

    updates = merge_chat_memory_candidate(None, candidate)

    assert updates["additional_preferences"] == [{"topic": "fabric", "preference": "prefers soft, breathable fabrics"}]


def test_generic_preference_same_topic_replaces_previous_value():
    """The corrected-requirement scenario: a later, contradictory durable
    statement about the SAME generic topic replaces the old value instead
    of both being kept forever."""
    existing = {
        "additional_preferences": [
            {"topic": "clothing coverage", "preference": "prefers more covered, modest clothing"}
        ]
    }
    candidate = ChatMemoryCandidate(
        additional_preferences=[GenericPreference(topic="clothing coverage", preference="prefers more open clothing now")]
    )

    updates = merge_chat_memory_candidate(existing, candidate)

    assert updates["additional_preferences"] == [
        {"topic": "clothing coverage", "preference": "prefers more open clothing now"}
    ]


def test_generic_preference_update_does_not_remove_unrelated_topics():
    existing = {
        "additional_preferences": [
            {"topic": "clothing coverage", "preference": "prefers more covered, modest clothing"},
            {"topic": "fabric", "preference": "prefers soft, breathable fabrics"},
        ]
    }
    candidate = ChatMemoryCandidate(
        additional_preferences=[GenericPreference(topic="clothing coverage", preference="prefers more open clothing now")]
    )

    updates = merge_chat_memory_candidate(existing, candidate)

    assert updates["additional_preferences"] == [
        {"topic": "clothing coverage", "preference": "prefers more open clothing now"},
        {"topic": "fabric", "preference": "prefers soft, breathable fabrics"},
    ]


def test_unrelated_structured_field_update_does_not_touch_generic_preferences():
    existing = {
        "additional_preferences": [{"topic": "fabric", "preference": "prefers cotton"}],
    }
    candidate = ChatMemoryCandidate(liked_colors=["blue"])

    updates = merge_chat_memory_candidate(existing, candidate)

    assert "additional_preferences" not in updates
    assert updates["liked_colors"] == ["blue"]


def test_generic_preference_merge_is_a_no_op_when_unchanged():
    existing = {"additional_preferences": [{"topic": "fabric", "preference": "prefers cotton"}]}
    candidate = ChatMemoryCandidate(additional_preferences=[GenericPreference(topic="fabric", preference="prefers cotton")])

    updates = merge_chat_memory_candidate(existing, candidate)

    assert "additional_preferences" not in updates


def test_generic_preferences_are_capped_across_many_separate_merges_by_dropping_oldest():
    existing = {
        "additional_preferences": [
            {"topic": f"topic{i}", "preference": "detail"} for i in range(MEMORY_MAX_GENERIC_PREFERENCES)
        ]
    }
    candidate = ChatMemoryCandidate(
        additional_preferences=[GenericPreference(topic="brand new topic", preference="brand new detail")]
    )

    updates = merge_chat_memory_candidate(existing, candidate)

    assert len(updates["additional_preferences"]) == MEMORY_MAX_GENERIC_PREFERENCES
    # The oldest entry ("topic0") was evicted; the newest is kept.
    topics = [entry["topic"] for entry in updates["additional_preferences"]]
    assert "topic0" not in topics
    assert "brand new topic" in topics


def test_empty_candidate_with_only_additional_preferences_is_not_treated_as_empty():
    """_candidate_is_empty must account for additional_preferences too, or
    a message that only contains a generic preference would be silently
    discarded as if nothing durable was said at all."""
    candidate = ChatMemoryCandidate(
        additional_preferences=[GenericPreference(topic="fabric", preference="prefers cotton")]
    )

    updates = merge_chat_memory_candidate(None, candidate)

    assert updates != {}
    assert updates["additional_preferences"] == [{"topic": "fabric", "preference": "prefers cotton"}]


def test_list_shaped_style_and_occasion_are_merged_element_by_element():
    """Regression test: outfit_items now store style/occasion as lists (the
    current wardrobe schema) - a whole tag list must never be appended as
    one nested element into preferred_styles/preferred_occasions (the
    historical bug this function had)."""
    items = [_item(color="red", style=["casual", "elegant"], occasion=["university", "travel"])]

    updates = update_memory_from_feedback(memory=None, outfit_items=items, rating=5, liked=True)

    assert updates["preferred_styles"] == ["casual", "elegant"]
    assert updates["preferred_occasions"] == ["university", "travel"]
    assert all(isinstance(v, str) for v in updates["preferred_styles"])
    assert all(isinstance(v, str) for v in updates["preferred_occasions"])
