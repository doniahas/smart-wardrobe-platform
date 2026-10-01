"""Adversarial cross-user authorization tests for the two resource
types without dedicated cross-user coverage elsewhere - every other
resource type
(wardrobe, feedback, outfits, posts, comments, notifications, chat_jobs,
DM attachments) already has dedicated cross-user coverage elsewhere in
the suite (test_ownership_isolation.py, test_outfits_route.py,
test_posts_route.py, test_comments.py, test_notifications_route.py,
test_chat_jobs_route.py, test_dm_media.py) and is deliberately not
duplicated here.

1. The long-term memory profile (GET/PUT/PATCH /memory) - always scoped
   to the caller's own JWT-derived user_id, with no client-suppliable id
   at all, but never proven adversarially before.
2. DM message *history* (GET /messages/{other_user_id}) - distinct from
   DM *attachments* (already thoroughly tested in test_dm_media.py,
   including the specific cross-conversation-leak case). This endpoint
   takes no conversation_id at all - only the other party's user_id - so
   its actual attack surface is "does a third party who also messages one
   of the two real participants ever see the *other* conversation's
   content", which is what's tested here.

Private DM *attachment* access by a non-participant is covered elsewhere
and not duplicated here: images in test_dm_media.py
(test_unrelated_user_cannot_retrieve_attachment and
test_cross_conversation_attachment_is_not_leaked), and MP4 video in
security/test_video_attachment_security.py.
"""
import pytest


# --- memory profile ---


@pytest.mark.asyncio
async def test_memory_get_never_returns_another_users_data(client, make_user):
    user_a = await make_user("crossusermemory_a")
    user_b = await make_user("crossusermemory_b")

    client.put(
        "/memory",
        json={"notes": "A's private note", "liked_colors": ["red"]},
        headers=user_a["headers"],
    )
    client.put(
        "/memory",
        json={"notes": "B's private note", "liked_colors": ["blue"]},
        headers=user_b["headers"],
    )

    a_view = client.get("/memory", headers=user_a["headers"]).json()
    b_view = client.get("/memory", headers=user_b["headers"]).json()

    assert a_view["notes"] == "A's private note"
    assert a_view["liked_colors"] == ["red"]
    assert b_view["notes"] == "B's private note"
    assert b_view["liked_colors"] == ["blue"]


@pytest.mark.asyncio
async def test_memory_put_never_mutates_another_users_document(client, make_user, db):
    """There is no client-suppliable id on this resource at all - PUT
    always targets the caller's own user_id - so the adversarial
    question is purely "does writing as B ever touch A's stored
    document", verified directly against the database, not just the API
    response.
    """
    user_a = await make_user("crossusermemoryput_a")
    user_b = await make_user("crossusermemoryput_b")

    client.put("/memory", json={"notes": "A's original note"}, headers=user_a["headers"])
    client.put("/memory", json={"notes": "B's note"}, headers=user_b["headers"])

    a_doc = await db.memory.find_one({"user_id": user_a["user_id"]})
    b_doc = await db.memory.find_one({"user_id": user_b["user_id"]})
    assert a_doc["notes"] == "A's original note"
    assert b_doc["notes"] == "B's note"
    assert await db.memory.count_documents({}) == 2


@pytest.mark.asyncio
async def test_memory_patch_never_mutates_another_users_document(client, make_user, db):
    user_a = await make_user("crossusermemorypatch_a")
    user_b = await make_user("crossusermemorypatch_b")
    client.put("/memory", json={"notes": "A's original note"}, headers=user_a["headers"])
    client.put("/memory", json={"notes": "B's original note"}, headers=user_b["headers"])

    resp = client.patch(
        "/memory", json={"notes": "B tries to overwrite"}, headers=user_b["headers"]
    )
    assert resp.status_code == 200

    a_doc = await db.memory.find_one({"user_id": user_a["user_id"]})
    assert a_doc["notes"] == "A's original note"


# --- DM message history ---


@pytest.mark.asyncio
async def test_message_history_never_leaks_a_conversation_you_are_not_part_of(client, make_user):
    """A and B have a private conversation. C also messages B separately.
    C requesting their own history with B must never include any of A's
    messages to B - the attacker (C) must only ever see their own
    conversation's content, never receive another user's data.
    """
    user_a = await make_user("crossuserdm_a")
    user_b = await make_user("crossuserdm_b")
    user_c = await make_user("crossuserdm_c")

    client.post(
        f"/messages/{user_b['user_id']}",
        data={"body": "A's private message to B"},
        headers=user_a["headers"],
    )
    client.post(
        f"/messages/{user_b['user_id']}",
        data={"body": "C's own message to B"},
        headers=user_c["headers"],
    )

    c_history = client.get(f"/messages/{user_b['user_id']}", headers=user_c["headers"])
    assert c_history.status_code == 200
    bodies = [m["body"] for m in c_history.json()["items"]]
    assert "A's private message to B" not in bodies
    assert bodies == ["C's own message to B"]


@pytest.mark.asyncio
async def test_message_history_with_never_contacted_user_is_empty_not_someone_elses(
    client, make_user
):
    """C has never exchanged messages with B at all. Requesting that
    history must return a genuinely empty page (the documented behavior
    for "never talked"), never any other user's conversation content.
    """
    user_a = await make_user("crossuserdmempty_a")
    user_b = await make_user("crossuserdmempty_b")
    user_c = await make_user("crossuserdmempty_c")

    client.post(
        f"/messages/{user_b['user_id']}",
        data={"body": "A's private message to B"},
        headers=user_a["headers"],
    )

    resp = client.get(f"/messages/{user_b['user_id']}", headers=user_c["headers"])

    assert resp.status_code == 200
    assert resp.json()["items"] == []
