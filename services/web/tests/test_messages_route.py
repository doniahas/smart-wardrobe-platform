import asyncio

import httpx
import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.main import app as fastapi_app
from app.services.conversations import get_or_create_conversation, pair_key


def _send(client, headers, other_user_id, body="hello") -> dict:
    return client.post(f"/messages/{other_user_id}", data={"body": body}, headers=headers)


# --- send ---


@pytest.mark.asyncio
async def test_send_message_requires_auth(client, make_user):
    other = await make_user("sendauthother")
    resp = client.post(f"/messages/{other['user_id']}", data={"body": "hi"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_send_message_success(client, make_user):
    sender = await make_user("sendersuccess")
    recipient = await make_user("recipientsuccess")

    resp = _send(client, sender["headers"], recipient["user_id"], body="Hello there")
    assert resp.status_code == 201
    body = resp.json()
    assert body["sender_id"] == sender["user_id"]
    assert body["body"] == "Hello there"
    assert body["id"]
    assert body["conversation_id"]
    assert body["created_at"]


@pytest.mark.asyncio
async def test_send_message_rejects_blank_body(client, make_user):
    sender = await make_user("blankmsgsender")
    recipient = await make_user("blankmsgrecipient")
    resp = _send(client, sender["headers"], recipient["user_id"], body="   ")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_send_message_rejects_body_over_max_length(client, make_user):
    sender = await make_user("longmsgsender")
    recipient = await make_user("longmsgrecipient")
    resp = _send(client, sender["headers"], recipient["user_id"], body="x" * 2001)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_send_message_accepts_body_at_max_length(client, make_user):
    sender = await make_user("maxmsgsender")
    recipient = await make_user("maxmsgrecipient")
    resp = _send(client, sender["headers"], recipient["user_id"], body="x" * 2000)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_send_message_to_self_rejected(client, make_user):
    user = await make_user("selfmsguser")
    resp = _send(client, user["headers"], user["user_id"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_send_message_to_nonexistent_user_returns_404(client, make_user):
    sender = await make_user("missingrecipientsender")
    resp = _send(client, sender["headers"], "000000000000000000000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_send_message_invalid_user_id_returns_400(client, make_user):
    sender = await make_user("invalidrecipientsender")
    resp = _send(client, sender["headers"], "not-an-id")
    assert resp.status_code == 400


# --- get-or-create idempotency & symmetry ---


@pytest.mark.asyncio
async def test_second_message_reuses_same_conversation(client, make_user, db):
    a = await make_user("reuseconvo_a")
    b = await make_user("reuseconvo_b")

    first = _send(client, a["headers"], b["user_id"], body="first").json()
    second = _send(client, a["headers"], b["user_id"], body="second").json()

    assert first["conversation_id"] == second["conversation_id"]
    count = await db.conversations.count_documents({})
    assert count == 1


@pytest.mark.asyncio
async def test_messaging_from_either_direction_uses_same_conversation(client, make_user):
    a = await make_user("symmetric_a")
    b = await make_user("symmetric_b")

    from_a = _send(client, a["headers"], b["user_id"], body="from a").json()
    from_b = _send(client, b["headers"], a["user_id"], body="from b").json()

    assert from_a["conversation_id"] == from_b["conversation_id"]


# --- history ---


@pytest.mark.asyncio
async def test_history_requires_auth(client, make_user):
    other = await make_user("historyauthother")
    resp = client.get(f"/messages/{other['user_id']}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_history_nonexistent_user_returns_404(client, make_user):
    user = await make_user("historymissinguser")
    resp = client.get("/messages/000000000000000000000000", headers=user["headers"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_history_invalid_user_id_returns_400(client, make_user):
    user = await make_user("historyinvaliduser")
    resp = client.get("/messages/not-an-id", headers=user["headers"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_history_empty_when_no_conversation_yet(client, make_user, db):
    a = await make_user("nohistory_a")
    b = await make_user("nohistory_b")
    resp = client.get(f"/messages/{b['user_id']}", headers=a["headers"])
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert await db.conversations.count_documents({}) == 0


@pytest.mark.asyncio
async def test_history_is_newest_first(client, make_user):
    a = await make_user("orderhistory_a")
    b = await make_user("orderhistory_b")
    _send(client, a["headers"], b["user_id"], body="first")
    _send(client, a["headers"], b["user_id"], body="second")
    _send(client, a["headers"], b["user_id"], body="third")

    resp = client.get(f"/messages/{b['user_id']}", headers=a["headers"])
    bodies = [m["body"] for m in resp.json()["items"]]
    assert bodies == ["third", "second", "first"]


@pytest.mark.asyncio
async def test_history_visible_to_both_participants(client, make_user):
    a = await make_user("bothsee_a")
    b = await make_user("bothsee_b")
    _send(client, a["headers"], b["user_id"], body="hi")

    as_a = client.get(f"/messages/{b['user_id']}", headers=a["headers"]).json()
    as_b = client.get(f"/messages/{a['user_id']}", headers=b["headers"]).json()
    assert len(as_a["items"]) == 1
    assert len(as_b["items"]) == 1
    assert as_a["items"][0]["body"] == as_b["items"][0]["body"] == "hi"


@pytest.mark.asyncio
async def test_history_pagination_has_more(client, make_user):
    a = await make_user("pagehistory_a")
    b = await make_user("pagehistory_b")
    for i in range(3):
        _send(client, a["headers"], b["user_id"], body=f"m{i}")

    resp = client.get(f"/messages/{b['user_id']}?page=1&page_size=2", headers=a["headers"])
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True

    resp = client.get(f"/messages/{b['user_id']}?page=2&page_size=2", headers=a["headers"])
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["has_more"] is False


# --- isolation (IDOR-equivalent: no conversation_id parameter to tamper with) ---


@pytest.mark.asyncio
async def test_third_party_conversation_never_leaks_into_unrelated_thread(client, make_user):
    a = await make_user("isolation_a")
    b = await make_user("isolation_b")
    c = await make_user("isolation_c")

    _send(client, a["headers"], b["user_id"], body="a to b")
    _send(client, c["headers"], b["user_id"], body="c to b")

    a_thread = client.get(f"/messages/{b['user_id']}", headers=a["headers"]).json()
    c_thread = client.get(f"/messages/{b['user_id']}", headers=c["headers"]).json()

    assert [m["body"] for m in a_thread["items"]] == ["a to b"]
    assert [m["body"] for m in c_thread["items"]] == ["c to b"]


# --- inbox ---


@pytest.mark.asyncio
async def test_list_conversations_requires_auth(client):
    resp = client.get("/messages/conversations")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_conversations_scoped_to_caller(client, make_user):
    a = await make_user("inbox_a")
    b = await make_user("inbox_b")
    c = await make_user("inbox_c")
    _send(client, a["headers"], b["user_id"])
    _send(client, c["headers"], b["user_id"])

    a_inbox = client.get("/messages/conversations", headers=a["headers"]).json()
    assert len(a_inbox["items"]) == 1
    assert a_inbox["items"][0]["other_user_id"] == b["user_id"]
    assert a_inbox["items"][0]["other_username"] == "inbox_b"


@pytest.mark.asyncio
async def test_list_conversations_sorted_by_most_recent_activity(client, make_user):
    a = await make_user("inboxorder_a")
    b = await make_user("inboxorder_b")
    c = await make_user("inboxorder_c")
    _send(client, a["headers"], b["user_id"])
    _send(client, a["headers"], c["user_id"])

    inbox = client.get("/messages/conversations", headers=a["headers"]).json()
    other_ids = [item["other_user_id"] for item in inbox["items"]]
    assert other_ids == [c["user_id"], b["user_id"]]


@pytest.mark.asyncio
async def test_list_conversations_pagination(client, make_user):
    a = await make_user("inboxpage_a")
    others = [await make_user(f"inboxpage_other{i}") for i in range(3)]
    for o in others:
        _send(client, a["headers"], o["user_id"])

    resp = client.get("/messages/conversations?page=1&page_size=2", headers=a["headers"])
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True


# --- data consistency (string user ids, ObjectId conversation refs) ---


@pytest.mark.asyncio
async def test_participant_and_sender_ids_are_strings_conversation_id_is_objectid(client, make_user, db):
    a = await make_user("stringid_a")
    b = await make_user("stringid_b")
    resp = _send(client, a["headers"], b["user_id"])
    conversation_id = resp.json()["conversation_id"]

    convo_doc = await db.conversations.find_one({"_id": ObjectId(conversation_id)})
    for p in convo_doc["participants"]:
        assert isinstance(p["user_id"], str)
    assert isinstance(convo_doc["_id"], ObjectId)

    message_doc = await db.direct_messages.find_one({"conversation_id": ObjectId(conversation_id)})
    assert isinstance(message_doc["sender_id"], str)
    assert isinstance(message_doc["conversation_id"], ObjectId)


@pytest.mark.asyncio
async def test_message_persisted_even_if_activity_update_would_fail(client, make_user, db, monkeypatch):
    """The message insert happens before the conversation.last_message_at
    update, so a failure in the second write must never lose the message
    itself."""
    a = await make_user("persistfirst_a")
    b = await make_user("persistfirst_b")

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure updating conversation activity")

    monkeypatch.setattr(type(db.conversations), "update_one", _boom)

    with pytest.raises(RuntimeError):
        _send_result = client.post(
            f"/messages/{b['user_id']}", data={"body": "still saved"}, headers=a["headers"]
        )

    count = await db.direct_messages.count_documents({"body": "still saved"})
    assert count == 1


# --- pair_key / get-or-create ---


def test_pair_key_is_order_independent():
    assert pair_key("a", "b") == pair_key("b", "a")


@pytest.mark.asyncio
async def test_get_or_create_falls_back_to_fetch_on_duplicate_key_error(db, monkeypatch):
    """Simulates the narrow race where two concurrent upserts both attempt
    an insert; the loser's find_one_and_update raises DuplicateKeyError and
    must fall back to fetching what the winner already created, rather than
    retrying or failing."""
    user_a = {"user_id": "u1", "username": "alice"}
    user_b = {"user_id": "u2", "username": "bob"}

    existing = await get_or_create_conversation(db, user_a, user_b)

    async def _always_duplicate(*args, **kwargs):
        raise DuplicateKeyError("simulated race")

    monkeypatch.setattr(type(db.conversations), "find_one_and_update", _always_duplicate)

    result = await get_or_create_conversation(db, user_a, user_b)
    assert result["_id"] == existing["_id"]


@pytest.mark.asyncio
async def test_concurrent_first_messages_create_exactly_one_conversation(client, make_user, db):
    a = await make_user("concurrentmsg_a")
    b = await make_user("concurrentmsg_b")

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        n = 5
        responses = await asyncio.gather(
            *[
                ac.post(f"/messages/{b['user_id']}", data={"body": f"m{i}"}, headers=a["headers"])
                for i in range(n)
            ]
        )

    for resp in responses:
        assert resp.status_code == 201

    assert await db.conversations.count_documents({}) == 1
    assert await db.direct_messages.count_documents({}) == n
