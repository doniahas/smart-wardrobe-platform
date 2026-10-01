import asyncio

import httpx
import pytest

from app.main import app as fastapi_app


def _create_post(client, headers, **overrides) -> str:
    payload = {"title": "t", "body": "b"}
    payload.update(overrides)
    resp = client.post("/posts", data=payload, headers=headers)
    return resp.json()["id"]


def _create_comment(client, post_id, headers, **overrides) -> dict:
    payload = {"body": "a comment"}
    payload.update(overrides)
    return client.post(f"/posts/{post_id}/comments", data=payload, headers=headers)


def _notifications(client, headers) -> list:
    return client.get("/notifications", headers=headers).json()["items"]


# --- messages ---


@pytest.mark.asyncio
async def test_sending_a_message_notifies_the_recipient(client, make_user):
    sender = await make_user("notifmsgsender")
    recipient = await make_user("notifmsgrecipient")

    client.post(f"/messages/{recipient['user_id']}", data={"body": "hi"}, headers=sender["headers"])

    recipient_notifs = _notifications(client, recipient["headers"])
    assert len(recipient_notifs) == 1
    assert recipient_notifs[0]["type"] == "message"
    assert recipient_notifs[0]["actor_id"] == sender["user_id"]
    assert recipient_notifs[0]["target_type"] == "message"

    assert _notifications(client, sender["headers"]) == []


# --- post reactions ---


@pytest.mark.asyncio
async def test_liking_a_post_notifies_the_author(client, make_user):
    author = await make_user("notiflikeauthor")
    reactor = await make_user("notiflikereactor")
    post_id = _create_post(client, author["headers"])

    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=reactor["headers"])

    notifs = _notifications(client, author["headers"])
    assert len(notifs) == 1
    assert notifs[0]["type"] == "like_post"
    assert notifs[0]["actor_id"] == reactor["user_id"]
    assert notifs[0]["target_type"] == "post"
    assert notifs[0]["target_id"] == post_id


@pytest.mark.asyncio
async def test_disliking_a_post_notifies_the_author(client, make_user):
    author = await make_user("notifdislikeauthor")
    reactor = await make_user("notifdislikereactor")
    post_id = _create_post(client, author["headers"])

    client.post(f"/posts/{post_id}/reactions", json={"value": -1}, headers=reactor["headers"])

    notifs = _notifications(client, author["headers"])
    assert len(notifs) == 1
    assert notifs[0]["type"] == "dislike_post"


@pytest.mark.asyncio
async def test_reacting_to_own_post_does_not_notify(client, make_user):
    author = await make_user("notifselfreactauthor")
    post_id = _create_post(client, author["headers"])

    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=author["headers"])

    assert _notifications(client, author["headers"]) == []


@pytest.mark.asyncio
async def test_liking_an_anonymous_post_still_notifies_the_real_author(client, make_user):
    author = await make_user("notifanonauthor")
    reactor = await make_user("notifanonreactor")
    post_id = _create_post(client, author["headers"], is_anonymous=True)

    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=reactor["headers"])

    notifs = _notifications(client, author["headers"])
    assert len(notifs) == 1
    assert notifs[0]["actor_id"] == reactor["user_id"]


@pytest.mark.asyncio
async def test_reacting_to_own_anonymous_post_does_not_self_notify(client, make_user):
    """Confirms self-suppression compares against the real author_id, not
    fooled by is_anonymous."""
    author = await make_user("notifanonselfauthor")
    post_id = _create_post(client, author["headers"], is_anonymous=True)

    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=author["headers"])

    assert _notifications(client, author["headers"]) == []


@pytest.mark.asyncio
async def test_removing_a_reaction_does_not_notify(client, make_user):
    author = await make_user("notifremoveauthor")
    reactor = await make_user("notifremovereactor")
    post_id = _create_post(client, author["headers"])

    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=reactor["headers"])
    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=reactor["headers"])  # toggle off

    notifs = _notifications(client, author["headers"])
    assert len(notifs) == 1  # only the original like; nothing for the removal


@pytest.mark.asyncio
async def test_switching_a_reaction_creates_only_one_new_notification(client, make_user):
    author = await make_user("notifswitchauthor")
    reactor = await make_user("notifswitchreactor")
    post_id = _create_post(client, author["headers"])

    client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=reactor["headers"])
    client.post(f"/posts/{post_id}/reactions", json={"value": -1}, headers=reactor["headers"])

    notifs = _notifications(client, author["headers"])
    assert len(notifs) == 2
    assert sorted(n["type"] for n in notifs) == ["dislike_post", "like_post"]


# --- comment reactions ---


@pytest.mark.asyncio
async def test_liking_a_comment_notifies_the_comment_author(client, make_user):
    post_author = await make_user("notifcommentpostauthor")
    commenter = await make_user("notifcommentauthor")
    reactor = await make_user("notifcommentreactor")
    post_id = _create_post(client, post_author["headers"])
    comment_id = _create_comment(client, post_id, commenter["headers"]).json()["id"]

    client.post(f"/comments/{comment_id}/reactions", json={"value": 1}, headers=reactor["headers"])

    notifs = _notifications(client, commenter["headers"])
    assert len(notifs) == 1
    assert notifs[0]["type"] == "like_comment"
    assert notifs[0]["target_type"] == "comment"
    assert notifs[0]["target_id"] == comment_id

    assert _notifications(client, post_author["headers"]) == []


@pytest.mark.asyncio
async def test_disliking_a_comment_notifies_the_comment_author(client, make_user):
    post_author = await make_user("notifcommentdislikepostauthor")
    commenter = await make_user("notifcommentdislikeauthor")
    reactor = await make_user("notifcommentdislikereactor")
    post_id = _create_post(client, post_author["headers"])
    comment_id = _create_comment(client, post_id, commenter["headers"]).json()["id"]

    client.post(f"/comments/{comment_id}/reactions", json={"value": -1}, headers=reactor["headers"])

    notifs = _notifications(client, commenter["headers"])
    assert len(notifs) == 1
    assert notifs[0]["type"] == "dislike_comment"


@pytest.mark.asyncio
async def test_reacting_to_own_comment_does_not_notify(client, make_user):
    author = await make_user("notifselfcommentauthor")
    post_id = _create_post(client, author["headers"])
    comment_id = _create_comment(client, post_id, author["headers"]).json()["id"]

    client.post(f"/comments/{comment_id}/reactions", json={"value": 1}, headers=author["headers"])

    assert _notifications(client, author["headers"]) == []


# --- scope confirmation ---


@pytest.mark.asyncio
async def test_comment_creation_does_not_notify_post_author(client, make_user):
    """New comments are explicitly NOT a notification trigger in this
    design (messages + likes/dislikes on posts and comments only)."""
    author = await make_user("notifnocommentauthor")
    commenter = await make_user("notifnocommentcommenter")
    post_id = _create_post(client, author["headers"])

    _create_comment(client, post_id, commenter["headers"])

    assert _notifications(client, author["headers"]) == []


# --- concurrency ---


@pytest.mark.asyncio
async def test_concurrent_likes_from_different_users_each_notify_once(client, make_user, db):
    author = await make_user("notifconcurrentauthor")
    post_id = _create_post(client, author["headers"])
    reactors = [await make_user(f"notifconcurrentreactor{i}") for i in range(5)]

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

    count = await db.notifications.count_documents({"recipient_id": author["user_id"]})
    assert count == 5
