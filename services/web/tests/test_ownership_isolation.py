"""Proves cross-user ownership isolation for wardrobe items and
feedback. Every route below already scopes its query by
(_id, user_id); these tests exist to lock that behavior in, not to change
it.

Each resource also gets one "identical response" assertion: a request for
another user's real resource and a request for a resource id that was never
created at all must be indistinguishable (same status code, same body) -
otherwise the response itself would leak whether a given id exists.
"""
import pytest
from bson import ObjectId

_MISSING_OID = "000000000000000000000000"


# --- wardrobe items ---


@pytest.mark.asyncio
async def test_get_wardrobe_item_not_owned_returns_404(client, make_user, make_wardrobe_item):
    owner = await make_user("wardrobeowner_get")
    intruder = await make_user("wardrobeintruder_get")
    item_id = await make_wardrobe_item(owner["user_id"])

    resp = client.get(f"/wardrobe/{item_id}", headers=intruder["headers"])

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Item not found"


@pytest.mark.asyncio
async def test_get_wardrobe_item_not_owned_matches_nonexistent_item_response(
    client, make_user, make_wardrobe_item
):
    owner = await make_user("wardrobeowner_get2")
    intruder = await make_user("wardrobeintruder_get2")
    item_id = await make_wardrobe_item(owner["user_id"])

    cross_user_resp = client.get(f"/wardrobe/{item_id}", headers=intruder["headers"])
    missing_resp = client.get(f"/wardrobe/{_MISSING_OID}", headers=intruder["headers"])

    assert cross_user_resp.status_code == missing_resp.status_code == 404
    assert cross_user_resp.json() == missing_resp.json()


@pytest.mark.asyncio
async def test_update_wardrobe_item_not_owned_returns_404(client, make_user, make_wardrobe_item, db):
    owner = await make_user("wardrobeowner_put")
    intruder = await make_user("wardrobeintruder_put")
    item_id = await make_wardrobe_item(owner["user_id"])

    resp = client.put(
        f"/wardrobe/{item_id}", data={"name": "Hijacked"}, headers=intruder["headers"]
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Item not found"
    # The owner's item must be genuinely untouched, not just the response withheld.
    unchanged = await db.wardrobe.find_one({"_id": ObjectId(item_id)})
    assert unchanged["name"] == "Item"


@pytest.mark.asyncio
async def test_delete_wardrobe_item_not_owned_returns_404(client, make_user, make_wardrobe_item, db):
    owner = await make_user("wardrobeowner_delete")
    intruder = await make_user("wardrobeintruder_delete")
    item_id = await make_wardrobe_item(owner["user_id"])

    resp = client.delete(f"/wardrobe/{item_id}", headers=intruder["headers"])

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Item not found"
    # The owner's item must still exist - deletion must not have happened.
    still_there = await db.wardrobe.find_one({"_id": ObjectId(item_id)})
    assert still_there is not None


@pytest.mark.asyncio
async def test_list_wardrobe_items_never_includes_another_users_items(
    client, make_user, make_wardrobe_item
):
    owner = await make_user("wardrobeowner_list")
    other = await make_user("wardrobeother_list")
    await make_wardrobe_item(owner["user_id"])
    await make_wardrobe_item(other["user_id"])

    resp = client.get("/wardrobe", headers=other["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["user_id"] == other["user_id"]


# --- feedback ---


@pytest.mark.asyncio
async def test_get_feedback_not_owned_returns_404(
    client, make_user, make_wardrobe_item, make_outfit, make_feedback
):
    owner = await make_user("feedbackowner_get")
    intruder = await make_user("feedbackintruder_get")
    item_id = await make_wardrobe_item(owner["user_id"])
    outfit_id = await make_outfit(owner["user_id"], [item_id])
    await make_feedback(owner["user_id"], outfit_id, rating=4, liked=True)
    feedback_id = client.get("/feedback", headers=owner["headers"]).json()[0]["id"]

    resp = client.get(f"/feedback/{feedback_id}", headers=intruder["headers"])

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Feedback not found"


@pytest.mark.asyncio
async def test_get_feedback_not_owned_matches_nonexistent_feedback_response(
    client, make_user, make_wardrobe_item, make_outfit, make_feedback
):
    owner = await make_user("feedbackowner_get2")
    intruder = await make_user("feedbackintruder_get2")
    item_id = await make_wardrobe_item(owner["user_id"])
    outfit_id = await make_outfit(owner["user_id"], [item_id])
    await make_feedback(owner["user_id"], outfit_id, rating=4, liked=True)
    feedback_id = client.get("/feedback", headers=owner["headers"]).json()[0]["id"]

    cross_user_resp = client.get(f"/feedback/{feedback_id}", headers=intruder["headers"])
    missing_resp = client.get(f"/feedback/{_MISSING_OID}", headers=intruder["headers"])

    assert cross_user_resp.status_code == missing_resp.status_code == 404
    assert cross_user_resp.json() == missing_resp.json()


@pytest.mark.asyncio
async def test_update_feedback_not_owned_returns_404(
    client, make_user, make_wardrobe_item, make_outfit, make_feedback, db
):
    owner = await make_user("feedbackowner_put")
    intruder = await make_user("feedbackintruder_put")
    item_id = await make_wardrobe_item(owner["user_id"])
    outfit_id = await make_outfit(owner["user_id"], [item_id])
    await make_feedback(owner["user_id"], outfit_id, rating=3, liked=None)
    feedback_id = client.get("/feedback", headers=owner["headers"]).json()[0]["id"]

    resp = client.put(
        f"/feedback/{feedback_id}", json={"rating": 1}, headers=intruder["headers"]
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Feedback not found"
    unchanged = await db.feedback.find_one({"_id": ObjectId(feedback_id)})
    assert unchanged["rating"] == 3


@pytest.mark.asyncio
async def test_delete_feedback_not_owned_returns_404(
    client, make_user, make_wardrobe_item, make_outfit, make_feedback, db
):
    owner = await make_user("feedbackowner_delete")
    intruder = await make_user("feedbackintruder_delete")
    item_id = await make_wardrobe_item(owner["user_id"])
    outfit_id = await make_outfit(owner["user_id"], [item_id])
    await make_feedback(owner["user_id"], outfit_id, rating=5, liked=True)
    feedback_id = client.get("/feedback", headers=owner["headers"]).json()[0]["id"]

    resp = client.delete(f"/feedback/{feedback_id}", headers=intruder["headers"])

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Feedback not found"
    still_there = await db.feedback.find_one({"_id": ObjectId(feedback_id)})
    assert still_there is not None


@pytest.mark.asyncio
async def test_list_feedback_never_includes_another_users_feedback(
    client, make_user, make_wardrobe_item, make_outfit, make_feedback
):
    owner = await make_user("feedbackowner_list")
    other = await make_user("feedbackother_list")
    owner_item = await make_wardrobe_item(owner["user_id"])
    other_item = await make_wardrobe_item(other["user_id"])
    owner_outfit = await make_outfit(owner["user_id"], [owner_item])
    other_outfit = await make_outfit(other["user_id"], [other_item])
    await make_feedback(owner["user_id"], owner_outfit, rating=5, liked=True)
    await make_feedback(other["user_id"], other_outfit, rating=2, liked=False)

    resp = client.get("/feedback", headers=other["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["user_id"] == other["user_id"]
