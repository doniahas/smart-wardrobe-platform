"""Stress testing: a broad, cross-feature multi-user scenario
where several different users perform genuinely DIFFERENT kinds of
operations - not the same operation repeated - all truly concurrently
(asyncio.gather over real HTTP against the running app via a raw ASGI
transport, the same idiom test_chat_queue_unification.py already uses for
real concurrent requests), proving both that every operation succeeds
correctly under concurrent cross-feature load and that per-user data
isolation holds throughout.

Complements the many existing narrow, single-feature concurrency tests
(chat queue/worker in test_chat_concurrency.py/test_chat_queue_unification.py,
reactions/comments/notifications dedup races in test_reactions.py/
test_notification_triggers.py, the DM conversation-creation race in
test_messages_route.py, the crawler semaphore in
services/catalog/tests/test_crawler_concurrency.py) - none of which combine
different feature areas in one concurrent burst. This file closes exactly
that one gap, not a general-purpose load harness.
"""
import asyncio

import httpx
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_multiple_users_different_operations_run_correctly_and_stay_isolated(
    client, db, make_user, make_wardrobe_item, monkeypatch
):
    """Four distinct users, four distinct operation types, fired at the
    exact same instant: wardrobe-item creation, a recommendation request, a
    forum post, and a direct message. Every operation must succeed, and
    afterward each user's own data must be exactly and only their own - no
    cross-user leakage caused by running genuinely different write paths
    concurrently against the same database.
    """

    # Only the app's OWN internal outbound call to the recommendation
    # service (matched by URL) is faked - everything else, including this
    # test's own outer ASGI-transport calls below (which are also
    # httpx.AsyncClient.post calls, just into the app rather than out of
    # it), must fall through to the real implementation unchanged.
    _real_post = httpx.AsyncClient.post

    async def fake_post(self, url, json=None, **kwargs):
        if str(url).endswith("/recommend"):
            return httpx.Response(200, json={"selected_items": [], "explanation": "ok"})
        return await _real_post(self, url, json=json, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    wardrobe_user = await make_user("mixop-wardrobe")
    rec_user = await make_user("mixop-rec")
    # rec_user needs an existing item for a meaningful recommendation call -
    # created before the concurrent burst, same as every other rate-limit/
    # recommendation test in this suite does.
    await make_wardrobe_item(rec_user["user_id"], name="Existing Item")
    post_user = await make_user("mixop-post")
    dm_sender = await make_user("mixop-dm-sender")
    dm_recipient = await make_user("mixop-dm-recipient")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        wardrobe_resp, rec_resp, post_resp, dm_resp = await asyncio.gather(
            ac.post(
                "/wardrobe",
                data={
                    "name": "Mixed-ops Concurrent Item",
                    "clothing_type": "top",
                    "color": "blue",
                    "season": "all",
                    "style": "casual",
                    "occasion": "casual",
                },
                headers=wardrobe_user["headers"],
            ),
            ac.post("/recommendations", json={}, headers=rec_user["headers"]),
            ac.post(
                "/posts",
                data={"title": "Mixed-ops post", "body": "Concurrent body text"},
                headers=post_user["headers"],
            ),
            ac.post(
                f"/messages/{dm_recipient['user_id']}",
                data={"body": "hello from concurrent mixed-ops test"},
                headers=dm_sender["headers"],
            ),
        )

    # --- every genuinely different operation succeeded ---
    assert wardrobe_resp.status_code == 201
    assert rec_resp.status_code == 200
    assert post_resp.status_code == 201
    assert dm_resp.status_code == 201

    # --- isolation: each user's own data is exactly and only their own ---

    all_users = [wardrobe_user, rec_user, post_user, dm_sender, dm_recipient]

    # wardrobe_user's own wardrobe list contains exactly the one item they
    # created - not duplicated, not merged with anyone else's.
    wardrobe_list_resp = client.get("/wardrobe", headers=wardrobe_user["headers"])
    assert wardrobe_list_resp.status_code == 200
    items = wardrobe_list_resp.json()
    assert len(items) == 1
    assert items[0]["name"] == "Mixed-ops Concurrent Item"

    # No other concurrently-active user's wardrobe gained this item as a
    # side effect of the concurrent burst.
    for other in (rec_user, post_user, dm_sender, dm_recipient):
        other_list = client.get("/wardrobe", headers=other["headers"])
        assert other_list.status_code == 200
        assert all(item["name"] != "Mixed-ops Concurrent Item" for item in other_list.json())

    # rec_user's wardrobe still contains only their own pre-existing item -
    # the concurrent recommendation request read but never wrote wardrobe data.
    rec_wardrobe_resp = client.get("/wardrobe", headers=rec_user["headers"])
    assert rec_wardrobe_resp.status_code == 200
    rec_items = rec_wardrobe_resp.json()
    assert len(rec_items) == 1
    assert rec_items[0]["name"] == "Existing Item"

    # post_user's post is correctly attributed and visible via /posts/mine -
    # never attributed to any of the other concurrently-active users.
    my_posts_resp = client.get("/posts/mine", headers=post_user["headers"])
    assert my_posts_resp.status_code == 200
    my_posts_items = my_posts_resp.json()["items"]
    assert len(my_posts_items) == 1
    assert my_posts_items[0]["title"] == "Mixed-ops post"

    for other in (wardrobe_user, rec_user, dm_sender, dm_recipient):
        other_posts_resp = client.get("/posts/mine", headers=other["headers"])
        assert other_posts_resp.status_code == 200
        assert other_posts_resp.json()["items"] == []

    # The DM is visible to its two real participants, in their shared
    # conversation - and to no one else who happened to be concurrently
    # active on an unrelated operation.
    sender_history = client.get(
        f"/messages/{dm_recipient['user_id']}", headers=dm_sender["headers"]
    )
    assert sender_history.status_code == 200
    assert any(
        m["body"] == "hello from concurrent mixed-ops test"
        for m in sender_history.json()["items"]
    )

    recipient_history = client.get(
        f"/messages/{dm_sender['user_id']}", headers=dm_recipient["headers"]
    )
    assert recipient_history.status_code == 200
    assert any(
        m["body"] == "hello from concurrent mixed-ops test"
        for m in recipient_history.json()["items"]
    )

    for outsider in (wardrobe_user, rec_user, post_user):
        outsider_convos = client.get("/messages/conversations", headers=outsider["headers"])
        assert outsider_convos.status_code == 200
        assert outsider_convos.json()["items"] == []

    # Sanity: every user really is distinct (no accidental id collision that
    # would make the isolation checks above trivially/accidentally pass).
    assert len({u["user_id"] for u in all_users}) == len(all_users)
