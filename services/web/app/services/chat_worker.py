"""Background workers that claim and process queued chat jobs.

Bounded concurrency for the worker pool comes from running exactly
CHAT_WORKER_CONCURRENCY independent loop tasks, each processing one job at
a time - claim_next_job's atomicity (see chat_queue.py) is what makes
running several loops against the same queue safe, so no additional
locking is needed for the loops themselves.

Chat-queue-unification follow-up: the synchronous `POST /chat` route
(routers/chat.py) now ALSO calls the local LLM, via run_claimed_job()
below, for a job it creates and claims itself (chat_queue.py::
claim_job_by_id) rather than pulling from the shared queue - so "N worker
loop tasks" alone no longer bounds total concurrent LLM calls the way the
module docstring above used to describe. `_get_llm_semaphore()` is
the explicit, shared primitive that now does that job for both entry
points: acquired around the actual LLM call in run_claimed_job(), never
around the surrounding DB writes, so at most CHAT_WORKER_CONCURRENCY LLM
calls are ever in flight at once, worker-loop-issued and synchronous-route-
issued combined. Total concurrent LLM calls across a *deployment* (as
opposed to one `web` process) is still (number of web processes/replicas) x
CHAT_WORKER_CONCURRENCY, since the semaphore is process-local - see the
config.py comment for why that isn't a single shared pool.

This module also runs a single, separate stale-job
reaper task (start_stale_job_reaper/stop_stale_job_reaper) that periodically
recovers "processing" jobs abandoned by a killed/crashed worker - see
chat_queue.py::reclaim_stale_jobs for the recovery rule itself. It's a
distinct task/stop_event pair from the worker pool (start_chat_workers/
stop_chat_workers), not folded into the same list, so worker-count
assumptions elsewhere (CHAT_WORKER_CONCURRENCY tasks == CHAT_WORKER_
CONCURRENCY concurrent LLM slots) stay exact and the two concerns can be
started/stopped/reasoned about independently.
"""
import asyncio
import logging

from app.core.config import (
    CHAT_JOB_MAX_ATTEMPTS,
    CHAT_JOB_RECOVERY_INTERVAL_SECONDS,
    CHAT_JOB_STALE_TIMEOUT_SECONDS,
    CHAT_WORKER_CONCURRENCY,
    CHAT_WORKER_POLL_INTERVAL_SECONDS,
    CHAT_WORKER_SHUTDOWN_TIMEOUT_SECONDS,
)
from app.models import CatalogProductPublic
from app.services.chat_catalog_grounding import validate_grounded_reply
from app.services.chat_memory_learning import learn_memory_from_chat
from app.services.chat_queue import (
    claim_next_job,
    complete_job,
    fail_job,
    reclaim_stale_jobs,
    still_owns_claim,
)
from app.services.chat_text_formatting import normalize_chat_reply
from app.services.llm_gateway import ChatGatewayFailure, call_llm

logger = logging.getLogger(__name__)

# Chat-queue-unification follow-up: the single, process-wide concurrency
# gate shared by every worker loop AND the synchronous /chat route (see the
# module docstring above and run_claimed_job() below).
#
# NOT constructed eagerly at import time: an asyncio.Semaphore's internal
# futures are bound to whichever event loop is running the first time it's
# awaited, and reusing it from a *different* loop later raises
# "bound to a different event loop". In this app's real production
# lifetime there is exactly one event loop for the process's whole life, so
# that would never bite - but the test suite runs many independent
# pytest-asyncio event loops (typically one per test function) against
# this same imported module, and a module-level singleton semaphore would
# be permanently wedged to whichever test happened to run (and create it)
# first. `_get_llm_semaphore()` instead lazily (re)creates it whenever the
# *currently running* loop differs from the one it was last built for -
# exactly once per real event loop, so production still gets one long-
# lived, truly shared semaphore, while each test transparently gets its
# own correctly-scoped one.
_llm_semaphore: asyncio.Semaphore | None = None
_llm_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _get_llm_semaphore() -> asyncio.Semaphore:
    global _llm_semaphore, _llm_semaphore_loop
    loop = asyncio.get_running_loop()
    if _llm_semaphore is None or _llm_semaphore_loop is not loop:
        _llm_semaphore = asyncio.Semaphore(CHAT_WORKER_CONCURRENCY)
        _llm_semaphore_loop = loop
    return _llm_semaphore


def _job_catalog_products(job: dict) -> tuple[CatalogProductPublic, ...] | None:
    """Reconstruct CatalogGrounding.products (see chat_catalog_grounding.py)
    from a job's stored `catalog_products` field for validate_grounded_
    reply() - None (not grounded) is preserved as-is; a stored list
    (possibly empty) is rebuilt into CatalogProductPublic instances from the
    exact same submission-time data routers/chat.py serialized, never
    re-fetched from the catalog service at this later point."""
    raw_products = job.get("catalog_products")
    if raw_products is None:
        return None
    return tuple(CatalogProductPublic(**product) for product in raw_products)


def _job_catalog_status(job: dict) -> str | None:
    """The same request's CatalogGrounding.status, stored verbatim on the
    job as `catalog_status` (see chat_queue.py::create_chat_job) - a plain
    string, no reconstruction needed. `.get(...)` rather than a bare
    subscript so a pre-existing job document from before this field
    existed (or any test that only sets `catalog_products`) safely reads
    as None, matching how the rest of this module already treats a job
    with no stored catalog_products."""
    return job.get("catalog_status")


async def run_claimed_job(db, job: dict, claim_attempts: int) -> str | ChatGatewayFailure:
    """Given an already-claimed ("processing") job, call the LLM and run the
    exact same normalize -> hallucination-guardrail -> memory-learn ->
    complete/fail pipeline every completed/failed job goes through - then
    return the outcome.

    Learning deliberately precedes completion: complete_job is what makes
    the reply visible to a polling client, so anything the client's very
    next request must be able to see has to be committed before it. See
    the ordering comment in the body.

    Chat-queue-unification follow-up: this is the ONE place that pipeline
    lives. It's shared by process_one_job() (the background worker loop,
    which claims the next-priority-pending job via claim_next_job) and the
    synchronous `POST /chat` route (routers/chat.py, which claims its own
    just-created job via chat_queue.py::claim_job_by_id) - both entry
    points must apply identical LLM-calling/normalization/guardrail/memory-
    learning behavior, not two independently hand-maintained copies of it.

    `job` and `claim_attempts` must come from whichever claim_* call the
    caller just made (claim_next_job or claim_job_by_id) - `claim_attempts`
    is the claim-fencing token (see chat_queue.py's "Claim fencing"
    docstring section) that must be passed back to complete_job/fail_job
    unchanged, even if this job is later reclaimed by the stale-job reaper
    while this call is still in flight.

    Only the actual LLM call is made under `_get_llm_semaphore()` -
    the DB reads/writes before and after it are cheap and not what
    CHAT_WORKER_CONCURRENCY exists to bound.

    Returns the final result text on success (already normalized and
    guardrail-validated - exactly what was persisted as the job's `result`
    when complete_job's fencing check passed), or the ChatGatewayFailure
    describing why the job failed. Callers use `.reason.value` the same way
    this function already does for `fail_job`'s stable `error_reason`, and
    `.status_code`/`.detail` to build an HTTPException where relevant (the
    synchronous route only - a background-processed job has no HTTP
    response to build).
    """
    async with _get_llm_semaphore():
        result = await call_llm(job["prompt"])

    if isinstance(result, ChatGatewayFailure):
        await fail_job(db, job["_id"], error_reason=result.reason.value, claim_attempts=claim_attempts)
        return result

    # Same conservative Markdown-artifact
    # cleanup applied at both entry points (see chat_text_formatting.py) -
    # applied to the stored result so GET /chat/jobs/{id} and the
    # synchronous route's response both return already-clean text.
    result = normalize_chat_reply(result)
    # Hallucination-robustness follow-up: hard, code-level check of the
    # LLM's actual reply against the exact catalog products retrieved for
    # this request (see chat_catalog_grounding.py::validate_grounded_reply)
    # - applied identically regardless of which entry point produced `job`.
    result = validate_grounded_reply(result, _job_catalog_products(job), _job_catalog_status(job))

    # Memory learning runs BEFORE complete_job, not after it.
    #
    # complete_job is the PUBLICATION point: the instant it sets
    # status="completed", the browser's next poll of GET /chat/jobs/{id}
    # renders the reply and re-enables the input (see app/static/chat.js -
    # the product path is `POST /chat/jobs` + polling, never the
    # synchronous `POST /chat`). Learning after that point left a window
    # as wide as a whole second LLM call - the extraction request, measured
    # at 5-20s - in which the user could read the answer, immediately send
    # their next message, and have its prompt built from memory that had
    # not been written yet (reproduced consistently through the real
    # browser sequence).
    #
    # The required invariant is now structural: once a job is reported
    # completed to the client, any durable memory learned from that
    # message is already committed.
    #
    # KNOWN COST, accepted deliberately: the reply text is computed before
    # this point, but its PUBLICATION is deferred until learning lands, so
    # the user waits one extra extraction call before seeing the answer.
    # That is inherent to the invariant - a reply cannot both be published
    # immediately and be guaranteed to have its memory committed, and
    # publishing first is exactly what produced the bug this ordering
    # fixes. Measured on CPU-only Ollama with a warm model, the cost is
    # small: median turn-1 completion 14.5s before vs 15.3s after (n=8 vs
    # n=10 real browser-sequence runs), because the extraction call's
    # schema-constrained output is short. The worst case is bounded by the
    # same LLM_SERVICE_TIMEOUT_SECONDS that already bounds the reply call,
    # and a learning failure never blocks completion - the try/except below
    # swallows it and the job still completes normally.
    #
    # Memory learning is a
    # completion-only SIDE EFFECT and must be fenced exactly like the job
    # document itself, so a stale claim whose job was reclaimed and re-run
    # by a newer attempt must not learn for a job it no longer owns.
    # complete_job's return value can no longer serve as that fence (it now
    # runs afterwards), so the same condition is checked directly and
    # read-only beforehand - see chat_queue.py::still_owns_claim, including
    # why the residual non-atomic window is harmless rather than merely
    # unlikely.
    #
    # Learning uses the user's own original message (never
    # the assistant's reply/`result`). A job created without a stored
    # message (e.g. pre-existing tests that only pass a prompt) simply
    # skips learning. Every expected failure already returns False from
    # learn_memory_from_chat without raising; an unexpected error is logged
    # and swallowed so it can never stop this job from being completed or
    # affect the caller's own response.
    message = job.get("message")
    if message and await still_owns_claim(db, job["_id"], claim_attempts):
        try:
            await learn_memory_from_chat(db, job["user_id"], message)
        except Exception:
            logger.exception("chat worker: unexpected error during memory learning")

    completed = await complete_job(db, job["_id"], result=result, claim_attempts=claim_attempts)
    if not completed:
        # Fenced out: this claim was reclaimed while it worked, and a newer
        # attempt already resolved the job. Its result stands; ours is
        # discarded. Logged rather than silent - the previous code could
        # only infer this from an absent side effect.
        logger.warning(
            "chat worker: completion fenced out for job %s (stale claim, attempts=%s)",
            job["_id"],
            claim_attempts,
        )
    return result


async def process_one_job(db) -> bool:
    """Claim and process a single pending job from the shared priority
    queue, if one exists.

    Returns False (no side effects) when the queue is empty, True once a
    claimed job has been run through run_claimed_job() to completion or
    failure.
    """
    job = await claim_next_job(db)
    if job is None:
        return False

    # `job["attempts"]` is this claim's
    # fencing token - it must be captured from *this* claim_next_job call
    # and passed unchanged into run_claimed_job(), even if this job is
    # later reclaimed by the stale-job reaper while this call is in flight.
    await run_claimed_job(db, job, claim_attempts=job["attempts"])
    return True


async def _worker_loop(db, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            processed = await process_one_job(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad job (e.g. an unexpected DB error) must not permanently
            # kill this worker task - log it and keep polling.
            logger.exception("chat worker: unexpected error processing job")
            processed = False

        if not processed:
            # Wait on stop_event rather than a plain sleep, so an idle
            # worker reacts to shutdown immediately instead of finishing out
            # a full poll interval first.
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=CHAT_WORKER_POLL_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass


def start_chat_workers(db) -> tuple[list[asyncio.Task], asyncio.Event]:
    """Start CHAT_WORKER_CONCURRENCY worker tasks against db."""
    stop_event = asyncio.Event()
    tasks = [
        asyncio.create_task(_worker_loop(db, stop_event))
        for _ in range(CHAT_WORKER_CONCURRENCY)
    ]
    return tasks, stop_event


async def stop_chat_workers(tasks: list[asyncio.Task], stop_event: asyncio.Event) -> None:
    """Signal all workers to stop, wait for a clean exit, and force-cancel
    stragglers past the shutdown timeout.

    Setting stop_event lets each loop finish its current job (if any) before
    exiting on its own, rather than being cancelled mid-request. Every task
    is always awaited (with return_exceptions=True after a forced cancel) so
    shutdown never leaves a dangling task or lets a task exception escape.
    """
    stop_event.set()
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks), timeout=CHAT_WORKER_SHUTDOWN_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _reap_stale_jobs_loop(db, stop_event: asyncio.Event) -> None:
    """Periodically recover abandoned "processing" jobs.

    Mirrors _worker_loop's shape (survive unexpected errors, wait on
    stop_event rather than a plain sleep so shutdown is immediate) but polls
    on CHAT_JOB_RECOVERY_INTERVAL_SECONDS instead of CHAT_WORKER_POLL_
    INTERVAL_SECONDS, since staleness is only ever checked in units of
    CHAT_JOB_STALE_TIMEOUT_SECONDS (tens of seconds to minutes) - polling as
    fast as the job workers do would just be wasted DB round-trips.
    """
    while not stop_event.is_set():
        try:
            outcome = await reclaim_stale_jobs(
                db, CHAT_JOB_STALE_TIMEOUT_SECONDS, CHAT_JOB_MAX_ATTEMPTS
            )
            if outcome["recovered"] or outcome["abandoned"]:
                logger.warning(
                    "chat job reaper: recovered=%d abandoned=%d (stale_after=%.1fs, max_attempts=%d)",
                    outcome["recovered"],
                    outcome["abandoned"],
                    CHAT_JOB_STALE_TIMEOUT_SECONDS,
                    CHAT_JOB_MAX_ATTEMPTS,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A bad sweep (e.g. a transient DB error) must not permanently
            # kill recovery - log it and keep polling, same principle as
            # _worker_loop's own exception handling.
            logger.exception("chat job reaper: unexpected error reclaiming stale jobs")

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=CHAT_JOB_RECOVERY_INTERVAL_SECONDS
            )
        except asyncio.TimeoutError:
            pass


def start_stale_job_reaper(db) -> tuple[asyncio.Task, asyncio.Event]:
    """Start the single background stale-job reaper task against db."""
    stop_event = asyncio.Event()
    task = asyncio.create_task(_reap_stale_jobs_loop(db, stop_event))
    return task, stop_event


async def stop_stale_job_reaper(task: asyncio.Task, stop_event: asyncio.Event) -> None:
    """Signal the reaper to stop and wait for a clean exit, force-cancelling
    past the shutdown timeout - same shape as stop_chat_workers, reusing the
    same CHAT_WORKER_SHUTDOWN_TIMEOUT_SECONDS grace period since both are
    "let the current DB operation finish, then stop" background tasks."""
    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=CHAT_WORKER_SHUTDOWN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
