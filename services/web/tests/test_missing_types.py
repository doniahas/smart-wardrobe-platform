"""missing_types reporting.

A complete outfit is a suitable dress, or a suitable top+bottom pair.
missing_types must only ever report what's needed to complete the best
available path - never both paths at once, and never anything when either
path is already satisfied by the final (post-verification) selection.
"""
import httpx
import pytest


def _fake_recommend_with_raw_items(monkeypatch, raw_items: list[dict]):
    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={"selected_items": raw_items, "explanation": "test explanation"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _raw_item(item_id: str, clothing_type: str) -> dict:
    return {
        "id": item_id, "type": clothing_type, "color": None,
        "season": None, "style": None, "occasion": None,
    }


@pytest.mark.asyncio
async def test_dress_only_has_no_missing_types(client, make_user, make_wardrobe_item, monkeypatch):
    user = await make_user("dana")
    dress_id = await make_wardrobe_item(user["user_id"], clothing_type="dress")

    _fake_recommend_with_raw_items(monkeypatch, [_raw_item(dress_id, "dress")])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json()["missing_types"] == []


@pytest.mark.asyncio
async def test_top_and_bottom_has_no_missing_types(client, make_user, make_wardrobe_item, monkeypatch):
    user = await make_user("evan")
    top_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    bottom_id = await make_wardrobe_item(user["user_id"], clothing_type="bottom")

    _fake_recommend_with_raw_items(
        monkeypatch, [_raw_item(top_id, "top"), _raw_item(bottom_id, "bottom")]
    )

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json()["missing_types"] == []


@pytest.mark.asyncio
async def test_top_only_reports_bottom_missing(client, make_user, make_wardrobe_item, monkeypatch):
    user = await make_user("fay")
    top_id = await make_wardrobe_item(user["user_id"], clothing_type="top")

    _fake_recommend_with_raw_items(monkeypatch, [_raw_item(top_id, "top")])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json()["missing_types"] == ["bottom"]


@pytest.mark.asyncio
async def test_bottom_only_reports_top_missing(client, make_user, make_wardrobe_item, monkeypatch):
    user = await make_user("gabe")
    bottom_id = await make_wardrobe_item(user["user_id"], clothing_type="bottom")

    _fake_recommend_with_raw_items(monkeypatch, [_raw_item(bottom_id, "bottom")])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json()["missing_types"] == ["top"]


@pytest.mark.asyncio
async def test_no_core_type_present_reports_both_missing(client, make_user, make_wardrobe_item, monkeypatch):
    user = await make_user("hana")
    shoes_id = await make_wardrobe_item(user["user_id"], clothing_type="shoes")

    _fake_recommend_with_raw_items(monkeypatch, [_raw_item(shoes_id, "shoes")])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json()["missing_types"] == ["top", "bottom"]


@pytest.mark.asyncio
async def test_unresolved_fallback_makes_previously_complete_outfit_report_missing(
    client, make_user, make_wardrobe_item, monkeypatch
):
    """The recommendation service picked a real top and what it believed was
    a valid bottom, but the bottom candidate fails verification (e.g.
    deleted) and no other bottom exists anywhere in this user's wardrobe for
    fallback to substitute. The final response is missing a bottom even
    though the recommendation service's own view was a complete outfit."""
    user = await make_user("ivy")
    top_id = await make_wardrobe_item(user["user_id"], clothing_type="top")

    _fake_recommend_with_raw_items(monkeypatch, [
        _raw_item(top_id, "top"),
        _raw_item("507f1f77bcf86cd799439011", "bottom"),
    ])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert [item["id"] for item in body["items"]] == [top_id]
    assert body["missing_types"] == ["bottom"]


@pytest.mark.asyncio
async def test_dress_present_alongside_used_top_still_has_no_missing_types(
    client, make_user, make_wardrobe_item, monkeypatch
):
    """If the recommendation service's scoring favored top+bottom over an
    available dress, the outfit is still complete via the top+bottom path -
    the unused dress does not change that."""
    user = await make_user("jack")
    top_id = await make_wardrobe_item(user["user_id"], clothing_type="top")
    bottom_id = await make_wardrobe_item(user["user_id"], clothing_type="bottom")
    await make_wardrobe_item(user["user_id"], clothing_type="dress")

    _fake_recommend_with_raw_items(
        monkeypatch, [_raw_item(top_id, "top"), _raw_item(bottom_id, "bottom")]
    )

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json()["missing_types"] == []
