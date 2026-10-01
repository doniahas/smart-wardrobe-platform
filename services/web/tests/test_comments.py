import asyncio

import httpx
import pytest
from bson import ObjectId

from app.main import app as fastapi_app
from app.services.reactions import ReactionConflict, apply_reaction


def _create_post(client, headers, **overrides) -> str:
    payload = {"title": "t", "body": "b", "is_anonymous": "false"}
    payload.update(overrides)
    resp = client.post("/posts", data=payload, headers=headers)
    return resp.json()["id"]


def _create_comment(client, post_id, headers, **overrides) -> dict:
    payload = {"body": "a comment"}
    payload.update(overrides)
    return client.post(f"/posts/{post_id}/comments", data=payload, headers=headers)


# --- create ---


@pytest.mark.asyncio
async def test_create_comment_requires_auth(client, make_user):
    user = await make_user("commentauthuser")
    post_id = _create_post(client, user["headers"])

    resp = client.post(f"/posts/{post_id}/comments", json={"body": "hi"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_comment_success(client, make_user):
    author = await make_user("postowner")
    commenter = await make_user("commentauthor")
    post_id = _create_post(client, author["headers"])

    resp = _create_comment(client, post_id, commenter["headers"], body="Nice post!")
    assert resp.status_code == 201
    body = resp.json()
    assert body["post_id"] == post_id
    assert body["author_id"] == commenter["user_id"]
    assert body["author_username"] == "commentauthor"
    assert body["body"] == "Nice post!"
    assert body["likes_count"] == 0
    assert body["dislikes_count"] == 0
    assert body["my_reaction"] is None
    assert body["id"]
    assert body["created_at"]


@pytest.mark.asyncio
async def test_create_comment_rejects_blank_body(client, make_user):
    user = await make_user("blankcommentuser")
    post_id = _create_post(client, user["headers"])

    resp = _create_comment(client, post_id, user["headers"], body="   ")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_comment_rejects_body_over_max_length(client, make_user):
    user = await make_user("longcommentuser")
    post_id = _create_post(client, user["headers"])

    resp = _create_comment(client, post_id, user["headers"], body="x" * 1001)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_comment_accepts_body_at_max_length(client, make_user):
    user = await make_user("maxcommentuser")
    post_id = _create_post(client, user["headers"])

    resp = _create_comment(client, post_id, user["headers"], body="x" * 1000)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_comment_on_nonexistent_post_returns_404(client, make_user):
    user = await make_user("commentmissingpostuser")
    resp = _create_comment(client, "000000000000000000000000", user["headers"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_comment_on_anonymous_post_still_shows_commenter(client, make_user):
    """Comments are never anonymous, regardless of the parent post's anonymity."""
    author = await make_user("anonpostowner")
    commenter = await make_user("anonpostcommenter")
    post_id = _create_post(client, author["headers"], is_anonymous=True)

    resp = _create_comment(client, post_id, commenter["headers"])
    body = resp.json()
    assert body["author_id"] == commenter["user_id"]
    assert body["author_username"] == "anonpostcommenter"


# --- list ---


@pytest.mark.asyncio
async def test_list_comments_requires_auth(client, make_user):
    user = await make_user("listcommentauthuser")
    post_id = _create_post(client, user["headers"])
    resp = client.get(f"/posts/{post_id}/comments")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_comments_on_nonexistent_post_returns_404(client, make_user):
    user = await make_user("listcommentmissinguser")
    resp = client.get("/posts/000000000000000000000000/comments", headers=user["headers"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_comments_oldest_first(client, make_user):
    user = await make_user("commentorderuser")
    post_id = _create_post(client, user["headers"])
    _create_comment(client, post_id, user["headers"], body="first")
    _create_comment(client, post_id, user["headers"], body="second")

    resp = client.get(f"/posts/{post_id}/comments", headers=user["headers"])
    bodies = [c["body"] for c in resp.json()["items"]]
    assert bodies == ["first", "second"]


@pytest.mark.asyncio
async def test_list_comments_pagination_has_more(client, make_user):
    user = await make_user("commentpageuser")
    post_id = _create_post(client, user["headers"])
    for i in range(3):
        _create_comment(client, post_id, user["headers"], body=f"c{i}")

    resp = client.get(f"/posts/{post_id}/comments?page=1&page_size=2", headers=user["headers"])
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True

    resp = client.get(f"/posts/{post_id}/comments?page=2&page_size=2", headers=user["headers"])
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["has_more"] is False


@pytest.mark.asyncio
async def test_list_comments_reports_reaction_summary_via_one_batch(client, make_user, monkeypatch):
    """Populating likes_count/dislikes_count/my_reaction for a page of
    comments must use one batched reaction-summary call, never a
    per-comment query."""
    author = await make_user("commentbatchauthor")
    reactor = await make_user("commentbatchreactor")
    post_id = _create_post(client, author["headers"])
    comment_ids = [
        _create_comment(client, post_id, author["headers"], body=f"c{i}").json()["id"]
        for i in range(3)
    ]
    for cid in comment_ids[:2]:
        client.post(f"/comments/{cid}/reactions", json={"value": 1}, headers=reactor["headers"])

    import app.routers.posts as posts_module

    call_count = 0
    original = posts_module.batch_reaction_summary

    async def _counting_wrapper(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(posts_module, "batch_reaction_summary", _counting_wrapper)

    resp = client.get(f"/posts/{post_id}/comments", headers=reactor["headers"])
    body = resp.json()

    assert call_count == 1
    by_id = {c["id"]: c for c in body["items"]}
    assert by_id[comment_ids[0]]["likes_count"] == 1
    assert by_id[comment_ids[0]]["my_reaction"] == 1
    assert by_id[comment_ids[2]]["likes_count"] == 0
    assert by_id[comment_ids[2]]["my_reaction"] is None


# --- delete ---


@pytest.mark.asyncio
async def test_delete_comment_requires_auth(client, make_user):
    user = await make_user("deletecommentauthuser")
    post_id = _create_post(client, user["headers"])
    comment_id = _create_comment(client, post_id, user["headers"]).json()["id"]

    resp = client.delete(f"/comments/{comment_id}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_own_comment_succeeds(client, make_user):
    user = await make_user("deleteowncommentuser")
    post_id = _create_post(client, user["headers"])
    comment_id = _create_comment(client, post_id, user["headers"]).json()["id"]

    resp = client.delete(f"/comments/{comment_id}", headers=user["headers"])
    assert resp.status_code == 204

    remaining = client.get(f"/posts/{post_id}/comments", headers=user["headers"]).json()
    assert remaining["items"] == []


@pytest.mark.asyncio
async def test_delete_other_users_comment_returns_404(client, make_user):
    owner = await make_user("commentvictimuser")
    attacker = await make_user("commentattackeruser")
    post_id = _create_post(client, owner["headers"])
    comment_id = _create_comment(client, post_id, owner["headers"]).json()["id"]

    resp = client.delete(f"/comments/{comment_id}", headers=attacker["headers"])
    assert resp.status_code == 404

    remaining = client.get(f"/posts/{post_id}/comments", headers=owner["headers"]).json()
    assert len(remaining["items"]) == 1


@pytest.mark.asyncio
async def test_delete_nonexistent_comment_returns_404(client, make_user):
    user = await make_user("deletenonexistcommentuser")
    resp = client.delete("/comments/000000000000000000000000", headers=user["headers"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_comment_invalid_id_returns_400(client, make_user):
    user = await make_user("deleteinvalidcommentuser")
    resp = client.delete("/comments/not-an-id", headers=user["headers"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_deleting_a_comment_removes_its_reactions(client, make_user, db):
    owner = await make_user("commentcascadeowner")
    reactor = await make_user("commentcascadereactor")
    post_id = _create_post(client, owner["headers"])
    comment_id = _create_comment(client, post_id, owner["headers"]).json()["id"]
    client.post(f"/comments/{comment_id}/reactions", json={"value": 1}, headers=reactor["headers"])

    resp = client.delete(f"/comments/{comment_id}", headers=owner["headers"])
    assert resp.status_code == 204

    count = await db.reactions.count_documents(
        {"target_type": "comment", "target_id": ObjectId(comment_id)}
    )
    assert count == 0


# --- comments_count on posts ---


@pytest.mark.asyncio
async def test_new_post_has_zero_comments_count(client, make_user):
    user = await make_user("zerocommentsuser")
    resp = client.post("/posts", data={"title": "t", "body": "b"}, headers=user["headers"])
    assert resp.json()["comments_count"] == 0


@pytest.mark.asyncio
async def test_feed_reports_comments_count(client, make_user):
    author = await make_user("commentscountauthor")
    commenter = await make_user("commentscountcommenter")
    post_id = _create_post(client, author["headers"])
    _create_comment(client, post_id, commenter["headers"])
    _create_comment(client, post_id, commenter["headers"])

    feed_item = client.get("/posts", headers=author["headers"]).json()["items"][0]
    assert feed_item["comments_count"] == 2

    mine_item = client.get("/posts/mine", headers=author["headers"]).json()["items"][0]
    assert mine_item["comments_count"] == 2


@pytest.mark.asyncio
async def test_comments_count_decreases_after_delete(client, make_user):
    author = await make_user("commentdecreaseauthor")
    commenter = await make_user("commentdecreasecommenter")
    post_id = _create_post(client, author["headers"])
    comment_id = _create_comment(client, post_id, commenter["headers"]).json()["id"]

    client.delete(f"/comments/{comment_id}", headers=commenter["headers"])

    feed_item = client.get("/posts", headers=author["headers"]).json()["items"][0]
    assert feed_item["comments_count"] == 0


# --- comment reactions: toggle/switch/remove ---


@pytest.mark.asyncio
async def test_like_a_comment(client, make_user):
    author = await make_user("likecommentauthor")
    reactor = await make_user("likecommentreactor")
    post_id = _create_post(client, author["headers"])
    comment_id = _create_comment(client, post_id, author["headers"]).json()["id"]

    resp = client.post(f"/comments/{comment_id}/reactions", json={"value": 1}, headers=reactor["headers"])
    assert resp.status_code == 200
    assert resp.json() == {"my_reaction": 1, "likes_count": 1, "dislikes_count": 0}


@pytest.mark.asyncio
async def test_clicking_active_comment_reaction_removes_it(client, make_user, db):
    author = await make_user("commenttoggleauthor")
    reactor = await make_user("commenttogglereactor")
    post_id = _create_post(client, author["headers"])
    comment_id = _create_comment(client, post_id, author["headers"]).json()["id"]

    client.post(f"/comments/{comment_id}/reactions", json={"value": 1}, headers=reactor["headers"])
    resp = client.post(f"/comments/{comment_id}/reactions", json={"value": 1}, headers=reactor["headers"])

    assert resp.json() == {"my_reaction": None, "likes_count": 0, "dislikes_count": 0}
    count = await db.reactions.count_documents(
        {"user_id": reactor["user_id"], "target_type": "comment", "target_id": ObjectId(comment_id)}
    )
    assert count == 0


@pytest.mark.asyncio
async def test_clicking_opposite_comment_reaction_switches_it(client, make_user, db):
    author = await make_user("commentswitchauthor")
    reactor = await make_user("commentswitchreactor")
    post_id = _create_post(client, author["headers"])
    comment_id = _create_comment(client, post_id, author["headers"]).json()["id"]

    client.post(f"/comments/{comment_id}/reactions", json={"value": 1}, headers=reactor["headers"])
    resp = client.post(f"/comments/{comment_id}/reactions", json={"value": -1}, headers=reactor["headers"])

    assert resp.json() == {"my_reaction": -1, "likes_count": 0, "dislikes_count": 1}
    docs = [
        doc
        async for doc in db.reactions.find(
            {"user_id": reactor["user_id"], "target_type": "comment", "target_id": ObjectId(comment_id)}
        )
    ]
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_react_to_nonexistent_comment_returns_404(client, make_user):
    user = await make_user("reactmissingcommentuser")
    resp = client.post(
        "/comments/000000000000000000000000/reactions", json={"value": 1}, headers=user["headers"]
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_react_to_comment_requires_auth(client, make_user):
    author = await make_user("commentreactauthuser")
    post_id = _create_post(client, author["headers"])
    comment_id = _create_comment(client, post_id, author["headers"]).json()["id"]

    resp = client.post(f"/comments/{comment_id}/reactions", json={"value": 1})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_comment_reaction_conflict_returns_409(db, monkeypatch):
    """apply_reaction is shared - the retry-exhaustion path
    behaves identically for target_type='comment' as it does for 'post'."""
    target_id = ObjectId()
    await db.reactions.insert_one(
        {"user_id": "u1", "target_type": "comment", "target_id": target_id, "value": -1}
    )

    class _NeverMatches:
        matched_count = 0

    async def _always_conflicts(*args, **kwargs):
        return _NeverMatches()

    monkeypatch.setattr(type(db.reactions), "update_one", _always_conflicts)

    with pytest.raises(ReactionConflict):
        await apply_reaction(db, "u1", "comment", target_id, 1)


# --- real concurrency on comment reactions ---


@pytest.mark.asyncio
async def test_concurrent_identical_comment_reactions_leave_correct_existence_parity(
    client, make_user, db
):
    author = await make_user("commentparityauthor")
    reactor = await make_user("commentparityreactor")
    post_id = _create_post(client, author["headers"])
    comment_id = _create_comment(client, post_id, author["headers"]).json()["id"]
    target_oid = ObjectId(comment_id)

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        n = 5
        responses = await asyncio.gather(
            *[
                ac.post(
                    f"/comments/{comment_id}/reactions",
                    json={"value": 1},
                    headers=reactor["headers"],
                )
                for _ in range(n)
            ]
        )

    for resp in responses:
        assert resp.status_code == 200

    count = await db.reactions.count_documents(
        {"user_id": reactor["user_id"], "target_type": "comment", "target_id": target_oid}
    )
    assert count == n % 2


@pytest.mark.asyncio
async def test_concurrent_likes_from_different_users_on_a_comment_are_all_recorded(
    client, make_user, db
):
    author = await make_user("commentmultiuserauthor")
    post_id = _create_post(client, author["headers"])
    comment_id = _create_comment(client, post_id, author["headers"]).json()["id"]
    target_oid = ObjectId(comment_id)
    reactors = [await make_user(f"commentmultireactor{i}") for i in range(5)]

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        responses = await asyncio.gather(
            *[
                ac.post(f"/comments/{comment_id}/reactions", json={"value": 1}, headers=r["headers"])
                for r in reactors
            ]
        )

    for resp in responses:
        assert resp.status_code == 200

    count = await db.reactions.count_documents(
        {"target_type": "comment", "target_id": target_oid, "value": 1}
    )
    assert count == 5


# --- post delete cascade ---


@pytest.mark.asyncio
async def test_deleting_a_post_cascades_comments_and_all_their_reactions(client, make_user, db):
    owner = await make_user("fullcascadeowner")
    reactor = await make_user("fullcascadereactor")
    post_id = _create_post(client, owner["headers"])
    post_oid = ObjectId(post_id)

    comment_ids = [
        _create_comment(client, post_id, owner["headers"], body=f"c{i}").json()["id"]
        for i in range(2)
    ]
    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=reactor["headers"])
    for cid in comment_ids:
        client.post(f"/comments/{cid}/reactions", json={"value": 1}, headers=reactor["headers"])

    resp = client.delete(f"/posts/{post_id}", headers=owner["headers"])
    assert resp.status_code == 204

    assert await db.comments.count_documents({"post_id": post_oid}) == 0
    assert await db.reactions.count_documents({"target_type": "post", "target_id": post_oid}) == 0
    comment_oids = [ObjectId(cid) for cid in comment_ids]
    assert (
        await db.reactions.count_documents(
            {"target_type": "comment", "target_id": {"$in": comment_oids}}
        )
        == 0
    )
