import pytest


def _post_payload(**overrides) -> dict:
    payload = {"title": "My title", "body": "My body text"}
    payload.update(overrides)
    return payload


# --- create_post with shared_outfit_id ---


@pytest.mark.asyncio
async def test_create_post_with_shared_outfit_stores_sanitized_snapshot(
    client, make_user, make_wardrobe_item
):
    user = await make_user("outfitsharer")
    item_id = await make_wardrobe_item(
        user["user_id"], name="Blue Top", clothing_type="top", color="blue",
        season="summer", style="casual", occasion="casual",
    )
    outfit_resp = client.post(
        "/outfits",
        json={"item_ids": [item_id], "name": "Summer look", "occasion": "casual",
              "season": "summer", "style": "casual"},
        headers=user["headers"],
    )
    assert outfit_resp.status_code == 201
    outfit_id = outfit_resp.json()["id"]

    resp = client.post(
        "/posts", data=_post_payload(shared_outfit_id=outfit_id), headers=user["headers"]
    )
    assert resp.status_code == 201
    body = resp.json()
    shared = body["shared_outfit"]
    assert shared["outfit_name"] == "Summer look"
    assert shared["outfit_occasion"] == "casual"
    assert shared["outfit_season"] == "summer"
    assert shared["outfit_style"] == "casual"
    assert shared["components"] == [
        {"clothing_type": "top", "color": "blue", "season": "summer", "style": "casual", "occasion": "casual"}
    ]
    # Security: no wardrobe item id, outfit id, image_url, name, or tags anywhere in the snapshot.
    assert item_id not in str(shared)
    assert outfit_id not in str(shared)
    assert "id" not in shared["components"][0]
    assert "name" not in shared["components"][0]
    assert "image_url" not in shared["components"][0]
    assert "tags" not in shared["components"][0]


@pytest.mark.asyncio
async def test_create_post_without_shared_outfit_has_null_snapshot(client, make_user):
    user = await make_user("noshareuser")
    resp = client.post("/posts", data=_post_payload(), headers=user["headers"])
    assert resp.status_code == 201
    assert resp.json()["shared_outfit"] is None


@pytest.mark.asyncio
async def test_create_post_rejects_another_users_outfit(client, make_user, make_wardrobe_item, make_outfit):
    owner = await make_user("outfitowner")
    other = await make_user("outfitthief")
    item_id = await make_wardrobe_item(owner["user_id"])
    outfit_id = await make_outfit(owner["user_id"], [item_id])

    resp = client.post(
        "/posts", data=_post_payload(shared_outfit_id=outfit_id), headers=other["headers"]
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_post_rejects_invalid_outfit_id(client, make_user):
    user = await make_user("badoutfitiduser")
    resp = client.post(
        "/posts", data=_post_payload(shared_outfit_id="not-an-id"), headers=user["headers"]
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_post_rejects_outfit_with_no_remaining_items(
    client, make_user, make_outfit
):
    """Defensive branch: an outfit doc whose item_ids point at nothing (a
    corrupted/stale record) must fail cleanly rather than store an empty
    snapshot. In normal operation this can't happen via the API - deleting
    a wardrobe item prunes/cascades the outfit itself (see
    wardrobe.py::delete_item) - so this is exercised via a direct DB
    insert (make_outfit bypasses the real ownership/existence validation
    that POST /outfits enforces), the same way make_outfit is used
    elsewhere to set up fixture state the API itself wouldn't produce."""
    user = await make_user("emptyoutfituser")
    outfit_id = await make_outfit(user["user_id"], ["000000000000000000000000"])

    resp = client.post(
        "/posts", data=_post_payload(shared_outfit_id=outfit_id), headers=user["headers"]
    )
    assert resp.status_code == 400


# --- GET /posts/{post_id}/recreate ---


@pytest.mark.asyncio
async def test_recreate_requires_auth(client, make_user, make_post):
    user = await make_user("recreateauthuser")
    post_id = await make_post(user["user_id"])
    resp = client.get(f"/posts/{post_id}/recreate")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_recreate_returns_404_for_nonexistent_post(client, make_user):
    user = await make_user("recreatenopostuser")
    resp = client.get("/posts/000000000000000000000000/recreate", headers=user["headers"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_recreate_returns_404_when_post_has_no_shared_outfit(client, make_user, make_post):
    user = await make_user("recreatenooutfituser")
    post_id = await make_post(user["user_id"])
    resp = client.get(f"/posts/{post_id}/recreate", headers=user["headers"])
    assert resp.status_code == 404


async def _make_post_with_shared_outfit(client, make_wardrobe_item, author, **item_overrides):
    item_id = await make_wardrobe_item(
        author["user_id"],
        clothing_type=item_overrides.get("clothing_type", "top"),
        color=item_overrides.get("color", "blue"),
        season=item_overrides.get("season", "summer"),
        style=item_overrides.get("style", "casual"),
        occasion=item_overrides.get("occasion", "casual"),
    )
    outfit_resp = client.post(
        "/outfits", json={"item_ids": [item_id]}, headers=author["headers"]
    )
    outfit_id = outfit_resp.json()["id"]
    post_resp = client.post(
        "/posts", data=_post_payload(shared_outfit_id=outfit_id), headers=author["headers"]
    )
    return post_resp.json()["id"], item_id, outfit_id


@pytest.mark.asyncio
async def test_recreate_exact_match_for_author(client, make_user, make_wardrobe_item):
    author = await make_user("recreateauthor1")
    post_id, item_id, _ = await _make_post_with_shared_outfit(client, make_wardrobe_item, author)

    resp = client.get(f"/posts/{post_id}/recreate", headers=author["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["post_id"] == post_id
    assert len(data["matches"]) == 1
    match = data["matches"][0]
    assert match["matched_item"]["id"] == item_id
    assert match["match_score"] == 1.0
    assert "Exact match" in match["match_explanation"]


@pytest.mark.asyncio
async def test_recreate_works_for_a_different_user_against_their_own_wardrobe(
    client, make_user, make_wardrobe_item
):
    author = await make_user("recreateauthor2")
    other = await make_user("recreateother2")
    post_id, _, _ = await _make_post_with_shared_outfit(client, make_wardrobe_item, author)

    # Other user's wardrobe has a partial match: same type, different color.
    other_item_id = await make_wardrobe_item(
        other["user_id"], clothing_type="top", color="red", season="summer", style="casual", occasion="casual"
    )

    resp = client.get(f"/posts/{post_id}/recreate", headers=other["headers"])
    assert resp.status_code == 200
    match = resp.json()["matches"][0]
    assert match["matched_item"]["id"] == other_item_id
    assert 0 < match["match_score"] < 1.0
    assert "differ" in match["match_explanation"]


@pytest.mark.asyncio
async def test_recreate_reports_missing_component_when_no_matching_type_exists(
    client, make_user, make_wardrobe_item
):
    author = await make_user("recreateauthor3")
    other = await make_user("recreateother3")
    post_id, _, _ = await _make_post_with_shared_outfit(client, make_wardrobe_item, author, clothing_type="dress")
    await make_wardrobe_item(other["user_id"], clothing_type="top")

    resp = client.get(f"/posts/{post_id}/recreate", headers=other["headers"])
    assert resp.status_code == 200
    match = resp.json()["matches"][0]
    assert match["matched_item"] is None
    assert match["match_score"] is None
    assert "No dress" in match["match_explanation"]


@pytest.mark.asyncio
async def test_recreate_never_reuses_the_same_wardrobe_item_across_components(
    client, make_user, make_wardrobe_item
):
    author = await make_user("recreateauthor4")
    item1 = await make_wardrobe_item(
        author["user_id"], clothing_type="top", color="blue", season="summer", style="casual", occasion="casual"
    )
    item2 = await make_wardrobe_item(
        author["user_id"], clothing_type="bottom", color="black", season="summer", style="casual", occasion="casual"
    )
    outfit_resp = client.post(
        "/outfits", json={"item_ids": [item1, item2]}, headers=author["headers"]
    )
    outfit_id = outfit_resp.json()["id"]
    post_resp = client.post(
        "/posts", data=_post_payload(shared_outfit_id=outfit_id), headers=author["headers"]
    )
    post_id = post_resp.json()["id"]

    # Requesting user has only ONE top that could match both slots if type
    # filtering were broken - it must never be double-assigned.
    other = await make_user("recreateother4")
    other_top = await make_wardrobe_item(
        other["user_id"], clothing_type="top", color="blue", season="summer", style="casual", occasion="casual"
    )

    resp = client.get(f"/posts/{post_id}/recreate", headers=other["headers"])
    assert resp.status_code == 200
    matches = resp.json()["matches"]
    assert matches[0]["matched_item"]["id"] == other_top
    # The bottom slot has no bottom-type item at all in this wardrobe.
    assert matches[1]["matched_item"] is None


@pytest.mark.asyncio
async def test_recreate_is_deterministic_across_repeated_calls_on_ties(
    client, make_user, make_wardrobe_item
):
    author = await make_user("recreateauthor5")
    post_id, _, _ = await _make_post_with_shared_outfit(client, make_wardrobe_item, author)

    other = await make_user("recreateother5")
    tie_item_a = await make_wardrobe_item(
        other["user_id"], clothing_type="top", color="red", season="winter", style="formal", occasion="formal"
    )
    tie_item_b = await make_wardrobe_item(
        other["user_id"], clothing_type="top", color="green", season="autumn", style="sport", occasion="work"
    )

    first = client.get(f"/posts/{post_id}/recreate", headers=other["headers"]).json()
    second = client.get(f"/posts/{post_id}/recreate", headers=other["headers"]).json()
    assert first == second
    # Both tie candidates score 0/4 - the stable-order rule (lowest _id
    # first) must pick the same one every time: whichever was created first.
    assert first["matches"][0]["matched_item"]["id"] == tie_item_a


@pytest.mark.asyncio
async def test_recreate_still_works_after_source_outfit_is_deleted(
    client, make_user, make_wardrobe_item
):
    author = await make_user("recreateauthor6")
    post_id, item_id, outfit_id = await _make_post_with_shared_outfit(client, make_wardrobe_item, author)

    del_resp = client.delete(f"/outfits/{outfit_id}", headers=author["headers"])
    assert del_resp.status_code == 204

    resp = client.get(f"/posts/{post_id}/recreate", headers=author["headers"])
    assert resp.status_code == 200
    assert resp.json()["matches"][0]["matched_item"]["id"] == item_id


@pytest.mark.asyncio
async def test_snapshot_and_recreate_unchanged_after_source_outfit_is_edited(
    client, make_user, make_wardrobe_item
):
    """Editing the source outfit via PATCH /outfits/{id} must
    never touch a Public Outfit Snapshot already frozen onto a post - same
    guarantee as the delete case above, now exercised through an edit
    instead. Reuses the existing _make_post_with_shared_outfit helper and
    the existing GET /posts/mine feed endpoint rather than adding anything
    new solely for this test."""
    author = await make_user("recreateauthor8")
    post_id, item_id, outfit_id = await _make_post_with_shared_outfit(client, make_wardrobe_item, author)

    before = client.get("/posts/mine", headers=author["headers"])
    assert before.status_code == 200
    snapshot_before = next(
        p for p in before.json()["items"] if p["id"] == post_id
    )["shared_outfit"]

    new_item_id = await make_wardrobe_item(
        author["user_id"], name="Different top", clothing_type="top", color="green"
    )
    patch_resp = client.patch(
        f"/outfits/{outfit_id}",
        json={"item_ids": [new_item_id], "name": "Edited name"},
        headers=author["headers"],
    )
    assert patch_resp.status_code == 200

    after = client.get("/posts/mine", headers=author["headers"])
    assert after.status_code == 200
    snapshot_after = next(
        p for p in after.json()["items"] if p["id"] == post_id
    )["shared_outfit"]

    assert snapshot_after == snapshot_before

    resp = client.get(f"/posts/{post_id}/recreate", headers=author["headers"])
    assert resp.status_code == 200
    # Still matches against the *original* shared item - the snapshot never
    # saw the edit, so the newly-added item never enters the comparison
    # except as an ordinary (lower-scoring) wardrobe candidate.
    assert resp.json()["matches"][0]["matched_item"]["id"] == item_id


@pytest.mark.asyncio
async def test_recreate_still_works_after_source_wardrobe_item_is_deleted(
    client, make_user, make_wardrobe_item
):
    author = await make_user("recreateauthor7")
    post_id, item_id, _ = await _make_post_with_shared_outfit(client, make_wardrobe_item, author)

    del_resp = client.delete(f"/wardrobe/{item_id}", headers=author["headers"])
    assert del_resp.status_code == 204

    resp = client.get(f"/posts/{post_id}/recreate", headers=author["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["post_id"] == post_id
    assert len(data["matches"]) == 1
    # The author's own wardrobe no longer has that item, so it's now "missing".
    assert data["matches"][0]["matched_item"] is None
