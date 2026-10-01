import pytest

from app.services.fallback import find_verified_replacements
from app.services.grounding import GroundedWardrobeItem, VerificationFailure, VerificationFailureReason
from app.services.personalization import score_candidate_for_request

_EMPTY_PREFS = {
    "disliked_colors": [], "disliked_item_types": [],
    "preferred_styles": [], "preferred_occasions": [],
    "color_scores": {}, "style_scores": {}, "occasion_scores": {},
    "season_scores": {}, "type_scores": {},
}


# --- score_candidate_for_request (pure function) ---


def test_score_candidate_hard_excludes_disliked_color():
    doc = {"color": "black", "clothing_type": "top", "season": "all", "style": "casual", "occasion": "casual"}
    prefs = {**_EMPTY_PREFS, "disliked_colors": ["black"]}
    assert score_candidate_for_request(doc, None, None, None, prefs) is None


def test_score_candidate_hard_excludes_disliked_type():
    doc = {"color": "red", "clothing_type": "shoes", "season": "all", "style": "casual", "occasion": "casual"}
    prefs = {**_EMPTY_PREFS, "disliked_item_types": ["shoes"]}
    assert score_candidate_for_request(doc, None, None, None, prefs) is None


def test_score_candidate_rejects_when_no_requested_criterion_matches():
    doc = {"color": "red", "clothing_type": "top", "season": "summer", "style": "casual", "occasion": "casual"}
    assert score_candidate_for_request(doc, "work", "winter", "formal", _EMPTY_PREFS) is None


def test_score_candidate_scores_matching_criteria_positively():
    doc = {"color": "red", "clothing_type": "top", "season": "winter", "style": "formal", "occasion": "work"}
    score = score_candidate_for_request(doc, "work", "winter", "formal", _EMPTY_PREFS)
    assert score is not None and score > 0


def test_score_candidate_no_requested_criteria_still_scored_when_no_hard_exclusion():
    doc = {"color": "red", "clothing_type": "top", "season": "all", "style": "casual", "occasion": "casual"}
    score = score_candidate_for_request(doc, None, None, None, _EMPTY_PREFS)
    assert score == 0.0


# --- Hard vs. soft constraint separation regression tests ---
# Mirrors services/recommendation/tests/test_rule_based.py's equivalent
# section: score_candidate_for_request must apply the same hard-gate
# semantics as the recommendation service's own scorer, since this is the
# rule fallback candidate selection is required to mirror.


def test_score_candidate_soft_preference_cannot_rescue_item_matching_no_requested_field():
    """The core bug this fix addresses: a huge learned/declared soft
    preference bonus must never let through a candidate that matches NONE
    of the caller's explicitly requested fields."""
    doc = {"color": "brown", "clothing_type": "shoes", "season": "summer", "style": "casual", "occasion": "beach"}
    prefs = {
        **_EMPTY_PREFS,
        "preferred_styles": ["casual"],
        "style_scores": {"casual": 100.0},
    }
    assert score_candidate_for_request(doc, "work", "winter", "formal", prefs) is None


def test_score_candidate_matching_only_one_of_several_requested_fields_is_rejected():
    """Satisfying occasion alone is not enough when season and style were
    also explicitly requested - every requested field is independently
    hard-required."""
    doc = {"color": "white", "clothing_type": "top", "season": "summer", "style": "casual", "occasion": "work"}
    assert score_candidate_for_request(doc, "work", "winter", "formal", _EMPTY_PREFS) is None


def test_score_candidate_soft_preference_ranks_valid_candidates_without_rescuing_invalid_one():
    """Among candidates that already pass the hard requested-occasion
    filter, a soft color-score bonus correctly ranks them; a candidate
    that fails the hard filter is excluded regardless of an even larger
    soft bonus."""
    valid_low = {"color": "blue", "clothing_type": "top", "season": "all", "style": "casual", "occasion": "work"}
    valid_high = {"color": "red", "clothing_type": "top", "season": "all", "style": "casual", "occasion": "work"}
    invalid = {"color": "green", "clothing_type": "top", "season": "all", "style": "casual", "occasion": "sport"}
    prefs = {**_EMPTY_PREFS, "color_scores": {"red": 2.0, "green": 100.0}}

    assert score_candidate_for_request(invalid, "work", None, None, prefs) is None
    low_score = score_candidate_for_request(valid_low, "work", None, None, prefs)
    high_score = score_candidate_for_request(valid_high, "work", None, None, prefs)
    assert low_score is not None and high_score is not None
    assert high_score > low_score


def test_score_candidate_all_season_still_satisfies_a_requested_season():
    doc = {"color": "white", "clothing_type": "top", "season": "all", "style": None, "occasion": None}
    assert score_candidate_for_request(doc, None, "winter", None, _EMPTY_PREFS) is not None


def test_score_candidate_missing_season_does_not_satisfy_a_requested_season():
    doc = {"color": "white", "clothing_type": "top", "season": None, "style": None, "occasion": None}
    assert score_candidate_for_request(doc, None, "winter", None, _EMPTY_PREFS) is None


# --- find_verified_replacements ---
# Note: find_verified_replacements takes no information about a failed
# candidate other than its VerificationFailure (requested_id + reason) -
# it never receives or uses anything the recommendation service echoed
# about the failed item, since that data is untrusted by construction.


@pytest.mark.asyncio
async def test_successful_fallback_replaces_failed_candidate(db, make_user, make_wardrobe_item):
    user = await make_user("alice")
    replacement_id = await make_wardrobe_item(
        user["user_id"], name="Backup Top", clothing_type="top", color="red",
        season="winter", occasion="work", style="formal",
    )

    failure = VerificationFailure(requested_id="deadbeefdeadbeefdeadbeef", reason=VerificationFailureReason.NOT_FOUND)

    replacements, unresolved = await find_verified_replacements(
        [failure], set(), set(), user["user_id"], db,
        "work", "winter", "formal", _EMPTY_PREFS,
    )

    assert unresolved == []
    assert "deadbeefdeadbeefdeadbeef" in replacements
    replaced = replacements["deadbeefdeadbeefdeadbeef"]
    assert isinstance(replaced, GroundedWardrobeItem)
    assert replaced.source_id == replacement_id
    assert replaced.item.name == "Backup Top"


@pytest.mark.asyncio
async def test_multiple_failed_candidates_each_get_a_distinct_replacement(db, make_user, make_wardrobe_item):
    user = await make_user("bob")
    top_id = await make_wardrobe_item(user["user_id"], name="Top", clothing_type="top")
    bottom_id = await make_wardrobe_item(user["user_id"], name="Bottom", clothing_type="bottom")

    failures = [
        VerificationFailure(requested_id="aaaaaaaaaaaaaaaaaaaaaaaa", reason=VerificationFailureReason.NOT_FOUND),
        VerificationFailure(requested_id="bbbbbbbbbbbbbbbbbbbbbbbb", reason=VerificationFailureReason.NOT_FOUND),
    ]

    replacements, unresolved = await find_verified_replacements(
        failures, set(), set(), user["user_id"], db, None, None, None, _EMPTY_PREFS,
    )

    assert unresolved == []
    assert len(replacements) == 2
    # No type hint exists to say which failure "wanted" which type - both
    # real wardrobe items are used exactly once between the two failures.
    assert {r.source_id for r in replacements.values()} == {top_id, bottom_id}


@pytest.mark.asyncio
async def test_no_available_replacement_is_unresolved_not_fabricated(db, make_user):
    user = await make_user("carol")
    # No wardrobe items at all for this user.
    failure = VerificationFailure(requested_id="cccccccccccccccccccccccc", reason=VerificationFailureReason.NOT_FOUND)

    replacements, unresolved = await find_verified_replacements(
        [failure], set(), set(), user["user_id"], db, None, None, None, _EMPTY_PREFS,
    )

    assert replacements == {}
    assert unresolved == [failure]


@pytest.mark.asyncio
async def test_fallback_never_reuses_an_already_selected_item(db, make_user, make_wardrobe_item):
    user = await make_user("dave")
    already_grounded_id = await make_wardrobe_item(user["user_id"], name="Already Used", clothing_type="top")

    failure = VerificationFailure(requested_id="dddddddddddddddddddddddd", reason=VerificationFailureReason.NOT_FOUND)

    # already_used_ids marks the one existing item as taken, so there is
    # nothing left to offer as a replacement.
    replacements, unresolved = await find_verified_replacements(
        [failure], {already_grounded_id}, set(),
        user["user_id"], db, None, None, None, _EMPTY_PREFS,
    )

    assert replacements == {}
    assert unresolved == [failure]


@pytest.mark.asyncio
async def test_fallback_does_not_duplicate_an_already_occupied_clothing_type(db, make_user, make_wardrobe_item):
    """The one-item-per-type outfit contract (enforced by the
    recommendation service's own build_recommendation) is preserved during
    fallback using each candidate's own verified clothing_type - never a
    hint from the failed item."""
    user = await make_user("erin")
    await make_wardrobe_item(user["user_id"], name="Spare Top", clothing_type="top")

    failure = VerificationFailure(requested_id="eeeeeeeeeeeeeeeeeeeeeeee", reason=VerificationFailureReason.NOT_FOUND)

    # "top" is already marked as an occupied slot (e.g. a top was already
    # successfully grounded), so fallback must not add a second top even
    # though it's the only wardrobe item available.
    replacements, unresolved = await find_verified_replacements(
        [failure], set(), {"top"},
        user["user_id"], db, None, None, None, _EMPTY_PREFS,
    )

    assert replacements == {}
    assert unresolved == [failure]


@pytest.mark.asyncio
async def test_fallback_picks_the_best_scoring_preference_aware_candidate(db, make_user, make_wardrobe_item):
    user = await make_user("frank")
    plain_id = await make_wardrobe_item(
        user["user_id"], name="Plain", clothing_type="top", color="beige",
        season="all", occasion="casual", style="casual",
    )
    preferred_id = await make_wardrobe_item(
        user["user_id"], name="Preferred", clothing_type="top", color="blue",
        season="all", occasion="casual", style="casual",
    )

    failure = VerificationFailure(requested_id="ffffffffffffffffffffffff", reason=VerificationFailureReason.NOT_FOUND)
    prefs = {**_EMPTY_PREFS, "color_scores": {"blue": 2.0}}

    replacements, unresolved = await find_verified_replacements(
        [failure], set(), set(), user["user_id"], db, None, None, None, prefs,
    )

    assert unresolved == []
    assert replacements["ffffffffffffffffffffffff"].source_id == preferred_id
    assert plain_id != preferred_id


@pytest.mark.asyncio
async def test_fallback_excludes_disliked_color_candidate(db, make_user, make_wardrobe_item):
    user = await make_user("grace")
    await make_wardrobe_item(user["user_id"], name="Disliked", clothing_type="top", color="black")
    ok_id = await make_wardrobe_item(user["user_id"], name="Ok", clothing_type="top", color="red")

    failure = VerificationFailure(requested_id="0123456789abcdef01234567", reason=VerificationFailureReason.NOT_FOUND)
    prefs = {**_EMPTY_PREFS, "disliked_colors": ["black"]}

    replacements, unresolved = await find_verified_replacements(
        [failure], set(), set(), user["user_id"], db, None, None, None, prefs,
    )

    assert unresolved == []
    assert replacements["0123456789abcdef01234567"].source_id == ok_id


@pytest.mark.asyncio
async def test_fallback_searches_across_all_types_regardless_of_failed_items_own_type(
    db, make_user, make_wardrobe_item
):
    user = await make_user("henry")
    item_id = await make_wardrobe_item(user["user_id"], name="Anything", clothing_type="accessory")

    failure = VerificationFailure(requested_id="1111111111111111111111aa", reason=VerificationFailureReason.INVALID_ID)

    replacements, unresolved = await find_verified_replacements(
        [failure], set(), set(), user["user_id"], db, None, None, None, _EMPTY_PREFS,
    )

    assert unresolved == []
    assert replacements["1111111111111111111111aa"].source_id == item_id


def test_fallback_has_no_way_to_receive_untrusted_type_information():
    """Guards the fix itself: find_verified_replacements' signature has no
    parameter through which a caller could pass along the recommendation
    service's echoed attributes for a failed candidate (e.g. a
    "type_hints" argument), so it cannot be steered by them even by
    accident. already_used_types is legitimate - it only ever carries
    types read from already-verified/grounded MongoDB documents, never
    anything echoed by the recommendation service about a failed item."""
    import inspect

    params = list(inspect.signature(find_verified_replacements).parameters)
    assert "already_used_types" in params  # trusted-data tracking is fine
    assert not any("hint" in p.lower() for p in params)
