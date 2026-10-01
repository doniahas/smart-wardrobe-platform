from app.models.recommendation import ClothingItem, RecommendationRequest, UserPreferences
from app.services.rule_based import build_recommendation, personalization_bonus, score_item


def _as_list(value):
    """style/occasion are now lists on ClothingItem (a garment may fit more
    than one of each) - existing tests below still pass a single scalar
    value per call, so this wraps it into the one-element list the field
    now expects, keeping every existing test's intent unchanged. Dedicated
    multi-value tests are added separately below."""
    return [] if value is None else [value]


def _top(id_, color, occasion=None, season=None, style=None):
    return ClothingItem(id=id_, type="top", color=color, occasion=_as_list(occasion), season=season, style=_as_list(style))


def _item(id_, type_, color=None, occasion=None, season=None, style=None):
    return ClothingItem(id=id_, type=type_, color=color, occasion=_as_list(occasion), season=season, style=_as_list(style))


def test_no_preferences_no_criteria_keeps_original_order():
    """With an empty preferences profile, ties are broken by original order,
    exactly as without personalization - regression guard for the no-feedback case."""
    request = RecommendationRequest(
        wardrobe_items=[_top("1", "blue"), _top("2", "red")],
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["1"]


def test_positive_color_preference_reorders_selection():
    request = RecommendationRequest(
        wardrobe_items=[_top("1", "blue"), _top("2", "red")],
        preferences=UserPreferences(color_scores={"red": 2.0}),
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["2"]


def test_negative_color_preference_reorders_selection():
    request = RecommendationRequest(
        wardrobe_items=[_top("1", "blue"), _top("2", "red")],
        preferences=UserPreferences(color_scores={"blue": -2.0}),
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["2"]


def test_personalization_cannot_override_requested_criteria_filter():
    """A huge personalization bonus must not resurrect an item that fails
    the requested occasion/season/style filter - personalization only
    re-ranks items that already qualify."""
    request = RecommendationRequest(
        occasion="work",
        wardrobe_items=[
            _top("1", "blue", occasion="sport"),
            _top("2", "red", occasion="work"),
        ],
        preferences=UserPreferences(color_scores={"blue": 100.0}),
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["2"]


def test_disliked_colors_hard_exclude_beats_personalization():
    request = RecommendationRequest(
        wardrobe_items=[_top("1", "blue"), _top("2", "red")],
        preferences=UserPreferences(disliked_colors=["blue"], color_scores={"blue": 100.0}),
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["2"]


def test_disliked_item_types_are_hard_excluded():
    """A memory-declared disliked item type must never be recommended,
    even when it's the only candidate for that outfit slot."""
    request = RecommendationRequest(
        wardrobe_items=[_item("1", "shoes", color="black"), _item("2", "top", color="white")],
        preferences=UserPreferences(disliked_item_types=["shoes"]),
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["2"]


def test_disliked_item_types_hard_exclude_beats_personalization():
    request = RecommendationRequest(
        wardrobe_items=[_item("1", "shoes", color="black"), _item("2", "top", color="white")],
        preferences=UserPreferences(disliked_item_types=["shoes"], type_scores={"shoes": 100.0}),
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["2"]


def test_preferred_occasions_reorders_selection():
    """A memory-declared preferred occasion gives a soft ranking bonus,
    same as preferred_styles, without requiring the caller to filter by it."""
    request = RecommendationRequest(
        wardrobe_items=[
            _top("1", "blue", occasion="sport"),
            _top("2", "red", occasion="work"),
        ],
        preferences=UserPreferences(preferred_occasions=["work"]),
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["2"]


def test_preferred_occasions_cannot_override_requested_criteria_filter():
    request = RecommendationRequest(
        occasion="sport",
        wardrobe_items=[
            _top("1", "blue", occasion="sport"),
            _top("2", "red", occasion="work"),
        ],
        preferences=UserPreferences(preferred_occasions=["work"]),
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["1"]


# --- Dress vs. top+bottom mutual exclusion ---


def test_dress_is_never_returned_with_top_or_bottom():
    """A dress is a complete garment on its own - it must never be combined
    with a separate top or bottom, even when the wardrobe has all three."""
    request = RecommendationRequest(
        wardrobe_items=[
            _item("1", "top", color="white"),
            _item("2", "bottom", color="black"),
            _item("3", "dress", color="red"),
        ],
    )
    selected = build_recommendation(request)
    types = {item.type for item in selected}
    assert "dress" not in types or ("top" not in types and "bottom" not in types)
    assert not ({"dress"} <= types and ({"top", "bottom"} & types))


def test_top_and_bottom_can_be_returned_together():
    request = RecommendationRequest(
        wardrobe_items=[
            _item("1", "top", color="white"),
            _item("2", "bottom", color="black"),
        ],
    )
    selected = build_recommendation(request)
    assert {item.id for item in selected} == {"1", "2"}


def test_better_scoring_dress_path_wins_over_top_bottom_path():
    """When both structures are possible, the higher total-scoring one is
    selected, and the losing structure's items are dropped entirely."""
    request = RecommendationRequest(
        occasion="work",
        season="winter",
        style="formal",
        wardrobe_items=[
            _item("1", "top", color="white", occasion="work"),
            _item("2", "bottom", color="black", occasion="work"),
            _item("3", "dress", color="red", occasion="work", season="winter", style="formal"),
        ],
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["3"]


def test_better_scoring_top_bottom_path_wins_over_dress_path():
    request = RecommendationRequest(
        occasion="work",
        season="winter",
        style="formal",
        wardrobe_items=[
            _item("1", "top", color="white", occasion="work", season="winter", style="formal"),
            _item("2", "bottom", color="black", occasion="work", season="winter", style="formal"),
            _item("3", "dress", color="red", occasion="work"),
        ],
    )
    selected = build_recommendation(request)
    assert {item.id for item in selected} == {"1", "2"}


def test_optional_categories_still_selected_alongside_dress():
    request = RecommendationRequest(
        wardrobe_items=[
            _item("1", "dress", color="red"),
            _item("2", "outerwear", color="black"),
            _item("3", "shoes", color="brown"),
            _item("4", "accessory", color="gold"),
        ],
    )
    selected = build_recommendation(request)
    assert {item.id for item in selected} == {"1", "2", "3", "4"}


def test_optional_categories_still_selected_alongside_top_and_bottom():
    request = RecommendationRequest(
        wardrobe_items=[
            _item("1", "top", color="white"),
            _item("2", "bottom", color="black"),
            _item("3", "outerwear", color="black"),
            _item("4", "shoes", color="brown"),
        ],
    )
    selected = build_recommendation(request)
    assert {item.id for item in selected} == {"1", "2", "3", "4"}


def test_incomplete_wardrobe_with_only_a_top_does_not_crash():
    """No dress and no bottom in the wardrobe: must not invent either one,
    and must still return the lone top without error."""
    request = RecommendationRequest(wardrobe_items=[_top("1", "blue")])
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["1"]


def test_incomplete_wardrobe_with_only_a_dress_does_not_crash():
    request = RecommendationRequest(wardrobe_items=[_item("1", "dress", color="red")])
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["1"]


def test_empty_wardrobe_after_filtering_returns_empty_without_crashing():
    """Every candidate hard-excluded: no core or optional item survives, and
    the function must return an empty list rather than raising."""
    request = RecommendationRequest(
        wardrobe_items=[_item("1", "dress", color="red")],
        preferences=UserPreferences(disliked_colors=["red"]),
    )
    selected = build_recommendation(request)
    assert selected == []


# --- Multi-value style/occasion: membership matching, no per-tag boost ---


def test_requested_style_matches_if_present_anywhere_in_items_style_list():
    """An item with several styles matches a request for any one of them -
    membership, not exact equality against a single value."""
    request = RecommendationRequest(
        style="minimal",
        wardrobe_items=[
            ClothingItem(id="1", type="top", color="white", style=["casual", "minimal"]),
            ClothingItem(id="2", type="top", color="black", style=["formal"]),
        ],
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["1"]


def test_requested_occasion_matches_if_present_anywhere_in_items_occasion_list():
    request = RecommendationRequest(
        occasion="travel",
        wardrobe_items=[
            ClothingItem(id="1", type="top", color="white", occasion=["university", "travel"]),
            ClothingItem(id="2", type="top", color="black", occasion=["work"]),
        ],
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["1"]


def test_more_style_tags_does_not_outscore_a_single_exact_match():
    """An item matching the requested style via one of several tags scores
    the same flat +2 as an item that only has that one style - having more
    tags must never be an implicit scoring advantage."""
    request = RecommendationRequest(
        style="casual",
        wardrobe_items=[
            ClothingItem(id="1", type="top", color="white", style=["casual"]),
            ClothingItem(id="2", type="top", color="black", style=["casual", "minimal", "streetwear"]),
        ],
    )
    tied = score_item(request.wardrobe_items[0], request) == score_item(request.wardrobe_items[1], request)
    assert tied


def test_personalization_bonus_uses_best_matching_value_not_sum():
    """An item with several learned-scored styles gets the single best
    (max) of them, never the sum of all of them."""
    preferences = UserPreferences(style_scores={"casual": 1.0, "minimal": 2.5, "formal": -1.0})
    item = ClothingItem(id="1", type="top", color="white", style=["casual", "minimal", "formal"])
    assert personalization_bonus(item, preferences) == 2.5


def test_personalization_bonus_for_multi_occasion_item_uses_best_value():
    preferences = UserPreferences(occasion_scores={"work": 0.5, "travel": 3.0})
    item = ClothingItem(id="1", type="top", color="white", occasion=["work", "travel"])
    assert personalization_bonus(item, preferences) == 3.0


# --- Hard vs. soft constraint separation regression tests ---
#
# Before this fix, requested season/occasion/style fed into one aggregate
# score alongside soft preference bonuses (preferred_styles/occasions,
# learned scores), gated by a single "score > 0" threshold. That let a
# strong-enough soft signal alone push a candidate over the line even when
# it matched none of the fields the user actually asked for, or matched
# only some of several requested fields. These tests lock in the fix:
# passes_requested_filters is a hard, per-field gate a soft bonus can never
# satisfy on an item's behalf.


def test_item_matching_none_of_the_requested_fields_is_excluded_even_with_huge_soft_bonus():
    """The core bug this fix addresses: an item that matches ZERO of the
    user's explicitly requested season/occasion/style must never be
    selected, no matter how large its soft preference bonus is. Before the
    fix, the preferred_styles overlap alone (+1) was enough to push this
    item's aggregate score above the old ``> 0`` threshold."""
    request = RecommendationRequest(
        season="winter",
        occasion="work",
        style="formal",
        wardrobe_items=[
            _item("1", "shoes", color="brown", season="summer", occasion="beach", style="casual"),
        ],
        preferences=UserPreferences(
            preferred_styles=["casual"],
            style_scores={"casual": 100.0},
        ),
    )
    selected = build_recommendation(request)
    assert selected == []


def test_item_matching_only_one_of_several_requested_fields_is_excluded():
    """Satisfying occasion alone is not enough when season and style were
    also explicitly requested - every requested field is independently
    hard-required, not just at least one of them."""
    request = RecommendationRequest(
        season="winter",
        occasion="work",
        style="formal",
        wardrobe_items=[
            # Matches occasion only - season is summer (not winter/all),
            # style is casual (not formal).
            _item("1", "top", color="white", season="summer", occasion="work", style="casual"),
        ],
    )
    selected = build_recommendation(request)
    assert selected == []


def test_item_matching_all_requested_fields_is_returned():
    """Sanity check alongside the exclusion tests above: an item that
    genuinely satisfies every requested field is still a valid, exact
    match and must be returned."""
    request = RecommendationRequest(
        season="winter",
        occasion="work",
        style="formal",
        wardrobe_items=[
            _item("1", "top", color="white", season="winter", occasion="work", style="formal"),
        ],
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["1"]


def test_soft_preference_ranks_between_two_otherwise_valid_matches_but_cannot_rescue_a_third():
    """Personalization is only a tie-breaker among candidates that already
    passed the hard filter - it must correctly rank two valid matches
    against each other, while a third item that fails the hard filter
    stays excluded regardless of an even larger soft bonus in its favor."""
    request = RecommendationRequest(
        occasion="work",
        wardrobe_items=[
            _item("1", "top", color="blue", occasion="work"),
            _item("2", "top", color="red", occasion="work"),
            _item("3", "top", color="green", occasion="sport"),
        ],
        preferences=UserPreferences(
            color_scores={"red": 2.0, "green": 100.0},
        ),
    )
    selected = build_recommendation(request)
    # Only one "top" slot exists in the outfit - the higher-ranked valid
    # match (red, boosted by its color score) wins over the other valid
    # match (blue); the invalid match (green, wrong occasion) never enters
    # the running at all despite its huge score.
    assert [item.id for item in selected] == ["2"]


def test_no_exact_candidate_returns_empty_not_an_unrelated_item():
    """When nothing in the wardrobe satisfies the explicit request, the
    function must return an empty list - never fall back to an unrelated
    item just to produce some result."""
    request = RecommendationRequest(
        season="winter",
        style="formal",
        wardrobe_items=[
            _item("1", "top", color="white", season="summer", style="casual"),
            _item("2", "bottom", color="black", season="spring", style="streetwear"),
        ],
    )
    selected = build_recommendation(request)
    assert selected == []


def test_all_season_item_still_satisfies_a_requested_season_hard_constraint():
    """Regression guard: the "all"-season wildcard must keep working as a
    genuine hard-constraint match (just ranked slightly below an exact
    season match), not be treated as a mismatch by the new hard gate."""
    request = RecommendationRequest(
        season="winter",
        wardrobe_items=[_item("1", "top", color="white", season="all")],
    )
    selected = build_recommendation(request)
    assert [item.id for item in selected] == ["1"]


def test_item_with_no_season_does_not_satisfy_a_requested_season():
    """An item with no season recorded at all can never be confirmed to
    match a requested season, so it must be excluded rather than let
    through on the benefit of the doubt."""
    request = RecommendationRequest(
        season="winter",
        wardrobe_items=[ClothingItem(id="1", type="top", color="white")],
    )
    selected = build_recommendation(request)
    assert selected == []
