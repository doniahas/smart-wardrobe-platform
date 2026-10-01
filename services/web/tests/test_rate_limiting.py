import httpx
import pytest

from app.core.config import (
    RATE_LIMIT_AUTH,
    RATE_LIMIT_CATALOG,
    RATE_LIMIT_CHAT,
    RATE_LIMIT_COMMENT_WRITE,
    RATE_LIMIT_FEEDBACK,
    RATE_LIMIT_MEMORY_WRITE,
    RATE_LIMIT_MESSAGE_WRITE,
    RATE_LIMIT_OUTFIT_WRITE,
    RATE_LIMIT_POST_WRITE,
    RATE_LIMIT_REACTION_WRITE,
    RATE_LIMIT_RECOMMENDATION,
    RATE_LIMIT_WARDROBE_WRITE,
)


def _limit_count(limit_str: str) -> int:
    """'5/minute' -> 5. Read from config so the test tracks the configured
    default instead of hard-coding a number that could silently drift."""
    return int(limit_str.split("/")[0])


def _register_payload(username: str) -> dict:
    return {"username": username, "email": f"{username}@example.com", "password": "password123"}


def _wardrobe_form() -> dict:
    return {
        "name": "Item",
        "clothing_type": "top",
        "color": "red",
        "season": "all",
        "style": "casual",
        "occasion": "casual",
    }


async def _fake_recommend_post(self, url, json=None, **kwargs):
    return httpx.Response(
        200,
        json={"selected_items": [], "explanation": "test explanation"},
    )


async def _fake_catalog_crawl_post(self, url, json=None, **kwargs):
    """GET /recommendations/catalog-suggestions calls the
    catalog service's POST /crawl (via catalog_gateway.py), not /recommend -
    a distinct fake response shape from _fake_recommend_post above."""
    return httpx.Response(200, json={"products": [], "source_status": []})


async def _fake_chat_llm_post(self, url, json=None, **kwargs):
    """POST /chat now creates and self-claims a real chat_jobs document and
    runs it through chat_worker.py::run_claimed_job (see chat-queue-
    unification), which calls the llm service's /generate over this same
    httpx.AsyncClient.post seam - faking it here is what lets each within-
    limit request actually reach 200 instead of failing for an unrelated
    reason (a real/mocked LLM outage) that would make this a false 429."""
    return httpx.Response(200, json={"response": "ok", "model": "qwen2.5:3b"})


# --- /auth/register and /auth/login ---


def test_register_allows_requests_up_to_the_limit_then_429(client):
    limit = _limit_count(RATE_LIMIT_AUTH)

    for i in range(limit):
        resp = client.post("/auth/register", json=_register_payload(f"reguser{i}"))
        assert resp.status_code == 201

    resp = client.post("/auth/register", json=_register_payload("reguser_overflow"))
    assert resp.status_code == 429


def test_login_allows_requests_up_to_the_limit_then_429(client):
    limit = _limit_count(RATE_LIMIT_AUTH)
    client.post("/auth/register", json=_register_payload("loginuser"))

    for _ in range(limit):
        resp = client.post(
            "/auth/login", json={"username": "loginuser", "password": "password123"}
        )
        assert resp.status_code == 200

    resp = client.post(
        "/auth/login", json={"username": "loginuser", "password": "password123"}
    )
    assert resp.status_code == 429


def test_one_normal_register_request_still_works(client):
    """Regression guard: a single, non-abusive request must never be blocked."""
    resp = client.post("/auth/register", json=_register_payload("normaluser"))
    assert resp.status_code == 201


# --- /recommendations ---


@pytest.mark.asyncio
async def test_recommendation_requests_are_rate_limited(
    client, make_user, make_wardrobe_item, monkeypatch
):
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_recommend_post)
    user = await make_user("recuser")
    await make_wardrobe_item(user["user_id"])

    limit = _limit_count(RATE_LIMIT_RECOMMENDATION)
    for _ in range(limit):
        resp = client.post("/recommendations", json={}, headers=user["headers"])
        assert resp.status_code == 200

    resp = client.post("/recommendations", json={}, headers=user["headers"])
    assert resp.status_code == 429


def test_recommendation_requires_auth_regardless_of_rate_limit_state(client):
    """Security check: an unauthenticated request must be rejected with 401,
    never leak data via a 429/200, and must not consume a rate-limit slot
    that would otherwise mask the missing auth check."""
    resp = client.post("/recommendations", json={})
    assert resp.status_code == 401


# --- GET /recommendations/catalog-suggestions ---


@pytest.mark.asyncio
async def test_catalog_suggestions_requests_are_rate_limited(client, make_user, monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_catalog_crawl_post)
    user = await make_user("cataloguser")

    limit = _limit_count(RATE_LIMIT_CATALOG)
    for _ in range(limit):
        resp = client.get(
            "/recommendations/catalog-suggestions",
            params={"clothing_type": "top"},
            headers=user["headers"],
        )
        assert resp.status_code == 200

    resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "top"},
        headers=user["headers"],
    )
    assert resp.status_code == 429


def test_catalog_suggestions_requires_auth_regardless_of_rate_limit_state(client):
    """Same security check as /recommendations above: an unauthenticated
    request must be rejected with 401, not 429/200, and must not consume a
    rate-limit slot that would otherwise mask the missing auth check."""
    resp = client.get(
        "/recommendations/catalog-suggestions", params={"clothing_type": "top"}
    )
    assert resp.status_code == 401


# --- GET /posts/{id}/recreate ---


@pytest.mark.asyncio
async def test_recreate_requests_are_rate_limited(client, make_user, make_wardrobe_item):
    """Recreate reuses RATE_LIMIT_RECOMMENDATION's configured value, but as
    its own independent bucket (key_style="endpoint") - not shared with
    /recommendations even though the two routes use the same limit string."""
    user = await make_user("recreateratelimituser")
    item_id = await make_wardrobe_item(user["user_id"])
    outfit_resp = client.post("/outfits", json={"item_ids": [item_id]}, headers=user["headers"])
    outfit_id = outfit_resp.json()["id"]
    post_resp = client.post(
        "/posts",
        data={"title": "t", "body": "b", "shared_outfit_id": outfit_id},
        headers=user["headers"],
    )
    post_id = post_resp.json()["id"]

    limit = _limit_count(RATE_LIMIT_RECOMMENDATION)
    for _ in range(limit):
        resp = client.get(f"/posts/{post_id}/recreate", headers=user["headers"])
        assert resp.status_code == 200

    resp = client.get(f"/posts/{post_id}/recreate", headers=user["headers"])
    assert resp.status_code == 429


def test_recreate_requires_auth(client):
    resp = client.get("/posts/000000000000000000000000/recreate")
    assert resp.status_code == 401


# --- POST /wardrobe (create item, incl. file-upload path) ---


@pytest.mark.asyncio
async def test_wardrobe_create_requests_are_rate_limited(client, make_user):
    user = await make_user("wardrobeuser")
    limit = _limit_count(RATE_LIMIT_WARDROBE_WRITE)

    for _ in range(limit):
        resp = client.post("/wardrobe", data=_wardrobe_form(), headers=user["headers"])
        assert resp.status_code == 201

    resp = client.post("/wardrobe", data=_wardrobe_form(), headers=user["headers"])
    assert resp.status_code == 429


def test_wardrobe_create_requires_auth(client):
    resp = client.post("/wardrobe", data=_wardrobe_form())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wardrobe_update_requests_are_rate_limited(client, make_user, make_wardrobe_item):
    user = await make_user("wardrobeupdateuser")
    item_id = await make_wardrobe_item(user["user_id"])
    limit = _limit_count(RATE_LIMIT_WARDROBE_WRITE)

    for _ in range(limit):
        resp = client.put(f"/wardrobe/{item_id}", data={"name": "Updated"}, headers=user["headers"])
        assert resp.status_code == 200

    resp = client.put(f"/wardrobe/{item_id}", data={"name": "Updated"}, headers=user["headers"])
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_wardrobe_delete_requests_are_rate_limited(client, make_user, make_wardrobe_item):
    user = await make_user("wardrobedeleteuser")
    limit = _limit_count(RATE_LIMIT_WARDROBE_WRITE)
    item_ids = [await make_wardrobe_item(user["user_id"]) for _ in range(limit)]

    for item_id in item_ids:
        resp = client.delete(f"/wardrobe/{item_id}", headers=user["headers"])
        assert resp.status_code == 204

    extra_item_id = await make_wardrobe_item(user["user_id"])
    resp = client.delete(f"/wardrobe/{extra_item_id}", headers=user["headers"])
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_wardrobe_write_limit_does_not_throttle_wardrobe_reads(
    client, make_user, make_wardrobe_item
):
    """Regression guard: GET /wardrobe(/{id}) has no @limiter.limit decorator,
    so it must keep working under the much higher RATE_LIMIT_DEFAULT even
    after the stricter write bucket for this same user is exhausted."""
    user = await make_user("wardrobereaduser")
    item_id = await make_wardrobe_item(user["user_id"])

    limit = _limit_count(RATE_LIMIT_WARDROBE_WRITE)
    for _ in range(limit):
        resp = client.put(f"/wardrobe/{item_id}", data={"name": "Updated"}, headers=user["headers"])
        assert resp.status_code == 200
    resp = client.put(f"/wardrobe/{item_id}", data={"name": "Updated"}, headers=user["headers"])
    assert resp.status_code == 429

    resp = client.get(f"/wardrobe/{item_id}", headers=user["headers"])
    assert resp.status_code == 200
    resp = client.get("/wardrobe", headers=user["headers"])
    assert resp.status_code == 200


# --- POST/DELETE /outfits ---


@pytest.mark.asyncio
async def test_outfit_create_requests_are_rate_limited(client, make_user, make_wardrobe_item):
    user = await make_user("outfitcreateuser")
    item_id = await make_wardrobe_item(user["user_id"])
    limit = _limit_count(RATE_LIMIT_OUTFIT_WRITE)

    for _ in range(limit):
        resp = client.post("/outfits", json={"item_ids": [item_id]}, headers=user["headers"])
        assert resp.status_code == 201

    resp = client.post("/outfits", json={"item_ids": [item_id]}, headers=user["headers"])
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_outfit_delete_requests_are_rate_limited(
    client, make_user, make_wardrobe_item, make_outfit
):
    user = await make_user("outfitdeleteuser")
    item_id = await make_wardrobe_item(user["user_id"])
    limit = _limit_count(RATE_LIMIT_OUTFIT_WRITE)
    outfit_ids = [await make_outfit(user["user_id"], [item_id]) for _ in range(limit)]

    for outfit_id in outfit_ids:
        resp = client.delete(f"/outfits/{outfit_id}", headers=user["headers"])
        assert resp.status_code == 204

    extra_outfit_id = await make_outfit(user["user_id"], [item_id])
    resp = client.delete(f"/outfits/{extra_outfit_id}", headers=user["headers"])
    assert resp.status_code == 429


# --- POST /feedback and PUT /feedback/{id} ---


@pytest.mark.asyncio
async def test_feedback_update_requests_are_rate_limited(
    client, make_user, make_wardrobe_item, make_outfit, make_feedback
):
    user = await make_user("feedbackuser")
    item_id = await make_wardrobe_item(user["user_id"])
    outfit_id = await make_outfit(user["user_id"], [item_id])
    await make_feedback(user["user_id"], outfit_id, rating=3, liked=None)

    feedback_list = client.get("/feedback", headers=user["headers"]).json()
    feedback_id = feedback_list[0]["id"]

    limit = _limit_count(RATE_LIMIT_FEEDBACK)
    for _ in range(limit):
        resp = client.put(
            f"/feedback/{feedback_id}", json={"rating": 4}, headers=user["headers"]
        )
        assert resp.status_code == 200

    resp = client.put(
        f"/feedback/{feedback_id}", json={"rating": 5}, headers=user["headers"]
    )
    assert resp.status_code == 429


def test_feedback_create_requires_auth(client):
    resp = client.post("/feedback", json={"outfit_id": "000000000000000000000000", "rating": 5})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_feedback_delete_requests_are_rate_limited(
    client, make_user, make_wardrobe_item, make_outfit, make_feedback
):
    user = await make_user("feedbackdeleteuser")
    item_id = await make_wardrobe_item(user["user_id"])
    limit = _limit_count(RATE_LIMIT_FEEDBACK)

    for _ in range(limit):
        outfit_id = await make_outfit(user["user_id"], [item_id])
        await make_feedback(user["user_id"], outfit_id, rating=3, liked=None)
    feedback_ids = [f["id"] for f in client.get("/feedback", headers=user["headers"]).json()]
    assert len(feedback_ids) == limit

    for feedback_id in feedback_ids:
        resp = client.delete(f"/feedback/{feedback_id}", headers=user["headers"])
        assert resp.status_code == 204

    outfit_id = await make_outfit(user["user_id"], [item_id])
    await make_feedback(user["user_id"], outfit_id, rating=3, liked=None)
    extra_feedback_id = client.get("/feedback", headers=user["headers"]).json()[0]["id"]
    resp = client.delete(f"/feedback/{extra_feedback_id}", headers=user["headers"])
    assert resp.status_code == 429


# --- POST /recommendation-feedback (reuses RATE_LIMIT_FEEDBACK) ---


@pytest.mark.asyncio
async def test_recommendation_feedback_create_requests_are_rate_limited(
    client, make_user, make_wardrobe_item
):
    user = await make_user("recfbratelimituser")
    item_id = await make_wardrobe_item(user["user_id"])
    limit = _limit_count(RATE_LIMIT_FEEDBACK)

    for _ in range(limit):
        resp = client.post(
            "/recommendation-feedback",
            json={"item_ids": [item_id], "liked": True},
            headers=user["headers"],
        )
        assert resp.status_code == 201

    resp = client.post(
        "/recommendation-feedback",
        json={"item_ids": [item_id], "liked": True},
        headers=user["headers"],
    )
    assert resp.status_code == 429


# --- PUT/PATCH /memory ---


@pytest.mark.asyncio
async def test_memory_put_requests_are_rate_limited(client, make_user):
    user = await make_user("memoryputuser")
    limit = _limit_count(RATE_LIMIT_MEMORY_WRITE)

    for _ in range(limit):
        resp = client.put("/memory", json={}, headers=user["headers"])
        assert resp.status_code == 200

    resp = client.put("/memory", json={}, headers=user["headers"])
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_memory_patch_requests_are_rate_limited(client, make_user):
    user = await make_user("memorypatchuser")
    setup_resp = client.put("/memory", json={}, headers=user["headers"])
    assert setup_resp.status_code == 200

    limit = _limit_count(RATE_LIMIT_MEMORY_WRITE)
    for _ in range(limit):
        resp = client.patch("/memory", json={}, headers=user["headers"])
        assert resp.status_code == 200

    resp = client.patch("/memory", json={}, headers=user["headers"])
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_memory_write_limit_does_not_throttle_memory_reads(client, make_user):
    """Regression guard: GET /memory has no @limiter.limit decorator, so it
    must keep working under RATE_LIMIT_DEFAULT even after this user's
    stricter RATE_LIMIT_MEMORY_WRITE bucket is exhausted."""
    user = await make_user("memoryreaduser")
    limit = _limit_count(RATE_LIMIT_MEMORY_WRITE)

    for _ in range(limit):
        resp = client.put("/memory", json={}, headers=user["headers"])
        assert resp.status_code == 200
    resp = client.put("/memory", json={}, headers=user["headers"])
    assert resp.status_code == 429

    resp = client.get("/memory", headers=user["headers"])
    assert resp.status_code == 200


# --- POST/DELETE /posts ---


@pytest.mark.asyncio
async def test_post_create_requests_are_rate_limited(client, make_user):
    user = await make_user("postcreateuser")
    limit = _limit_count(RATE_LIMIT_POST_WRITE)

    for _ in range(limit):
        resp = client.post(
            "/posts", data={"title": "t", "body": "b"}, headers=user["headers"]
        )
        assert resp.status_code == 201

    resp = client.post("/posts", data={"title": "t", "body": "b"}, headers=user["headers"])
    assert resp.status_code == 429


def test_post_create_requires_auth_regardless_of_rate_limit_state(client):
    """Security check: an unauthenticated request must be rejected with 401,
    never leak data via a 429/201, and must not consume a rate-limit slot
    that would otherwise mask the missing auth check."""
    resp = client.post("/posts", data={"title": "t", "body": "b"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_post_delete_requests_are_rate_limited(client, make_user, make_post):
    user = await make_user("postdeleteuser")
    limit = _limit_count(RATE_LIMIT_POST_WRITE)
    post_ids = [await make_post(user["user_id"]) for _ in range(limit)]

    for post_id in post_ids:
        resp = client.delete(f"/posts/{post_id}", headers=user["headers"])
        assert resp.status_code == 204

    extra_post_id = await make_post(user["user_id"])
    resp = client.delete(f"/posts/{extra_post_id}", headers=user["headers"])
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_post_write_limit_does_not_throttle_post_reads(client, make_user):
    """Regression guard: GET /posts and GET /posts/mine have no
    @limiter.limit decorator, so they must keep working under the much
    higher RATE_LIMIT_DEFAULT even after this user's stricter
    RATE_LIMIT_POST_WRITE bucket is exhausted."""
    user = await make_user("postreaduser")
    limit = _limit_count(RATE_LIMIT_POST_WRITE)

    for _ in range(limit):
        resp = client.post(
            "/posts", data={"title": "t", "body": "b"}, headers=user["headers"]
        )
        assert resp.status_code == 201
    resp = client.post("/posts", data={"title": "t", "body": "b"}, headers=user["headers"])
    assert resp.status_code == 429

    resp = client.get("/posts", headers=user["headers"])
    assert resp.status_code == 200
    resp = client.get("/posts/mine", headers=user["headers"])
    assert resp.status_code == 200


# --- POST /posts/{id}/reactions ---


@pytest.mark.asyncio
async def test_reaction_requests_are_rate_limited(client, make_user):
    author = await make_user("reactratelimitauthor")
    reactor = await make_user("reactratelimituser")
    resp = client.post("/posts", data={"title": "t", "body": "b"}, headers=author["headers"])
    post_id = resp.json()["id"]

    limit = _limit_count(RATE_LIMIT_REACTION_WRITE)
    for i in range(limit):
        resp = client.post(
            f"/posts/{post_id}/reactions",
            json={"value": 1 if i % 2 == 0 else -1},
            headers=reactor["headers"],
        )
        assert resp.status_code == 200

    resp = client.post(
        f"/posts/{post_id}/reactions", json={"value": 1}, headers=reactor["headers"]
    )
    assert resp.status_code == 429


def test_reaction_requires_auth_regardless_of_rate_limit_state(client):
    """Security check: an unauthenticated request must be rejected with 401,
    never leak data via a 429/200, and must not consume a rate-limit slot
    that would otherwise mask the missing auth check."""
    resp = client.post("/posts/000000000000000000000000/reactions", json={"value": 1})
    assert resp.status_code == 401


# --- POST /posts/{id}/comments, DELETE /comments/{id} ---


@pytest.mark.asyncio
async def test_comment_create_requests_are_rate_limited(client, make_user):
    author = await make_user("commentratelimitauthor")
    commenter = await make_user("commentratelimituser")
    resp = client.post("/posts", data={"title": "t", "body": "b"}, headers=author["headers"])
    post_id = resp.json()["id"]

    limit = _limit_count(RATE_LIMIT_COMMENT_WRITE)
    for _ in range(limit):
        resp = client.post(
            f"/posts/{post_id}/comments", data={"body": "hi"}, headers=commenter["headers"]
        )
        assert resp.status_code == 201

    resp = client.post(
        f"/posts/{post_id}/comments", data={"body": "hi"}, headers=commenter["headers"]
    )
    assert resp.status_code == 429


def test_comment_create_requires_auth_regardless_of_rate_limit_state(client):
    resp = client.post("/posts/000000000000000000000000/comments", data={"body": "hi"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_comment_delete_requests_are_rate_limited(client, make_user, make_post, make_comment):
    author_id = (await make_user("commentdeleteauthor"))["user_id"]
    user = await make_user("commentdeleteuser")
    post_id = await make_post(author_id)
    limit = _limit_count(RATE_LIMIT_COMMENT_WRITE)
    comment_ids = [await make_comment(post_id, user["user_id"]) for _ in range(limit)]

    for comment_id in comment_ids:
        resp = client.delete(f"/comments/{comment_id}", headers=user["headers"])
        assert resp.status_code == 204

    extra_comment_id = await make_comment(post_id, user["user_id"])
    resp = client.delete(f"/comments/{extra_comment_id}", headers=user["headers"])
    assert resp.status_code == 429


# --- POST /messages/{other_user_id} ---


@pytest.mark.asyncio
async def test_message_send_requests_are_rate_limited(client, make_user):
    sender = await make_user("msgratelimitsender")
    recipient = await make_user("msgratelimitrecipient")
    limit = _limit_count(RATE_LIMIT_MESSAGE_WRITE)

    for _ in range(limit):
        resp = client.post(
            f"/messages/{recipient['user_id']}", data={"body": "hi"}, headers=sender["headers"]
        )
        assert resp.status_code == 201

    resp = client.post(
        f"/messages/{recipient['user_id']}", data={"body": "hi"}, headers=sender["headers"]
    )
    assert resp.status_code == 429


def test_message_send_requires_auth_regardless_of_rate_limit_state(client):
    """Security check: an unauthenticated request must be rejected with 401,
    never leak data via a 429/201, and must not consume a rate-limit slot
    that would otherwise mask the missing auth check."""
    resp = client.post("/messages/000000000000000000000000", data={"body": "hi"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_message_write_limit_does_not_throttle_message_reads(client, make_user):
    """Regression guard: GET /messages/{id} and GET /messages/conversations
    have no @limiter.limit decorator, so they must keep working under the
    much higher RATE_LIMIT_DEFAULT even after this user's stricter
    RATE_LIMIT_MESSAGE_WRITE bucket is exhausted."""
    sender = await make_user("msgreaduser")
    recipient = await make_user("msgreadrecipient")
    limit = _limit_count(RATE_LIMIT_MESSAGE_WRITE)

    for _ in range(limit):
        resp = client.post(
            f"/messages/{recipient['user_id']}", data={"body": "hi"}, headers=sender["headers"]
        )
        assert resp.status_code == 201
    resp = client.post(
        f"/messages/{recipient['user_id']}", data={"body": "hi"}, headers=sender["headers"]
    )
    assert resp.status_code == 429

    resp = client.get(f"/messages/{recipient['user_id']}", headers=sender["headers"])
    assert resp.status_code == 200
    resp = client.get("/messages/conversations", headers=sender["headers"])
    assert resp.status_code == 200


# --- POST /chat ---


@pytest.mark.asyncio
async def test_chat_requests_are_rate_limited(client, make_user, monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_chat_llm_post)
    user = await make_user("chatratelimituser")

    limit = _limit_count(RATE_LIMIT_CHAT)
    for _ in range(limit):
        resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])
        assert resp.status_code == 200

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])
    assert resp.status_code == 429


def test_chat_requires_auth_regardless_of_rate_limit_state(client):
    """Security check: an unauthenticated request must be rejected with 401,
    never leak data via a 429/200, and must not consume a rate-limit slot
    that would otherwise mask the missing auth check."""
    resp = client.post("/chat", json={"message": "Hi"})
    assert resp.status_code == 401