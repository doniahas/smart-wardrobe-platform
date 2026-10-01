import pytest
from bson import ObjectId

from app.services.notifications import create_notification


async def _seed(db, recipient_id: str, actor_id: str = "actor", actor_username: str = "actor_name") -> None:
    await create_notification(
        db,
        recipient_id=recipient_id,
        actor_id=actor_id,
        actor_username=actor_username,
        notif_type="message",
        target_type="message",
        target_id=ObjectId(),
    )


# --- list ---


@pytest.mark.asyncio
async def test_list_notifications_requires_auth(client):
    resp = client.get("/notifications")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_notifications_scoped_to_caller(client, make_user, db):
    a = await make_user("notiflist_a")
    b = await make_user("notiflist_b")
    await _seed(db, a["user_id"])
    await _seed(db, b["user_id"])

    resp = client.get("/notifications", headers=a["headers"])
    assert len(resp.json()["items"]) == 1


@pytest.mark.asyncio
async def test_list_notifications_newest_first(client, make_user, db):
    a = await make_user("notiforder_a")
    await _seed(db, a["user_id"], actor_id="x1", actor_username="x1")
    await _seed(db, a["user_id"], actor_id="x2", actor_username="x2")

    resp = client.get("/notifications", headers=a["headers"])
    actors = [n["actor_id"] for n in resp.json()["items"]]
    assert actors == ["x2", "x1"]


@pytest.mark.asyncio
async def test_list_notifications_pagination(client, make_user, db):
    a = await make_user("notifpage_a")
    for i in range(3):
        await _seed(db, a["user_id"], actor_id=f"actor{i}", actor_username=f"actor{i}")

    resp = client.get("/notifications?page=1&page_size=2", headers=a["headers"])
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True


@pytest.mark.asyncio
async def test_list_notifications_shape(client, make_user, db):
    a = await make_user("notifshape_a")
    await _seed(db, a["user_id"])

    item = client.get("/notifications", headers=a["headers"]).json()["items"][0]
    assert item["type"] == "message"
    assert item["actor_id"] == "actor"
    assert item["actor_username"] == "actor_name"
    assert item["target_type"] == "message"
    assert item["read"] is False
    assert item["id"]
    assert item["target_id"]
    assert item["created_at"]


# --- unread count ---


@pytest.mark.asyncio
async def test_unread_count_requires_auth(client):
    resp = client.get("/notifications/unread-count")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unread_count_correct(client, make_user, db):
    a = await make_user("unreadcount_a")
    await _seed(db, a["user_id"])
    await _seed(db, a["user_id"])

    resp = client.get("/notifications/unread-count", headers=a["headers"])
    assert resp.json()["count"] == 2


# --- mark one read ---


@pytest.mark.asyncio
async def test_mark_one_read_success(client, make_user, db):
    a = await make_user("markone_a")
    await _seed(db, a["user_id"])
    notif_id = client.get("/notifications", headers=a["headers"]).json()["items"][0]["id"]

    resp = client.post(f"/notifications/{notif_id}/read", headers=a["headers"])
    assert resp.status_code == 200
    assert resp.json()["read"] is True

    count = client.get("/notifications/unread-count", headers=a["headers"]).json()["count"]
    assert count == 0


@pytest.mark.asyncio
async def test_mark_one_read_is_idempotent(client, make_user, db):
    a = await make_user("markidempotent_a")
    await _seed(db, a["user_id"])
    notif_id = client.get("/notifications", headers=a["headers"]).json()["items"][0]["id"]

    resp1 = client.post(f"/notifications/{notif_id}/read", headers=a["headers"])
    resp2 = client.post(f"/notifications/{notif_id}/read", headers=a["headers"])
    assert resp1.status_code == 200
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_mark_one_read_requires_auth(client, make_user, db):
    a = await make_user("markauthrequired_a")
    await _seed(db, a["user_id"])
    notif_id = client.get("/notifications", headers=a["headers"]).json()["items"][0]["id"]

    resp = client.post(f"/notifications/{notif_id}/read")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mark_one_read_nonexistent_returns_404(client, make_user):
    a = await make_user("marknonexistent_a")
    resp = client.post("/notifications/000000000000000000000000/read", headers=a["headers"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mark_one_read_invalid_id_returns_400(client, make_user):
    a = await make_user("markinvalid_a")
    resp = client.post("/notifications/not-an-id/read", headers=a["headers"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cannot_mark_another_users_notification_read(client, make_user, db):
    a = await make_user("idor_a")
    b = await make_user("idor_b")
    await _seed(db, a["user_id"])
    notif_id = client.get("/notifications", headers=a["headers"]).json()["items"][0]["id"]

    resp = client.post(f"/notifications/{notif_id}/read", headers=b["headers"])
    assert resp.status_code == 404

    count = client.get("/notifications/unread-count", headers=a["headers"]).json()["count"]
    assert count == 1


# --- mark all read ---


@pytest.mark.asyncio
async def test_mark_all_read_requires_auth(client):
    resp = client.post("/notifications/mark-all-read")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mark_all_read_scoped_to_caller(client, make_user, db):
    a = await make_user("markall_a")
    b = await make_user("markall_b")
    for _ in range(3):
        await _seed(db, a["user_id"])
    await _seed(db, b["user_id"])

    resp = client.post("/notifications/mark-all-read", headers=a["headers"])
    assert resp.status_code == 200
    assert resp.json()["updated_count"] == 3

    assert client.get("/notifications/unread-count", headers=a["headers"]).json()["count"] == 0
    assert client.get("/notifications/unread-count", headers=b["headers"]).json()["count"] == 1


# --- create_notification self-suppression (unit-level) ---


@pytest.mark.asyncio
async def test_create_notification_suppresses_self_notification(db):
    await create_notification(
        db,
        recipient_id="same",
        actor_id="same",
        actor_username="same",
        notif_type="message",
        target_type="message",
        target_id=ObjectId(),
    )
    count = await db.notifications.count_documents({})
    assert count == 0
