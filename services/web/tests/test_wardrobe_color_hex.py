"""Color picker Pass: color stays the semantic category (unchanged,
free-text); color_hex is a new, purely optional exact-shade field that
only ever comes from the frontend's curated swatch grid or the AI
suggestion's category-default lookup - see services/wardrobe_ai.py and
static/app.js. Covers the API/model plumbing (create, update, list/get)
and backward compatibility with items that predate this field.
"""
import pytest


def _create_item(client, user, **overrides):
    data = {
        "name": "Item",
        "clothing_type": "top",
        "color": "olive",
        "season": "all",
        "style": "casual",
        "occasion": "casual",
    }
    data.update(overrides)
    return client.post("/wardrobe", data=data, headers=user["headers"])


@pytest.mark.asyncio
async def test_create_item_with_color_hex_is_stored_and_returned(client, make_user):
    user = await make_user("colorhex_create")
    resp = _create_item(client, user, color="olive", color_hex="#6B7A32")

    assert resp.status_code == 201
    body = resp.json()
    assert body["color"] == "olive"
    assert body["color_hex"] == "#6b7a32"


@pytest.mark.asyncio
async def test_create_item_normalizes_hex_without_leading_hash(client, make_user):
    user = await make_user("colorhex_nohash")
    resp = _create_item(client, user, color_hex="6B7A32")

    assert resp.status_code == 201
    assert resp.json()["color_hex"] == "#6b7a32"


@pytest.mark.asyncio
async def test_create_item_without_color_hex_leaves_it_null(client, make_user):
    """color_hex is optional - omitting it entirely (e.g. a client that
    hasn't adopted the picker) must not be an error."""
    user = await make_user("colorhex_omitted")
    resp = _create_item(client, user)

    assert resp.status_code == 201
    assert resp.json()["color_hex"] is None


@pytest.mark.asyncio
async def test_create_item_rejects_malformed_color_hex(client, make_user):
    user = await make_user("colorhex_malformed")
    resp = _create_item(client, user, color_hex="not-a-color")

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_item_sets_color_hex(client, make_user, make_wardrobe_item):
    user = await make_user("colorhex_update")
    item_id = await make_wardrobe_item(user["user_id"], color="navy")

    resp = client.put(
        f"/wardrobe/{item_id}",
        data={"color": "navy", "color_hex": "#14213D"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["color_hex"] == "#14213d"


@pytest.mark.asyncio
async def test_update_item_rejects_malformed_color_hex(client, make_user, make_wardrobe_item):
    user = await make_user("colorhex_update_bad")
    item_id = await make_wardrobe_item(user["user_id"])

    resp = client.put(
        f"/wardrobe/{item_id}",
        data={"color_hex": "purple"},
        headers=user["headers"],
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_legacy_item_without_color_hex_field_loads_fine(client, make_user, make_wardrobe_item):
    """A wardrobe document from before this field existed (make_wardrobe_item's
    fixture doc has no color_hex key at all, exactly like a pre-Pass-3
    saved item) must still load through GET (list and single) without a
    validation error, with color_hex simply absent/null."""
    user = await make_user("colorhex_legacy")
    item_id = await make_wardrobe_item(user["user_id"], name="Legacy shirt", color="olive green")

    list_resp = client.get("/wardrobe", headers=user["headers"])
    assert list_resp.status_code == 200
    listed = next(item for item in list_resp.json() if item["id"] == item_id)
    assert listed["color"] == "olive green"
    assert listed["color_hex"] is None

    get_resp = client.get(f"/wardrobe/{item_id}", headers=user["headers"])
    assert get_resp.status_code == 200
    assert get_resp.json()["color_hex"] is None
