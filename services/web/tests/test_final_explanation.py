"""The final `explanation` string returned to the client must be built
only from verified, final data (grounded/fallback WardrobeItemPublic items)
- never from the recommendation service's own (untrusted) `explanation`
field or any attribute it echoed back. See
app/routers/recommendations.py::_build_final_explanation.

The explanation describes only what was selected. It never states what's
missing - that's the dedicated job of the `missing_types` field (rendered
by the client in its own #rec-missing UI element), so stating it in the
explanation too would duplicate the same warning in two places.
"""
import httpx
import pytest

FAKE_ID_1 = "507f1f77bcf86cd799439011"
FAKE_ID_2 = "507f1f77bcf86cd799439012"


def _raw_item(item_id: str, clothing_type: str, **overrides) -> dict:
    entry = {
        "id": item_id, "type": clothing_type, "color": None,
        "season": None, "style": None, "occasion": None,
    }
    entry.update(overrides)
    return entry


def _fake_recommend(monkeypatch, raw_items: list[dict], explanation: str):
    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={"selected_items": raw_items, "explanation": explanation},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


# --- A. Untrusted explanation is discarded ---


@pytest.mark.asyncio
async def test_untrusted_recommendation_service_explanation_is_discarded(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("alice_expl")
    item_id = await make_wardrobe_item(user["user_id"], name="Plain Top", clothing_type="top")

    _fake_recommend(
        monkeypatch,
        [_raw_item(item_id, "top")],
        explanation="Selected Invented Purple Leather Pants and a Gold Shirt.",
    )

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    explanation = resp.json()["explanation"]
    assert "Invented Purple Leather Pants" not in explanation
    assert "Gold Shirt" not in explanation


# --- B. Explanation uses real MongoDB item names ---


@pytest.mark.asyncio
async def test_explanation_uses_real_wardrobe_name_not_echoed_attributes(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("bob_expl")
    item_id = await make_wardrobe_item(
        user["user_id"], name="Real Blue Shirt", clothing_type="top", color="blue"
    )

    _fake_recommend(
        monkeypatch,
        [_raw_item(item_id, "shoes", color="invented-color", season="invented-season",
                    style="invented-style", occasion="invented-occasion")],
        explanation="Selected an invented-style invented-color item.",
    )

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    explanation = resp.json()["explanation"]
    assert "Real Blue Shirt" in explanation
    assert "invented" not in explanation.lower()


# --- C. Rejected item is not mentioned ---


@pytest.mark.asyncio
async def test_rejected_candidate_not_mentioned_missing_bottom_reported(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("carol_expl")
    top_id = await make_wardrobe_item(user["user_id"], name="Good Top", clothing_type="top")
    # No other wardrobe item exists, so the nonexistent bottom candidate has
    # no possible verified fallback replacement.

    _fake_recommend(
        monkeypatch,
        [_raw_item(top_id, "top"), _raw_item(FAKE_ID_1, "bottom")],
        explanation="Selected Good Top and Fabricated Bottom from your wardrobe.",
    )

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    returned_ids = [item["id"] for item in body["items"]]
    assert returned_ids == [top_id]
    assert body["missing_types"] == ["bottom"]

    explanation = body["explanation"]
    assert "Good Top" in explanation
    # The explanation describes only the final selected items - the missing
    # bottom is reported solely via missing_types (asserted above), not
    # restated inside the explanation text.
    assert "not found" not in explanation.lower()
    assert "Fabricated Bottom" not in explanation
    assert FAKE_ID_1 not in explanation
    # Must not claim a bottom was actually selected.
    assert "selected good top and" not in explanation.lower()


# --- D. Verified fallback is reflected accurately ---


@pytest.mark.asyncio
async def test_verified_fallback_reflected_with_real_name_not_failed_candidate(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("dave_expl")
    backup_id = await make_wardrobe_item(
        user["user_id"], name="Reliable Backup Top", clothing_type="top"
    )

    _fake_recommend(
        monkeypatch,
        [_raw_item(FAKE_ID_1, "top")],
        explanation="Selected a Nonexistent Designer Jacket from your wardrobe.",
    )

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    returned_ids = [item["id"] for item in body["items"]]
    assert returned_ids == [backup_id]

    explanation = body["explanation"]
    assert "Reliable Backup Top" in explanation
    assert "Nonexistent Designer Jacket" not in explanation
    assert FAKE_ID_1 not in explanation


# --- E. Complete top-and-bottom outfit ---


@pytest.mark.asyncio
async def test_complete_top_and_bottom_outfit_explanation(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("erin_expl")
    top_id = await make_wardrobe_item(user["user_id"], name="Blue Shirt", clothing_type="top")
    bottom_id = await make_wardrobe_item(user["user_id"], name="Black Pants", clothing_type="bottom")

    _fake_recommend(
        monkeypatch,
        [_raw_item(top_id, "top"), _raw_item(bottom_id, "bottom")],
        explanation="whatever the recommendation service made up",
    )

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["missing_types"] == []

    explanation = body["explanation"]
    assert "Blue Shirt" in explanation
    assert "Black Pants" in explanation
    assert "not found" not in explanation.lower()
    assert "missing" not in explanation.lower()


# --- F. Complete dress outfit ---


@pytest.mark.asyncio
async def test_complete_dress_outfit_explanation(client, make_user, make_wardrobe_item, monkeypatch):
    user = await make_user("frank_expl")
    dress_id = await make_wardrobe_item(user["user_id"], name="Summer Dress", clothing_type="dress")

    _fake_recommend(
        monkeypatch,
        [_raw_item(dress_id, "dress")],
        explanation="an invented dress description",
    )

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["missing_types"] == []

    explanation = body["explanation"]
    assert "Summer Dress" in explanation
    assert "top" not in explanation.lower()
    assert "bottom" not in explanation.lower()


# --- G. Controlled empty final response ---


@pytest.mark.asyncio
async def test_empty_final_selection_has_controlled_truthful_explanation(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("grace_expl")
    # Exists, but matches none of the requested criteria - no fallback found.
    await make_wardrobe_item(
        user["user_id"], name="Irrelevant Item", clothing_type="shoes",
        season="summer", occasion="casual", style="casual",
    )

    _fake_recommend(
        monkeypatch,
        [_raw_item(FAKE_ID_1, "top")],
        explanation="Selected a Fabricated Jacket from your wardrobe.",
    )

    resp = client.post(
        "/recommendations",
        json={"occasion": "formal", "season": "winter", "style": "formal"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["missing_types"] == ["top", "bottom"]

    explanation = body["explanation"]
    assert "Fabricated Jacket" not in explanation
    assert "error" not in explanation.lower()
    assert "exception" not in explanation.lower()
    assert "verif" not in explanation.lower()
    assert "grounding" not in explanation.lower()
    assert "no suitable items" in explanation.lower()
    # Missing categories are reported via missing_types only, not restated
    # inside the neutral empty-selection explanation.
    assert "top" not in explanation.lower()
    assert "bottom" not in explanation.lower()


# --- H. Explanation and missing_types agree (regression) ---


@pytest.mark.asyncio
async def test_missing_top_only_message_matches_missing_types(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("henry_expl")
    bottom_id = await make_wardrobe_item(user["user_id"], name="Denim Bottom", clothing_type="bottom")

    _fake_recommend(monkeypatch, [_raw_item(bottom_id, "bottom")], explanation="x")

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["missing_types"] == ["top"]
    explanation = body["explanation"]
    # The explanation names what was selected (the item, whose own name
    # happens to contain the word "bottom") but never restates the missing
    # category - that's missing_types' job alone.
    assert "Denim Bottom" in explanation
    assert "not found" not in explanation.lower()


@pytest.mark.asyncio
async def test_missing_bottom_only_message_matches_missing_types(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("ivy_expl")
    top_id = await make_wardrobe_item(user["user_id"], name="Cotton Top", clothing_type="top")

    _fake_recommend(monkeypatch, [_raw_item(top_id, "top")], explanation="x")

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["missing_types"] == ["bottom"]
    explanation = body["explanation"]
    assert "Cotton Top" in explanation
    assert "not found" not in explanation.lower()


@pytest.mark.asyncio
async def test_missing_both_message_matches_missing_types(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("jack_expl")
    # Exists, but matches none of the requested criteria below, so it can
    # never be picked as a verified fallback replacement.
    await make_wardrobe_item(
        user["user_id"], name="Random Shoes", clothing_type="shoes",
        season="summer", occasion="casual", style="casual",
    )

    _fake_recommend(
        monkeypatch,
        [_raw_item(FAKE_ID_1, "top"), _raw_item(FAKE_ID_2, "bottom")],
        explanation="x",
    )

    resp = client.post(
        "/recommendations",
        json={"occasion": "formal", "season": "winter", "style": "formal"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["missing_types"] == ["top", "bottom"]
    explanation = body["explanation"].lower()
    assert "no suitable items" in explanation
    assert "not found" not in explanation
    assert "top" not in explanation
    assert "bottom" not in explanation


@pytest.mark.asyncio
async def test_partial_outfit_explanation_names_only_selected_item_no_duplicate_warning(
    client, make_user, make_wardrobe_item, monkeypatch
):
    """Regression for the double-warning UI bug: for a partial outfit, the
    explanation must describe only the final selected item(s) - the missing
    category is reported exactly once, via missing_types, which the client
    renders in its own dedicated #rec-missing element. The explanation must
    not restate it."""
    user = await make_user("liam_expl")
    top_id = await make_wardrobe_item(
        user["user_id"], name="Rust 100% Cotton Regular Fit Tshirt", clothing_type="top"
    )

    _fake_recommend(monkeypatch, [_raw_item(top_id, "top")], explanation="x")

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["missing_types"] == ["bottom"]
    assert body["explanation"] == "Selected Rust 100% Cotton Regular Fit Tshirt from your wardrobe."


@pytest.mark.asyncio
async def test_no_missing_types_produces_no_missing_statement(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("kate_expl")
    dress_id = await make_wardrobe_item(user["user_id"], name="Evening Gown", clothing_type="dress")

    _fake_recommend(monkeypatch, [_raw_item(dress_id, "dress")], explanation="x")

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["missing_types"] == []
    assert "not found" not in body["explanation"].lower()
