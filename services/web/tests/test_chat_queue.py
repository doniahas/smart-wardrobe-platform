import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from app.services.chat_queue import (
    DEFAULT_JOB_PRIORITY,
    MAX_JOB_PRIORITY,
    MIN_JOB_PRIORITY,
    RETRY_JOB_PRIORITY,
    STALE_ABANDONED_REASON,
    claim_next_job,
    complete_job,
    create_chat_job,
    fail_job,
    reclaim_stale_jobs,
)


async def _insert_job(
    db,
    *,
    priority: int,
    created_at: datetime,
    status: str = "pending",
    started_at: datetime | None = None,
    attempts: int = 0,
):
    result = await db.chat_jobs.insert_one(
        {
            "user_id": "someone",
            "prompt": "prompt",
            "priority": priority,
            "status": status,
            "result": None,
            "error_reason": None,
            "attempts": attempts,
            "created_at": created_at,
            "started_at": started_at,
            "completed_at": None,
        }
    )
    return result.inserted_id


@pytest.mark.asyncio
async def test_create_chat_job_inserts_pending_job_with_default_priority(db):
    doc = await create_chat_job(db, "user-1", "the prompt")

    assert doc["user_id"] == "user-1"
    assert doc["prompt"] == "the prompt"
    assert doc["priority"] == DEFAULT_JOB_PRIORITY == 0
    assert doc["status"] == "pending"
    assert doc["result"] is None
    assert doc["error_reason"] is None
    assert doc["started_at"] is None
    assert doc["completed_at"] is None
    assert doc["created_at"] is not None

    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "pending"


@pytest.mark.asyncio
async def test_claim_next_job_returns_none_when_queue_empty(db):
    assert await claim_next_job(db) is None


@pytest.mark.asyncio
async def test_claim_next_job_claims_and_marks_processing(db):
    doc = await create_chat_job(db, "user-1", "prompt")

    claimed = await claim_next_job(db)

    assert claimed["_id"] == doc["_id"]
    assert claimed["status"] == "processing"
    assert claimed["started_at"] is not None

    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "processing"


@pytest.mark.asyncio
async def test_claim_next_job_ignores_non_pending_jobs(db):
    await _insert_job(db, priority=0, created_at=datetime.now(timezone.utc), status="processing")
    await _insert_job(db, priority=0, created_at=datetime.now(timezone.utc), status="completed")
    await _insert_job(db, priority=0, created_at=datetime.now(timezone.utc), status="failed")

    assert await claim_next_job(db) is None


@pytest.mark.asyncio
async def test_claim_next_job_orders_by_priority_desc_then_created_at_asc(db):
    now = datetime.now(timezone.utc)
    low_priority_older = await _insert_job(db, priority=0, created_at=now - timedelta(minutes=10))
    high_priority_newer = await _insert_job(db, priority=5, created_at=now)
    high_priority_older = await _insert_job(db, priority=5, created_at=now - timedelta(minutes=5))

    # Highest priority first; within priority=5, the older job (FIFO) first.
    first = await claim_next_job(db)
    assert first["_id"] == high_priority_older

    second = await claim_next_job(db)
    assert second["_id"] == high_priority_newer

    third = await claim_next_job(db)
    assert third["_id"] == low_priority_older

    assert await claim_next_job(db) is None


@pytest.mark.asyncio
async def test_claim_next_job_is_atomic_under_concurrent_claims(db):
    doc = await create_chat_job(db, "user-1", "prompt")

    results = await asyncio.gather(claim_next_job(db), claim_next_job(db))

    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1
    assert claimed[0]["_id"] == doc["_id"]


@pytest.mark.asyncio
async def test_complete_job_marks_completed_with_result(db):
    doc = await create_chat_job(db, "user-1", "prompt")
    claimed = await claim_next_job(db)

    result = await complete_job(db, doc["_id"], result="Try a red top.", claim_attempts=claimed["attempts"])

    assert result is True  # this claim actually performed the transition
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "completed"
    assert stored["result"] == "Try a red top."
    assert stored["error_reason"] is None
    assert stored["completed_at"] is not None


@pytest.mark.asyncio
async def test_fail_job_marks_failed_with_error_reason(db):
    doc = await create_chat_job(db, "user-1", "prompt")
    claimed = await claim_next_job(db)

    result = await fail_job(db, doc["_id"], error_reason="llm_timeout", claim_attempts=claimed["attempts"])

    assert result is True  # this claim actually performed the transition
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "failed"
    assert stored["error_reason"] == "llm_timeout"
    assert stored["result"] is None
    assert stored["completed_at"] is not None


@pytest.mark.asyncio
async def test_complete_job_is_noop_when_job_not_processing(db):
    doc = await create_chat_job(db, "user-1", "prompt")  # still "pending"

    result = await complete_job(db, doc["_id"], result="ignored", claim_attempts=doc["attempts"])

    assert result is False  # no-op: never claimed, so this can't be the owning claim
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "pending"
    assert stored["result"] is None


@pytest.mark.asyncio
async def test_fail_job_is_noop_when_job_not_processing(db):
    doc = await create_chat_job(db, "user-1", "prompt")  # still "pending"

    result = await fail_job(db, doc["_id"], error_reason="llm_timeout", claim_attempts=doc["attempts"])

    assert result is False  # no-op: never claimed, so this can't be the owning claim
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "pending"
    assert stored["error_reason"] is None


# --- Claim fencing ---
#
# Reproduces the exact race from the review: worker A claims a job
# (attempts=1), the reaper decides it's stale and recovers it, worker B
# reclaims it (attempts=2), and worker A only THEN returns and tries to
# resolve the job using its now-obsolete attempts=1 claim. Status-only
# filtering (`status == "processing"`) would incorrectly let A's write
# through, since B's claim is also "processing" - these tests prove the
# `attempts` fencing token stops that.


@pytest.mark.asyncio
async def test_stale_attempt_cannot_complete_job_owned_by_a_newer_attempt(db):
    doc = await create_chat_job(db, "user-1", "prompt")
    attempt_one = await claim_next_job(db)  # worker A's claim: attempts=1
    assert attempt_one["attempts"] == 1

    # The reaper recovers the job (simulated directly - reclaim_stale_jobs
    # itself is covered separately below) and worker B reclaims it.
    await db.chat_jobs.update_one(
        {"_id": doc["_id"]}, {"$set": {"status": "pending", "started_at": None}}
    )
    attempt_two = await claim_next_job(db)  # worker B's claim: attempts=2
    assert attempt_two["attempts"] == 2

    # Worker A was never actually dead - it now returns with a stale result
    # and tries to complete the job using its original (obsolete) claim.
    result = await complete_job(db, doc["_id"], result="stale result from attempt 1",
                                 claim_attempts=attempt_one["attempts"])

    assert result is False  # fenced out: this claim no longer owns the job
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "processing"  # still owned by attempt 2, untouched
    assert stored["result"] is None
    assert stored["attempts"] == 2


@pytest.mark.asyncio
async def test_stale_attempt_cannot_fail_job_owned_by_a_newer_attempt(db):
    doc = await create_chat_job(db, "user-1", "prompt")
    attempt_one = await claim_next_job(db)

    await db.chat_jobs.update_one(
        {"_id": doc["_id"]}, {"$set": {"status": "pending", "started_at": None}}
    )
    attempt_two = await claim_next_job(db)
    assert attempt_two["attempts"] == 2

    result = await fail_job(db, doc["_id"], error_reason="stale_llm_timeout",
                             claim_attempts=attempt_one["attempts"])

    assert result is False  # fenced out: this claim no longer owns the job
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "processing"  # still owned by attempt 2, untouched
    assert stored["error_reason"] is None
    assert stored["attempts"] == 2


@pytest.mark.asyncio
async def test_current_attempt_can_complete_normally_after_a_reclaim(db):
    doc = await create_chat_job(db, "user-1", "prompt")
    await claim_next_job(db)  # attempt 1, later abandoned

    await db.chat_jobs.update_one(
        {"_id": doc["_id"]}, {"$set": {"status": "pending", "started_at": None}}
    )
    attempt_two = await claim_next_job(db)
    assert attempt_two["attempts"] == 2

    result = await complete_job(db, doc["_id"], result="real result from attempt 2",
                                 claim_attempts=attempt_two["attempts"])

    assert result is True  # this claim (attempt 2) actually owns the job
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "completed"
    assert stored["result"] == "real result from attempt 2"


# --- Priority-queue policy (Task 3) ---


@pytest.mark.asyncio
async def test_create_chat_job_rejects_priority_above_max(db):
    with pytest.raises(ValueError):
        await create_chat_job(db, "user-1", "prompt", priority=MAX_JOB_PRIORITY + 1)


@pytest.mark.asyncio
async def test_create_chat_job_rejects_priority_below_min(db):
    with pytest.raises(ValueError):
        await create_chat_job(db, "user-1", "prompt", priority=MIN_JOB_PRIORITY - 1)


@pytest.mark.asyncio
async def test_create_chat_job_accepts_validated_internal_priority(db):
    doc = await create_chat_job(db, "user-1", "prompt", priority=MAX_JOB_PRIORITY)
    assert doc["priority"] == MAX_JOB_PRIORITY

    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["priority"] == MAX_JOB_PRIORITY


@pytest.mark.asyncio
async def test_public_api_shaped_call_always_gets_default_priority(client, make_user, db):
    """routers/chat.py's submit_chat_job never passes a priority kwarg to
    create_chat_job - ChatRequest has no priority field at all, so a client
    submitting extra JSON has nothing to bypass. This proves the *server*
    side of that contract: calling create_chat_job the same way the public
    route does always yields DEFAULT_JOB_PRIORITY, regardless of what a
    client might try to smuggle into the request body."""
    user = await make_user("priority-abuser")
    resp = client.post(
        "/chat/jobs",
        json={"message": "Hi", "priority": MAX_JOB_PRIORITY},
        headers=user["headers"],
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    stored = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert stored["priority"] == DEFAULT_JOB_PRIORITY


# --- Stale/interrupted "processing" job recovery (Task 2) ---


@pytest.mark.asyncio
async def test_claim_next_job_increments_attempts(db):
    doc = await create_chat_job(db, "user-1", "prompt")
    assert doc["attempts"] == 0

    claimed = await claim_next_job(db)
    assert claimed["attempts"] == 1


@pytest.mark.asyncio
async def test_reclaim_stale_jobs_ignores_recently_claimed_active_job(db):
    """A job claimed moments ago (well within the staleness window) must
    never be reclaimed - it may still be genuinely in flight."""
    now = datetime.now(timezone.utc)
    job_id = await _insert_job(
        db, priority=0, created_at=now, status="processing", started_at=now, attempts=1
    )

    outcome = await reclaim_stale_jobs(db, stale_after_seconds=120, max_attempts=3)

    assert outcome == {"recovered": 0, "abandoned": 0}
    stored = await db.chat_jobs.find_one({"_id": job_id})
    assert stored["status"] == "processing"


@pytest.mark.asyncio
async def test_reclaim_stale_jobs_recovers_abandoned_job_to_pending(db):
    """A job stuck "processing" well past the staleness cutoff, with
    attempts still under the max, is recovered back to pending (at the
    demoted RETRY_JOB_PRIORITY - see the module docstring's "Retry-priority
    policy" section) and becomes claimable again."""
    now = datetime.now(timezone.utc)
    stale_started_at = now - timedelta(seconds=200)
    job_id = await _insert_job(
        db,
        priority=0,
        created_at=stale_started_at,
        status="processing",
        started_at=stale_started_at,
        attempts=1,
    )

    outcome = await reclaim_stale_jobs(db, stale_after_seconds=120, max_attempts=3)

    assert outcome == {"recovered": 1, "abandoned": 0}
    stored = await db.chat_jobs.find_one({"_id": job_id})
    assert stored["status"] == "pending"
    assert stored["started_at"] is None
    assert stored["priority"] == RETRY_JOB_PRIORITY

    # And it's now eligible to be claimed again, like any other pending job.
    claimed = await claim_next_job(db)
    assert claimed["_id"] == job_id
    assert claimed["attempts"] == 2


@pytest.mark.asyncio
async def test_reclaim_stale_jobs_never_promotes_an_already_low_priority_job(db):
    """A job already at or below RETRY_JOB_PRIORITY must not be *raised* to
    RETRY_JOB_PRIORITY by recovery - only jobs above it are ever demoted."""
    now = datetime.now(timezone.utc)
    stale_started_at = now - timedelta(seconds=200)
    already_low_priority = RETRY_JOB_PRIORITY - 3
    job_id = await _insert_job(
        db,
        priority=already_low_priority,
        created_at=stale_started_at,
        status="processing",
        started_at=stale_started_at,
        attempts=1,
    )

    outcome = await reclaim_stale_jobs(db, stale_after_seconds=120, max_attempts=3)

    assert outcome == {"recovered": 1, "abandoned": 0}
    stored = await db.chat_jobs.find_one({"_id": job_id})
    assert stored["status"] == "pending"
    assert stored["priority"] == already_low_priority


@pytest.mark.asyncio
async def test_reclaim_stale_jobs_demotion_is_not_cumulative_across_repeat_reclaims(db):
    """A job reclaimed more than once must settle at RETRY_JOB_PRIORITY, not
    keep sinking lower with every reclaim (no unbounded decay/starvation
    beyond the single fixed demotion step)."""
    now = datetime.now(timezone.utc)
    stale_started_at = now - timedelta(seconds=200)
    job_id = await _insert_job(
        db,
        priority=DEFAULT_JOB_PRIORITY,
        created_at=stale_started_at,
        status="processing",
        started_at=stale_started_at,
        attempts=1,
    )

    await reclaim_stale_jobs(db, stale_after_seconds=120, max_attempts=5)
    stored = await db.chat_jobs.find_one({"_id": job_id})
    assert stored["priority"] == RETRY_JOB_PRIORITY

    # Simulate this same job going stale again on its second attempt.
    await db.chat_jobs.update_one(
        {"_id": job_id},
        {"$set": {"status": "processing", "started_at": stale_started_at, "attempts": 2}},
    )
    await reclaim_stale_jobs(db, stale_after_seconds=120, max_attempts=5)
    stored = await db.chat_jobs.find_one({"_id": job_id})
    assert stored["priority"] == RETRY_JOB_PRIORITY  # unchanged, not demoted further


@pytest.mark.asyncio
async def test_reclaim_stale_jobs_marks_permanently_failed_past_max_attempts(db):
    """A job that has already been claimed max_attempts times and is still
    stuck "processing" is abandoned (failed) instead of recycled again, so
    it can't loop pending<->processing forever."""
    now = datetime.now(timezone.utc)
    stale_started_at = now - timedelta(seconds=200)
    job_id = await _insert_job(
        db,
        priority=0,
        created_at=stale_started_at,
        status="processing",
        started_at=stale_started_at,
        attempts=3,
    )

    outcome = await reclaim_stale_jobs(db, stale_after_seconds=120, max_attempts=3)

    assert outcome == {"recovered": 0, "abandoned": 1}
    stored = await db.chat_jobs.find_one({"_id": job_id})
    assert stored["status"] == "failed"
    assert stored["error_reason"] == STALE_ABANDONED_REASON
    assert stored["completed_at"] is not None

    # A failed job is never reprocessed - a later sweep is a no-op for it.
    outcome_again = await reclaim_stale_jobs(db, stale_after_seconds=120, max_attempts=3)
    assert outcome_again == {"recovered": 0, "abandoned": 0}


@pytest.mark.asyncio
async def test_reclaim_stale_jobs_never_touches_pending_completed_or_failed_jobs(db):
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=999)
    pending_id = await _insert_job(db, priority=0, created_at=old, status="pending")
    completed_id = await _insert_job(
        db, priority=0, created_at=old, status="completed", started_at=old
    )
    failed_id = await _insert_job(db, priority=0, created_at=old, status="failed", started_at=old)

    outcome = await reclaim_stale_jobs(db, stale_after_seconds=1, max_attempts=3)

    assert outcome == {"recovered": 0, "abandoned": 0}
    assert (await db.chat_jobs.find_one({"_id": pending_id}))["status"] == "pending"
    assert (await db.chat_jobs.find_one({"_id": completed_id}))["status"] == "completed"
    assert (await db.chat_jobs.find_one({"_id": failed_id}))["status"] == "failed"


@pytest.mark.asyncio
async def test_reclaim_stale_jobs_handles_mixed_batch_correctly(db):
    """One sweep, several jobs in different states at once - recovered and
    abandoned counts must reflect exactly the eligible subset, and every
    other job must be left completely alone."""
    now = datetime.now(timezone.utc)
    stale = now - timedelta(seconds=200)

    active_id = await _insert_job(
        db, priority=0, created_at=now, status="processing", started_at=now, attempts=1
    )
    recoverable_id = await _insert_job(
        db, priority=0, created_at=stale, status="processing", started_at=stale, attempts=1
    )
    abandon_id = await _insert_job(
        db, priority=0, created_at=stale, status="processing", started_at=stale, attempts=5
    )
    pending_id = await _insert_job(db, priority=0, created_at=stale, status="pending")

    outcome = await reclaim_stale_jobs(db, stale_after_seconds=120, max_attempts=3)

    assert outcome == {"recovered": 1, "abandoned": 1}
    assert (await db.chat_jobs.find_one({"_id": active_id}))["status"] == "processing"
    assert (await db.chat_jobs.find_one({"_id": recoverable_id}))["status"] == "pending"
    assert (await db.chat_jobs.find_one({"_id": abandon_id}))["status"] == "failed"
    assert (await db.chat_jobs.find_one({"_id": pending_id}))["status"] == "pending"
