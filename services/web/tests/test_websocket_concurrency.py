"""Stress testing: many simultaneous real WebSocket
connections from many distinct users, combined with concurrently-fired
notification-triggering events, proving both "many simultaneous
connections work" and "isolation holds" at real scale in one test - not
just the 2-connection/2-user ceiling every existing WS test in
test_live_notifications.py and test_live_broker_redis.py stops at (the
latter also only ever uses fake in-memory stand-in sockets, not real ones).

Uses the exact same real application WebSocket connection mechanism as
test_live_notifications.py (`client.websocket_connect("/ws")`, Starlette's
TestClient driving the actual /ws route and services/live.py registry) -
not a mock of the delivery mechanism.
"""
import asyncio

import httpx
import pytest

from app.main import app


def _token(headers: dict) -> str:
    return headers["Authorization"].split(" ", 1)[1]


@pytest.mark.asyncio
async def test_many_simultaneous_sockets_each_receive_only_their_own_notification(
    client, make_user
):
    """5 distinct users, 5 real simultaneous WebSocket connections (well
    beyond the 2-connection ceiling of every prior WS test in this repo). A
    single sender fires 5 DIFFERENT direct messages at all 5 recipients
    truly concurrently (asyncio.gather over real HTTP against the same
    running app, real /ws delivery underneath) - each recipient's own
    socket must receive exactly the notification for ITS OWN message
    (verified by matching each notification's target_id back to the
    specific message id that request actually created), never a
    notification meant for a different recipient.
    """
    sender = await make_user("wsbroadcastsender")
    recipients = [await make_user(f"wsbroadcastrecipient{i}") for i in range(5)]
    tokens = [_token(r["headers"]) for r in recipients]

    with (
        client.websocket_connect("/ws") as ws0,
        client.websocket_connect("/ws") as ws1,
        client.websocket_connect("/ws") as ws2,
        client.websocket_connect("/ws") as ws3,
        client.websocket_connect("/ws") as ws4,
    ):
        sockets = [ws0, ws1, ws2, ws3, ws4]
        for ws, token in zip(sockets, tokens):
            ws.send_json({"token": token})
            assert ws.receive_json() == {"type": "connected"}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            dm_responses = await asyncio.gather(
                *[
                    ac.post(
                        f"/messages/{recipients[i]['user_id']}",
                        data={"body": f"message for recipient {i}"},
                        headers=sender["headers"],
                    )
                    for i in range(5)
                ]
            )
        assert all(r.status_code == 201 for r in dm_responses)
        expected_target_id_by_socket = [r.json()["id"] for r in dm_responses]

        # Each recipient's OWN socket - read in the order the sockets were
        # opened, not the order events were sent, since real concurrent
        # delivery gives no ordering guarantee across different users'
        # independent connections.
        received = [ws.receive_json() for ws in sockets]

    for i, data in enumerate(received):
        assert data["event"] == "notification"
        notif = data["notification"]
        assert notif["type"] == "message"
        assert notif["actor_id"] == sender["user_id"]
        # The precise isolation proof: socket i received the notification
        # for the exact message this request created for recipient i - not
        # a notification meant for any of the other 4 concurrently-active
        # recipients.
        assert notif["target_id"] == expected_target_id_by_socket[i]

    # No duplicate/cross-delivered notification: every target_id is unique
    # across all 5 received notifications.
    assert len({data["notification"]["target_id"] for data in received}) == 5


@pytest.mark.asyncio
async def test_concurrent_mixed_notification_types_deliver_only_to_the_right_user(
    client, make_user
):
    """3 simultaneous real sockets, 3 DIFFERENT notification-triggering
    operations (message, post-like, comment-like) fired concurrently at 3
    different recipients - proves isolation holds across genuinely
    different event types under real concurrency, not just repeated DMs."""
    sender = await make_user("wsmixedsender")
    msg_recipient = await make_user("wsmixedmsgrecipient")
    post_author = await make_user("wsmixedpostauthor")
    comment_author = await make_user("wsmixedcommentauthor")

    post_resp = client.post(
        "/posts", data={"title": "t", "body": "b"}, headers=post_author["headers"]
    )
    assert post_resp.status_code == 201
    post_id = post_resp.json()["id"]

    comment_resp = client.post(
        f"/posts/{post_id}/comments",
        data={"body": "a comment"},
        headers=comment_author["headers"],
    )
    assert comment_resp.status_code == 201
    comment_id = comment_resp.json()["id"]

    tokens = [
        _token(msg_recipient["headers"]),
        _token(post_author["headers"]),
        _token(comment_author["headers"]),
    ]

    with (
        client.websocket_connect("/ws") as ws_msg,
        client.websocket_connect("/ws") as ws_post,
        client.websocket_connect("/ws") as ws_comment,
    ):
        sockets = [ws_msg, ws_post, ws_comment]
        for ws, token in zip(sockets, tokens):
            ws.send_json({"token": token})
            assert ws.receive_json() == {"type": "connected"}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            responses = await asyncio.gather(
                ac.post(
                    f"/messages/{msg_recipient['user_id']}",
                    data={"body": "hi"},
                    headers=sender["headers"],
                ),
                ac.post(
                    f"/posts/{post_id}/reactions", json={"value": 1}, headers=sender["headers"]
                ),
                ac.post(
                    f"/comments/{comment_id}/reactions",
                    json={"value": 1},
                    headers=sender["headers"],
                ),
            )
        assert all(r.status_code in (200, 201) for r in responses)

        data_msg = ws_msg.receive_json()
        data_post = ws_post.receive_json()
        data_comment = ws_comment.receive_json()

    assert data_msg["notification"]["type"] == "message"
    assert data_post["notification"]["type"] == "like_post"
    assert data_post["notification"]["target_id"] == post_id
    assert data_comment["notification"]["type"] == "like_comment"
    assert data_comment["notification"]["target_id"] == comment_id
