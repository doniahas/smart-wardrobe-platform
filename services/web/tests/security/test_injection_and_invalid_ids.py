"""Adversarial tests for NoSQL/operator-style injection attempts and
injection-shaped identifiers (plain invalid IDs are already covered
across many existing route test files and are deliberately not
re-tested here beyond one representative injection-shaped-ID case, to
avoid duplicating existing coverage).

Two distinct attack shapes are covered:
  1. Structured NoSQL-operator payloads (e.g. {"$ne": null}) submitted
     where a plain string is expected - proven rejected by Pydantic's own
     type validation before any application/query code ever sees them.
  2. Free-text fields and query-string values containing
     injection-*looking* strings (operator syntax, $where-style text,
     MongoDB-special characters) - proven stored/matched as ordinary
     inert data, never specially interpreted, because pymongo only
     treats real dict/BSON structures as query operators, never string
     values that merely look like JSON.
"""
import pytest

# A string that looks like a MongoDB $where/injection attempt if it were
# ever misused, but is just data if handled correctly (bounded well under
# every relevant field's max_length so length constraints never mask the
# actual behavior being tested).
_INJECTION_LIKE_TEXT = "'; return true; //  $where: this.password"


# --- 1. structured NoSQL-operator payloads rejected by type validation ---


@pytest.mark.asyncio
async def test_login_rejects_operator_payload_as_credentials(client, make_user):
    """UserLogin.username/password are typed str - an operator object
    where a string is expected must be rejected by Pydantic's own type
    validation, never reach the database lookup at all.
    """
    await make_user("injectionlogintarget")

    resp = client.post(
        "/auth/login",
        json={"username": {"$ne": None}, "password": {"$ne": None}},
    )

    assert resp.status_code == 422
    assert "Traceback (most recent call last)" not in resp.text


@pytest.mark.asyncio
async def test_register_rejects_operator_payload_as_username(client, db):
    before = await db.users.count_documents({})

    resp = client.post(
        "/auth/register",
        json={
            "username": {"$gt": ""},
            "email": "injection@example.com",
            "password": "password123",
        },
    )

    assert resp.status_code == 422
    after = await db.users.count_documents({})
    assert after == before


@pytest.mark.asyncio
async def test_create_outfit_rejects_operator_payload_in_item_ids(client, make_user, db):
    """SavedOutfitCreate.item_ids is list[str] - an operator object as a
    list element must be rejected by type validation, never reach the
    ownership-check query that resolves item_ids against the database.
    """
    user = await make_user("injectionoutfituser")
    before = await db.outfits.count_documents({})

    resp = client.post(
        "/outfits", json={"item_ids": [{"$ne": None}]}, headers=user["headers"]
    )

    assert resp.status_code == 422
    after = await db.outfits.count_documents({})
    assert after == before


# --- 2. injection-looking strings treated as ordinary, inert data ---


@pytest.mark.asyncio
async def test_wardrobe_filter_operator_like_string_matches_nothing_not_bypassed(
    client, make_user, make_wardrobe_item
):
    """`color` is a plain Optional[str] query param - a string that looks
    like a MongoDB operator can only ever arrive as a literal string
    value (query strings cannot carry real nested JSON objects), so it
    must be compared as ordinary text and match nothing - never
    interpreted as an operator, and never silently returning every item
    (which would be an authorization/filter-bypass, not just "no match").
    """
    user = await make_user("injectionwardrobeuser")
    await make_wardrobe_item(user["user_id"], color="red")
    await make_wardrobe_item(user["user_id"], color="blue")

    resp = client.get(
        "/wardrobe",
        params={"color": '{"$ne": "red"}'},
        headers=user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_feedback_comment_injection_like_text_stored_as_literal(
    client, make_user, make_wardrobe_item, make_outfit, db
):
    user = await make_user("injectionfeedbackuser")
    item_id = await make_wardrobe_item(user["user_id"])
    outfit_id = await make_outfit(user["user_id"], [item_id])

    resp = client.post(
        "/feedback",
        json={"outfit_id": outfit_id, "rating": 5, "comment": _INJECTION_LIKE_TEXT},
        headers=user["headers"],
    )

    assert resp.status_code == 201
    assert resp.json()["comment"] == _INJECTION_LIKE_TEXT
    # No collateral damage: exactly one feedback document exists, and the
    # database as a whole wasn't touched beyond it.
    stored = await db.feedback.find_one({"outfit_id": outfit_id})
    assert stored["comment"] == _INJECTION_LIKE_TEXT
    assert await db.feedback.count_documents({}) == 1


@pytest.mark.asyncio
async def test_memory_notes_injection_like_text_stored_as_literal(client, make_user, db):
    user = await make_user("injectionmemoryuser")

    resp = client.put(
        "/memory", json={"notes": _INJECTION_LIKE_TEXT}, headers=user["headers"]
    )

    assert resp.status_code == 200
    assert resp.json()["notes"] == _INJECTION_LIKE_TEXT
    stored = await db.memory.find_one({"user_id": user["user_id"]})
    assert stored["notes"] == _INJECTION_LIKE_TEXT


@pytest.mark.asyncio
async def test_register_and_login_with_mongo_special_character_username(client, db):
    """Characters like $ and . are restricted in MongoDB *field names*,
    not in ordinary string *values* - a username value containing them
    must work as perfectly ordinary data (register, then log in with the
    exact same value), never trigger special interpretation or an error.
    """
    username = "a.b$c"  # 5 chars, within UserRegister's min_length=3/max_length=30

    register_resp = client.post(
        "/auth/register",
        json={"username": username, "email": "special@example.com", "password": "password123"},
    )
    assert register_resp.status_code == 201
    assert register_resp.json()["username"] == username

    login_resp = client.post(
        "/auth/login", json={"username": username, "password": "password123"}
    )
    assert login_resp.status_code == 200
    assert await db.users.count_documents({"username": username}) == 1


# --- 3. one representative injection-shaped invalid ID (not duplicating
#        the already-complete general invalid-ID coverage elsewhere) ---


@pytest.mark.asyncio
async def test_get_wardrobe_item_with_operator_shaped_id_string_returns_400(client, make_user):
    user = await make_user("injectionidshapeduser")

    resp = client.get('/wardrobe/{"$ne": null}', headers=user["headers"])

    assert resp.status_code == 400
    assert "Traceback (most recent call last)" not in resp.text
