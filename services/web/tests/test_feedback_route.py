import pytest


@pytest.mark.asyncio
async def test_liked_feedback_updates_preferred_memory(client, make_user, make_wardrobe_item, make_outfit):
    user = await make_user("alice")
    item_id = await make_wardrobe_item(
        user["user_id"], color="red", style="elegant", occasion="formal", clothing_type="dress"
    )
    outfit_id = await make_outfit(user["user_id"], [item_id])

    resp = client.post(
        "/feedback",
        json={"outfit_id": outfit_id, "rating": 3, "liked": True},
        headers=user["headers"],
    )
    assert resp.status_code == 201

    memory = client.get("/memory", headers=user["headers"]).json()
    assert memory["preferred_styles"] == ["elegant"]
    assert memory["preferred_occasions"] == ["formal"]
    assert memory["liked_colors"] == ["red"]
    assert memory["disliked_colors"] == []
    assert memory["disliked_item_types"] == []


@pytest.mark.asyncio
async def test_high_rating_alone_updates_preferred_memory(client, make_user, make_wardrobe_item, make_outfit):
    user = await make_user("bob")
    item_id = await make_wardrobe_item(user["user_id"], color="blue", style="sport", occasion="sport")
    outfit_id = await make_outfit(user["user_id"], [item_id])

    resp = client.post("/feedback", json={"outfit_id": outfit_id, "rating": 4}, headers=user["headers"])
    assert resp.status_code == 201

    memory = client.get("/memory", headers=user["headers"]).json()
    assert memory["preferred_styles"] == ["sport"]
    assert memory["preferred_occasions"] == ["sport"]
    assert memory["liked_colors"] == ["blue"]


@pytest.mark.asyncio
async def test_disliked_feedback_updates_disliked_memory(client, make_user, make_wardrobe_item, make_outfit):
    user = await make_user("carol")
    item_id = await make_wardrobe_item(user["user_id"], color="orange", clothing_type="shoes")
    outfit_id = await make_outfit(user["user_id"], [item_id])

    resp = client.post(
        "/feedback",
        json={"outfit_id": outfit_id, "rating": 3, "liked": False},
        headers=user["headers"],
    )
    assert resp.status_code == 201

    memory = client.get("/memory", headers=user["headers"]).json()
    assert memory["disliked_colors"] == ["orange"]
    assert memory["disliked_item_types"] == ["shoes"]
    assert memory["preferred_styles"] == []
    assert memory["liked_colors"] == []


@pytest.mark.asyncio
async def test_low_rating_alone_updates_disliked_memory(client, make_user, make_wardrobe_item, make_outfit):
    user = await make_user("dave")
    item_id = await make_wardrobe_item(user["user_id"], color="green", clothing_type="accessory")
    outfit_id = await make_outfit(user["user_id"], [item_id])

    resp = client.post("/feedback", json={"outfit_id": outfit_id, "rating": 2}, headers=user["headers"])
    assert resp.status_code == 201

    memory = client.get("/memory", headers=user["headers"]).json()
    assert memory["disliked_colors"] == ["green"]
    assert memory["disliked_item_types"] == ["accessory"]


@pytest.mark.asyncio
async def test_put_feedback_changing_rating_updates_memory(client, make_user, make_wardrobe_item, make_outfit):
    user = await make_user("erin")
    item_id = await make_wardrobe_item(user["user_id"], color="purple", style="streetwear", occasion="casual")
    outfit_id = await make_outfit(user["user_id"], [item_id])

    create_resp = client.post(
        "/feedback",
        json={"outfit_id": outfit_id, "rating": 3, "liked": None},
        headers=user["headers"],
    )
    feedback_id = create_resp.json()["id"]

    # Neutral at first (rating 3, no liked flag): memory must stay untouched.
    memory = client.get("/memory", headers=user["headers"]).json()
    assert memory["preferred_styles"] == []

    put_resp = client.put(f"/feedback/{feedback_id}", json={"rating": 5}, headers=user["headers"])
    assert put_resp.status_code == 200

    memory = client.get("/memory", headers=user["headers"]).json()
    assert memory["preferred_styles"] == ["streetwear"]
    assert memory["preferred_occasions"] == ["casual"]
    assert memory["liked_colors"] == ["purple"]


@pytest.mark.asyncio
async def test_put_feedback_changing_liked_flag_updates_memory(client, make_user, make_wardrobe_item, make_outfit):
    user = await make_user("henry")
    item_id = await make_wardrobe_item(user["user_id"], color="maroon", clothing_type="outerwear")
    outfit_id = await make_outfit(user["user_id"], [item_id])

    create_resp = client.post(
        "/feedback",
        json={"outfit_id": outfit_id, "rating": 3, "liked": None},
        headers=user["headers"],
    )
    feedback_id = create_resp.json()["id"]

    put_resp = client.put(f"/feedback/{feedback_id}", json={"liked": False}, headers=user["headers"])
    assert put_resp.status_code == 200

    memory = client.get("/memory", headers=user["headers"]).json()
    assert memory["disliked_colors"] == ["maroon"]
    assert memory["disliked_item_types"] == ["outerwear"]


@pytest.mark.asyncio
async def test_put_feedback_comment_only_does_not_change_memory(client, make_user, make_wardrobe_item, make_outfit):
    user = await make_user("frank")
    item_id = await make_wardrobe_item(user["user_id"], color="teal", style="formal")
    outfit_id = await make_outfit(user["user_id"], [item_id])

    create_resp = client.post(
        "/feedback",
        json={"outfit_id": outfit_id, "rating": 5, "liked": True},
        headers=user["headers"],
    )
    feedback_id = create_resp.json()["id"]

    memory_after_create = client.get("/memory", headers=user["headers"]).json()
    assert memory_after_create["preferred_styles"] == ["formal"]

    put_resp = client.put(
        f"/feedback/{feedback_id}",
        json={"comment": "great outfit"},
        headers=user["headers"],
    )
    assert put_resp.status_code == 200

    memory_after_comment = client.get("/memory", headers=user["headers"]).json()
    assert memory_after_comment == memory_after_create


@pytest.mark.asyncio
async def test_manual_memory_fields_survive_feedback_learning(client, make_user, make_wardrobe_item, make_outfit):
    user = await make_user("grace")

    client.put(
        "/memory",
        json={
            "notes": "loves minimalist pieces",
            "preferred_fit": "relaxed",
            "communication_style": "concise",
        },
        headers=user["headers"],
    )

    item_id = await make_wardrobe_item(user["user_id"], color="navy", style="casual", occasion="work")
    outfit_id = await make_outfit(user["user_id"], [item_id])

    client.post(
        "/feedback",
        json={"outfit_id": outfit_id, "rating": 5, "liked": True},
        headers=user["headers"],
    )

    memory = client.get("/memory", headers=user["headers"]).json()
    assert memory["notes"] == "loves minimalist pieces"
    assert memory["preferred_fit"] == "relaxed"
    assert memory["communication_style"] == "concise"
    assert memory["preferred_styles"] == ["casual"]
    assert memory["preferred_occasions"] == ["work"]
    assert memory["liked_colors"] == ["navy"]
