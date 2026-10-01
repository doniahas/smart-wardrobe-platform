from ..models.recommendation import ClothingItem, RecommendationRequest, UserPreferences

# A dress is a complete garment on its own, so it is mutually exclusive with
# top+bottom: an outfit is either "dress + optionals" or "top+bottom + optionals",
# never a dress alongside a separate top or bottom.
OPTIONAL_TYPES_ORDER = ["outerwear", "shoes", "accessory"]


def _season_match(item_season: str | None, requested_season: str) -> tuple[bool, int]:
    """Whether an item's season satisfies a requested season, and the
    ranking bonus if so. An "all"-season item fits any requested season,
    but an exact match still ranks slightly higher. An item with no season
    at all can never be confirmed to satisfy a requested season."""
    if not item_season:
        return False, 0
    item_season = item_season.lower()
    if item_season == requested_season.lower():
        return True, 3
    if item_season == "all":
        return True, 2
    return False, 0


def passes_requested_filters(item: ClothingItem, request: RecommendationRequest) -> bool:
    """Hard eligibility against the caller's own explicit request.

    Every criterion the user actually asked for (season/occasion/style) is
    a hard constraint the item itself must satisfy - never something a soft
    preference bonus (preferred_styles/preferred_occasions, or the
    learned scores folded in by personalization_bonus) can substitute for.
    An item that fails any one requested field is excluded outright,
    independent of every other field and independent of how well it
    otherwise scores. Fields the caller didn't ask about impose no
    constraint at all.
    """
    if request.season:
        matches, _ = _season_match(item.season, request.season)
        if not matches:
            return False
    if request.occasion:
        item_occasions = {o.lower() for o in item.occasion}
        if request.occasion.lower() not in item_occasions:
            return False
    if request.style:
        item_styles = {s.lower() for s in item.style}
        if request.style.lower() not in item_styles:
            return False
    return True


def score_item(item: ClothingItem, request: RecommendationRequest) -> int:
    """Rank an item that has already passed passes_requested_filters (the
    hard gate) - purely a tie-breaker among otherwise-eligible items, never
    a substitute for that gate.

    style/occasion match by list membership (an item may have more than
    one of each): the request's single requested value matches if it's
    anywhere in the item's list, worth the same flat bonus an exact scalar
    match always was - never more just because the item has more tags.
    """
    score = 0

    if item.color and item.color.lower() in [c.lower() for c in request.preferences.disliked_colors]:
        return -100

    if item.type and item.type.lower() in [t.lower() for t in request.preferences.disliked_item_types]:
        return -100

    if request.season and item.season:
        _, bonus = _season_match(item.season, request.season)
        score += bonus

    item_occasions = {o.lower() for o in item.occasion}
    item_styles = {s.lower() for s in item.style}

    if request.occasion and item_occasions and request.occasion.lower() in item_occasions:
        score += 3
    if request.style and item_styles and request.style.lower() in item_styles:
        score += 2
    if item_styles & {s.lower() for s in request.preferences.preferred_styles}:
        score += 1
    if item_occasions & {o.lower() for o in request.preferences.preferred_occasions}:
        score += 1

    return score


def personalization_bonus(item: ClothingItem, preferences: UserPreferences) -> float:
    """Score adjustment learned from the user's saved outfits and feedback.

    Applied on top of the base rule-based score, never used to decide
    whether an item is eligible at all - only how highly it ranks among
    already-eligible items. With an empty preferences profile this is
    always 0, so behavior is unchanged for users with no feedback yet.

    style/occasion take the *best* (max) of the item's own values rather
    than summing across all of them, so an item tagged with more styles/
    occasions is never rewarded just for having more tags.
    """
    bonus = 0.0
    if item.color:
        bonus += preferences.color_scores.get(item.color.lower(), 0.0)
    item_styles = {s.lower() for s in item.style}
    if item_styles:
        bonus += max(preferences.style_scores.get(s, 0.0) for s in item_styles)
    item_occasions = {o.lower() for o in item.occasion}
    if item_occasions:
        bonus += max(preferences.occasion_scores.get(o, 0.0) for o in item_occasions)
    if item.season:
        bonus += preferences.season_scores.get(item.season.lower(), 0.0)
    if item.type:
        bonus += preferences.type_scores.get(item.type.lower(), 0.0)
    return bonus


def build_recommendation(request: RecommendationRequest) -> list[ClothingItem]:
    """Return outfit items only from the wardrobe items supplied in the request."""
    scored = [(item, score_item(item, request)) for item in request.wardrobe_items]

    # Disliked colors and disliked item types are always excluded.
    scored = [(item, score) for item, score in scored if score > -100]

    # Every criterion the user actually asked for (season/occasion/style) is
    # a hard requirement: an item that doesn't match one of them is excluded
    # outright, even if it scores well on the others or on soft preferences.
    # This is what stops a soft preference bonus from silently smuggling in
    # an item that doesn't satisfy something the user explicitly requested.
    scored = [(item, score) for item, score in scored if passes_requested_filters(item, request)]

    # Personalization only re-ranks the already-eligible candidates above -
    # it never lets in an item that failed the hard filters.
    scored = [
        (item, score + personalization_bonus(item, request.preferences))
        for item, score in scored
    ]

    scored.sort(key=lambda pair: pair[1], reverse=True)

    # Best (highest-scoring) available item per type, in descending-score order.
    best_by_type: dict[str, tuple[ClothingItem, float]] = {}
    for item, score in scored:
        item_type = item.type.lower()
        if item_type not in best_by_type:
            best_by_type[item_type] = (item, score)

    # Pick whichever of the two mutually-exclusive core structures scores
    # higher. If only one of them is available in the wardrobe, use that one
    # instead of inventing the missing piece (dress, or top, or bottom).
    dress_entry = best_by_type.get("dress")
    top_bottom_entries = [e for e in (best_by_type.get("top"), best_by_type.get("bottom")) if e]

    selected: list[ClothingItem] = []
    if dress_entry and top_bottom_entries:
        top_bottom_score = sum(score for _, score in top_bottom_entries)
        if dress_entry[1] > top_bottom_score:
            selected.append(dress_entry[0])
        else:
            selected.extend(item for item, _ in top_bottom_entries)
    elif dress_entry:
        selected.append(dress_entry[0])
    elif top_bottom_entries:
        selected.extend(item for item, _ in top_bottom_entries)

    used_types = {item.type.lower() for item in selected}
    for wanted_type in OPTIONAL_TYPES_ORDER:
        for item, _ in scored:
            item_type = item.type.lower()
            if item_type == wanted_type and item_type not in used_types:
                selected.append(item)
                used_types.add(item_type)
                break

    return selected
