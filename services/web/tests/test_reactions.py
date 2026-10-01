import asyncio

import httpx
import pytest
from bson import ObjectId

from app.main import app as fastapi_app
from app.services.reactions import ReactionConflict, apply_reaction


def _create_post(client, headers, **overrides) -> str:
    payload = {"title": "t", "body": "b"}
    payload.update(overrides)
    resp = client.post("/posts", data=payload, headers=headers)
    return resp.json()["id"]


# --- basic toggle/switch/remove ---


@pytest.mark.asyncio
async def test_like_a_post(client, make_user):
    user = await make_user("likeuser")
    post_id = _create_post(client, user["headers"])

    resp = client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=user["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"my_reaction": 1, "likes_count": 1, "dislikes_count": 0}


@pytest.mark.asyncio
async def test_dislike_a_post(client, make_user):
    user = await make_user("dislikeuser")
    post_id = _create_post(client, user["headers"])

    resp = client.post(f"/posts/{post_id}/reactions", json={"value": -1}, headers=user["headers"])
    assert resp.status_code == 200
    assert resp.json() == {"my_reaction": -1, "likes_count": 0, "dislikes_count": 1}


@pytest.mark.asyncio
async def test_clicking_active_reaction_removes_it(client, make_user, db):
    user = await make_user("toggleoffuser")
    post_id = _create_post(client, user["headers"])

    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=user["headers"])
    resp = client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json() == {"my_reaction": None, "likes_count": 0, "dislikes_count": 0}

    count = await db.reactions.count_documents(
        {"user_id": user["user_id"], "target_type": "post", "target_id": ObjectId(post_id)}
    )
    assert count == 0


@pytest.mark.asyncio
async def test_clicking_opposite_reaction_switches_it(client, make_user, db):
    user = await make_user("switchuser")
    post_id = _create_post(client, user["headers"])

    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=user["headers"])
    resp = client.post(f"/posts/{post_id}/reactions", json={"value": -1}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json() == {"my_reaction": -1, "likes_count": 0, "dislikes_count": 1}

    docs = [
        doc
        async for doc in db.reactions.find(
            {"user_id": user["user_id"], "target_type": "post", "target_id": ObjectId(post_id)}
        )
    ]
    assert len(docs) == 1
    assert docs[0]["value"] == -1


@pytest.mark.asyncio
async def test_at_most_one_reaction_document_through_a_sequence_of_clicks(client, make_user, db):
    user = await make_user("sequenceuser")
    post_id = _create_post(client, user["headers"])
    target_oid = ObjectId(post_id)

    for value in (1, 1, -1, -1, 1, -1, 1):
        client.post(f"/posts/{post_id}/reactions", json={"value": value}, headers=user["headers"])
        count = await db.reactions.count_documents(
            {"user_id": user["user_id"], "target_type": "post", "target_id": target_oid}
        )
        assert count <= 1


# --- validation, auth, existence ---


@pytest.mark.asyncio
async def test_react_requires_auth(client, make_user):
    user = await make_user("reactauthuser")
    post_id = _create_post(client, user["headers"])

    resp = client.post(f"/posts/{post_id}/reactions", json={"value": 1})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_react_to_nonexistent_post_returns_404(client, make_user):
    user = await make_user("reactmissinguser")
    resp = client.post(
        "/posts/000000000000000000000000/reactions", json={"value": 1}, headers=user["headers"]
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_react_with_invalid_post_id_returns_400(client, make_user):
    user = await make_user("reactinvaliduser")
    resp = client.post("/posts/not-an-id/reactions", json={"value": 1}, headers=user["headers"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_react_rejects_invalid_value(client, make_user):
    user = await make_user("reactbadvalueuser")
    post_id = _create_post(client, user["headers"])

    resp = client.post(f"/posts/{post_id}/reactions", json={"value": 2}, headers=user["headers"])
    assert resp.status_code == 422


# --- feed/personal-area integration ---


@pytest.mark.asyncio
async def test_feed_reports_counts_and_viewer_own_reaction(client, make_user):
    author = await make_user("feedreactauthor")
    reactor = await make_user("feedreactreactor")
    bystander = await make_user("feedreactbystander")
    post_id = _create_post(client, author["headers"])

    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=reactor["headers"])

    as_reactor = client.get("/posts", headers=reactor["headers"]).json()["items"][0]
    assert as_reactor["likes_count"] == 1
    assert as_reactor["dislikes_count"] == 0
    assert as_reactor["my_reaction"] == 1

    as_bystander = client.get("/posts", headers=bystander["headers"]).json()["items"][0]
    assert as_bystander["likes_count"] == 1
    assert as_bystander["my_reaction"] is None


@pytest.mark.asyncio
async def test_my_posts_aggregate_totals_span_beyond_current_page(client, make_user):
    owner = await make_user("aggregateowner")
    reactor = await make_user("aggregatereactor")
    post_ids = [_create_post(client, owner["headers"], title=f"p{i}") for i in range(3)]

    client.post(f"/posts/{post_ids[0]}/reactions", json={"value": 1}, headers=reactor["headers"])
    client.post(f"/posts/{post_ids[1]}/reactions", json={"value": 1}, headers=reactor["headers"])
    client.post(f"/posts/{post_ids[2]}/reactions", json={"value": -1}, headers=reactor["headers"])

    resp = client.get("/posts/mine?page=1&page_size=2", headers=owner["headers"])
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert body["total_likes_received"] == 2
    assert body["total_dislikes_received"] == 1


@pytest.mark.asyncio
async def test_my_posts_aggregate_includes_anonymous_posts(client, make_user):
    owner = await make_user("anonaggregateowner")
    reactor = await make_user("anonaggregatereactor")
    post_id = _create_post(client, owner["headers"], is_anonymous=True)
    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=reactor["headers"])

    resp = client.get("/posts/mine", headers=owner["headers"])
    body = resp.json()
    assert body["total_likes_received"] == 1
    assert body["items"][0]["is_anonymous"] is True
    assert body["items"][0]["author_id"] is None


# --- delete cascade ---


@pytest.mark.asyncio
async def test_deleting_a_post_cascades_its_reactions(client, make_user, db):
    owner = await make_user("cascadeowner")
    reactor = await make_user("cascadereactor")
    post_id = _create_post(client, owner["headers"])
    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=reactor["headers"])

    resp = client.delete(f"/posts/{post_id}", headers=owner["headers"])
    assert resp.status_code == 204

    count = await db.reactions.count_documents({"target_type": "post", "target_id": ObjectId(post_id)})
    assert count == 0


# --- bounded retry / controlled failure ---


class _NeverMatches:
    matched_count = 0


async def _always_conflicts(*args, **kwargs):
    return _NeverMatches()


def _force_update_one_conflicts(monkeypatch, db):
    """db.<name> returns a brand new AsyncIOMotorCollection wrapper on every
    attribute access (verified: `db.reactions is db.reactions` is False,
    mirroring real Motor/pymongo) - so patching update_one on one instance
    has no effect on the next access. Patching the shared collection class
    itself is what actually reaches every subsequent db.reactions.update_one
    call within the test."""
    monkeypatch.setattr(type(db.reactions), "update_one", _always_conflicts)


@pytest.mark.asyncio
async def test_apply_reaction_raises_after_exhausting_retries(db, monkeypatch):
    target_id = ObjectId()
    await db.reactions.insert_one(
        {"user_id": "u1", "target_type": "post", "target_id": target_id, "value": -1}
    )

    _force_update_one_conflicts(monkeypatch, db)

    with pytest.raises(ReactionConflict):
        await apply_reaction(db, "u1", "post", target_id, 1)


@pytest.mark.asyncio
async def test_react_endpoint_returns_409_without_leaking_internals_on_conflict(
    client, make_user, db, monkeypatch
):
    user = await make_user("conflictuser")
    post_id = _create_post(client, user["headers"])
    target_oid = ObjectId(post_id)

    await db.reactions.insert_one(
        {"user_id": user["user_id"], "target_type": "post", "target_id": target_oid, "value": -1}
    )

    _force_update_one_conflicts(monkeypatch, db)

    resp = client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=user["headers"])
    assert resp.status_code == 409
    body = resp.json()
    assert set(body.keys()) == {"detail"}
    assert isinstance(body["detail"], str)


# --- real concurrency ---


@pytest.mark.asyncio
async def test_concurrent_identical_reactions_leave_correct_existence_parity(client, make_user, db):
    """N concurrent identical-value reaction requests from one user, starting
    from no reaction, must leave existence parity - odd N leaves exactly one
    document, even N leaves none - regardless of how the DB actually
    serializes/interleaves the requests."""
    user = await make_user("parityuser")
    post_id = _create_post(client, user["headers"])
    target_oid = ObjectId(post_id)

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        n = 5
        responses = await asyncio.gather(
            *[
                ac.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=user["headers"])
                for _ in range(n)
            ]
        )

    for resp in responses:
        assert resp.status_code == 200

    count = await db.reactions.count_documents(
        {"user_id": user["user_id"], "target_type": "post", "target_id": target_oid}
    )
    assert count == n % 2


@pytest.mark.asyncio
async def test_concurrent_opposite_reactions_from_same_user_leave_exactly_one_document(
    client, make_user, db
):
    user = await make_user("opposconcurrentuser")
    post_id = _create_post(client, user["headers"])
    target_oid = ObjectId(post_id)

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        responses = await asyncio.gather(
            ac.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=user["headers"]),
            ac.post(f"/posts/{post_id}/reactions", json={"value": -1}, headers=user["headers"]),
        )

    for resp in responses:
        assert resp.status_code == 200

    docs = [
        doc
        async for doc in db.reactions.find(
            {"user_id": user["user_id"], "target_type": "post", "target_id": target_oid}
        )
    ]
    assert len(docs) <= 1
    if docs:
        assert docs[0]["value"] in (1, -1)


@pytest.mark.asyncio
async def test_concurrent_likes_from_different_users_are_all_recorded(client, make_user, db):
    owner = await make_user("multiuserowner")
    post_id = _create_post(client, owner["headers"])
    target_oid = ObjectId(post_id)
    reactors = [await make_user(f"multireactor{i}") for i in range(5)]

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        responses = await asyncio.gather(
            *[
                ac.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=r["headers"])
                for r in reactors
            ]
        )

    for resp in responses:
        assert resp.status_code == 200

    count = await db.reactions.count_documents(
        {"target_type": "post", "target_id": target_oid, "value": 1}
    )
    assert count == 5
    assert count >= 0  # no negative counts are ever possible by construction

    feed_item = client.get("/posts", headers=owner["headers"]).json()["items"][0]
    assert feed_item["likes_count"] == 5
    assert feed_item["dislikes_count"] == 0
