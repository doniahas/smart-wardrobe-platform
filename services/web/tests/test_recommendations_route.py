import httpx
import pytest


def _fake_recommend_response(monkeypatch, captured_payloads: list, selected_ids: list[str]):
    """Patch the outbound call to the recommendation microservice so tests
    don't need it running, and record the payload the web service builds."""

    async def fake_post(self, url, json=None, **kwargs):
        captured_payloads.append(json)
        return httpx.Response(
            200,
            json={
                "selected_items": [
                    {"id": item_id, "type": "top", "color": None, "season": None,
                     "style": None, "occasion": None}
                    for item_id in selected_ids
                ],
                "explanation": "test explanation",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def test_recommendation_requires_auth(client):
    resp = client.post("/recommendations", json={})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_no_feedback_still_returns_recommendation(client, make_user, make_wardrobe_item, monkeypatch):
    user = await make_user("alice")
    item_id = await make_wardrobe_item(user["user_id"], color="red")

    captured = []
    _fake_recommend_response(monkeypatch, captured, selected_ids=[item_id])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()["items"]] == [item_id]
    assert captured[0]["preferences"] == {
        "color_scores": {},
        "style_scores": {},
        "occasion_scores": {},
        "season_scores": {},
        "type_scores": {},
        "disliked_colors": [],
        "preferred_styles": [],
        "disliked_item_types": [],
        "preferred_occasions": [],
    }


@pytest.mark.asyncio
async def test_recommendation_never_returns_items_outside_users_wardrobe(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("bob")
    real_item_id = await make_wardrobe_item(user["user_id"])

    captured = []
    # The recommendation service response includes an id that was never in
    # this user's wardrobe (e.g. a bug/attack response) - it must be dropped.
    _fake_recommend_response(monkeypatch, captured, selected_ids=[real_item_id, "not-a-real-item-id"])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned_ids = [item["id"] for item in resp.json()["items"]]
    assert returned_ids == [real_item_id]

    # And only this user's own wardrobe items were ever offered as candidates.
    sent_ids = {item["id"] for item in captured[0]["wardrobe_items"]}
    assert sent_ids == {real_item_id}


@pytest.mark.asyncio
async def test_user_feedback_does_not_leak_into_another_users_recommendations(
    client, make_user, make_wardrobe_item, make_outfit, make_feedback, monkeypatch
):
    alice = await make_user("alice2")
    bob = await make_user("bob2")

    alice_item = await make_wardrobe_item(alice["user_id"], color="red")
    alice_outfit = await make_outfit(alice["user_id"], [alice_item])
    await make_feedback(alice["user_id"], alice_outfit, rating=5, liked=True)

    bob_item = await make_wardrobe_item(bob["user_id"], color="blue")
    bob_outfit = await make_outfit(bob["user_id"], [bob_item])
    await make_feedback(bob["user_id"], bob_outfit, rating=1, liked=False)

    captured = []
    _fake_recommend_response(monkeypatch, captured, selected_ids=[alice_item])
    client.post("/recommendations", json={}, headers=alice["headers"])
    alice_payload = captured[-1]

    _fake_recommend_response(monkeypatch, captured, selected_ids=[bob_item])
    client.post("/recommendations", json={}, headers=bob["headers"])
    bob_payload = captured[-1]

    assert alice_payload["preferences"]["color_scores"].get("red", 0) > 0
    assert "blue" not in alice_payload["preferences"]["color_scores"]

    assert bob_payload["preferences"]["color_scores"].get("blue", 0) < 0
    assert "red" not in bob_payload["preferences"]["color_scores"]


@pytest.mark.asyncio
async def test_memory_disliked_colors_and_types_are_sent_to_recommendation_service(
    client, make_user, make_wardrobe_item, monkeypatch
):
    """A saved memory profile's disliked colors/types and preferred
    styles/occasions must reach the recommendation service's preferences
    payload, alongside (not instead of) the learned scores."""
    user = await make_user("carol")
    item_id = await make_wardrobe_item(user["user_id"], color="red")

    put_resp = client.put(
        "/memory",
        json={
            "disliked_colors": ["blue"],
            "disliked_item_types": ["shoes"],
            "preferred_styles": ["formal"],
            "preferred_occasions": ["work"],
        },
        headers=user["headers"],
    )
    assert put_resp.status_code == 200

    captured = []
    _fake_recommend_response(monkeypatch, captured, selected_ids=[item_id])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    prefs = captured[0]["preferences"]
    assert prefs["disliked_colors"] == ["blue"]
    assert prefs["disliked_item_types"] == ["shoes"]
    assert prefs["preferred_styles"] == ["formal"]
    assert prefs["preferred_occasions"] == ["work"]


@pytest.mark.asyncio
async def test_no_memory_saved_sends_empty_memory_preferences(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("dana")
    item_id = await make_wardrobe_item(user["user_id"], color="red")

    captured = []
    _fake_recommend_response(monkeypatch, captured, selected_ids=[item_id])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    prefs = captured[0]["preferences"]
    assert prefs["disliked_colors"] == []
    assert prefs["disliked_item_types"] == []
    assert prefs["preferred_styles"] == []
    assert prefs["preferred_occasions"] == []


# --- Grounding wired into the recommendation route ---


@pytest.mark.asyncio
async def test_attributes_from_recommendation_service_are_ignored(
    client, make_user, make_wardrobe_item, monkeypatch
):
    """The response must reflect the item's real MongoDB attributes, not
    whatever the recommendation service echoed back for it."""
    user = await make_user("mona")
    item_id = await make_wardrobe_item(
        user["user_id"], name="Real Shirt", color="red", clothing_type="top"
    )

    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={
                "selected_items": [
                    {"id": item_id, "type": "shoes", "color": "invented-color",
                     "season": "invented-season", "style": "invented-style",
                     "occasion": "invented-occasion"}
                ],
                "explanation": "test explanation",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned = resp.json()["items"][0]
    assert returned["name"] == "Real Shirt"
    assert returned["color"] == "red"
    assert returned["clothing_type"] == "top"


@pytest.mark.asyncio
async def test_nonexistent_wellformed_id_from_recommendation_service_is_excluded(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("nina")
    real_item_id = await make_wardrobe_item(user["user_id"])
    # Well-formed ObjectId, never inserted.
    fake_but_wellformed_id = "507f1f77bcf86cd799439011"

    captured = []
    _fake_recommend_response(
        monkeypatch, captured, selected_ids=[real_item_id, fake_but_wellformed_id]
    )

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned_ids = [item["id"] for item in resp.json()["items"]]
    assert returned_ids == [real_item_id]


@pytest.mark.asyncio
async def test_another_users_real_item_id_cannot_appear_in_recommendation(
    client, make_user, make_wardrobe_item, monkeypatch
):
    victim = await make_user("olga")
    attacker = await make_user("piotr")
    victim_item_id = await make_wardrobe_item(victim["user_id"])
    attacker_item_id = await make_wardrobe_item(attacker["user_id"])

    captured = []
    # Simulate a compromised/buggy recommendation service response that
    # includes another real user's actual wardrobe item id.
    _fake_recommend_response(
        monkeypatch, captured, selected_ids=[attacker_item_id, victim_item_id]
    )

    resp = client.post("/recommendations", json={}, headers=attacker["headers"])

    assert resp.status_code == 200
    returned_ids = [item["id"] for item in resp.json()["items"]]
    assert returned_ids == [attacker_item_id]
    assert victim_item_id not in returned_ids


@pytest.mark.asyncio
async def test_grounded_item_order_is_preserved(client, make_user, make_wardrobe_item, monkeypatch):
    user = await make_user("quinn")
    item_a = await make_wardrobe_item(user["user_id"], name="A", clothing_type="top")
    item_b = await make_wardrobe_item(user["user_id"], name="B", clothing_type="bottom")
    item_c = await make_wardrobe_item(user["user_id"], name="C", clothing_type="shoes")

    captured = []
    # An invalid id interleaved between valid ones must not disturb the
    # relative order of the valid ones.
    _fake_recommend_response(
        monkeypatch, captured, selected_ids=[item_b, "not-a-real-item-id", item_c, item_a]
    )

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned_ids = [item["id"] for item in resp.json()["items"]]
    assert returned_ids == [item_b, item_c, item_a]


@pytest.mark.asyncio
async def test_item_deleted_during_recommendation_call_is_excluded_from_response(
    client, make_user, make_wardrobe_item, db, monkeypatch
):
    """Regression test for the core grounding guarantee: grounding re-checks
    MongoDB live at response-build time, not from the wardrobe snapshot
    fetched before the recommendation service was called. An item deleted
    mid-request must not be returned even though it was still present when
    that snapshot was taken."""
    from bson import ObjectId

    user = await make_user("rex")
    keep_item = await make_wardrobe_item(user["user_id"], name="Keep")
    doomed_item = await make_wardrobe_item(user["user_id"], name="Doomed")

    async def fake_post(self, url, json=None, **kwargs):
        # Simulate a concurrent delete happening between the wardrobe fetch
        # (already done before this call) and the response being built.
        await db.wardrobe.delete_one({"_id": ObjectId(doomed_item)})
        return httpx.Response(
            200,
            json={
                "selected_items": [
                    {"id": keep_item, "type": "top", "color": None, "season": None,
                     "style": None, "occasion": None},
                    {"id": doomed_item, "type": "top", "color": None, "season": None,
                     "style": None, "occasion": None},
                ],
                "explanation": "test explanation",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned_ids = [item["id"] for item in resp.json()["items"]]
    assert returned_ids == [keep_item]


# --- Verified fallback selection ---


def _fake_recommend_with_raw_items(monkeypatch, raw_items: list[dict]):
    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={"selected_items": raw_items, "explanation": "test explanation"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


@pytest.mark.asyncio
async def test_failed_candidate_is_replaced_by_verified_fallback(client, make_user, make_wardrobe_item, monkeypatch):
    user = await make_user("sam")
    backup_id = await make_wardrobe_item(user["user_id"], name="Backup", clothing_type="top")

    # The recommendation service "recommends" an item id that does not
    # exist in this user's wardrobe at all.
    _fake_recommend_with_raw_items(monkeypatch, [
        {"id": "507f1f77bcf86cd799439011", "type": "top", "color": None,
         "season": None, "style": None, "occasion": None},
    ])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned_ids = [item["id"] for item in resp.json()["items"]]
    assert returned_ids == [backup_id]


@pytest.mark.asyncio
async def test_multiple_failed_candidates_each_replaced(client, make_user, make_wardrobe_item, monkeypatch):
    user = await make_user("tara")
    backup_top = await make_wardrobe_item(user["user_id"], name="BackupTop", clothing_type="top")
    backup_bottom = await make_wardrobe_item(user["user_id"], name="BackupBottom", clothing_type="bottom")

    _fake_recommend_with_raw_items(monkeypatch, [
        {"id": "507f1f77bcf86cd799439011", "type": "top", "color": None,
         "season": None, "style": None, "occasion": None},
        {"id": "507f1f77bcf86cd799439012", "type": "bottom", "color": None,
         "season": None, "style": None, "occasion": None},
    ])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned_ids = {item["id"] for item in resp.json()["items"]}
    assert returned_ids == {backup_top, backup_bottom}


@pytest.mark.asyncio
async def test_no_available_replacement_yields_controlled_partial_response(
    client, make_user, make_wardrobe_item, monkeypatch
):
    """No verified replacement exists anywhere in the user's wardrobe that
    matches the actual requested criteria - the item is dropped, not
    fabricated, and no internal failure detail leaks to the client.

    Since fallback no longer restricts its search by the recommendation
    service's (untrusted) echoed type, unavailability has to come from the
    verified request criteria genuinely matching nothing - not merely from
    a type mismatch."""
    user = await make_user("uma")
    # This item exists but matches none of the requested criteria below.
    await make_wardrobe_item(
        user["user_id"], clothing_type="shoes", season="summer",
        occasion="casual", style="casual",
    )

    _fake_recommend_with_raw_items(monkeypatch, [
        {"id": "507f1f77bcf86cd799439011", "type": "top", "color": None,
         "season": None, "style": None, "occasion": None},
    ])

    resp = client.post(
        "/recommendations",
        json={"occasion": "formal", "season": "winter", "style": "formal"},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert "fail" not in body["explanation"].lower()
    assert "verif" not in body["explanation"].lower()


@pytest.mark.asyncio
async def test_fallback_never_duplicates_an_already_selected_item(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("victor")
    only_top = await make_wardrobe_item(user["user_id"], name="OnlyTop", clothing_type="top")

    # One real, valid candidate plus a failed candidate that would also
    # want a "top" - there is no second top available, so it must stay
    # unresolved (dropped) rather than duplicating the first one.
    _fake_recommend_with_raw_items(monkeypatch, [
        {"id": only_top, "type": "top", "color": None, "season": None, "style": None, "occasion": None},
        {"id": "507f1f77bcf86cd799439011", "type": "top", "color": None,
         "season": None, "style": None, "occasion": None},
    ])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned_ids = [item["id"] for item in resp.json()["items"]]
    assert returned_ids == [only_top]
    assert returned_ids.count(only_top) == 1


@pytest.mark.asyncio
async def test_fallback_ignores_recommendation_services_untrusted_type_and_still_replaces(
    client, make_user, make_wardrobe_item, monkeypatch
):
    """The recommendation service is not authoritative for wardrobe
    attributes - not even for items it recommended that failed
    verification. A misleading echoed "type" (claiming "shoes" when the
    user only owns a "top") must not suppress or steer fallback: the
    verified "top" must still be found and returned."""
    user = await make_user("yuri")
    only_item = await make_wardrobe_item(user["user_id"], name="OnlyItem", clothing_type="top")

    _fake_recommend_with_raw_items(monkeypatch, [
        {"id": "507f1f77bcf86cd799439011", "type": "shoes", "color": None,
         "season": None, "style": None, "occasion": None},
    ])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned_ids = [item["id"] for item in resp.json()["items"]]
    assert returned_ids == [only_item]


@pytest.mark.asyncio
async def test_fallback_respects_memory_preferences(client, make_user, make_wardrobe_item, monkeypatch):
    user = await make_user("wendy")
    await make_wardrobe_item(user["user_id"], name="Disliked", clothing_type="top", color="black")
    liked_id = await make_wardrobe_item(user["user_id"], name="Liked", clothing_type="top", color="red")

    put_resp = client.put(
        "/memory", json={"disliked_colors": ["black"]}, headers=user["headers"]
    )
    assert put_resp.status_code == 200

    _fake_recommend_with_raw_items(monkeypatch, [
        {"id": "507f1f77bcf86cd799439011", "type": "top", "color": None,
         "season": None, "style": None, "occasion": None},
    ])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned_ids = [item["id"] for item in resp.json()["items"]]
    assert returned_ids == [liked_id]


@pytest.mark.asyncio
async def test_recommendation_quality_unchanged_when_all_candidates_verify(
    client, make_user, make_wardrobe_item, monkeypatch
):
    """Regression: when grounding fully succeeds, fallback must never be
    invoked and behavior matches plain grounding exactly."""
    user = await make_user("xena")
    item_id = await make_wardrobe_item(user["user_id"])

    captured = []
    _fake_recommend_response(monkeypatch, captured, selected_ids=[item_id])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned_ids = [item["id"] for item in resp.json()["items"]]
    assert returned_ids == [item_id]


# --- Logging and failure handling for malformed responses ---


@pytest.mark.asyncio
async def test_non_json_response_from_recommendation_service_returns_controlled_502(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("yolanda")
    await make_wardrobe_item(user["user_id"])

    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(200, content=b"not valid json at all")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 502
    assert "detail" in resp.json()


@pytest.mark.asyncio
async def test_unexpected_status_from_recommendation_service_is_logged_without_leaking_data(
    client, make_user, make_wardrobe_item, monkeypatch, caplog
):
    """Diagnostic logging: an unexpected (non-200/400/404) status
    from the recommendation service must be logged with enough to diagnose
    it (status code + user id) but never with the response body, which
    could echo back wardrobe/prompt data - mirrors the caplog pattern in
    test_fallback_failure_log_does_not_expose_wardrobe_contents and
    test_logging_privacy.py."""
    import logging

    user = await make_user("finalcheckuser")
    await make_wardrobe_item(
        user["user_id"], name="Top Secret Item", color="magenta", clothing_type="top",
    )

    _SENTINEL_BODY = "sentinel-500-body-should-never-be-logged"

    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(500, content=_SENTINEL_BODY.encode())

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with caplog.at_level(logging.WARNING):
        resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 502
    assert "detail" in resp.json()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "status=500" in log_text
    assert user["user_id"] in log_text
    assert _SENTINEL_BODY not in caplog.text
    assert "Top Secret Item" not in caplog.text
    assert "magenta" not in caplog.text


@pytest.mark.asyncio
async def test_selected_items_not_a_list_degrades_to_empty_result(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("zack")
    await make_wardrobe_item(user["user_id"])

    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(200, json={"selected_items": "oops-not-a-list", "explanation": "x"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_malformed_candidate_entries_are_skipped_not_crashed(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("amber")
    good_item = await make_wardrobe_item(user["user_id"])

    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={
                "selected_items": [
                    "not-a-dict",
                    42,
                    {"no_id_field": True},
                    {"id": good_item, "type": "top", "color": None,
                     "season": None, "style": None, "occasion": None},
                ],
                "explanation": "test",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    returned_ids = [item["id"] for item in resp.json()["items"]]
    assert returned_ids == [good_item]


@pytest.mark.asyncio
async def test_malformed_body_on_400_falls_back_to_generic_detail(
    client, make_user, make_wardrobe_item, monkeypatch
):
    user = await make_user("bruno")
    await make_wardrobe_item(user["user_id"])

    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(400, content=b"not json")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Bad request to recommendation service."


@pytest.mark.asyncio
async def test_recommendation_response_never_leaks_internal_verification_or_fallback_details(
    client, make_user, make_wardrobe_item, monkeypatch
):
    """Comprehensive check: a mix of a successfully grounded item,
    a failure resolved via fallback, and a failure with no available
    replacement, all in one request - the public response must expose
    nothing beyond the documented OutfitRecommendation/WardrobeItemPublic
    shape (no source_type, verified_at, reason, or any other internal
    grounding/fallback field)."""
    user = await make_user("celine")
    real_item = await make_wardrobe_item(user["user_id"], name="Real", clothing_type="top")
    backup_item = await make_wardrobe_item(user["user_id"], name="Backup", clothing_type="bottom")

    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={
                "selected_items": [
                    {"id": real_item, "type": "top", "color": None, "season": None,
                     "style": None, "occasion": None},
                    {"id": "507f1f77bcf86cd799439011", "type": "bottom", "color": None,
                     "season": None, "style": None, "occasion": None},
                    {"id": "507f1f77bcf86cd799439012", "type": "shoes", "color": None,
                     "season": None, "style": None, "occasion": None},
                ],
                "explanation": "test explanation",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"items", "explanation", "missing_types"}

    returned_ids = [item["id"] for item in body["items"]]
    assert set(returned_ids) == {real_item, backup_item}
    # top+bottom both verified in the final response -> outfit is complete.
    assert body["missing_types"] == []

    known_item_fields = {
        "id", "user_id", "name", "clothing_type", "color", "color_hex", "season",
        "style", "occasion", "image_url", "tags",
    }
    for item in body["items"]:
        assert set(item.keys()) == known_item_fields

    body_text = resp.text.lower()
    for leaked_term in ("source_type", "verified_at", "verified_fields", "reason", "not_found", "not_owned", "unresolved"):
        assert leaked_term not in body_text


@pytest.mark.asyncio
async def test_fallback_failure_log_does_not_expose_wardrobe_contents(
    client, make_user, make_wardrobe_item, monkeypatch, caplog
):
    import logging

    user = await make_user("derek")
    # season="all" would satisfy any requested season, so pick a concrete
    # mismatching season/style/occasion to ensure no fallback replacement
    # is found (proving the unresolved-failure log path, not the
    # replacement-found path).
    await make_wardrobe_item(
        user["user_id"], name="Top Secret Item", color="magenta", clothing_type="top",
        season="summer", style="casual", occasion="casual",
    )

    async def fake_post(self, url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={
                "selected_items": [
                    {"id": "507f1f77bcf86cd799439011", "type": "shoes", "color": None,
                     "season": None, "style": None, "occasion": None},
                ],
                "explanation": "test",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with caplog.at_level(logging.INFO):
        resp = client.post(
            "/recommendations",
            json={"occasion": "formal", "season": "winter", "style": "formal"},
            headers=user["headers"],
        )

    assert resp.status_code == 200
    assert resp.json()["items"] == []

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Top Secret Item" not in log_text
    assert "magenta" not in log_text


@pytest.mark.asyncio
async def test_corrupted_legacy_memory_does_not_break_recommendations(
    client, make_user, make_wardrobe_item, db, monkeypatch
):
    """Regression test: a pre-fix corrupted memory document (a whole tag
    list appended as a single nested element into preferred_occasions/
    preferred_styles - see personalization.py::update_memory_from_feedback's
    historical bug) must be tolerated, not crash the whole /recommend call
    with a 422/502."""
    user = await make_user("legacymemoryuser")
    item_id = await make_wardrobe_item(user["user_id"], color="red")
    await db.memory.insert_one({
        "user_id": user["user_id"],
        "preferred_occasions": ["casual", ["casual", "university", "travel"]],
        "preferred_styles": ["casual", ["elegant", "casual"]],
    })

    captured = []
    _fake_recommend_response(monkeypatch, captured, selected_ids=[item_id])

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    assert captured[0]["preferences"]["preferred_occasions"] == ["casual", "university", "travel"]
    assert captured[0]["preferences"]["preferred_styles"] == ["casual", "elegant"]
