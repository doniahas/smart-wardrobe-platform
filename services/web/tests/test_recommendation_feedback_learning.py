import pytest


def _feedback(client, headers, item_ids, liked, **overrides):
    body = {"item_ids": item_ids, "liked": liked}
    body.update(overrides)
    return client.post("/recommendation-feedback", json=body, headers=headers)


def _memory(client, headers):
    return client.get("/memory", headers=headers).json()


@pytest.mark.asyncio
async def test_explicit_color_dislike_writes_memory_on_first_event(client, make_user, make_wardrobe_item):
    user = await make_user("learnuser1")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top", color="red")

    resp = _feedback(
        client, user["headers"], [item_id], False, reason_scope="color", reason_item_id=item_id
    )
    assert resp.status_code == 201

    assert "red" in _memory(client, user["headers"])["disliked_colors"]


@pytest.mark.asyncio
async def test_explicit_style_like_writes_memory_on_first_event(client, make_user, make_wardrobe_item):
    user = await make_user("learnuser1b")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top", style="elegant")

    resp = _feedback(
        client, user["headers"], [item_id], True, reason_scope="style", reason_item_id=item_id
    )
    assert resp.status_code == 201

    assert "elegant" in _memory(client, user["headers"])["preferred_styles"]


@pytest.mark.asyncio
async def test_bare_dislike_events_1_and_2_write_nothing(client, make_user, make_wardrobe_item):
    """Two different outfits sharing color=red - below the repetition
    threshold, so red must not appear in memory yet."""
    user = await make_user("learnuser2")
    item1 = await make_wardrobe_item(user["user_id"], clothing_type="top", color="red")
    item2 = await make_wardrobe_item(user["user_id"], clothing_type="bottom", color="red")

    assert _feedback(client, user["headers"], [item1], False).status_code == 201
    assert _feedback(client, user["headers"], [item2], False).status_code == 201

    assert "red" not in _memory(client, user["headers"])["disliked_colors"]


@pytest.mark.asyncio
async def test_third_distinct_bare_dislike_promotes_only_the_repeated_attribute(
    client, make_user, make_wardrobe_item
):
    """Three different outfits sharing only color=red - style and clothing
    type differ across all three and must NOT be promoted, only the one
    attribute that actually recurred."""
    user = await make_user("learnuser3")
    item1 = await make_wardrobe_item(user["user_id"], clothing_type="top", color="red", style="casual")
    item2 = await make_wardrobe_item(user["user_id"], clothing_type="bottom", color="red", style="formal")
    item3 = await make_wardrobe_item(user["user_id"], clothing_type="shoes", color="red", style="sport")

    assert _feedback(client, user["headers"], [item1], False).status_code == 201
    assert _feedback(client, user["headers"], [item2], False).status_code == 201
    assert _feedback(client, user["headers"], [item3], False).status_code == 201

    memory = _memory(client, user["headers"])
    assert memory["disliked_colors"] == ["red"]
    assert memory["disliked_item_types"] == []


@pytest.mark.asyncio
async def test_bare_dislike_of_same_outfit_repeated_promotes_nothing(
    client, make_user, make_wardrobe_item
):
    """Disliking the exact same outfit three times is one data point
    repeated, not three independent pieces of evidence - it must not
    promote anything, unlike three genuinely different outfits sharing
    one attribute (see the distinct-outfits test above). This is exactly
    the overgeneralization bug (update_memory_from_feedback) this path
    exists to avoid reintroducing in the recommendation-feedback path."""
    user = await make_user("learnuser3b")
    item_id = await make_wardrobe_item(
        user["user_id"], clothing_type="top", color="red", style="casual"
    )

    for _ in range(5):
        assert _feedback(client, user["headers"], [item_id], False).status_code == 201

    memory = _memory(client, user["headers"])
    assert memory["disliked_colors"] == []
    assert memory["disliked_item_types"] == []


@pytest.mark.asyncio
async def test_explicit_events_are_excluded_from_implicit_counting(client, make_user, make_wardrobe_item):
    """Two explicit, color-scoped likes on items that also happen to share
    style=elegant, followed by one bare like of a third elegant item.
    Explicit events must never count toward the implicit style tally -
    if they did, this would be the "3rd" elegant event and would
    incorrectly promote it after only one real implicit event."""
    user = await make_user("learnuser4")
    item1 = await make_wardrobe_item(user["user_id"], clothing_type="top", color="gold", style="elegant")
    item2 = await make_wardrobe_item(user["user_id"], clothing_type="bottom", color="silver", style="elegant")
    item3 = await make_wardrobe_item(user["user_id"], clothing_type="shoes", color="black", style="elegant")

    assert _feedback(
        client, user["headers"], [item1], True, reason_scope="color", reason_item_id=item1
    ).status_code == 201
    assert _feedback(
        client, user["headers"], [item2], True, reason_scope="color", reason_item_id=item2
    ).status_code == 201
    assert _feedback(client, user["headers"], [item3], True).status_code == 201

    assert "elegant" not in _memory(client, user["headers"])["preferred_styles"]


@pytest.mark.asyncio
async def test_conflict_with_existing_opposite_signal_is_skipped(client, make_user, make_wardrobe_item):
    """A color already liked from a different signal source must not be
    moved to disliked_colors by a recommendation-feedback dislike -
    merge_chat_memory_candidate's existing skip-on-conflict behavior,
    reused unchanged."""
    user = await make_user("learnuser5")
    put_resp = client.put("/memory", json={"liked_colors": ["red"]}, headers=user["headers"])
    assert put_resp.status_code == 200

    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top", color="red")
    resp = _feedback(
        client, user["headers"], [item_id], False, reason_scope="color", reason_item_id=item_id
    )
    assert resp.status_code == 201

    memory = _memory(client, user["headers"])
    assert "red" in memory["liked_colors"]
    assert "red" not in memory["disliked_colors"]


@pytest.mark.asyncio
async def test_occasion_season_mismatch_never_writes_memory(client, make_user, make_wardrobe_item):
    user = await make_user("learnuser6")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top", color="teal")

    for _ in range(5):
        resp = _feedback(
            client, user["headers"], [item_id], False,
            reason_scope="occasion_season_mismatch", occasion="work",
        )
        assert resp.status_code == 201

    memory = _memory(client, user["headers"])
    assert memory["disliked_colors"] == []
    assert memory["disliked_item_types"] == []


@pytest.mark.asyncio
async def test_recommendation_feedback_never_affects_outfit_feedback_memory_path(
    client, make_user, make_wardrobe_item, make_outfit
):
    """Recommendation feedback is purely additive - update_memory_from_feedback's
    existing outfit-feedback path must remain unaffected. A saved-outfit
    dislike still blames every attribute of that outfit at once (the
    existing, unrelated behavior), independent of anything
    recommendation-feedback does."""
    user = await make_user("learnuser7")
    item_id = await make_wardrobe_item(
        user["user_id"], clothing_type="top", color="purple", style="streetwear"
    )
    outfit_id = await make_outfit(user["user_id"], [item_id])

    resp = client.post(
        "/feedback",
        json={"outfit_id": outfit_id, "rating": 1, "liked": False},
        headers=user["headers"],
    )
    assert resp.status_code == 201

    memory = _memory(client, user["headers"])
    assert "purple" in memory["disliked_colors"]
    assert "top" in memory["disliked_item_types"]
