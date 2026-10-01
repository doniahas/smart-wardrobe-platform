import pytest
from starlette.websockets import WebSocketDisconnect

from app.services import live


def _token(headers: dict) -> str:
    return headers["Authorization"].split(" ", 1)[1]


# --- registry hygiene ---


def test_live_registry_is_empty_at_test_start():
    """Indirectly proves the autouse _reset_live_connections fixture in
    conftest.py actually runs - if a previous test's connection leaked
    through, this would fail depending on test order."""
    assert live._connections == {}


# --- auth handshake ---


@pytest.mark.asyncio
async def test_successful_auth_receives_connected_ack(client, make_user):
    user = await make_user("wsauthuser")
    token = _token(user["headers"])

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"token": token})
        assert ws.receive_json() == {"type": "connected"}


@pytest.mark.asyncio
async def test_invalid_token_closes_connection(client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"token": "not-a-real-token"})
            ws.receive_json()
    assert exc_info.value.code == 1008


@pytest.mark.asyncio
async def test_malformed_first_frame_closes_connection(client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"not_a_token_field": "x"})
            ws.receive_json()
    assert exc_info.value.code == 1008


@pytest.mark.asyncio
async def test_auth_timeout_closes_connection(client, monkeypatch):
    import app.routers.ws as ws_module

    monkeypatch.setattr(ws_module, "WS_AUTH_TIMEOUT_SECONDS", 0.1)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # never send the auth frame
    assert exc_info.value.code == 1008


# --- connection limit ---


@pytest.mark.asyncio
async def test_connection_limit_enforced(client, make_user, monkeypatch):
    import app.services.live as live_module

    monkeypatch.setattr(live_module, "MAX_WS_CONNECTIONS_PER_USER", 2)

    user = await make_user("wslimituser")
    token = _token(user["headers"])

    with client.websocket_connect("/ws") as ws1:
        ws1.send_json({"token": token})
        ws1.receive_json()

        with client.websocket_connect("/ws") as ws2:
            ws2.send_json({"token": token})
            ws2.receive_json()

            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect("/ws") as ws3:
                    ws3.send_json({"token": token})
                    ws3.receive_json()
            assert exc_info.value.code == 1008


@pytest.mark.asyncio
async def test_slot_frees_after_disconnect(client, make_user, monkeypatch):
    import app.services.live as live_module

    monkeypatch.setattr(live_module, "MAX_WS_CONNECTIONS_PER_USER", 1)

    user = await make_user("wsslotuser")
    token = _token(user["headers"])

    with client.websocket_connect("/ws") as ws1:
        ws1.send_json({"token": token})
        ws1.receive_json()
    # ws1 is now closed - its slot must be free

    with client.websocket_connect("/ws") as ws2:
        ws2.send_json({"token": token})
        assert ws2.receive_json() == {"type": "connected"}


# --- HTTP auth regression (get_user_from_token extraction) ---


@pytest.mark.asyncio
async def test_http_auth_unchanged_after_refactor(client, make_user):
    """Regression guard: extracting get_user_from_token out of
    get_current_user must not change HTTP auth behavior at all."""
    user = await make_user("httpauthunchanged")

    resp = client.get("/auth/me", headers=user["headers"])
    assert resp.status_code == 200

    resp = client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Could not validate credentials"

    resp = client.get("/auth/me")
    assert resp.status_code == 401


# --- live delivery ---


@pytest.mark.asyncio
async def test_message_notification_delivered_live(client, make_user):
    sender = await make_user("wsmsgsender")
    recipient = await make_user("wsmsgrecipient")
    token = _token(recipient["headers"])

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"token": token})
        ws.receive_json()  # connected ack

        client.post(
            f"/messages/{recipient['user_id']}", data={"body": "hi"}, headers=sender["headers"]
        )

        data = ws.receive_json()
        assert data["event"] == "notification"
        notif = data["notification"]
        assert notif["type"] == "message"
        assert notif["actor_id"] == sender["user_id"]
        assert notif["target_type"] == "message"
        assert "body" not in notif  # identifiers-only, no message content inlined


@pytest.mark.asyncio
async def test_like_post_notification_delivered_live(client, make_user):
    author = await make_user("wslikeauthor")
    reactor = await make_user("wslikereactor")
    token = _token(author["headers"])

    post_resp = client.post("/posts", data={"title": "t", "body": "b"}, headers=author["headers"])
    post_id = post_resp.json()["id"]

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"token": token})
        ws.receive_json()

        client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=reactor["headers"])

        data = ws.receive_json()
        assert data["event"] == "notification"
        assert data["notification"]["type"] == "like_post"
        assert data["notification"]["target_id"] == post_id


@pytest.mark.asyncio
async def test_self_reaction_delivers_no_push(client, make_user):
    author = await make_user("wsselfreactauthor")
    token = _token(author["headers"])

    post_resp = client.post("/posts", data={"title": "t", "body": "b"}, headers=author["headers"])
    post_id = post_resp.json()["id"]

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"token": token})
        ws.receive_json()

        client.post(f"/posts/{post_id}/reactions", json={"value": 1}, headers=author["headers"])

        # Nothing further should ever arrive - close from our side and
        # confirm no notification was queued by checking persistence too.
        notifs = client.get("/notifications", headers=author["headers"]).json()["items"]
        assert notifs == []


@pytest.mark.asyncio
async def test_offline_recipient_notification_still_persisted(client, make_user):
    sender = await make_user("wsofflinesender")
    recipient = await make_user("wsofflinerecipient")  # never connects

    resp = client.post(
        f"/messages/{recipient['user_id']}", data={"body": "hi"}, headers=sender["headers"]
    )
    assert resp.status_code == 201

    notifs = client.get("/notifications", headers=recipient["headers"]).json()["items"]
    assert len(notifs) == 1
    assert notifs[0]["type"] == "message"


@pytest.mark.asyncio
async def test_all_tabs_receive_the_same_event(client, make_user):
    sender = await make_user("wsmultitabsender")
    recipient = await make_user("wsmultitabrecipient")
    token = _token(recipient["headers"])

    with client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        ws1.send_json({"token": token})
        ws1.receive_json()
        ws2.send_json({"token": token})
        ws2.receive_json()

        client.post(
            f"/messages/{recipient['user_id']}", data={"body": "hi"}, headers=sender["headers"]
        )

        data1 = ws1.receive_json()
        data2 = ws2.receive_json()
        assert data1["event"] == "notification"
        assert data2["event"] == "notification"


@pytest.mark.asyncio
async def test_failed_send_does_not_prevent_delivery_to_other_sockets(client, make_user):
    sender = await make_user("wsfailsender")
    recipient = await make_user("wsfailrecipient")
    token = _token(recipient["headers"])
    user_id = recipient["user_id"]

    with client.websocket_connect("/ws") as good_ws:
        good_ws.send_json({"token": token})
        good_ws.receive_json()

        class _BrokenWebSocket:
            async def send_json(self, data):
                raise RuntimeError("simulated broken connection")

        broken = _BrokenWebSocket()
        live.register(user_id, broken)

        client.post(
            f"/messages/{user_id}", data={"body": "hi"}, headers=sender["headers"]
        )

        data = good_ws.receive_json()
        assert data["event"] == "notification"
        assert data["notification"]["type"] == "message"

        assert broken not in live._connections.get(user_id, set())
