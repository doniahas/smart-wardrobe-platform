"""Build a per-user personalization profile from saved outfits and feedback.

Pure functions only (no DB access) so the scoring logic is easy to unit test
in isolation from Mongo.
"""
from app.models import (
    MEMORY_MAX_GENERIC_PREFERENCES,
    ChatMemoryCandidate,
    Occasion,
    Style,
    _as_list,
    _sanitize_legacy_enum_list,
)

_MAX_SCORE = 3.0


def _outfit_weight(feedback: dict | None) -> float:
    """How strongly this saved outfit should push similar outfits up/down.

    - No feedback yet, just saved: a mild positive signal (the user chose to
      keep it).
    - Feedback present: rating (1-5) and liked/disliked combine into a
      stronger positive or negative signal.
    """
    if feedback is None:
        return 1.0

    weight = 0.0
    rating = feedback.get("rating")
    if rating is not None:
        weight += (rating - 3) * 0.5  # 5 -> +1, 3 -> 0, 1 -> -1

    liked = feedback.get("liked")
    if liked is True:
        weight += 2.0
    elif liked is False:
        weight -= 2.0

    return weight


def _add(scores: dict[str, float], key, weight: float) -> None:
    if not key:
        return
    normalized = str(key).lower()
    scores[normalized] = scores.get(normalized, 0.0) + weight


def _clamp_all(scores: dict[str, float]) -> dict[str, float]:
    return {key: max(-_MAX_SCORE, min(_MAX_SCORE, value)) for key, value in scores.items()}


def build_user_preferences(
    outfits: list[dict],
    feedback_by_outfit: dict[str, dict],
    wardrobe_by_id: dict[str, dict],
) -> dict:
    """Turn a user's saved outfits + feedback into signed attribute weights.

    `outfits` are this user's saved-outfit documents, `feedback_by_outfit`
    maps outfit_id -> this user's feedback document for it, and
    `wardrobe_by_id` maps item_id -> this user's wardrobe item document.
    Callers are expected to have already scoped all three to a single user,
    which keeps one user's feedback from ever affecting another's profile.

    Returns a dict matching the recommendation service's UserPreferences
    shape (color_scores/style_scores/occasion_scores/season_scores/type_scores).
    With no outfits, every dict is empty, which leaves the base rule-based
    recommendation behavior unchanged.
    """
    color_scores: dict[str, float] = {}
    style_scores: dict[str, float] = {}
    occasion_scores: dict[str, float] = {}
    season_scores: dict[str, float] = {}
    type_scores: dict[str, float] = {}

    for outfit in outfits:
        outfit_id = str(outfit["_id"])
        weight = _outfit_weight(feedback_by_outfit.get(outfit_id))
        if weight == 0:
            continue

        for item_id in outfit.get("item_ids", []):
            item = wardrobe_by_id.get(item_id)
            if item is None:
                continue
            _add(color_scores, item.get("color"), weight)
            # style/occasion may now be lists (or a legacy scalar, via
            # _as_list) - each of the item's values independently
            # contributes the outfit's full weight, same as a single-
            # valued item always did; this is a learned-preference tally
            # per distinct value, not a per-item score, so an item with
            # more tags isn't rewarded here, only more of its individual
            # style/occasion values get a data point.
            for style in _as_list(item.get("style")):
                _add(style_scores, style, weight)
            for occasion in _as_list(item.get("occasion")):
                _add(occasion_scores, occasion, weight)
            _add(season_scores, item.get("season"), weight)
            _add(type_scores, item.get("clothing_type"), weight)

    return {
        "color_scores": _clamp_all(color_scores),
        "style_scores": _clamp_all(style_scores),
        "occasion_scores": _clamp_all(occasion_scores),
        "season_scores": _clamp_all(season_scores),
        "type_scores": _clamp_all(type_scores),
    }


_MAX_LEARNED_LIST_SIZE = 20


def _append_unique(values: list[str], value) -> None:
    if value and value not in values and len(values) < _MAX_LEARNED_LIST_SIZE:
        values.append(value)


def update_memory_from_feedback(
    memory: dict | None,
    outfit_items: list[dict],
    rating: int | None,
    liked: bool | None,
) -> dict:
    """Turn one piece of outfit feedback into memory updates.

    Returns only the fields that should be `$set` on the user's memory doc.
    Every list is grown from its existing value, never replaced or cleared,
    so manually-set entries (and untouched fields like notes/preferred_fit)
    are preserved. A color already on the opposite liked/disliked list is
    skipped rather than moved, so the two lists never end up overlapping.
    Returns {} when the feedback is neutral (no rating extreme, no like/dislike).
    """
    positive = liked is True or (rating is not None and rating >= 4)
    negative = liked is False or (rating is not None and rating <= 2)
    if not positive and not negative:
        return {}

    memory = memory or {}
    preferred_styles = list(memory.get("preferred_styles", []))
    preferred_occasions = list(memory.get("preferred_occasions", []))
    liked_colors = list(memory.get("liked_colors", []))
    disliked_colors = list(memory.get("disliked_colors", []))
    disliked_item_types = list(memory.get("disliked_item_types", []))

    for item in outfit_items:
        color = (item.get("color") or "").strip().lower()
        if positive:
            for style in _as_list(item.get("style")):
                _append_unique(preferred_styles, style)
            for occasion in _as_list(item.get("occasion")):
                _append_unique(preferred_occasions, occasion)
            if color and color not in disliked_colors:
                _append_unique(liked_colors, color)
        elif negative:
            if color and color not in liked_colors:
                _append_unique(disliked_colors, color)
            _append_unique(disliked_item_types, item.get("clothing_type"))

    return {
        "preferred_styles": _sanitize_legacy_enum_list(preferred_styles, Style),
        "preferred_occasions": _sanitize_legacy_enum_list(preferred_occasions, Occasion),
        "liked_colors": liked_colors,
        "disliked_colors": disliked_colors,
        "disliked_item_types": disliked_item_types,
    }


def _candidate_is_empty(candidate: ChatMemoryCandidate) -> bool:
    return (
        not candidate.preferred_styles
        and not candidate.liked_colors
        and not candidate.disliked_colors
        and not candidate.disliked_item_types
        and not candidate.preferred_occasions
        and candidate.preferred_fit is None
        and candidate.communication_style is None
        and not candidate.additional_preferences
    )


def _merge_generic_preferences(existing: list, candidate_prefs: list) -> list[dict]:
    """Merge candidate GenericPreference entries into an existing
    additional_preferences list (long-term-memory follow-up completion).

    Topic-keyed replace, not append-only: a candidate entry whose (already
    lowercase-normalized) topic matches an existing entry REPLACES that
    entry's preference text in place - this is how a later, contradictory
    durable statement about the same generic topic ("I prefer more open
    clothing now") correctly supersedes an earlier one ("I prefer more
    covered clothing"), the same "later statement wins" principle
    merge_chat_memory_candidate already applies to preferred_fit/
    communication_style/color-reversal. A genuinely new topic is appended.
    An unrelated existing topic is never touched.

    Bounded even across many separate conversations: if the merged result
    would exceed MEMORY_MAX_GENERIC_PREFERENCES, the OLDEST entries are
    dropped first (the per-write cap in app/models.py::
    _validate_generic_preferences only bounds a single write; this bounds
    unbounded growth over the account's whole lifetime).
    """
    merged: list[dict] = [dict(entry) for entry in existing if isinstance(entry, dict) and "topic" in entry]
    index_by_topic = {entry["topic"]: i for i, entry in enumerate(merged)}

    for pref in candidate_prefs:
        topic = pref.topic
        entry = {"topic": topic, "preference": pref.preference}
        if topic in index_by_topic:
            merged[index_by_topic[topic]] = entry
        else:
            merged.append(entry)
            index_by_topic[topic] = len(merged) - 1

    if len(merged) > MEMORY_MAX_GENERIC_PREFERENCES:
        merged = merged[-MEMORY_MAX_GENERIC_PREFERENCES:]
    return merged


def _enum_value(value):
    return value.value if hasattr(value, "value") else value


def merge_chat_memory_candidate(
    memory: dict | None,
    candidate: ChatMemoryCandidate,
    *,
    allow_color_preference_reversal: bool = False,
) -> dict:
    """Merge an already-approved ChatMemoryCandidate into a user's existing
    memory profile.

    Returns only the fields that should be `$set` on the user's memory doc,
    or {} when the candidate has nothing to contribute. Deliberately takes
    only a candidate - not the should_save/confidence gate that produced it
    (see services/memory_extraction.py::is_chat_memory_candidate_approved) -
    so this merge stays generic enough to reuse for future signal sources.
    It is in fact already reused by a second, independent signal source -
    services/recommendation_feedback_learning.py::
    learn_memory_from_recommendation_feedback (implicit/explicit signals
    from liking or disliking a single recommended item) - alongside
    services/chat_memory_learning.py's explicit, LLM-vetted conversational
    statements; onboarding preferences and forum-derived signals could
    reuse this one too.

    Mirrors update_memory_from_feedback's non-destructive shape for every
    field EXCEPT the one deliberate, opt-in exception below: lists only
    ever grow from their existing value (never replaced/cleared, never
    pruned), and untouched fields - including notes, which this function
    never reads or returns - are never part of the returned updates.
    preferred_fit/communication_style are included only when the candidate
    states an explicit value different from the current one - this is how
    a contradictory later statement correctly replaces the old value
    rather than being dropped, per the long-term-memory requirement's "the
    AI should remember when the user changes their mind" guidance.

    `allow_color_preference_
    reversal` (default False, preserving every pre-existing caller's exact
    behavior) lets liked_colors/disliked_colors apply that same "later
    statement wins" rule to each other, not just within themselves. When
    True, a color the candidate newly states as liked that was previously
    on disliked_colors (or vice versa) MOVES to the new list instead of
    being silently skipped - e.g. a user who previously said "I hate
    black" and later says "I've changed my style, I like black clothes
    now" ends up with black in liked_colors and no longer in
    disliked_colors. (The old always-skip behavior left the older,
    contradicted value in place forever once set once - exactly the
    stale-memory bug a long-term preference memory ("the AI won't
    forget or hallucinate the user's preferences") must avoid.)

    Deliberately opt-in, not the new default, because the two real callers
    have different trust levels for a color-preference *reversal*
    specifically: chat_memory_learning.py passes True - an explicit,
    first-person, durable statement that already passed the LLM's should_
    save/confidence gate is trustworthy enough to override a prior stated
    preference. recommendation_feedback_learning.py does NOT pass it (stays
    False, i.e. the original skip behavior, unchanged) - a single swipe on
    one recommended item is a much weaker, noisier, implicit signal about a
    general color preference, and should never be allowed to silently
    overwrite an explicit one (see
    test_recommendation_feedback_learning.py::
    test_conflict_with_existing_opposite_signal_is_skipped, which still
    covers and locks in that unchanged behavior). Only within one candidate
    can liked_colors and disliked_colors never both name the same color -
    ChatMemoryCandidate's own validator already rejects that (see
    app/models.py::_check_color_overlap) - so there is no ambiguity about
    which list a single call's reversal applies to.

    Long-term-memory follow-up completion: `additional_preferences`
    (app/models.py::GenericPreference) is the generic fallback for a
    durable preference that doesn't fit any structured field above - see
    _merge_generic_preferences for its own topic-keyed replace semantics,
    which apply "later statement wins" the same way preferred_fit/
    communication_style already do, without needing an opt-in flag (there
    is no second, lower-trust caller for this field the way there is for
    colors - recommendation_feedback_learning.py has no concept of a
    "topic" to attach a generic preference to in the first place).
    """
    if _candidate_is_empty(candidate):
        return {}

    memory = memory or {}
    preferred_styles = list(memory.get("preferred_styles", []))
    preferred_occasions = list(memory.get("preferred_occasions", []))
    liked_colors = list(memory.get("liked_colors", []))
    disliked_colors = list(memory.get("disliked_colors", []))
    disliked_item_types = list(memory.get("disliked_item_types", []))

    for style in candidate.preferred_styles:
        _append_unique(preferred_styles, _enum_value(style))
    for occasion in candidate.preferred_occasions:
        _append_unique(preferred_occasions, _enum_value(occasion))
    for item_type in candidate.disliked_item_types:
        _append_unique(disliked_item_types, _enum_value(item_type))

    for color in candidate.liked_colors:
        if color in disliked_colors:
            if not allow_color_preference_reversal:
                continue
            disliked_colors.remove(color)
        _append_unique(liked_colors, color)
    for color in candidate.disliked_colors:
        if color in liked_colors:
            if not allow_color_preference_reversal:
                continue
            liked_colors.remove(color)
        _append_unique(disliked_colors, color)

    updates: dict = {
        "preferred_styles": preferred_styles,
        "preferred_occasions": preferred_occasions,
        "liked_colors": liked_colors,
        "disliked_colors": disliked_colors,
        "disliked_item_types": disliked_item_types,
    }

    if candidate.preferred_fit is not None:
        new_fit = _enum_value(candidate.preferred_fit)
        if new_fit != memory.get("preferred_fit"):
            updates["preferred_fit"] = new_fit

    if candidate.communication_style is not None:
        new_style = _enum_value(candidate.communication_style)
        if new_style != memory.get("communication_style"):
            updates["communication_style"] = new_style

    if candidate.additional_preferences:
        merged_generic = _merge_generic_preferences(
            memory.get("additional_preferences", []), candidate.additional_preferences
        )
        if merged_generic != memory.get("additional_preferences", []):
            updates["additional_preferences"] = merged_generic

    return updates


def apply_memory_preferences(preferences: dict, memory: dict | None) -> dict:
    """Fold a user's long-term memory profile into the preferences
    payload sent to the recommendation service.

    `memory` is this user's own memory document (or None if they've never
    saved one), already scoped by the caller. Disliked colors/item types
    become hard exclusions and preferred styles/occasions become soft
    ranking bonuses in the recommendation service's scoring - on top of,
    not instead of, the learned score_scores/etc. above.

    The returned dict is scoring input only. It (and `memory` itself,
    including free-text `notes`) must never be used to construct or mutate
    a WardrobeItemPublic/GroundedWardrobeItem - verified item facts come
    exclusively from each item's own MongoDB document, never from a user's
    stated preferences or opinions.

    Long-term-memory follow-up completion: `additional_preferences` is
    deliberately NOT folded in here either, for the same reason
    `preferred_fit`/`communication_style` already aren't (see this
    module's git history) - wardrobe items have no corresponding
    attribute the rule-based recommendation service could score an
    arbitrary free-text topic/preference against (`WardrobeItemCreate` has
    clothing_type/color/season/style/occasion only), and forcing one in
    would mean either ignoring the actual text or inventing a scoring
    heuristic with no real signal behind it. It remains fully available to
    the conversational AI via chat_context.py::build_chat_prompt instead.
    """
    merged = dict(preferences)
    memory = memory or {}
    merged["disliked_colors"] = list(memory.get("disliked_colors", []))
    merged["preferred_styles"] = _sanitize_legacy_enum_list(memory.get("preferred_styles", []), Style)
    merged["disliked_item_types"] = list(memory.get("disliked_item_types", []))
    merged["preferred_occasions"] = _sanitize_legacy_enum_list(memory.get("preferred_occasions", []), Occasion)
    return merged


def candidate_passes_requested_filters(
    doc: dict, occasion: str | None, season: str | None, style: str | None
) -> bool:
    """Hard eligibility for a wardrobe document against the caller's own
    explicit request - mirrors rule_based.py::passes_requested_filters on
    the recommendation service. Every criterion the user actually asked
    for (season/occasion/style) must be satisfied by the document itself;
    a soft preference bonus can never substitute for a requested field the
    item doesn't actually match. Fields the caller didn't ask about impose
    no constraint. An item with no season at all can never be confirmed to
    satisfy a requested season.
    """
    item_season = (doc.get("season") or "").lower()
    item_occasions = {o.lower() for o in _as_list(doc.get("occasion"))}
    item_styles = {s.lower() for s in _as_list(doc.get("style"))}

    if season:
        if not item_season or (item_season != season.lower() and item_season != "all"):
            return False
    if occasion and occasion.lower() not in item_occasions:
        return False
    if style and style.lower() not in item_styles:
        return False
    return True


def score_candidate_for_request(
    doc: dict,
    occasion: str | None,
    season: str | None,
    style: str | None,
    preferences: dict,
) -> float | None:
    """Score a wardrobe document as a fallback replacement.

    Mirrors the recommendation service's own rule-based criteria (season/
    occasion/style matching, memory hard exclusions, learned + memory soft bonuses) so
    a fallback replacement is picked by the same rules the original
    candidate would have been judged by - evaluated locally since fallback
    selection lives entirely in the web service (the recommendation
    service itself is not called again and stays unchanged).

    Reuses the same `preferences` dict already built once per request by
    build_user_preferences()/apply_memory_preferences() above, rather than
    recomputing anything.

    Returns None if the item is hard-excluded: disliked color/type, or
    failing any season/occasion/style the caller actually requested (see
    candidate_passes_requested_filters) - every requested field must be
    satisfied by the document itself, never by a soft preference bonus.

    style/occasion match by list membership now (an item may have more
    than one of each - see models.py): the requested value matches if it's
    anywhere in the item's list, worth the same flat bonus as an exact
    scalar match always was - never more for having more tags. The
    learned-score bonus similarly takes the *best* (max) of the item's
    values rather than summing across all of them, so an item tagged with
    more styles/occasions isn't rewarded just for having more tags.
    """
    color = (doc.get("color") or "").lower()
    clothing_type = (doc.get("clothing_type") or "").lower()
    item_season = (doc.get("season") or "").lower()
    item_occasions = {o.lower() for o in _as_list(doc.get("occasion"))}
    item_styles = {s.lower() for s in _as_list(doc.get("style"))}

    disliked_colors = {c.lower() for c in preferences.get("disliked_colors", [])}
    disliked_types = {t.lower() for t in preferences.get("disliked_item_types", [])}
    if color in disliked_colors or clothing_type in disliked_types:
        return None

    if not candidate_passes_requested_filters(doc, occasion, season, style):
        return None

    score = 0.0
    if season and item_season:
        if item_season == season.lower():
            score += 3
        elif item_season == "all":
            score += 2
    if occasion and item_occasions and occasion.lower() in item_occasions:
        score += 3
    if style and item_styles and style.lower() in item_styles:
        score += 2
    if item_styles & {s.lower() for s in preferences.get("preferred_styles", [])}:
        score += 1
    if item_occasions & {o.lower() for o in preferences.get("preferred_occasions", [])}:
        score += 1

    style_scores = preferences.get("style_scores", {})
    occasion_scores = preferences.get("occasion_scores", {})
    score += preferences.get("color_scores", {}).get(color, 0.0)
    score += max((style_scores.get(s, 0.0) for s in item_styles), default=0.0)
    score += max((occasion_scores.get(o, 0.0) for o in item_occasions), default=0.0)
    score += preferences.get("season_scores", {}).get(item_season, 0.0)
    score += preferences.get("type_scores", {}).get(clothing_type, 0.0)

    return score