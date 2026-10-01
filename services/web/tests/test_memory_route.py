import pytest


def test_get_memory_requires_auth(client):
    resp = client.get("/memory")
    assert resp.status_code == 401


def test_put_memory_requires_auth(client):
    resp = client.put("/memory", json={})
    assert resp.status_code == 401


def test_patch_memory_requires_auth(client):
    resp = client.patch("/memory", json={})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_memory_returns_default_profile_when_none_saved(client, make_user):
    user = await make_user("alice")
    resp = client.get("/memory", headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["preferred_styles"] == []
    assert body["liked_colors"] == []
    assert body["disliked_colors"] == []
    assert body["disliked_item_types"] == []
    assert body["preferred_occasions"] == []
    assert body["preferred_fit"] is None
    assert body["communication_style"] is None
    assert body["additional_preferences"] == []
    assert body["notes"] is None
    assert body["updated_at"] is None


@pytest.mark.asyncio
async def test_put_memory_creates_and_get_reflects_it(client, make_user):
    user = await make_user("bob")
    payload = {
        "preferred_styles": ["casual", "formal"],
        "liked_colors": ["Red", "Blue"],
        "disliked_colors": ["black"],
        "disliked_item_types": ["shoes"],
        "preferred_occasions": ["work"],
        "preferred_fit": "relaxed",
        "communication_style": "concise",
        "notes": "prefers neutral tones",
    }

    put_resp = client.put("/memory", json=payload, headers=user["headers"])
    assert put_resp.status_code == 200
    body = put_resp.json()
    assert body["preferred_styles"] == ["casual", "formal"]
    assert body["liked_colors"] == ["red", "blue"]  # normalized to lowercase
    assert body["disliked_colors"] == ["black"]
    assert body["disliked_item_types"] == ["shoes"]
    assert body["preferred_occasions"] == ["work"]
    assert body["preferred_fit"] == "relaxed"
    assert body["communication_style"] == "concise"
    assert body["notes"] == "prefers neutral tones"
    assert body["updated_at"] is not None

    get_resp = client.get("/memory", headers=user["headers"])
    assert get_resp.json() == body


@pytest.mark.asyncio
async def test_put_memory_fully_replaces_existing_profile(client, make_user):
    user = await make_user("carol")
    client.put(
        "/memory",
        json={"liked_colors": ["red"], "notes": "first note"},
        headers=user["headers"],
    )

    second = client.put(
        "/memory",
        json={"liked_colors": ["green"]},
        headers=user["headers"],
    )

    assert second.status_code == 200
    body = second.json()
    assert body["liked_colors"] == ["green"]
    assert body["notes"] is None  # PUT replaces, doesn't merge


@pytest.mark.asyncio
async def test_patch_memory_updates_only_provided_fields(client, make_user):
    user = await make_user("dave")
    client.put(
        "/memory",
        json={"liked_colors": ["red"], "notes": "keep me"},
        headers=user["headers"],
    )

    patch_resp = client.patch(
        "/memory",
        json={"disliked_colors": ["blue"]},
        headers=user["headers"],
    )

    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["liked_colors"] == ["red"]  # untouched
    assert body["notes"] == "keep me"  # untouched
    assert body["disliked_colors"] == ["blue"]  # updated


@pytest.mark.asyncio
async def test_patch_memory_without_existing_profile_returns_404(client, make_user):
    user = await make_user("erin")
    resp = client.patch("/memory", json={"notes": "hello"}, headers=user["headers"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_users_cannot_access_each_others_memory(client, make_user):
    alice = await make_user("frank")
    bob = await make_user("gina")

    client.put(
        "/memory",
        json={"liked_colors": ["red"], "notes": "alice's private note"},
        headers=alice["headers"],
    )

    bob_get = client.get("/memory", headers=bob["headers"])
    assert bob_get.status_code == 200
    bob_body = bob_get.json()
    assert bob_body["liked_colors"] == []
    assert bob_body["notes"] is None

    # Bob writing to his own profile must never touch alice's.
    client.put("/memory", json={"notes": "bob's note"}, headers=bob["headers"])

    alice_get = client.get("/memory", headers=alice["headers"])
    assert alice_get.json()["notes"] == "alice's private note"


@pytest.mark.asyncio
async def test_invalid_enum_value_is_rejected(client, make_user):
    user = await make_user("hank")
    resp = client.put(
        "/memory",
        json={"preferred_fit": "not-a-real-fit"},
        headers=user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_color_cannot_be_both_liked_and_disliked(client, make_user):
    user = await make_user("iris")
    resp = client.put(
        "/memory",
        json={"liked_colors": ["red"], "disliked_colors": ["red"]},
        headers=user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_overlap_validated_against_merged_state(client, make_user):
    user = await make_user("jack")
    client.put("/memory", json={"liked_colors": ["red"]}, headers=user["headers"])

    resp = client.patch(
        "/memory",
        json={"disliked_colors": ["red"]},
        headers=user["headers"],
    )
    assert resp.status_code == 400


# --- Long-term memory follow-up completion: additional_preferences via
# the manual profile endpoint (the generic durable-preference fallback) ---


@pytest.mark.asyncio
async def test_put_memory_accepts_generic_additional_preferences(client, make_user):
    user = await make_user("kim")
    payload = {
        "additional_preferences": [
            {"topic": "  Clothing Coverage  ", "preference": "  prefers more covered, modest clothing  "}
        ]
    }

    resp = client.put("/memory", json=payload, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["additional_preferences"] == [
        {"topic": "clothing coverage", "preference": "prefers more covered, modest clothing"}
    ]

    get_resp = client.get("/memory", headers=user["headers"])
    assert get_resp.json()["additional_preferences"] == body["additional_preferences"]


@pytest.mark.asyncio
async def test_patch_memory_can_update_generic_preferences_without_touching_other_fields(client, make_user):
    user = await make_user("liam")
    client.put(
        "/memory",
        json={"liked_colors": ["red"], "additional_preferences": [{"topic": "fabric", "preference": "prefers cotton"}]},
        headers=user["headers"],
    )

    patch_resp = client.patch(
        "/memory",
        json={"additional_preferences": [{"topic": "fabric", "preference": "prefers wool"}]},
        headers=user["headers"],
    )

    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["liked_colors"] == ["red"]  # untouched
    assert body["additional_preferences"] == [{"topic": "fabric", "preference": "prefers wool"}]


@pytest.mark.asyncio
async def test_too_many_generic_preferences_is_rejected(client, make_user):
    user = await make_user("nina")
    resp = client.put(
        "/memory",
        json={"additional_preferences": [{"topic": f"topic{i}", "preference": "detail"} for i in range(11)]},
        headers=user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_oversized_generic_preference_text_is_rejected(client, make_user):
    user = await make_user("owen")
    resp = client.put(
        "/memory",
        json={"additional_preferences": [{"topic": "fabric", "preference": "x" * 500}]},
        headers=user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_generic_preference_with_unknown_field_is_rejected(client, make_user):
    user = await make_user("paula")
    resp = client.put(
        "/memory",
        json={"additional_preferences": [{"topic": "fabric", "preference": "prefers cotton", "extra": "nope"}]},
        headers=user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_users_cannot_access_each_others_generic_preferences(client, make_user):
    alice = await make_user("quinn")
    bob = await make_user("rick")

    client.put(
        "/memory",
        json={"additional_preferences": [{"topic": "fabric", "preference": "alice's private fabric preference"}]},
        headers=alice["headers"],
    )

    bob_get = client.get("/memory", headers=bob["headers"])
    assert bob_get.json()["additional_preferences"] == []

    client.put(
        "/memory",
        json={"additional_preferences": [{"topic": "fabric", "preference": "bob's own fabric preference"}]},
        headers=bob["headers"],
    )

    alice_get = client.get("/memory", headers=alice["headers"])
    assert alice_get.json()["additional_preferences"] == [
        {"topic": "fabric", "preference": "alice's private fabric preference"}
    ]


@pytest.mark.asyncio
async def test_corrupted_legacy_memory_document_is_normalized_not_500(client, make_user, db):
    """Regression test: a pre-fix corrupted memory document (a whole tag
    list appended as a single nested element - see
    personalization.py::update_memory_from_feedback's historical bug) must
    return a normalized flat profile, not a 500."""
    user = await make_user("legacymemorygetuser")
    await db.memory.insert_one({
        "user_id": user["user_id"],
        "preferred_occasions": ["casual", ["casual", "university", "travel"]],
        "preferred_styles": ["casual", ["elegant", "casual"]],
    })

    resp = client.get("/memory", headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["preferred_occasions"] == ["casual", "university", "travel"]
    assert body["preferred_styles"] == ["casual", "elegant"]
