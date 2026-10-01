import pytest


@pytest.mark.asyncio
async def test_create_recommendation_feedback_bare_dislike_success(client, make_user, make_wardrobe_item):
    user = await make_user("recfbuser1")
    item_id = await make_wardrobe_item(
        user["user_id"], clothing_type="top", color="red", season="summer", style="casual", occasion="casual",
    )

    resp = client.post(
        "/recommendation-feedback",
        json={"item_ids": [item_id], "liked": False},
        headers=user["headers"],
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["liked"] is False
    assert body["reason_scope"] is None
    assert body["reason_component_index"] is None
    assert body["components"] == [
        {"clothing_type": "top", "color": "red", "season": "summer", "style": "casual", "occasion": "casual"}
    ]


@pytest.mark.asyncio
async def test_create_recommendation_feedback_explicit_color_success(client, make_user, make_wardrobe_item):
    user = await make_user("recfbuser2")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top", color="red")

    resp = client.post(
        "/recommendation-feedback",
        json={"item_ids": [item_id], "liked": False, "reason_scope": "color", "reason_item_id": item_id},
        headers=user["headers"],
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["reason_scope"] == "color"
    assert body["reason_component_index"] == 0


def test_create_recommendation_feedback_requires_auth(client):
    resp = client.post(
        "/recommendation-feedback", json={"item_ids": ["000000000000000000000000"], "liked": True}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_recommendation_feedback_rejects_empty_item_ids(client, make_user):
    user = await make_user("recfbuser3")
    resp = client.post(
        "/recommendation-feedback", json={"item_ids": [], "liked": True}, headers=user["headers"]
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_recommendation_feedback_rejects_invalid_item_id_format(client, make_user):
    user = await make_user("recfbuser4")
    resp = client.post(
        "/recommendation-feedback",
        json={"item_ids": ["not-an-id"], "liked": True},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid item id"


@pytest.mark.asyncio
async def test_create_recommendation_feedback_rejects_item_not_owned(client, make_user, make_wardrobe_item):
    owner = await make_user("recfbowner")
    other = await make_user("recfbother")
    item_id = await make_wardrobe_item(owner["user_id"], clothing_type="top")

    resp = client.post(
        "/recommendation-feedback",
        json={"item_ids": [item_id], "liked": True},
        headers=other["headers"],
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "One or more items do not exist or do not belong to you"


@pytest.mark.asyncio
async def test_create_recommendation_feedback_rejects_missing_reason_item_id_for_color_scope(
    client, make_user, make_wardrobe_item
):
    user = await make_user("recfbuser5")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    resp = client.post(
        "/recommendation-feedback",
        json={"item_ids": [item_id], "liked": False, "reason_scope": "color"},
        headers=user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_recommendation_feedback_rejects_reason_item_id_for_combination_scope(
    client, make_user, make_wardrobe_item
):
    user = await make_user("recfbuser6")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    resp = client.post(
        "/recommendation-feedback",
        json={
            "item_ids": [item_id], "liked": False,
            "reason_scope": "combination", "reason_item_id": item_id,
        },
        headers=user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_recommendation_feedback_rejects_reason_item_id_not_in_item_ids(
    client, make_user, make_wardrobe_item
):
    user = await make_user("recfbuser7")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    other_item_id = await make_wardrobe_item(user["user_id"], clothing_type="bottom")
    resp = client.post(
        "/recommendation-feedback",
        json={
            "item_ids": [item_id], "liked": False,
            "reason_scope": "color", "reason_item_id": other_item_id,
        },
        headers=user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_recommendation_feedback_combination_scope_success(
    client, make_user, make_wardrobe_item
):
    user = await make_user("recfbuser8")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    resp = client.post(
        "/recommendation-feedback",
        json={"item_ids": [item_id], "liked": False, "reason_scope": "combination"},
        headers=user["headers"],
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_recommendation_feedback_occasion_season_mismatch_success(
    client, make_user, make_wardrobe_item
):
    user = await make_user("recfbuser9")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    resp = client.post(
        "/recommendation-feedback",
        json={
            "item_ids": [item_id], "liked": False,
            "reason_scope": "occasion_season_mismatch", "occasion": "work",
        },
        headers=user["headers"],
    )
    assert resp.status_code == 201
    assert resp.json()["occasion"] == "work"


@pytest.mark.asyncio
async def test_create_recommendation_feedback_multi_item_preserves_order(
    client, make_user, make_wardrobe_item
):
    user = await make_user("recfbuser10")
    top_id = await make_wardrobe_item(user["user_id"], clothing_type="top", color="blue")
    bottom_id = await make_wardrobe_item(user["user_id"], clothing_type="bottom", color="black")

    resp = client.post(
        "/recommendation-feedback",
        json={"item_ids": [top_id, bottom_id], "liked": True},
        headers=user["headers"],
    )

    assert resp.status_code == 201
    components = resp.json()["components"]
    assert [c["clothing_type"] for c in components] == ["top", "bottom"]
