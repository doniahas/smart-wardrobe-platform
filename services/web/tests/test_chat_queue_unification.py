"""Chat-queue-unification: focused tests proving the synchronous POST /chat
route now goes through the same Mongo-backed queue/worker machinery as
POST /chat/jobs (chat_queue.py::claim_job_by_id + chat_worker.py::
run_claimed_job), rather than calling the local LLM directly and bypassing
it - while /chat/jobs itself, the frontend, and every other guarantee
(auth/isolation, memory, catalog grounding, the hallucination guardrail,
error-code mapping) stay exactly as before.

Complements (does not replace) the existing suites this refactor must not
break: test_chat_route.py, test_chat_catalog_grounding_route.py,
test_chat_memory_learning_integration.py, test_chat_grounding_guardrail.py,
test_chat_queue.py, test_chat_worker.py, test_chat_concurrency.py,
test_chat_jobs_route.py - all of which continue to pass unchanged and are
the real regression net for this change; this file only adds the specific,
new proof points for the unification itself.
"""
import asyncio

import pytest
from bson import ObjectId

from app.services.chat_queue import (
    claim_job_by_id,
    claim_next_job,
    create_chat_job,
)
from app.services.chat_worker import process_one_job, start_chat_workers, stop_chat_workers
from app.services.llm_gateway import ChatGatewayFailure, ChatGatewayFailureReason


# --- 1. /chat creates and claims a real persisted job ----------------------


@pytest.mark.asyncio
async def test_chat_creates_and_completes_a_persisted_chat_jobs_document(
    client, db, make_user, monkeypatch
):
    """The synchronous route must not bypass the queue machinery: a real
    `chat_jobs` document is inserted, claimed (attempts=1), and completed -
    not merely called against the LLM in-request with nothing persisted."""
    user = await make_user("uni-alice")

    async def fake_call_llm(prompt):
        return "a synchronous reply"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json() == {"response": "a synchronous reply"}

    jobs = [doc async for doc in db.chat_jobs.find({"user_id": user["user_id"]})]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["status"] == "completed"
    assert job["result"] == "a synchronous reply"
    assert job["attempts"] == 1
    assert job["message"] == "Hi"


# --- 2. /chat and background worker jobs share one concurrency cap ---------


class _ConcurrencyTracker:
    def __init__(self):
        self.current = 0
        self.max_seen = 0
        self.lock = asyncio.Lock()
        self.completed = 0

    async def call(self, prompt: str, *, delay: float = 0.05) -> str:
        async with self.lock:
            self.current += 1
            self.max_seen = max(self.max_seen, self.current)
        try:
            await asyncio.sleep(delay)
        finally:
            async with self.lock:
                self.current -= 1
        self.completed += 1
        return f"reply to {prompt}"


@pytest.mark.asyncio
async def test_sync_chat_and_worker_jobs_share_the_same_concurrency_bound(
    client, db, make_user, monkeypatch
):
    """CHAT_WORKER_CONCURRENCY=2. Fire 3 concurrent /chat requests AND 3
    concurrent /chat/jobs (processed by real background workers) at once -
    the number of fake-LLM calls ever in flight simultaneously, across BOTH
    entry points combined, must never exceed 2."""
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_CONCURRENCY", 2)
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_POLL_INTERVAL_SECONDS", 0.01)

    tracker = _ConcurrencyTracker()
    monkeypatch.setattr("app.services.chat_worker.call_llm", tracker.call)

    users = [await make_user(f"conc-{i}") for i in range(6)]

    # 3 jobs go through the queue, processed by real background workers...
    for i in range(3):
        resp = client.post(
            "/chat/jobs", json={"message": f"job-{i}"}, headers=users[i]["headers"]
        )
        assert resp.status_code == 202

    tasks, stop_event = start_chat_workers(db)
    try:
        # ...while 3 more hit the synchronous route directly and
        # concurrently, via real async HTTP against the same running app.
        import httpx
        from app.main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            sync_calls = [
                ac.post(
                    "/chat",
                    json={"message": f"sync-{i}"},
                    headers=users[3 + i]["headers"],
                )
                for i in range(3)
            ]
            responses = await asyncio.gather(*sync_calls)

        assert all(r.status_code == 200 for r in responses)

        elapsed = 0.0
        while tracker.completed < 6 and elapsed < 5.0:
            await asyncio.sleep(0.01)
            elapsed += 0.01
    finally:
        await stop_chat_workers(tasks, stop_event)

    assert tracker.completed == 6
    assert tracker.max_seen <= 2
    # Real overlap must have actually happened (not accidental full
    # serialization), otherwise this isn't proving anything about the cap.
    assert tracker.max_seen >= 2


# --- 3. /chat/jobs priority/FIFO behavior is unchanged ----------------------


@pytest.mark.asyncio
async def test_chat_jobs_priority_fifo_order_unaffected_by_sync_chat_traffic(
    client, db, make_user, monkeypatch
):
    """A synchronous /chat call claims and completes only its OWN job
    (claim_job_by_id, scoped by _id) - it must never be picked up by
    claim_next_job(), and must never disturb the priority-then-FIFO order
    of jobs actually submitted through /chat/jobs."""
    user = await make_user("fifo-user")

    async def fake_call_llm(prompt):
        return f"reply to {prompt}"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    await create_chat_job(db, "user-low-1", "low-1", priority=0)
    await create_chat_job(db, "user-high-1", "high-1", priority=5)

    # A synchronous /chat call in between - it must self-claim its own job
    # immediately, not enter the same FIFO line as the two jobs above.
    resp = client.post("/chat", json={"message": "sync-message"}, headers=user["headers"])
    assert resp.status_code == 200

    await create_chat_job(db, "user-low-2", "low-2", priority=0)
    await create_chat_job(db, "user-high-2", "high-2", priority=5)

    claimed_order = []
    while True:
        job = await claim_next_job(db)
        if job is None:
            break
        claimed_order.append(job["prompt"])

    # Exactly the four /chat/jobs-style jobs, in priority-then-FIFO order -
    # the sync call's own job never appears here (it already completed via
    # claim_job_by_id, out of this queue's normal claim path).
    assert claimed_order == ["high-1", "high-2", "low-1", "low-2"]
    assert "sync-message" not in claimed_order


# --- 4. A user cannot claim another user's job ------------------------------


@pytest.mark.asyncio
async def test_claim_job_by_id_rejects_a_different_users_job(db):
    doc = await create_chat_job(db, "owner", "prompt")

    stolen = await claim_job_by_id(db, doc["_id"], "not-the-owner")
    assert stolen is None

    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "pending"
    assert stored["attempts"] == 0

    # The real owner can still claim it afterward - proves the rejection
    # above wasn't a side effect that corrupted the job.
    claimed = await claim_job_by_id(db, doc["_id"], "owner")
    assert claimed is not None
    assert claimed["status"] == "processing"
    assert claimed["attempts"] == 1


@pytest.mark.asyncio
async def test_claim_job_by_id_rejects_already_claimed_job():
    """A second claim attempt (e.g. a retried request) must not re-claim an
    already-processing job, mirroring claim_next_job's own atomicity."""
    from mongomock_motor import AsyncMongoMockClient

    db = AsyncMongoMockClient()["smartwardrobe_test"]
    doc = await create_chat_job(db, "owner", "prompt")

    first = await claim_job_by_id(db, doc["_id"], "owner")
    assert first is not None

    second = await claim_job_by_id(db, doc["_id"], "owner")
    assert second is None


@pytest.mark.asyncio
async def test_sync_chat_endpoints_never_cross_user_leak_via_http(client, make_user, monkeypatch):
    """End-to-end: two users' concurrent-ish /chat calls each only ever see
    their own reply - claim_job_by_id's user_id scoping holds under the
    real route, not just the unit-level check above."""
    alice = await make_user("cross-alice")
    bob = await make_user("cross-bob")

    async def fake_call_llm(prompt):
        return f"reply for [{prompt}]"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    resp_a = client.post("/chat", json={"message": "alice-msg"}, headers=alice["headers"])
    resp_b = client.post("/chat", json={"message": "bob-msg"}, headers=bob["headers"])

    assert "alice-msg" in resp_a.json()["response"]
    assert "bob-msg" in resp_b.json()["response"]
    assert "bob-msg" not in resp_a.json()["response"]
    assert "alice-msg" not in resp_b.json()["response"]


# --- 5. Memory learning happens exactly once --------------------------------


@pytest.mark.asyncio
async def test_sync_chat_learns_memory_exactly_once(client, db, make_user, monkeypatch):
    calls = []

    async def fake_call_llm(prompt):
        return "ok"

    async def spy_learn_memory_from_chat(db_arg, user_id, message):
        calls.append((user_id, message))
        return False  # no-op extraction outcome; call count is what matters here

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "app.services.chat_worker.learn_memory_from_chat", spy_learn_memory_from_chat
    )

    user = await make_user("mem-once-sync")
    resp = client.post("/chat", json={"message": "I like blue."}, headers=user["headers"])

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0] == (user["user_id"], "I like blue.")


@pytest.mark.asyncio
async def test_worker_processed_job_learns_memory_exactly_once(db, monkeypatch):
    calls = []

    async def fake_call_llm(prompt):
        return "ok"

    async def spy_learn_memory_from_chat(db_arg, user_id, message):
        calls.append((user_id, message))
        return False

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "app.services.chat_worker.learn_memory_from_chat", spy_learn_memory_from_chat
    )

    await create_chat_job(db, "worker-user", "prompt", message="I hate black.")
    processed = await process_one_job(db)

    assert processed is True
    assert len(calls) == 1
    assert calls[0] == ("worker-user", "I hate black.")


# --- 6. Catalog grounding + hallucination guardrail on both paths ----------


@pytest.mark.asyncio
async def test_sync_chat_applies_catalog_grounding_and_guardrail(client, make_user, monkeypatch):
    """Through the new claim-and-run pipeline: a message that grounds
    against a real retrieved product is allowed unchanged; an invented
    product is rejected. Proves run_claimed_job (not routers/chat.py
    directly) is what applies the guardrail now."""
    import httpx

    user = await make_user("ground-sync")

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/crawl"):
            return httpx.Response(
                200,
                json={
                    "products": [
                        {
                            "source": "shopify_ata",
                            "source_url": "https://atawear.co.il/products/1",
                            "source_product_id": "ATA-1",
                            "product_name": "Black Linen Shirt",
                            "clothing_type": "top",
                            "color": "black",
                            "material": "linen",
                            "price": 199.0,
                            "currency": "ILS",
                            "image_url": "https://atawear.co.il/images/1.jpg",
                            "availability": "in_stock",
                            "fetched_at": "2025-01-01T00:00:00Z",
                        }
                    ],
                    "source_status": [],
                },
            )
        if url.endswith("/generate"):
            return httpx.Response(
                200,
                json={
                    "response": "I recommend the Sunset Boho Dress: https://fake.example.com/sunset",
                    "model": "qwen2.5:3b",
                },
            )
        raise AssertionError(f"unexpected outbound call: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.post(
        "/chat", json={"message": "Does ATA sell a black linen shirt?"}, headers=user["headers"]
    )

    assert resp.status_code == 200
    reply = resp.json()["response"]
    assert "Sunset Boho Dress" not in reply
    assert "fake.example.com" not in reply
    assert "https://atawear.co.il/products/1" in reply


@pytest.mark.asyncio
async def test_worker_processed_job_applies_catalog_grounding_and_guardrail(
    client, db, make_user, monkeypatch
):
    """Same guardrail behavior for a job submitted via /chat/jobs and
    completed by the shared run_claimed_job() through a real worker."""
    import httpx

    user = await make_user("ground-async")

    async def fake_post(self, url, json=None, **kwargs):
        if url.endswith("/crawl"):
            return httpx.Response(
                200,
                json={
                    "products": [
                        {
                            "source": "shopify_ata",
                            "source_url": "https://atawear.co.il/products/1",
                            "source_product_id": "ATA-1",
                            "product_name": "Black Linen Shirt",
                            "clothing_type": "top",
                            "color": "black",
                            "material": "linen",
                            "price": 199.0,
                            "currency": "ILS",
                            "image_url": "https://atawear.co.il/images/1.jpg",
                            "availability": "in_stock",
                            "fetched_at": "2025-01-01T00:00:00Z",
                        }
                    ],
                    "source_status": [],
                },
            )
        raise AssertionError(f"unexpected outbound call during job submission: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.post(
        "/chat/jobs",
        json={"message": "Does ATA sell a black linen shirt?"},
        headers=user["headers"],
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    async def fake_call_llm(prompt):
        return "I recommend the Sunset Boho Dress: https://fake.example.com/sunset"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)
    assert await process_one_job(db) is True

    stored = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert stored["status"] == "completed"
    assert "Sunset Boho Dress" not in stored["result"]
    assert "https://atawear.co.il/products/1" in stored["result"]


# --- 7. Existing timeout/unavailable/upstream error mappings -------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected_status,expected_reason",
    [
        (
            ChatGatewayFailure(
                reason=ChatGatewayFailureReason.LLM_UNAVAILABLE,
                status_code=503,
                detail="LLM service is unavailable.",
            ),
            503,
            "llm_unavailable",
        ),
        (
            ChatGatewayFailure(
                reason=ChatGatewayFailureReason.LLM_TIMEOUT,
                status_code=504,
                detail="Ollama did not respond in time.",
            ),
            504,
            "llm_timeout",
        ),
        (
            ChatGatewayFailure(
                reason=ChatGatewayFailureReason.LLM_INVALID_RESPONSE,
                status_code=502,
                detail="Ollama returned an unexpected response.",
            ),
            502,
            "llm_invalid_response",
        ),
    ],
)
async def test_sync_chat_error_mapping_matches_stable_reason_and_persists_it(
    client, db, make_user, monkeypatch, failure, expected_status, expected_reason
):
    """The synchronous route's HTTP status/detail must still match exactly
    what call_llm's ChatGatewayFailure carries, AND the underlying job must
    be persisted as 'failed' with the same stable error_reason a
    /chat/jobs failure would get - proving both entry points now share one
    real failure-classification path, not just the same status codes by
    coincidence."""
    user = await make_user(f"err-{expected_reason}")

    async def failing_call_llm(prompt):
        return failure

    monkeypatch.setattr("app.services.chat_worker.call_llm", failing_call_llm)

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])

    assert resp.status_code == expected_status
    assert resp.json()["detail"] == failure.detail

    jobs = [doc async for doc in db.chat_jobs.find({"user_id": user["user_id"]})]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["error_reason"] == expected_reason
    assert jobs[0]["result"] is None
