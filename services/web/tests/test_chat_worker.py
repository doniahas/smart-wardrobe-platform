import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from mongomock_motor import AsyncMongoMockClient
from pymongo import ReturnDocument

from app.core.config import CHAT_WORKER_CONCURRENCY
from app.services.chat_queue import complete_job, create_chat_job
from app.services.chat_queue import reclaim_stale_jobs as real_reclaim_stale_jobs
from app.services.chat_worker import (
    process_one_job,
    start_chat_workers,
    start_stale_job_reaper,
    stop_chat_workers,
    stop_stale_job_reaper,
)
from app.services.llm_gateway import ChatGatewayFailure, ChatGatewayFailureReason


@pytest.fixture()
def db():
    return AsyncMongoMockClient()["smartwardrobe_test"]


@pytest.mark.asyncio
async def test_process_one_job_returns_false_when_queue_empty(db):
    assert await process_one_job(db) is False


@pytest.mark.asyncio
async def test_process_one_job_marks_completed_on_llm_success(db, monkeypatch):
    doc = await create_chat_job(db, "user-1", "What should I wear?")

    async def fake_call_llm(prompt):
        assert prompt == "What should I wear?"
        return "Try a red top."

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    processed = await process_one_job(db)

    assert processed is True
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "completed"
    assert stored["result"] == "Try a red top."
    assert stored["error_reason"] is None
    assert stored["completed_at"] is not None


@pytest.mark.asyncio
async def test_process_one_job_normalizes_markdown_artifacts_in_stored_result(db, monkeypatch):
    """The same conservative normalization the
    synchronous route applies (see test_chat_route.py) must also apply to
    the async job's stored result, so GET /chat/jobs/{id} never surfaces
    literal '**'/'#' characters either."""
    doc = await create_chat_job(db, "user-1", "What should I wear?")

    async def fake_call_llm(prompt):
        return "# Style Recommendations\n**Try a red top** with __dark jeans__."

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    processed = await process_one_job(db)

    assert processed is True
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "completed"
    assert stored["result"] == "Style Recommendations\nTry a red top with dark jeans."


@pytest.mark.asyncio
async def test_process_one_job_marks_failed_with_stable_reason_on_llm_failure(db, monkeypatch):
    doc = await create_chat_job(db, "user-1", "prompt")

    async def fake_call_llm(prompt):
        return ChatGatewayFailure(
            reason=ChatGatewayFailureReason.LLM_TIMEOUT,
            status_code=504,
            detail="Ollama did not respond in time.",
        )

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    processed = await process_one_job(db)

    assert processed is True
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "failed"
    # Stable machine-readable reason, never the human-readable detail string.
    assert stored["error_reason"] == "llm_timeout"
    assert stored["result"] is None


@pytest.mark.asyncio
async def test_process_one_job_survives_unexpected_exception_from_call_llm(db, monkeypatch):
    doc = await create_chat_job(db, "user-1", "prompt")

    async def raising_call_llm(prompt):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.chat_worker.call_llm", raising_call_llm)

    with pytest.raises(RuntimeError):
        await process_one_job(db)

    # The job was claimed (now "processing") but never resolved - process_one_job
    # itself propagates; it's the worker loop's job to contain this.
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "processing"


# --- Memory-learning side-effect fencing ---
#
# complete_job's claim-fencing protects the stored chat_jobs document, but
# process_one_job's memory-learning call (services/chat_memory_learning.py::
# learn_memory_from_chat) is a SEPARATE side effect that runs after
# complete_job returns - these tests prove that side effect is fenced too,
# not just the document write.


@pytest.mark.asyncio
async def test_process_one_job_runs_memory_learning_on_normal_completion(db, monkeypatch):
    """Baseline: an ordinary, first-attempt completion still triggers
    memory learning exactly as before this correction."""
    doc = await create_chat_job(db, "user-1", "prompt", message="I love wearing blue")

    async def fake_call_llm(prompt):
        return "Try a blue top."

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    learned_calls = []

    async def fake_learn(db_, user_id, message):
        learned_calls.append((user_id, message))

    monkeypatch.setattr("app.services.chat_worker.learn_memory_from_chat", fake_learn)

    processed = await process_one_job(db)

    assert processed is True
    assert learned_calls == [("user-1", "I love wearing blue")]
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "completed"


@pytest.mark.asyncio
async def test_process_one_job_skips_memory_learning_when_completion_is_fenced_out(db, monkeypatch):
    """Reproduces the exact race from the review: while THIS attempt's
    call_llm is still in flight, the job is reclaimed and completed by a
    newer attempt (simulated directly here). When this attempt's own
    call_llm finally returns and it calls complete_job, that write is
    correctly fenced out (attempts no longer match) - and, critically, this
    attempt must NOT run memory learning for a job it no longer owns."""
    doc = await create_chat_job(db, "user-1", "prompt", message="I love wearing blue")

    async def fake_call_llm(prompt):
        # Simulate: the reaper reclaims this job and a newer attempt
        # completes it, all while this attempt's LLM call is still running.
        await db.chat_jobs.update_one(
            {"_id": doc["_id"]}, {"$set": {"status": "pending", "started_at": None}}
        )
        newer_claim = await db.chat_jobs.find_one_and_update(
            {"_id": doc["_id"], "status": "pending"},
            {"$set": {"status": "processing"}, "$inc": {"attempts": 1}},
            return_document=ReturnDocument.AFTER,
        )
        newer_completed = await complete_job(
            db, doc["_id"], result="result from the newer attempt",
            claim_attempts=newer_claim["attempts"],
        )
        assert newer_completed is True
        return "stale result from the original attempt"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    learned_calls = []

    async def fake_learn(db_, user_id, message):
        learned_calls.append((user_id, message))

    monkeypatch.setattr("app.services.chat_worker.learn_memory_from_chat", fake_learn)

    processed = await process_one_job(db)

    assert processed is True
    assert learned_calls == []  # the fenced-out (stale) attempt must NOT have learned memory
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "completed"
    assert stored["result"] == "result from the newer attempt"  # untouched by the stale attempt


@pytest.mark.asyncio
async def test_process_one_job_runs_memory_learning_for_the_current_attempt_after_a_real_reclaim(
    db, monkeypatch
):
    """The mirror image of the fenced-out case above, driven through the
    real reclaim_stale_jobs path: once a job has been genuinely recovered
    and reprocessed by a NEW attempt, that attempt is the current owner and
    must run memory learning normally."""
    doc = await create_chat_job(db, "user-1", "prompt", message="I love wearing blue")
    stale_started_at = datetime.now(timezone.utc) - timedelta(seconds=200)
    await db.chat_jobs.update_one(
        {"_id": doc["_id"]},
        {"$set": {"status": "processing", "started_at": stale_started_at, "attempts": 1}},
    )
    outcome = await real_reclaim_stale_jobs(db, stale_after_seconds=120, max_attempts=3)
    assert outcome == {"recovered": 1, "abandoned": 0}

    async def fake_call_llm(prompt):
        return "Try a blue top."

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    learned_calls = []

    async def fake_learn(db_, user_id, message):
        learned_calls.append((user_id, message))

    monkeypatch.setattr("app.services.chat_worker.learn_memory_from_chat", fake_learn)

    processed = await process_one_job(db)  # this IS the current, newer-generation attempt

    assert processed is True
    assert learned_calls == [("user-1", "I love wearing blue")]
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "completed"
    assert stored["attempts"] == 2


@pytest.mark.asyncio
async def test_start_chat_workers_creates_configured_task_count(db):
    tasks, stop_event = start_chat_workers(db)
    try:
        assert len(tasks) == CHAT_WORKER_CONCURRENCY
        assert all(isinstance(t, asyncio.Task) for t in tasks)
    finally:
        await stop_chat_workers(tasks, stop_event)


@pytest.mark.asyncio
async def test_workers_drain_queue_and_shut_down_cleanly(db, monkeypatch):
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_POLL_INTERVAL_SECONDS", 0.01)

    processed = []

    async def fake_call_llm(prompt):
        processed.append(prompt)
        return "ok"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    await create_chat_job(db, "user-1", "job-1")
    await create_chat_job(db, "user-2", "job-2")

    tasks, stop_event = start_chat_workers(db)

    for _ in range(100):
        if len(processed) >= 2:
            break
        await asyncio.sleep(0.01)

    await stop_chat_workers(tasks, stop_event)

    assert sorted(processed) == ["job-1", "job-2"]
    assert all(task.done() for task in tasks)
    assert all(not task.cancelled() for task in tasks)


@pytest.mark.asyncio
async def test_worker_survives_unexpected_exception_and_keeps_polling(db, monkeypatch):
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_CONCURRENCY", 1)

    call_count = {"n": 0}

    async def flaky_call_llm(prompt):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return "ok"

    monkeypatch.setattr("app.services.chat_worker.call_llm", flaky_call_llm)

    await create_chat_job(db, "user-1", "job-1")
    # a second job so the worker has something to succeed on after the
    # first job's unexpected failure leaves job-1 stuck "processing"
    await create_chat_job(db, "user-2", "job-2")

    tasks, stop_event = start_chat_workers(db)

    for _ in range(100):
        if call_count["n"] >= 2:
            break
        await asyncio.sleep(0.01)

    await stop_chat_workers(tasks, stop_event)

    assert call_count["n"] >= 2
    assert all(task.done() for task in tasks)
    assert all(not task.cancelled() for task in tasks)
    assert all(not task.exception() for task in tasks if task.done())


# --- Stale-job reaper (Task 2) ---


@pytest.mark.asyncio
async def test_start_stale_job_reaper_creates_one_task(db):
    task, stop_event = start_stale_job_reaper(db)
    try:
        assert isinstance(task, asyncio.Task)
        assert not task.done()
    finally:
        await stop_stale_job_reaper(task, stop_event)
    assert task.done()
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_stale_job_reaper_recovers_job_and_a_worker_reprocesses_it(db, monkeypatch):
    """End-to-end: a job abandoned mid-"processing" (simulating a killed
    worker) is swept back to pending by the reaper, then picked up and
    completed by an ordinary chat worker - proving recovery and normal
    processing compose correctly, not just that the DB flag flips."""
    monkeypatch.setattr("app.services.chat_worker.CHAT_JOB_STALE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("app.services.chat_worker.CHAT_JOB_RECOVERY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_POLL_INTERVAL_SECONDS", 0.01)

    doc = await create_chat_job(db, "user-1", "abandoned prompt")
    # Simulate a worker that claimed the job and then died: flip it to
    # "processing" directly (bypassing claim_next_job/process_one_job,
    # which would also resolve it) with a started_at already stale.
    stale_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    await db.chat_jobs.update_one(
        {"_id": doc["_id"]},
        {"$set": {"status": "processing", "started_at": stale_started_at, "attempts": 1}},
    )

    async def fake_call_llm(prompt):
        return "recovered response"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    reaper_task, reaper_stop = start_stale_job_reaper(db)
    worker_tasks, worker_stop = start_chat_workers(db)
    try:
        stored = None
        for _ in range(200):
            stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
            if stored["status"] == "completed":
                break
            await asyncio.sleep(0.01)
    finally:
        await stop_stale_job_reaper(reaper_task, reaper_stop)
        await stop_chat_workers(worker_tasks, worker_stop)

    assert stored["status"] == "completed"
    assert stored["result"] == "recovered response"
    assert stored["attempts"] == 2  # original claim + the reclaim-driven re-claim


@pytest.mark.asyncio
async def test_stale_reclaim_and_late_original_worker_write_stay_correct_concurrently(db, monkeypatch):
    """Claim fencing under concurrent workers: a job claimed by "worker A" goes stale
    and is recovered by the real reaper + reprocessed by a real worker pool
    while A is (simulated) still holding its original claim; once the pool
    has completed the job under its own newer claim generation, A's late
    completion attempt using the original (obsolete) generation must be
    silently rejected rather than overwriting the real result - proving
    reclaim_stale_jobs and complete_job's `attempts` fencing compose
    correctly under the real background tasks, not just in isolation (see
    test_chat_queue.py's dedicated claim-fencing tests for the isolated
    version of this race)."""
    monkeypatch.setattr("app.services.chat_worker.CHAT_JOB_STALE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("app.services.chat_worker.CHAT_JOB_RECOVERY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("app.services.chat_worker.CHAT_WORKER_POLL_INTERVAL_SECONDS", 0.01)

    from app.services.chat_queue import claim_next_job, complete_job

    doc = await create_chat_job(db, "user-1", "prompt")
    # "Worker A" claims the job directly (bypassing process_one_job) so its
    # claim generation can be held onto independently of the real pool
    # below, then backdate started_at so the reaper treats it as stale.
    attempt_one = await claim_next_job(db)
    assert attempt_one["attempts"] == 1
    await db.chat_jobs.update_one(
        {"_id": doc["_id"]},
        {"$set": {"started_at": datetime.now(timezone.utc) - timedelta(seconds=5)}},
    )

    async def fake_call_llm(prompt):
        return "response from the real pool"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    reaper_task, reaper_stop = start_stale_job_reaper(db)
    worker_tasks, worker_stop = start_chat_workers(db)
    try:
        stored = None
        for _ in range(200):
            stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
            if stored["status"] == "completed":
                break
            await asyncio.sleep(0.01)
    finally:
        await stop_stale_job_reaper(reaper_task, reaper_stop)
        await stop_chat_workers(worker_tasks, worker_stop)

    assert stored["status"] == "completed"
    assert stored["result"] == "response from the real pool"
    assert stored["attempts"] == 2  # reclaimed once, then reprocessed by the newer attempt

    # "Worker A" finally returns with its own stale result and tries to
    # complete the job using its original (attempts=1) claim generation.
    await complete_job(
        db, doc["_id"], result="STALE - should never be stored",
        claim_attempts=attempt_one["attempts"],
    )

    final = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert final["status"] == "completed"
    assert final["result"] == "response from the real pool"  # untouched by the stale write


@pytest.mark.asyncio
async def test_stale_job_reaper_survives_unexpected_exception_and_keeps_polling(db, monkeypatch):
    monkeypatch.setattr("app.services.chat_worker.CHAT_JOB_RECOVERY_INTERVAL_SECONDS", 0.01)

    call_count = {"n": 0}

    async def flaky_reclaim(db, stale_after_seconds, max_attempts):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return await real_reclaim_stale_jobs(db, stale_after_seconds, max_attempts)

    monkeypatch.setattr("app.services.chat_worker.reclaim_stale_jobs", flaky_reclaim)

    task, stop_event = start_stale_job_reaper(db)
    try:
        for _ in range(200):
            if call_count["n"] >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await stop_stale_job_reaper(task, stop_event)

    assert call_count["n"] >= 2
    assert task.done()
    assert not task.cancelled()
    assert not task.exception()


@pytest.mark.asyncio
async def test_stop_stale_job_reaper_leaves_no_dangling_task(db):
    task, stop_event = start_stale_job_reaper(db)
    await stop_stale_job_reaper(task, stop_event)

    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None
