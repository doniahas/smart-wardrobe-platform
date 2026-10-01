"""Adversarial tests proving genuinely malformed (syntactically
broken, not merely semantically invalid) JSON request bodies against our
own JSON-accepting endpoints always fail safely with a clean 4xx response
- never a 500, and never any state change - regardless of which endpoint
or HTTP method receives them.

Distinct from the extensive existing "wrong field type / missing required
field" coverage already spread across the suite (e.g. test_outfits_route.py's
various reject tests) - those exercise Pydantic validation on JSON that
parsed successfully. These tests exercise FastAPI's own JSON *decode*
step, which runs before Pydantic validation ever sees the body at all, so
every payload below is not valid JSON syntax at all.

Endpoints covered: /auth/register and /auth/login (the two public, most
attacker-exposed JSON endpoints), POST /outfits and PATCH /outfits/{id},
POST /feedback, and PUT /memory - spanning public/authenticated and
POST/PATCH/PUT.
"""
import pytest
from bson import ObjectId

_MALFORMED_JSON_BODIES = [
    b'{"incomplete": "json"',
    b'{"trailing": "comma",}',
    b"{unquoted_key: \"value\"}",
    b"{'single': 'quotes'}",
    b'{"valid": "json"} trailing garbage',
    b"\x00\x01\x02not json at all\xff",
    b"",
]
_MALFORMED_JSON_IDS = [
    "truncated",
    "trailing-comma",
    "unquoted-key",
    "single-quoted",
    "trailing-garbage",
    "binary-garbage",
    "empty-body",
]


def _send_raw(client, method, url, body, headers=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    return client.request(method, url, content=body, headers=hdrs)


def _assert_fails_safely(resp):
    assert 400 <= resp.status_code < 500, (
        f"expected a clean 4xx for a malformed body, got {resp.status_code}: {resp.text[:200]}"
    )
    # A leaked stack trace/traceback would indicate an unhandled exception
    # reached the client instead of FastAPI's own request-validation error
    # handling - never acceptable regardless of status code.
    assert "Traceback (most recent call last)" not in resp.text


# --- POST /auth/register ---


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _MALFORMED_JSON_BODIES, ids=_MALFORMED_JSON_IDS)
async def test_register_malformed_json_body_fails_safely_no_user_created(client, db, body):
    before = await db.users.count_documents({})

    resp = _send_raw(client, "POST", "/auth/register", body)

    _assert_fails_safely(resp)
    after = await db.users.count_documents({})
    assert after == before


# --- POST /auth/login ---


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _MALFORMED_JSON_BODIES, ids=_MALFORMED_JSON_IDS)
async def test_login_malformed_json_body_fails_safely(client, make_user, body):
    await make_user("malformedjsonlogin")

    resp = _send_raw(client, "POST", "/auth/login", body)

    _assert_fails_safely(resp)


# --- POST /outfits ---


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _MALFORMED_JSON_BODIES, ids=_MALFORMED_JSON_IDS)
async def test_create_outfit_malformed_json_body_fails_safely_no_outfit_created(
    client, make_user, db, body
):
    user = await make_user("malformedjsonoutfitcreate")
    before = await db.outfits.count_documents({})

    resp = _send_raw(client, "POST", "/outfits", body, headers=user["headers"])

    _assert_fails_safely(resp)
    after = await db.outfits.count_documents({})
    assert after == before


# --- PATCH /outfits/{outfit_id} ---


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _MALFORMED_JSON_BODIES, ids=_MALFORMED_JSON_IDS)
async def test_patch_outfit_malformed_json_body_fails_safely_outfit_unchanged(
    client, make_user, make_wardrobe_item, make_outfit, db, body
):
    user = await make_user("malformedjsonoutfitpatch")
    item_id = await make_wardrobe_item(user["user_id"])
    outfit_id = await make_outfit(user["user_id"], [item_id], name="Original Name")

    resp = _send_raw(
        client, "PATCH", f"/outfits/{outfit_id}", body, headers=user["headers"]
    )

    _assert_fails_safely(resp)
    unchanged = await db.outfits.find_one({"_id": ObjectId(outfit_id)})
    assert unchanged["name"] == "Original Name"
    assert unchanged["item_ids"] == [item_id]


# --- POST /feedback ---


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _MALFORMED_JSON_BODIES, ids=_MALFORMED_JSON_IDS)
async def test_create_feedback_malformed_json_body_fails_safely_no_feedback_created(
    client, make_user, db, body
):
    user = await make_user("malformedjsonfeedback")
    before = await db.feedback.count_documents({})

    resp = _send_raw(client, "POST", "/feedback", body, headers=user["headers"])

    _assert_fails_safely(resp)
    after = await db.feedback.count_documents({})
    assert after == before


# --- PUT /memory ---


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _MALFORMED_JSON_BODIES, ids=_MALFORMED_JSON_IDS)
async def test_put_memory_malformed_json_body_fails_safely_no_profile_created(
    client, make_user, db, body
):
    user = await make_user("malformedjsonmemory")

    resp = _send_raw(client, "PUT", "/memory", body, headers=user["headers"])

    _assert_fails_safely(resp)
    profile = await db.memory.find_one({"user_id": user["user_id"]})
    assert profile is None
