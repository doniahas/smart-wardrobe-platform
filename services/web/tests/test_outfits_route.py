import pytest


@pytest.mark.asyncio
async def test_create_outfit_success(client, make_user, make_wardrobe_item):
    user = await make_user("outfituser")
    top_id = await make_wardrobe_item(user["user_id"], name="Top", clothing_type="top")
    bottom_id = await make_wardrobe_item(user["user_id"], name="Bottom", clothing_type="bottom")

    resp = client.post(
        "/outfits",
        json={"item_ids": [top_id, bottom_id], "name": "Everyday look"},
        headers=user["headers"],
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Everyday look"
    assert {item["id"] for item in body["items"]} == {top_id, bottom_id}


def test_create_outfit_requires_auth(client):
    resp = client.post("/outfits", json={"item_ids": ["000000000000000000000000"]})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_outfit_rejects_empty_item_ids(client, make_user):
    user = await make_user("emptyitemsuser")

    resp = client.post("/outfits", json={"item_ids": []}, headers=user["headers"])

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_outfit_rejects_invalid_item_id_format(client, make_user):
    user = await make_user("badidformatuser")

    resp = client.post("/outfits", json={"item_ids": ["not-a-valid-id"]}, headers=user["headers"])

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid item id"


@pytest.mark.asyncio
async def test_create_outfit_rejects_duplicate_item_ids(client, make_user, make_wardrobe_item):
    user = await make_user("dupuser")
    top_id = await make_wardrobe_item(user["user_id"], clothing_type="top")

    resp = client.post(
        "/outfits", json={"item_ids": [top_id, top_id]}, headers=user["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Duplicate items are not allowed in an outfit."


@pytest.mark.asyncio
async def test_create_outfit_rejects_item_not_owned(client, make_user, make_wardrobe_item):
    owner = await make_user("owneruser")
    other = await make_user("otheruser")
    item_id = await make_wardrobe_item(owner["user_id"], clothing_type="top")

    resp = client.post("/outfits", json={"item_ids": [item_id]}, headers=other["headers"])

    assert resp.status_code == 400
    assert resp.json()["detail"] == "One or more items do not exist or do not belong to you"


@pytest.mark.asyncio
async def test_create_outfit_rejects_item_that_does_not_exist(client, make_user):
    user = await make_user("nonexistentitemuser")

    resp = client.post(
        "/outfits", json={"item_ids": ["000000000000000000000000"]}, headers=user["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "One or more items do not exist or do not belong to you"


@pytest.mark.asyncio
async def test_create_outfit_rejects_second_bottom(client, make_user, make_wardrobe_item):
    user = await make_user("secondbottomuser")
    bottom1 = await make_wardrobe_item(user["user_id"], clothing_type="bottom")
    bottom2 = await make_wardrobe_item(user["user_id"], clothing_type="bottom")

    resp = client.post(
        "/outfits", json={"item_ids": [bottom1, bottom2]}, headers=user["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Only one bottom is allowed per outfit."


@pytest.mark.asyncio
async def test_create_outfit_rejects_second_shoes(client, make_user, make_wardrobe_item):
    user = await make_user("secondshoesuser")
    shoes1 = await make_wardrobe_item(user["user_id"], clothing_type="shoes")
    shoes2 = await make_wardrobe_item(user["user_id"], clothing_type="shoes")

    resp = client.post(
        "/outfits", json={"item_ids": [shoes1, shoes2]}, headers=user["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Only one pair of shoes is allowed per outfit."


@pytest.mark.asyncio
async def test_create_outfit_rejects_second_dress(client, make_user, make_wardrobe_item):
    user = await make_user("seconddressuser")
    dress1 = await make_wardrobe_item(user["user_id"], clothing_type="dress")
    dress2 = await make_wardrobe_item(user["user_id"], clothing_type="dress")

    resp = client.post(
        "/outfits", json={"item_ids": [dress1, dress2]}, headers=user["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Only one dress is allowed per outfit."


@pytest.mark.asyncio
async def test_create_outfit_rejects_dress_and_bottom_together(client, make_user, make_wardrobe_item):
    user = await make_user("dressbottomuser")
    dress_id = await make_wardrobe_item(user["user_id"], clothing_type="dress")
    bottom_id = await make_wardrobe_item(user["user_id"], clothing_type="bottom")

    resp = client.post(
        "/outfits", json={"item_ids": [dress_id, bottom_id]}, headers=user["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "An outfit cannot include both a dress and a bottom."


@pytest.mark.asyncio
async def test_create_outfit_rejects_dress_and_top_together(client, make_user, make_wardrobe_item):
    user = await make_user("dresstopuser")
    dress_id = await make_wardrobe_item(user["user_id"], clothing_type="dress")
    top_id = await make_wardrobe_item(user["user_id"], clothing_type="top")

    resp = client.post(
        "/outfits", json={"item_ids": [dress_id, top_id]}, headers=user["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "An outfit cannot include both a dress and a top."


@pytest.mark.asyncio
async def test_create_outfit_rejects_dress_and_multiple_tops_together(
    client, make_user, make_wardrobe_item
):
    user = await make_user("dressmultitopuser")
    dress_id = await make_wardrobe_item(user["user_id"], clothing_type="dress")
    top1 = await make_wardrobe_item(user["user_id"], clothing_type="top")
    top2 = await make_wardrobe_item(user["user_id"], clothing_type="top")

    resp = client.post(
        "/outfits", json={"item_ids": [dress_id, top1, top2]}, headers=user["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "An outfit cannot include both a dress and a top."


@pytest.mark.asyncio
async def test_create_outfit_rejects_dress_top_and_bottom_together(
    client, make_user, make_wardrobe_item
):
    """When a dress conflicts with both a top and a bottom at once, the
    top violation is reported first (checked first in
    _validate_outfit_items) - a stable, documented precedence rather than
    an arbitrary one."""
    user = await make_user("dresstopbottomuser")
    dress_id = await make_wardrobe_item(user["user_id"], clothing_type="dress")
    top_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    bottom_id = await make_wardrobe_item(user["user_id"], clothing_type="bottom")

    resp = client.post(
        "/outfits", json={"item_ids": [dress_id, top_id, bottom_id]}, headers=user["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "An outfit cannot include both a dress and a top."


@pytest.mark.asyncio
async def test_create_outfit_allows_dress_alone(client, make_user, make_wardrobe_item):
    user = await make_user("dressaloneuser")
    dress_id = await make_wardrobe_item(user["user_id"], clothing_type="dress")

    resp = client.post("/outfits", json={"item_ids": [dress_id]}, headers=user["headers"])

    assert resp.status_code == 201
    assert {item["id"] for item in resp.json()["items"]} == {dress_id}


@pytest.mark.asyncio
async def test_create_outfit_allows_dress_with_shoes_and_accessory(
    client, make_user, make_wardrobe_item
):
    """A dress is only exclusive with tops/bottom - shoes and accessories
    are unaffected and may still be combined with it."""
    user = await make_user("dressshoesaccessoryuser")
    dress_id = await make_wardrobe_item(user["user_id"], clothing_type="dress")
    shoes_id = await make_wardrobe_item(user["user_id"], clothing_type="shoes")
    accessory_id = await make_wardrobe_item(user["user_id"], clothing_type="accessory")

    resp = client.post(
        "/outfits",
        json={"item_ids": [dress_id, shoes_id, accessory_id]},
        headers=user["headers"],
    )

    assert resp.status_code == 201
    assert {item["id"] for item in resp.json()["items"]} == {dress_id, shoes_id, accessory_id}


@pytest.mark.asyncio
async def test_create_outfit_allows_multiple_tops(client, make_user, make_wardrobe_item):
    user = await make_user("multitopuser")
    top1 = await make_wardrobe_item(user["user_id"], clothing_type="top")
    top2 = await make_wardrobe_item(user["user_id"], clothing_type="top")

    resp = client.post("/outfits", json={"item_ids": [top1, top2]}, headers=user["headers"])

    assert resp.status_code == 201
    assert {item["id"] for item in resp.json()["items"]} == {top1, top2}


@pytest.mark.asyncio
async def test_create_outfit_allows_multiple_outerwear(client, make_user, make_wardrobe_item):
    user = await make_user("multiouterwearuser")
    jacket = await make_wardrobe_item(user["user_id"], clothing_type="outerwear")
    scarf = await make_wardrobe_item(user["user_id"], clothing_type="outerwear")

    resp = client.post("/outfits", json={"item_ids": [jacket, scarf]}, headers=user["headers"])

    assert resp.status_code == 201
    assert {item["id"] for item in resp.json()["items"]} == {jacket, scarf}


@pytest.mark.asyncio
async def test_create_outfit_allows_multiple_accessories(client, make_user, make_wardrobe_item):
    user = await make_user("multiaccessoryuser")
    belt = await make_wardrobe_item(user["user_id"], clothing_type="accessory")
    hat = await make_wardrobe_item(user["user_id"], clothing_type="accessory")

    resp = client.post("/outfits", json={"item_ids": [belt, hat]}, headers=user["headers"])

    assert resp.status_code == 201
    assert {item["id"] for item in resp.json()["items"]} == {belt, hat}


@pytest.mark.asyncio
async def test_list_outfits_scoped_to_user(client, make_user, make_wardrobe_item, make_outfit):
    alice = await make_user("alice_outfits")
    bob = await make_user("bob_outfits")
    alice_item = await make_wardrobe_item(alice["user_id"], clothing_type="top")
    bob_item = await make_wardrobe_item(bob["user_id"], clothing_type="top")
    await make_outfit(alice["user_id"], [alice_item], name="Alice's outfit")
    await make_outfit(bob["user_id"], [bob_item], name="Bob's outfit")

    resp = client.get("/outfits", headers=alice["headers"])

    assert resp.status_code == 200
    names = [o["name"] for o in resp.json()]
    assert names == ["Alice's outfit"]


@pytest.mark.asyncio
async def test_get_outfit_not_found(client, make_user):
    user = await make_user("getnotfounduser")
    resp = client.get("/outfits/000000000000000000000000", headers=user["headers"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_outfit_not_owned(client, make_user, make_wardrobe_item, make_outfit):
    owner = await make_user("getowneruser")
    other = await make_user("getotheruser")
    item_id = await make_wardrobe_item(owner["user_id"], clothing_type="top")
    outfit_id = await make_outfit(owner["user_id"], [item_id])

    resp = client.get(f"/outfits/{outfit_id}", headers=other["headers"])

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_outfit_updates_name_only_leaves_items_unchanged(
    client, make_user, make_wardrobe_item, make_outfit
):
    user = await make_user("patchnameuser")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    outfit_id = await make_outfit(user["user_id"], [item_id], name="Old name", occasion="casual")

    resp = client.patch(
        f"/outfits/{outfit_id}", json={"name": "New name"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "New name"
    assert body["occasion"] == "casual"
    assert body["item_ids"] == [item_id]


@pytest.mark.asyncio
async def test_patch_outfit_updates_item_ids(client, make_user, make_wardrobe_item, make_outfit):
    user = await make_user("patchitemsuser")
    old_item = await make_wardrobe_item(user["user_id"], clothing_type="top")
    new_item = await make_wardrobe_item(user["user_id"], clothing_type="top")
    outfit_id = await make_outfit(user["user_id"], [old_item])

    resp = client.patch(
        f"/outfits/{outfit_id}", json={"item_ids": [new_item]}, headers=user["headers"]
    )

    assert resp.status_code == 200
    assert resp.json()["item_ids"] == [new_item]


def test_patch_outfit_requires_auth(client):
    resp = client.patch("/outfits/000000000000000000000000", json={"name": "x"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_outfit_not_found(client, make_user):
    user = await make_user("patchnotfounduser")
    resp = client.patch(
        "/outfits/000000000000000000000000", json={"name": "x"}, headers=user["headers"]
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_outfit_not_owned(client, make_user, make_wardrobe_item, make_outfit):
    owner = await make_user("patchowneruser")
    other = await make_user("patchotheruser")
    item_id = await make_wardrobe_item(owner["user_id"], clothing_type="top")
    outfit_id = await make_outfit(owner["user_id"], [item_id])

    resp = client.patch(
        f"/outfits/{outfit_id}", json={"name": "hijacked"}, headers=other["headers"]
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_outfit_rejects_empty_body(client, make_user, make_wardrobe_item, make_outfit):
    user = await make_user("patchemptybodyuser")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    outfit_id = await make_outfit(user["user_id"], [item_id])

    resp = client.patch(f"/outfits/{outfit_id}", json={}, headers=user["headers"])

    assert resp.status_code == 400
    assert resp.json()["detail"] == "No fields to update"


@pytest.mark.asyncio
async def test_patch_outfit_rejects_empty_item_ids(
    client, make_user, make_wardrobe_item, make_outfit
):
    user = await make_user("patchemptyitemsuser")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    outfit_id = await make_outfit(user["user_id"], [item_id])

    resp = client.patch(f"/outfits/{outfit_id}", json={"item_ids": []}, headers=user["headers"])

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_outfit_rejects_null_item_ids(
    client, make_user, make_wardrobe_item, make_outfit
):
    user = await make_user("patchnullitemsuser")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    outfit_id = await make_outfit(user["user_id"], [item_id])

    resp = client.patch(f"/outfits/{outfit_id}", json={"item_ids": None}, headers=user["headers"])

    assert resp.status_code == 422
    assert resp.json()["detail"] == "item_ids cannot be null"


@pytest.mark.asyncio
async def test_patch_outfit_rejects_null_name(client, make_user, make_wardrobe_item, make_outfit):
    user = await make_user("patchnullnameuser")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    outfit_id = await make_outfit(user["user_id"], [item_id], name="Keep me")

    resp = client.patch(f"/outfits/{outfit_id}", json={"name": None}, headers=user["headers"])

    assert resp.status_code == 422
    assert resp.json()["detail"] == "name cannot be null"


@pytest.mark.asyncio
async def test_patch_outfit_reuses_duplicate_item_ids_validation(
    client, make_user, make_wardrobe_item, make_outfit
):
    user = await make_user("patchdupuser")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    outfit_id = await make_outfit(user["user_id"], [item_id])

    resp = client.patch(
        f"/outfits/{outfit_id}", json={"item_ids": [item_id, item_id]}, headers=user["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Duplicate items are not allowed in an outfit."


@pytest.mark.asyncio
async def test_patch_outfit_reuses_not_owned_item_validation(
    client, make_user, make_wardrobe_item, make_outfit
):
    owner = await make_user("patchnotownedowneruser")
    other = await make_user("patchnotownedotheruser")
    owner_item = await make_wardrobe_item(owner["user_id"], clothing_type="top")
    other_item = await make_wardrobe_item(other["user_id"], clothing_type="top")
    outfit_id = await make_outfit(owner["user_id"], [owner_item])

    resp = client.patch(
        f"/outfits/{outfit_id}", json={"item_ids": [other_item]}, headers=owner["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "One or more items do not exist or do not belong to you"


@pytest.mark.asyncio
async def test_patch_outfit_reuses_second_bottom_validation(
    client, make_user, make_wardrobe_item, make_outfit
):
    user = await make_user("patchsecondbottomuser")
    top_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    bottom1 = await make_wardrobe_item(user["user_id"], clothing_type="bottom")
    bottom2 = await make_wardrobe_item(user["user_id"], clothing_type="bottom")
    outfit_id = await make_outfit(user["user_id"], [top_id])

    resp = client.patch(
        f"/outfits/{outfit_id}", json={"item_ids": [bottom1, bottom2]}, headers=user["headers"]
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Only one bottom is allowed per outfit."


@pytest.mark.asyncio
async def test_patch_outfit_reuses_dress_and_bottom_validation(
    client, make_user, make_wardrobe_item, make_outfit
):
    user = await make_user("patchdressbottomuser")
    top_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    dress_id = await make_wardrobe_item(user["user_id"], clothing_type="dress")
    bottom_id = await make_wardrobe_item(user["user_id"], clothing_type="bottom")
    outfit_id = await make_outfit(user["user_id"], [top_id])

    resp = client.patch(
        f"/outfits/{outfit_id}",
        json={"item_ids": [dress_id, bottom_id]},
        headers=user["headers"],
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "An outfit cannot include both a dress and a bottom."


@pytest.mark.asyncio
async def test_patch_outfit_reuses_dress_and_top_validation(
    client, make_user, make_wardrobe_item, make_outfit
):
    user = await make_user("patchdresstopuser")
    bottom_id = await make_wardrobe_item(user["user_id"], clothing_type="bottom")
    dress_id = await make_wardrobe_item(user["user_id"], clothing_type="dress")
    top_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    outfit_id = await make_outfit(user["user_id"], [bottom_id])

    resp = client.patch(
        f"/outfits/{outfit_id}",
        json={"item_ids": [dress_id, top_id]},
        headers=user["headers"],
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "An outfit cannot include both a dress and a top."


@pytest.mark.asyncio
async def test_patch_outfit_partial_update_does_not_touch_other_fields(
    client, make_user, make_wardrobe_item, make_outfit
):
    user = await make_user("patchpartialuser")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    outfit_id = await make_outfit(
        user["user_id"], [item_id], name="Original", occasion="casual",
        season="summer", style="casual",
    )

    resp = client.patch(
        f"/outfits/{outfit_id}", json={"season": "winter"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["season"] == "winter"
    assert body["name"] == "Original"
    assert body["occasion"] == "casual"
    assert body["style"] == "casual"
    assert body["item_ids"] == [item_id]


@pytest.mark.asyncio
async def test_delete_outfit_success_cascades_feedback(
    client, make_user, make_wardrobe_item, make_outfit, db
):
    user = await make_user("deleteuser")
    item_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    outfit_id = await make_outfit(user["user_id"], [item_id])
    await db.feedback.insert_one(
        {"user_id": user["user_id"], "outfit_id": outfit_id, "rating": 5}
    )

    resp = client.delete(f"/outfits/{outfit_id}", headers=user["headers"])

    assert resp.status_code == 204
    remaining_feedback = await db.feedback.count_documents({"outfit_id": outfit_id})
    assert remaining_feedback == 0


@pytest.mark.asyncio
async def test_delete_outfit_not_found(client, make_user):
    user = await make_user("deletenotfounduser")
    resp = client.delete("/outfits/000000000000000000000000", headers=user["headers"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_outfit_not_owned(client, make_user, make_wardrobe_item, make_outfit):
    owner = await make_user("deleteowneruser")
    other = await make_user("deleteotheruser")
    item_id = await make_wardrobe_item(owner["user_id"], clothing_type="top")
    outfit_id = await make_outfit(owner["user_id"], [item_id])

    resp = client.delete(f"/outfits/{outfit_id}", headers=other["headers"])

    assert resp.status_code == 404
