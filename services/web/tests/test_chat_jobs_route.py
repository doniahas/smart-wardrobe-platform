import pytest
from bson import ObjectId


def test_submit_chat_job_requires_auth(client):
    resp = client.post("/chat/jobs", json={"message": "Hi"})
    assert resp.status_code == 401


def test_get_chat_job_requires_auth(client):
    resp = client.get("/chat/jobs/000000000000000000000000")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_submit_chat_job_creates_pending_job(client, make_user):
    user = await make_user("alice")

    resp = client.post("/chat/jobs", json={"message": "What should I wear?"}, headers=user["headers"])

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert body["result"] is None
    assert body["error_reason"] is None
    assert body["completed_at"] is None
    assert body["job_id"]


@pytest.mark.asyncio
async def test_submit_chat_job_empty_message_returns_422(client, make_user):
    user = await make_user("bob")
    resp = client.post("/chat/jobs", json={"message": "   "}, headers=user["headers"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_submit_chat_job_stores_memory_aware_prompt(client, db, make_user):
    user = await make_user("carol")
    put_resp = client.put("/memory", json={"liked_colors": ["blue"]}, headers=user["headers"])
    assert put_resp.status_code == 200

    resp = client.post("/chat/jobs", json={"message": "What should I wear?"}, headers=user["headers"])
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    stored = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert "Liked colors: blue" in stored["prompt"]
    assert "[USER_MESSAGE]\nWhat should I wear?\n[/USER_MESSAGE]" in stored["prompt"]
    assert stored["priority"] == 0
    assert stored["status"] == "pending"


@pytest.mark.asyncio
async def test_get_chat_job_returns_submitted_job(client, make_user):
    user = await make_user("dave")
    submit_resp = client.post("/chat/jobs", json={"message": "Hi"}, headers=user["headers"])
    job_id = submit_resp.json()["job_id"]

    resp = client.get(f"/chat/jobs/{job_id}", headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["status"] == "pending"


@pytest.mark.asyncio
async def test_get_chat_job_cross_user_returns_404(client, make_user):
    owner = await make_user("erin")
    other = await make_user("frank")
    submit_resp = client.post("/chat/jobs", json={"message": "Hi"}, headers=owner["headers"])
    job_id = submit_resp.json()["job_id"]

    resp = client.get(f"/chat/jobs/{job_id}", headers=other["headers"])

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_chat_job_invalid_id_returns_400(client, make_user):
    user = await make_user("grace")
    resp = client.get("/chat/jobs/not-a-valid-object-id", headers=user["headers"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_chat_job_nonexistent_valid_id_returns_404(client, make_user):
    user = await make_user("heidi")
    resp = client.get("/chat/jobs/000000000000000000000000", headers=user["headers"])
    assert resp.status_code == 404
