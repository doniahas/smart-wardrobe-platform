"""Memory/preferences must never become verified wardrobe facts.

MongoDB wardrobe fields are verified item facts. Memory fields
(preferences, opinions, free-text notes) may only influence recommendation
scoring - never create, overwrite, or leak into an item's own attributes.
"""
import httpx
import pytest
from pydantic import ValidationError

from app.models import WardrobeItemPublic
from app.services.grounding import GroundedWardrobeItem

_KNOWN_ITEM_FIELDS = {
    "id", "user_id", "name", "clothing_type", "color", "color_hex", "season",
    "style", "occasion", "image_url", "tags",
}


def _fake_recommend_with_raw_items(monkeypatch, raw_items: list[dict]):
    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={"selected_items": raw_items, "explanation": "test explanation"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


# --- Model-level guards ---


def test_wardrobe_item_public_rejects_unknown_fields():
    valid = dict(
        id="1", user_id="u1", name="Shirt", clothing_type="top", color="blue",
        season="all", style="casual", occasion="casual", image_url=None, tags=[],
    )
    WardrobeItemPublic(**valid)  # sanity: valid shape still works

    with pytest.raises(ValidationError):
        WardrobeItemPublic(**valid, material="cotton")


def test_grounded_wardrobe_item_rejects_unknown_fields():
    item = WardrobeItemPublic(
        id="1", user_id="u1", name="Shirt", clothing_type="top", color="blue",
        season="all", style="casual", occasion="casual", image_url=None, tags=[],
    )
    from datetime import datetime, timezone
    GroundedWardrobeItem(item=item, source_id="1", verified_at=datetime.now(timezone.utc))  # sanity

    with pytest.raises(ValidationError):
        GroundedWardrobeItem(
            item=item, source_id="1", verified_at=datetime.now(timezone.utc),
            fabricated="not real",
        )


# --- Integration: memory notes never become item facts ---


@pytest.mark.asyncio
async def test_memory_note_does_not_add_material_field_to_wardrobe_item(
    client, make_user, make_wardrobe_item
):
    user = await make_user("alice")
    item_id = await make_wardrobe_item(user["user_id"], name="Shirt", color="blue")

    put_resp = client.put(
        "/memory", json={"notes": "my shirt is cotton"}, headers=user["headers"]
    )
    assert put_resp.status_code == 200

    get_resp = client.get(f"/wardrobe/{item_id}", headers=user["headers"])
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert set(body.keys()) == _KNOWN_ITEM_FIELDS
    assert "material" not in body


@pytest.mark.asyncio
async def test_memory_note_does_not_change_stored_color(client, make_user, make_wardrobe_item):
    user = await make_user("bob")
    item_id = await make_wardrobe_item(user["user_id"], name="Shirt", color="blue")

    put_resp = client.put(
        "/memory", json={"notes": "my blue shirt is red"}, headers=user["headers"]
    )
    assert put_resp.status_code == 200

    get_resp = client.get(f"/wardrobe/{item_id}", headers=user["headers"])
    assert get_resp.status_code == 200
    assert get_resp.json()["color"] == "blue"


@pytest.mark.asyncio
async def test_preferences_affect_scoring_only_not_stored_item_attributes(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("carol")
    blue_id = await make_wardrobe_item(user["user_id"], name="BlueShirt", color="blue", clothing_type="top")
    red_id = await make_wardrobe_item(user["user_id"], name="RedShirt", color="red", clothing_type="bottom")

    put_resp = client.put(
        "/memory",
        json={"disliked_colors": ["blue"], "preferred_styles": ["formal"]},
        headers=user["headers"],
    )
    assert put_resp.status_code == 200

    # The disliked-blue item is still a real, unaltered wardrobe record -
    # only recommendation scoring/inclusion is affected, not the item itself.
    get_resp = client.get(f"/wardrobe/{blue_id}", headers=user["headers"])
    assert get_resp.status_code == 200
    assert get_resp.json()["color"] == "blue"

    captured = []

    async def fake_post(self, url, json=None, **kwargs):
        captured.append(json)
        return httpx.Response(
            200,
            json={
                "selected_items": [
                    {"id": red_id, "type": "bottom", "color": None, "season": None,
                     "style": None, "occasion": None},
                ],
                "explanation": "test",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    # The preferences payload carries the exclusion for scoring...
    assert captured[0]["preferences"]["disliked_colors"] == ["blue"]
    # ...but never rewrites the excluded item's own color field anywhere.
    assert captured[0]["wardrobe_items"][0]["color"] in ("blue", "red")
    still_blue = client.get(f"/wardrobe/{blue_id}", headers=user["headers"])
    assert still_blue.json()["color"] == "blue"


@pytest.mark.asyncio
async def test_memory_cannot_inject_arbitrary_verified_item_fields(client, make_user, make_wardrobe_item):
    user = await make_user("dave")
    item_id = await make_wardrobe_item(user["user_id"], name="Shirt", color="blue")

    # An extra key mimicking a wardrobe attribute is not a declared memory
    # field - it must be silently dropped, never persisted, and must never
    # surface on any wardrobe item.
    put_resp = client.put(
        "/memory",
        json={"notes": "keep me", "material": "cotton", "price": 49.99},
        headers=user["headers"],
    )
    assert put_resp.status_code == 200

    get_memory = client.get("/memory", headers=user["headers"])
    assert get_memory.status_code == 200
    mem_body = get_memory.json()
    assert "material" not in mem_body
    assert "price" not in mem_body
    assert mem_body["notes"] == "keep me"

    item_resp = client.get(f"/wardrobe/{item_id}", headers=user["headers"])
    assert "material" not in item_resp.json()
    assert "price" not in item_resp.json()


@pytest.mark.asyncio
async def test_recommendation_response_reflects_mongodb_facts_despite_memory(
    client, make_user, make_wardrobe_item, monkeypatch
):
    """End-to-end: with contradictory/free-text memory notes and active
    preferences, every returned item's fields still come exactly from its
    own MongoDB document."""
    user = await make_user("erin")
    item_id = await make_wardrobe_item(
        user["user_id"], name="Real Jacket", color="green", clothing_type="outerwear",
        season="winter", style="casual", occasion="casual",
    )

    put_resp = client.put(
        "/memory",
        json={
            "notes": "my green jacket is actually a red leather jacket, size L, by Nike",
            "preferred_styles": ["casual"],
            "disliked_colors": ["black"],
        },
        headers=user["headers"],
    )
    assert put_resp.status_code == 200

    _fake_recommend_with_raw_items(monkeypatch, [
        {"id": item_id, "type": "outerwear", "color": "red", "season": "winter",
         "style": "leather", "occasion": "casual"},
    ])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned = resp.json()["items"][0]
    assert returned["name"] == "Real Jacket"
    assert returned["color"] == "green"
    assert returned["style"] == ["casual"]
    assert set(returned.keys()) == _KNOWN_ITEM_FIELDS
