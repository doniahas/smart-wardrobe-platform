"""Persistent priority queue for chat jobs.

DB-backed job creation and all status transitions for the `chat_jobs`
collection: pending -> processing (claim_next_job) -> completed/failed
(complete_job/fail_job), with a recovery path back to pending/failed for
abandoned jobs (reclaim_stale_jobs). No LLM calls or worker/reaper loops
live here - that's chat_worker.py; this module only owns job state.
`claim_next_job` and `reclaim_stale_jobs` are safe to call concurrently: all
writes here are single, atomic server-side MongoDB operations (either one
`find_one_and_update`, or an `update_many` whose filter always includes the
current `status`, so two concurrent callers can never both transition the
same document) - no separate read-then-write anywhere in this module.

## Claim fencing

Status alone (`status == "processing"`) is NOT sufficient to make
complete_job/fail_job safe once stale-job recovery exists. Concretely:

    worker A claims job  -> attempts=1, status=processing
    reaper decides it's stale -> status=pending
    worker B reclaims it -> attempts=2, status=processing
    worker A finally returns (it was never actually dead, just slow/stuck)
    worker A calls complete_job/fail_job -> status is "processing" again
    (worker B's claim) -> a status-only filter would match and let A's
    OBSOLETE result overwrite/resolve worker B's newer, still-in-flight
    claim.

Atomic status transitions prevent two writers from BOTH succeeding on the
same write; they do nothing to stop a late writer from a *previous* claim
generation from succeeding once the status has cycled back around to
"processing" under a *different* claim. Preventing that requires a fencing
token that changes every time the job is (re)claimed - here, the existing
monotonically increasing `attempts` counter (returned by claim_next_job)
doubles as that token. Every caller of complete_job/fail_job must carry the
exact `attempts` value its own claim_next_job call returned and pass it
back as `claim_attempts`; the update's filter requires
`status == "processing" AND attempts == claim_attempts`, so a write from an
obsolete generation matches zero documents and is silently and safely a
no-op, exactly like the pre-existing "job isn't processing at all" no-op
case.

**Fencing covers post-completion side effects too, not just the stored
document.** `complete_job`/`fail_job`
return `True` iff THIS call's `claim_attempts` is what actually performed
the transition, `False` if it was fenced out. A caller with any
completion-only side effect (e.g. chat_worker.py::process_one_job's
memory-learning call) MUST check this return value and skip the side
effect entirely when it's `False` - a fenced-out claim no longer owns the
job, so nothing downstream of "the job completed" belongs to it either.
Checking only the stored `chat_jobs` document's `status`/`result` is not
enough on its own to prevent a stale claim from performing an unwanted
side effect that a newer claim (or nobody, if the newer claim's own side
effect already ran) is responsible for.

## Priority-queue policy

`priority` is a plain integer: **larger claims first** (priority DESC).
Jobs sharing a priority are claimed oldest-`created_at`-first (FIFO) - see
claim_next_job's sort. This is the entire ordering rule; there is no
separate scheduling concept beyond these two keys.

`DEFAULT_JOB_PRIORITY` (0) is the only priority the public API can produce:
`ChatRequest` (routers/chat.py) has no `priority` field, so a client can
never influence a job's priority through JSON - there is no
validation-only defense to bypass, the field is structurally absent from
the request shape. Every job submitted through `POST /chat/jobs` today is
therefore the same, single priority class.

`create_chat_job`'s `priority` parameter exists purely as a **server-
controlled** hook for internal callers (e.g. a future maintenance/system
job that must be able to jump ahead of user-submitted chat) - never for
end users, and never for manufactured tiers (paid/admin/etc.) that don't
exist in this project's requirements. Any value passed is validated against
`MIN_JOB_PRIORITY..MAX_JOB_PRIORITY` and rejected outside that range, so
even a trusted internal caller can't accidentally starve the queue with an
extreme value.

## Retry-priority policy

A job recovered by `reclaim_stale_jobs` (attempts still under the limit) is
requeued at `RETRY_JOB_PRIORITY` - a single fixed level below
`DEFAULT_JOB_PRIORITY` - rather than keeping its original priority. This is
a small, server-controlled *technical* policy, not a user/product tier: a
job that stalled once (a wedged/slow worker, a transient DB hiccup) no
longer sits ahead of fresh, normal-priority interactive chat the instant
it's requeued, so one problematic retry can't repeatedly cut in front of
brand-new user requests.

Deliberately bounded and non-cumulative:

- **Flat, not relative.** Recovery always sets priority to exactly
  `RETRY_JOB_PRIORITY`, never `current_priority - N`. A job already at or
  below `RETRY_JOB_PRIORITY` (e.g. a hypothetical low-priority internal
  job) is left at its own (lower) priority instead of being *promoted* up
  to `RETRY_JOB_PRIORITY` - see the two disjoint `update_many` filters in
  reclaim_stale_jobs.
- **Not re-demoted on repeat reclaims.** Once a job is at
  `RETRY_JOB_PRIORITY`, a later reclaim of the *same* job (if it goes
  stale again) leaves priority unchanged rather than subtracting again -
  there is no unbounded decay toward `MIN_JOB_PRIORITY`.

**Known, accepted limitation - strict priority admits starvation.** This
queue has no aging/priority-boost mechanism: under a sustained, unbroken
stream of fresh `DEFAULT_JOB_PRIORITY` jobs, a job sitting at
`RETRY_JOB_PRIORITY` could in theory wait arbitrarily long before being
claimed. This is inherent to any strict (non-aging) priority scheme, not
specific to this feature, and is judged acceptable here because: (1) the
demotion is a single fixed step, never cumulative, so the delay it can
cause is bounded by how busy the queue is, not by how many times the job
was reclaimed; (2) `CHAT_JOB_MAX_ATTEMPTS` already bounds how many times a
job can be *claimed* before it's abandoned permanently - a demoted job that
never gets reclaimed just waits, it does not consume additional attempts or
loop; (3) this project's real traffic (a small multi-user demo, not a
public high-volume service) makes truly unbounded starvation implausible in
practice. If sustained starvation ever becomes a real concern, the safer
fix is an aging mechanism (e.g. periodically promoting long-waiting
low-priority jobs) rather than removing the demotion.
"""
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

DEFAULT_JOB_PRIORITY = 0
# Bounds for the internal-only `priority` argument to create_chat_job - see
# the module docstring's "Priority-queue policy" section. Deliberately a
# small, symmetric range: enough to express "ahead of" / "behind" normal
# user jobs without inventing an open-ended ranking system.
MIN_JOB_PRIORITY = -10
MAX_JOB_PRIORITY = 10

# See the module docstring's "Retry-priority policy" section. Kept as a
# named constant (never a magic number in reclaim_stale_jobs) and validated
# to stay within the same bounds as every other priority value.
RETRY_JOB_PRIORITY = DEFAULT_JOB_PRIORITY - 1
assert MIN_JOB_PRIORITY <= RETRY_JOB_PRIORITY <= MAX_JOB_PRIORITY

# Stable, machine-readable error_reason for a job the
# stale-job reaper gave up on after CHAT_JOB_MAX_ATTEMPTS claims - mirrors
# llm_gateway.ChatGatewayFailureReason's style (stable value, never a
# human-readable string) so callers can treat it the same way.
STALE_ABANDONED_REASON = "stale_abandoned"


async def create_chat_job(
    db,
    user_id: str,
    prompt: str,
    message: str | None = None,
    priority: int = DEFAULT_JOB_PRIORITY,
    catalog_products: list[dict] | None = None,
    catalog_status: str | None = None,
) -> dict:
    """Insert a new pending chat job and return the inserted document.

    `priority` defaults to DEFAULT_JOB_PRIORITY for every real caller today
    (routers/chat.py never passes anything else - see the module docstring).
    It's still validated here rather than trusted blindly, since this is the
    one server-controlled seam an internal caller would use.

    `message` is the caller's original, unmodified chat text (not the
    already-built `prompt`) - stored so the worker can later run the
    chat-memory-learning pipeline (services/chat_memory_learning.py::
    learn_memory_from_chat) from the user's own words once the job
    completes, without ever needing the assistant's reply. Optional and
    defaulting to None purely for backward compatibility with existing
    callers/tests that only pass a prompt; a job with no stored message
    simply skips memory learning when processed (see chat_worker.py).

    `catalog_products` is the caller's already-serialized (model_dump(mode=
    "json")) CatalogGrounding.products from chat_catalog_grounding.py -
    None when catalog grounding was never attempted for this message
    (ordinary conversation), a list (possibly empty) of the exact real
    products retrieved otherwise. Stored as-is so chat_worker.py can
    reconstruct it and run validate_grounded_reply() against the LLM's
    reply once the job completes, without re-crawling the catalog at that
    later point. Optional/defaulting to None for the same backward-
    compatibility reason as `message`.

    `catalog_status` is the same request's CatalogGrounding.status - "verified"/"no_match"/"unavailable"/
    "needs_clarification", or None alongside catalog_products=None when
    grounding was never attempted. Stored separately from the job's own
    lifecycle `status` field (pending/processing/completed/failed) - a
    different concept entirely, hence the distinct name - so
    chat_worker.py can pass the exact grounding outcome into
    validate_grounded_reply() and pick the semantically-honest safe
    message for a zero-verified-products outcome, instead of collapsing
    no_match/unavailable/needs_clarification into one generic fallback.
    """
    if not (MIN_JOB_PRIORITY <= priority <= MAX_JOB_PRIORITY):
        raise ValueError(
            f"priority {priority} out of allowed range "
            f"[{MIN_JOB_PRIORITY}, {MAX_JOB_PRIORITY}]"
        )
    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user_id,
        "prompt": prompt,
        "message": message,
        "catalog_products": catalog_products,
        "catalog_status": catalog_status,
        "priority": priority,
        "status": "pending",
        "result": None,
        "error_reason": None,
        # Counts every claim_next_job that has ever
        # picked up this job (initial claim + any stale-recovery reclaims).
        # Also doubles as the claim-fencing token passed back into
        # complete_job/fail_job - see the module docstring's "Claim
        # fencing" section and reclaim_stale_jobs below.
        "attempts": 0,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }
    result = await db.chat_jobs.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def claim_next_job(db) -> dict | None:
    """Atomically claim the highest-priority, oldest-eligible pending job.

    Ordering: priority DESC, then created_at ASC (FIFO within the same
    priority) - see the module docstring's "Priority-queue policy" section.
    Returns None if no job is pending. Marks the claimed job "processing"
    and increments `attempts` as part of the same atomic operation - nothing
    here processes the job any further yet.

    The caller MUST hold on to the returned document's `attempts` value and
    pass it back to complete_job/fail_job as `claim_attempts` - see the
    module docstring's "Claim fencing" section for why status alone is not
    enough once reclaim_stale_jobs exists.
    """
    return await db.chat_jobs.find_one_and_update(
        {"status": "pending"},
        {
            "$set": {"status": "processing", "started_at": datetime.now(timezone.utc)},
            "$inc": {"attempts": 1},
        },
        sort=[("priority", -1), ("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


async def claim_job_by_id(db, job_id, user_id: str) -> dict | None:
    """Atomically claim one specific, already-known job - the synchronous
    `POST /chat` route's counterpart to claim_next_job(), used to take
    ownership of exactly the job that same request just inserted via
    create_chat_job(), never an arbitrary next-in-line job. This is what
    lets `/chat` stay synchronous (get its own result back in this same
    request) while still creating a real, persisted `chat_jobs` document
    and going through the identical claim/complete/fail machinery every
    other job uses - it does not wait in the shared priority/FIFO line
    claim_next_job() enforces for `/chat/jobs`, by design (see
    chat_worker.py::run_claimed_job's module-level docstring).

    Same atomic transition as claim_next_job() (a single find_one_and_
    update, status pending -> processing, attempts incremented) and the
    exact same claim-fencing contract - the caller must still capture the
    returned document's `attempts` and pass it to complete_job/fail_job (or
    run_claimed_job(), which does this internally) unchanged.

    Scoped by BOTH `_id` and `user_id` - defense-in-depth: only the request
    that just created this job could plausibly know its id, but this makes
    it structurally impossible for one user's request to claim another
    user's job even if that ever changed.

    Returns None if the job doesn't exist, isn't `user_id`'s, or is no
    longer `pending` - callers must treat this as "unable to claim" and
    fail safely, never assume it always succeeds just because the id was
    created by this same request a moment ago.
    """
    return await db.chat_jobs.find_one_and_update(
        {"_id": job_id, "user_id": user_id, "status": "pending"},
        {
            "$set": {"status": "processing", "started_at": datetime.now(timezone.utc)},
            "$inc": {"attempts": 1},
        },
        return_document=ReturnDocument.AFTER,
    )


async def still_owns_claim(db, job_id, claim_attempts: int) -> bool:
    """Whether `claim_attempts` is still the claim that owns this job.

    Read-only; changes nothing. Exists so a completion-time side effect that
    must run BEFORE the job is published as "completed" (memory learning -
    see chat_worker.py::run_claimed_job) can still be fenced the same way
    complete_job's own write is, instead of being fenced by complete_job's
    return value *after* the job has already become visible to the client.

    Matches complete_job's filter exactly (still "processing", `attempts`
    still equal to this claim's token), so an obsolete claim - one whose
    job was reclaimed by the stale-job reaper and re-run by a newer
    attempt - reads False here just as its complete_job write would be
    rejected later.

    This is a check, not a lock: it cannot be atomic with the work that
    follows it, so a reclaim landing in the microseconds between this read
    and the learning call would not be caught. That residual case is
    harmless rather than merely unlikely - both attempts learn from the
    same stored `message` for the same user, and merge_chat_memory_
    candidate only ever adds already-present values, so a duplicate learn
    converges on identical memory. complete_job remains the authoritative
    fence for the job document itself.
    """
    return await db.chat_jobs.find_one(
        {"_id": job_id, "status": "processing", "attempts": claim_attempts},
        projection={"_id": 1},
    ) is not None


async def complete_job(db, job_id, result: str, claim_attempts: int) -> bool:
    """Atomically mark a claimed job completed with its LLM result.

    `claim_attempts` is the fencing token: the exact `attempts` value this
    worker's own `claim_next_job` call returned. The update only applies
    when the job is BOTH still "processing" AND its stored `attempts`
    still equals `claim_attempts` - see the module docstring's "Claim
    fencing" section for why status alone would let an obsolete claim
    (from before a stale-job reclaim) incorrectly resolve a newer claim's
    job. Any job that doesn't match either condition (already resolved,
    reclaimed back to pending, or claimed by a later generation) is left
    completely alone.

    Returns True iff THIS call's claim_attempts is what actually
    transitioned the job to "completed" (`modified_count == 1`), False if
    it was fenced out as a no-op. Callers must not perform any
    completion-only side effect (e.g. memory learning - see
    chat_worker.py::process_one_job) unless this returns True: a fenced-out
    write means this claim no longer owns the job, so anything downstream
    of "the job completed" is no longer this claim's to do either - see the
    module docstring's "Claim fencing" section (post-completion side
    effects, not just the stored result, must be fenced).
    """
    outcome = await db.chat_jobs.update_one(
        {"_id": job_id, "status": "processing", "attempts": claim_attempts},
        {
            "$set": {
                "status": "completed",
                "result": result,
                "completed_at": datetime.now(timezone.utc),
            }
        },
    )
    return outcome.modified_count == 1


async def fail_job(db, job_id, error_reason: str, claim_attempts: int) -> bool:
    """Atomically mark a claimed job failed.

    `error_reason` must be a stable, machine-readable value (see
    llm_gateway.ChatGatewayFailureReason) - never a human-readable detail
    string. `claim_attempts` is the same fencing token described in
    complete_job's docstring - required here for the identical reason.

    Returns True iff THIS call's claim_attempts is what actually
    transitioned the job to "failed", False if it was fenced out as a
    no-op - same contract as complete_job's return value, kept for API
    consistency even though no current caller has a fail-only side effect
    to gate on it yet.
    """
    outcome = await db.chat_jobs.update_one(
        {"_id": job_id, "status": "processing", "attempts": claim_attempts},
        {
            "$set": {
                "status": "failed",
                "error_reason": error_reason,
                "completed_at": datetime.now(timezone.utc),
            }
        },
    )
    return outcome.modified_count == 1


async def reclaim_stale_jobs(
    db,
    stale_after_seconds: float,
    max_attempts: int,
) -> dict:
    """Recover "processing" jobs abandoned by a dead/killed worker.

    A job is eligible once it has been "processing" for at least
    `stale_after_seconds` (compared against `started_at`, set atomically by
    claim_next_job - never touched by anything else). Three disjoint,
    atomic `update_many` calls handle the possible outcomes:

    - `attempts < max_attempts` AND `priority > RETRY_JOB_PRIORITY`: sent
      back to "pending" (started_at cleared) at the demoted
      `RETRY_JOB_PRIORITY`, so it re-enters the claim cycle behind fresh
      default-priority work - see the module docstring's "Retry-priority
      policy" section.
    - `attempts < max_attempts` AND `priority <= RETRY_JOB_PRIORITY`: sent
      back to "pending" with its priority left unchanged - already at or
      below the retry level, so demoting further would be an unbounded,
      cumulative decay (and, for a job already lower than
      `RETRY_JOB_PRIORITY`, this also prevents ever *promoting* it).
    - `attempts >= max_attempts`: marked permanently "failed" with
      error_reason=STALE_ABANDONED_REASON instead of being recycled again -
      bounds a job that reliably kills its worker to a few tries rather than
      cycling pending<->processing forever.

    All filters require status == "processing", exactly like complete_job/
    fail_job, so this can never touch a job that isn't currently claimed,
    and can never race with itself into a double transition. (This governs
    the *pending/failed* transition only; it does not by itself make a
    later complete_job/fail_job from an obsolete claim safe - that's what
    the `attempts` fencing check in complete_job/fail_job is for.)

    Returns {"recovered": n, "abandoned": n} for observability/logging.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    stale_filter = {"status": "processing", "started_at": {"$lte": cutoff}}
    recoverable_filter = {**stale_filter, "attempts": {"$lt": max_attempts}}

    recovered_demoted = await db.chat_jobs.update_many(
        {**recoverable_filter, "priority": {"$gt": RETRY_JOB_PRIORITY}},
        {"$set": {"status": "pending", "started_at": None, "priority": RETRY_JOB_PRIORITY}},
    )
    recovered_unchanged = await db.chat_jobs.update_many(
        {**recoverable_filter, "priority": {"$lte": RETRY_JOB_PRIORITY}},
        {"$set": {"status": "pending", "started_at": None}},
    )
    abandoned = await db.chat_jobs.update_many(
        {**stale_filter, "attempts": {"$gte": max_attempts}},
        {
            "$set": {
                "status": "failed",
                "error_reason": STALE_ABANDONED_REASON,
                "completed_at": datetime.now(timezone.utc),
            }
        },
    )
    return {
        "recovered": recovered_demoted.modified_count + recovered_unchanged.modified_count,
        "abandoned": abandoned.modified_count,
    }
