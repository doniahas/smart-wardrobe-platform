"""Deterministic concurrency-correctness
tests for the chat queue/worker pool, driven end-to-end through
start_chat_workers/process_one_job against mongomock - no real LLM, no
Docker/Ollama required, so this stays part of the regular fast suite (see
scripts/chat_load_test.py for the separate live-stack load test).

Concurrency is made observable by having the fake call_llm hold a counter
open for a short, fixed asyncio.sleep - long enough that if the worker pool
ever exceeded its configured concurrency, the "max concurrent in-flight"
counter would catch it, but short enough these tests stay fast.
"""
import asyncio

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.services.chat_queue import claim_next_job, create_chat_job
from app.services.chat_worker import process_one_job, start_chat_workers, stop_chat_workers
from app.services.llm_gateway import ChatGatewayFailure, ChatGatewayFailureReason


@pytest.fixture()
def db():
    return AsyncMongoMockClient()["smartwardrobe_test"]


class _ConcurrencyTracker:
    """Tracks how many fake LLM calls are in flight at once."""

    def __init__(self):
        self.current = 0
        self.max_seen = 0
        self.lock = asyncio.Lock()
        self.completed_prompts: list[str] = []

    async def call(self, prompt: str, *, delay: float = 0.03) -> str:
        async with self.lock:
            self.current += 1
            self.max_seen = max(self.max_seen, self.current)
        try:
            await asyncio.sleep(delay)
        finally:
            async with self.lock:
                self.current -= 1
        self.completed_prompts.append(prompt)
        return f"response to: {prompt}"


async def _run_workers_until(db, condition, *, timeout_s: float = 5.0, poll_s: float = 0.01):
    """Poll `condition()` (sync callable) until it's True or timeout."""
    elapsed = 0.0
    while not condition() and elapsed < timeout_s:
        await asyncio.sleep(poll_s)
        elapsed += poll_s
    assert condition(), "condition not met before timeout"


@pytest.mark.asyncio
async def test_worker_pool_respects_configured_concurrency_bound(db, monkeypatch):
    """CHAT_WORKER_CONCURRENCY=3, with 9 jobs whose fake LLM calls overlap in
    time: the number of jobs ever in flight at once must never exceed 3."""
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_CONCURRENCY", 3)
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_POLL_INTERVAL_SECONDS", 0.01)

    tracker = _ConcurrencyTracker()
    monkeypatch.setattr("app.services.chat_worker.call_llm", tracker.call)

    for i in range(9):
        await create_chat_job(db, f"user-{i}", f"job-{i}")

    tasks, stop_event = start_chat_workers(db)
    try:
        await _run_workers_until(db, lambda: len(tracker.completed_prompts) >= 9)
    finally:
        await stop_chat_workers(tasks, stop_event)

    assert len(tracker.completed_prompts) == 9
    assert tracker.max_seen <= 3
    # With 9 jobs and a bound of 3, real overlap should have actually
    # happened (not just "never exceeded the bound" trivially via serial
    # execution) - otherwise this test wouldn't be proving concurrency at all.
    assert tracker.max_seen >= 2


@pytest.mark.asyncio
async def test_every_job_reaches_a_terminal_state_under_normal_conditions(db, monkeypatch):
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_CONCURRENCY", 3)
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_POLL_INTERVAL_SECONDS", 0.01)

    job_ids = []
    for i in range(10):
        doc = await create_chat_job(db, f"user-{i}", f"job-{i}")
        job_ids.append(doc["_id"])

    call_count = {"n": 0}

    async def sometimes_failing_call_llm(prompt):
        call_count["n"] += 1
        await asyncio.sleep(0.01)
        if call_count["n"] % 3 == 0:
            return ChatGatewayFailure(
                reason=ChatGatewayFailureReason.LLM_TIMEOUT,
                status_code=504,
                detail="timed out",
            )
        return "ok"

    monkeypatch.setattr("app.services.chat_worker.call_llm", sometimes_failing_call_llm)

    tasks, stop_event = start_chat_workers(db)
    try:
        async def all_terminal():
            docs = [await db.chat_jobs.find_one({"_id": jid}) for jid in job_ids]
            return all(d["status"] in ("completed", "failed") for d in docs)

        elapsed = 0.0
        while not await all_terminal() and elapsed < 5.0:
            await asyncio.sleep(0.01)
            elapsed += 0.01
        assert await all_terminal()
    finally:
        await stop_chat_workers(tasks, stop_event)

    docs = [await db.chat_jobs.find_one({"_id": jid}) for jid in job_ids]
    assert all(d["status"] in ("completed", "failed") for d in docs)
    assert any(d["status"] == "completed" for d in docs)
    assert any(d["status"] == "failed" for d in docs)


@pytest.mark.asyncio
async def test_no_job_is_ever_processed_twice_under_concurrent_workers(db, monkeypatch):
    """Every job is claimed and resolved exactly once when nothing goes
    wrong - claim_next_job's atomicity (proven directly in
    test_chat_queue.py) composing correctly with several real worker loops
    running at once, not merely claimed-but-never-double-claimed in
    isolation."""
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_CONCURRENCY", 4)
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_POLL_INTERVAL_SECONDS", 0.01)

    tracker = _ConcurrencyTracker()
    monkeypatch.setattr("app.services.chat_worker.call_llm", tracker.call)

    job_ids = []
    for i in range(20):
        doc = await create_chat_job(db, f"user-{i}", f"job-{i}")
        job_ids.append(doc["_id"])

    tasks, stop_event = start_chat_workers(db)
    try:
        await _run_workers_until(db, lambda: len(tracker.completed_prompts) >= 20)
    finally:
        await stop_chat_workers(tasks, stop_event)

    # Each prompt appears in the "processed" log exactly once.
    assert sorted(tracker.completed_prompts) == sorted(f"job-{i}" for i in range(20))
    # And every job's own attempts counter (incremented once per
    # claim_next_job call) is exactly 1 - never reclaimed, never re-claimed.
    docs = [await db.chat_jobs.find_one({"_id": jid}) for jid in job_ids]
    assert all(d["attempts"] == 1 for d in docs)
    assert all(d["status"] == "completed" for d in docs)


@pytest.mark.asyncio
async def test_claim_order_is_priority_then_fifo_under_a_single_worker(db, monkeypatch):
    """With one worker (fully sequential processing), the exact claim order
    is observable and must match the priority-then-FIFO policy."""
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_CONCURRENCY", 1)
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_POLL_INTERVAL_SECONDS", 0.01)

    tracker = _ConcurrencyTracker()
    monkeypatch.setattr("app.services.chat_worker.call_llm", lambda p: tracker.call(p, delay=0.0))

    # Internal-priority jobs (server-controlled, see chat_queue.py) mixed
    # with default-priority jobs submitted "later" in insertion order.
    await create_chat_job(db, "user-low-1", "low-1", priority=0)
    await create_chat_job(db, "user-high-1", "high-1", priority=5)
    await create_chat_job(db, "user-low-2", "low-2", priority=0)
    await create_chat_job(db, "user-high-2", "high-2", priority=5)

    tasks, stop_event = start_chat_workers(db)
    try:
        await _run_workers_until(db, lambda: len(tracker.completed_prompts) >= 4)
    finally:
        await stop_chat_workers(tasks, stop_event)

    assert tracker.completed_prompts == ["high-1", "high-2", "low-1", "low-2"]


@pytest.mark.asyncio
async def test_worker_errors_are_isolated_across_concurrent_workers(db, monkeypatch):
    """One job whose LLM call raises an unexpected exception must not stop
    other, concurrently-running workers from completing their own jobs."""
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_CONCURRENCY", 3)
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_POLL_INTERVAL_SECONDS", 0.01)

    async def flaky_call_llm(prompt):
        await asyncio.sleep(0.01)
        if prompt == "poison":
            raise RuntimeError("boom")
        return "ok"

    monkeypatch.setattr("app.services.chat_worker.call_llm", flaky_call_llm)

    poison_doc = await create_chat_job(db, "user-x", "poison")
    good_ids = []
    for i in range(6):
        doc = await create_chat_job(db, f"user-{i}", f"good-{i}")
        good_ids.append(doc["_id"])

    tasks, stop_event = start_chat_workers(db)
    try:
        async def all_good_done():
            docs = [await db.chat_jobs.find_one({"_id": gid}) for gid in good_ids]
            return all(d["status"] == "completed" for d in docs)

        elapsed = 0.0
        while not await all_good_done() and elapsed < 5.0:
            await asyncio.sleep(0.01)
            elapsed += 0.01
        assert await all_good_done()
    finally:
        await stop_chat_workers(tasks, stop_event)

    poison_stored = await db.chat_jobs.find_one({"_id": poison_doc["_id"]})
    # The poisoned job is left "processing" (process_one_job propagates the
    # unexpected error; the worker loop swallows it - see chat_worker.py) -
    # it never crashes the pool, and the stale-job reaper (Task 2) is what
    # eventually recovers it in production.
    assert poison_stored["status"] == "processing"
    good_docs = [await db.chat_jobs.find_one({"_id": gid}) for gid in good_ids]
    assert all(d["status"] == "completed" for d in good_docs)


@pytest.mark.asyncio
async def test_ownership_stays_correct_when_many_users_jobs_are_processed_concurrently(
    client, db, make_user, monkeypatch
):
    """End-to-end through the real HTTP routes: several users submit jobs at
    once, a real worker pool processes them concurrently, and each user can
    only ever fetch their own job afterwards."""
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_CONCURRENCY", 3)
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_POLL_INTERVAL_SECONDS", 0.01)

    async def fake_call_llm(prompt):
        await asyncio.sleep(0.01)
        return f"reply to {prompt}"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    # This is the only test in this file whose jobs carry a `message` (it
    # posts through the real /chat/jobs route), so it is the only one that
    # reaches memory learning. Learning now runs BEFORE complete_job - see
    # chat_worker.py's ordering comment - so leaving it unmocked would put
    # a real, unreachable-host httpx call (~3.9s to fail) on the completion
    # path and blow this test's 5s budget. Mocking it keeps this test about
    # what it is actually asserting - per-user ownership isolation under a
    # real concurrent worker pool - exactly as before; no assertion below
    # is changed or relaxed.
    async def fake_learn(db_, user_id, message):
        return False

    monkeypatch.setattr("app.services.chat_worker.learn_memory_from_chat", fake_learn)

    users = [await make_user(f"owner-{i}") for i in range(4)]
    job_ids_by_user = {}
    for i, user in enumerate(users):
        resp = client.post(
            "/chat/jobs", json={"message": f"hi from {i}"}, headers=user["headers"]
        )
        assert resp.status_code == 202
        job_ids_by_user[i] = resp.json()["job_id"]

    tasks, stop_event = start_chat_workers(db)
    try:
        async def all_completed():
            for job_id in job_ids_by_user.values():
                from bson import ObjectId

                doc = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
                if doc["status"] != "completed":
                    return False
            return True

        elapsed = 0.0
        while not await all_completed() and elapsed < 5.0:
            await asyncio.sleep(0.01)
            elapsed += 0.01
        assert await all_completed()
    finally:
        await stop_chat_workers(tasks, stop_event)

    # Each user can fetch their own completed job...
    for i, user in enumerate(users):
        resp = client.get(f"/chat/jobs/{job_ids_by_user[i]}", headers=user["headers"])
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    # ...but never anyone else's.
    for i, user in enumerate(users):
        other_i = (i + 1) % len(users)
        resp = client.get(f"/chat/jobs/{job_ids_by_user[other_i]}", headers=user["headers"])
        assert resp.status_code == 404
