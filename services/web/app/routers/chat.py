import logging

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import get_current_user
from app.core.config import RATE_LIMIT_CHAT
from app.core.database import get_db
from app.core.limiter import limiter
from app.models import ChatJobPublic, ChatRequest, ChatResponse
from app.services.chat_catalog_grounding import CatalogGrounding, get_catalog_grounding
from app.services.chat_context import build_chat_prompt
from app.services.chat_queue import claim_job_by_id, create_chat_job
from app.services.chat_worker import run_claimed_job
from app.services.llm_gateway import ChatGatewayFailure

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)


def _serialize_catalog_products(grounding: CatalogGrounding) -> list[dict] | None:
    """CatalogGrounding.products -> the JSON-safe form create_chat_job()
    persists on a job's `catalog_products` field (see chat_queue.py) - the
    same transformation both /chat and /chat/jobs need, kept in one place
    rather than duplicated at each call site below."""
    if grounding.products is None:
        return None
    return [product.model_dump(mode="json") for product in grounding.products]


@router.post("", response_model=ChatResponse)
@limiter.limit(RATE_LIMIT_CHAT)
async def chat(
    request: Request,
    body: ChatRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Synchronous chat: an immediate reply in this same HTTP request.

    Chat-queue-unification follow-up: this no longer calls the local LLM
    directly. It builds the memory-/catalog-aware prompt exactly as before,
    persists it as a real `chat_jobs` document (create_chat_job - the same
    call `/chat/jobs` makes), then immediately claims that specific job
    (chat_queue.py::claim_job_by_id) and runs it through the same
    LLM-call/normalize/hallucination-guardrail/complete-or-fail/memory-
    learn pipeline every job goes through (chat_worker.py::run_claimed_job)
    - including that pipeline's shared, process-wide concurrency limit, so
    a burst of direct `/chat` callers can no longer run unboundedly many
    concurrent LLM calls alongside the background worker pool.

    Deliberately does NOT wait in the shared priority/FIFO line the way a
    background-processed `/chat/jobs` submission does - claim_job_by_id()
    only ever targets the job this same request just created, never
    someone else's queued job, so this route can still return its own
    result synchronously instead of blocking behind arbitrary queue depth.
    See claim_job_by_id()'s and run_claimed_job()'s own docstrings for the
    full reasoning.
    """
    user_id = str(current_user["_id"])
    memory_doc = await db.memory.find_one({"user_id": user_id})
    # Only calls the catalog service when the message itself needs
    # external-product verification (see chat_catalog_grounding.py) -
    # ordinary chat never reaches the catalog service.
    grounding = await get_catalog_grounding(body.message)
    prompt = build_chat_prompt(body.message, memory_doc, grounding.context_text)

    doc = await create_chat_job(
        db, user_id, prompt, body.message,
        catalog_products=_serialize_catalog_products(grounding),
        catalog_status=grounding.status,
    )
    job = await claim_job_by_id(db, doc["_id"], user_id)
    if job is None:
        # Defensive only: a job this same request just inserted should
        # always still be claimable immediately afterward - nothing else
        # can plausibly know its id yet. Fail safely rather than ever
        # silently hanging if this is somehow reached.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat service temporarily unavailable.",
        )

    result = await run_claimed_job(db, job, claim_attempts=job["attempts"])
    if isinstance(result, ChatGatewayFailure):
        raise HTTPException(status_code=result.status_code, detail=result.detail)

    return ChatResponse(response=result)


def _parse_job_oid(job_id: str) -> ObjectId:
    try:
        return ObjectId(job_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job id")


def _job_to_public(doc: dict) -> ChatJobPublic:
    return ChatJobPublic(
        job_id=str(doc["_id"]),
        status=doc["status"],
        result=doc.get("result"),
        error_reason=doc.get("error_reason"),
        created_at=doc["created_at"],
        completed_at=doc.get("completed_at"),
    )


@router.post("/jobs", response_model=ChatJobPublic, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(RATE_LIMIT_CHAT)
async def submit_chat_job(
    request: Request,
    body: ChatRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Queue a chat message for later processing.

    The memory- and catalog-aware prompt is built once, here, at submission
    time and stored on the job - not rebuilt when the job is eventually
    processed. Catalog grounding (see chat_catalog_grounding.py) runs
    at this same submission-time step, exactly like the memory context does
    - the background worker (chat_worker.py) only ever calls the LLM with
    the already-built prompt and never talks to the catalog service itself,
    so the queue/worker/concurrency behavior is untouched by this.

    Hallucination-robustness follow-up: the exact structured products this
    submission's grounding retrieved (CatalogGrounding.products) are stored
    on the job alongside the prompt, so chat_worker.py can run the same
    validate_grounded_reply() check once the LLM's reply actually comes
    back - re-fetching the catalog at completion time would risk validating
    against a different (later) crawl result than what the prompt was
    actually built from.
    """
    user_id = str(current_user["_id"])
    memory_doc = await db.memory.find_one({"user_id": user_id})
    grounding = await get_catalog_grounding(body.message)
    prompt = build_chat_prompt(body.message, memory_doc, grounding.context_text)

    doc = await create_chat_job(
        db, user_id, prompt, body.message,
        catalog_products=_serialize_catalog_products(grounding),
        catalog_status=grounding.status,
    )
    return _job_to_public(doc)


@router.get("/jobs/{job_id}", response_model=ChatJobPublic)
async def get_chat_job(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_job_oid(job_id)
    user_id = str(current_user["_id"])
    doc = await db.chat_jobs.find_one({"_id": oid, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat job not found")
    return _job_to_public(doc)
