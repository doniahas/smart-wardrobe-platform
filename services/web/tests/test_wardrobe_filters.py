import pytest


@pytest.mark.asyncio
async def test_list_wardrobe_filters_by_season(client, make_user, make_wardrobe_item):
    user = await make_user("filteruser")
    await make_wardrobe_item(user["user_id"], name="Summer top", season="summer")
    await make_wardrobe_item(user["user_id"], name="Winter coat", season="winter")

    resp = client.get("/wardrobe", params={"season": "summer"}, headers=user["headers"])

    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert names == ["Summer top"]


@pytest.mark.asyncio
async def test_list_wardrobe_filters_combine_with_user_scope(
    client, make_user, make_wardrobe_item
):
    """A filter that matches another user's item must never leak into this
    user's results - filtering narrows within the user, it never widens
    beyond it."""
    alice = await make_user("alice_filter")
    bob = await make_user("bob_filter")

    await make_wardrobe_item(alice["user_id"], name="Alice red top", color="red")
    await make_wardrobe_item(bob["user_id"], name="Bob red top", color="red")

    resp = client.get("/wardrobe", params={"color": "red"}, headers=alice["headers"])

    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert names == ["Alice red top"]


@pytest.mark.asyncio
async def test_list_wardrobe_no_filters_returns_all_own_items(
    client, make_user, make_wardrobe_item
):
    user = await make_user("nofilteruser")
    await make_wardrobe_item(user["user_id"], name="Item A", style="casual")
    await make_wardrobe_item(user["user_id"], name="Item B", style="formal")

    resp = client.get("/wardrobe", headers=user["headers"])

    assert resp.status_code == 200
    names = {item["name"] for item in resp.json()}
    assert names == {"Item A", "Item B"}


def test_list_wardrobe_requires_auth(client):
    resp = client.get("/wardrobe")
    assert resp.status_code == 401