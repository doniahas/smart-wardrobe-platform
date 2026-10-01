"""Orchestrates the full automatic memory-learning pipeline
for a single chat message - build the extraction prompt, call the local
LLM through the existing gateway, parse and validate its response, apply
the should_save/confidence trust gate, merge an approved candidate into
the user's existing memory, and persist only the resulting updates.

Not wired into any chat route or worker yet (see routers/chat.py and
chat_worker.py) - this module only provides the shared pipeline both call
sites will eventually invoke, the same way services/llm_gateway.py::
call_llm is already shared between them.

Every expected outcome - an LLM gateway failure, a response that fails to
parse or validate, should_save=false, low confidence, an empty candidate,
or a merge that produces no changes - is reported by returning False, never
by raising. None of the reused pieces below ever raise for their own
expected failure modes (call_llm, parse_memory_extraction_response,
is_chat_memory_candidate_approved, merge_chat_memory_candidate are all
either total or explicitly documented as non-raising), so no try/except is
needed here to achieve that. A genuinely unexpected error (e.g. a real
MongoDB failure) is not swallowed - it propagates to the caller, the same
way no other DB-calling function in this codebase (routers/memory.py,
routers/feedback.py) wraps its own database calls in a blanket
try/except; that responsibility belongs to whatever eventually calls this
function (compare chat_worker.py's _worker_loop, which catches broad
exceptions around an entire job-processing call, not inside it).
"""
import logging
from datetime import datetime, timezone

from app.services.llm_gateway import ChatGatewayFailure, call_llm
from app.services.memory_extraction import (
    MEMORY_EXTRACTION_SCHEMA,
    build_memory_extraction_prompt,
    is_chat_memory_candidate_approved,
    message_has_time_anchor,
    parse_memory_extraction_response,
)
from app.services.personalization import merge_chat_memory_candidate

logger = logging.getLogger(__name__)

# Stable, machine-readable outcome codes for the diagnostic log below.
# Same posture as llm_gateway.py's ChatGatewayFailureReason: a code is safe
# to grep for and to keep stable, unlike free-form wording.
OUTCOME_LEARNED = "learned"
OUTCOME_LLM_UNAVAILABLE = "llm_call_failed"
OUTCOME_UNPARSABLE = "unparsable_or_schema_invalid"
OUTCOME_NOT_DURABLE = "model_judged_not_durable"
OUTCOME_LOW_CONFIDENCE = "confidence_below_threshold"
OUTCOME_TIME_ANCHORED = "message_time_anchored"
OUTCOME_NO_CHANGE = "no_new_information"


def _log_outcome(user_id: str, outcome: str, **details) -> None:
    """Record why one extraction did or didn't reach memory.

    Deliberately logs only the user id, a stable outcome code, and small
    structural facts (field NAMES, counts, the confidence number) - never
    the chat message, never the model's `reasoning` text (which can
    paraphrase the message), and never any extracted preference VALUE. The
    previous silent-return behavior is exactly why this defect survived
    undetected in production; see tests/test_logging_privacy.py for the
    standing rule this respects.
    """
    extra = " ".join(f"{key}={value}" for key, value in sorted(details.items()))
    logger.info(
        "chat memory learning: user=%s outcome=%s%s",
        user_id,
        outcome,
        f" {extra}" if extra else "",
    )


async def learn_memory_from_chat(db, user_id: str, message: str) -> bool:
    """Run the full chat-driven memory-learning pipeline for one message.

    Returns True only when the user's memory document was actually updated
    as a result of this call. Returns False for every expected non-update
    outcome (LLM unavailable/timed out/invalid response, unparsable or
    schema-invalid LLM output, should_save=false, confidence below the
    trust gate's threshold, an empty candidate, or a merge that produces no
    changes) without ever raising for any of them.

    `user_id` must be the caller's own authenticated/internal id - it is
    only ever used to scope the read and the write to that single user's
    memory document. It is never derived from `message` or from the LLM's
    output, and the LLM's output can never smuggle in a different one
    (ChatMemoryCandidate has no user_id field and rejects unknown fields).
    """
    prompt = build_memory_extraction_prompt(message)

    # Structured output: the decoder is grammar-constrained to
    # MEMORY_EXTRACTION_SCHEMA rather than merely asked for JSON in prose -
    # see that schema's own comment for the real-model evidence behind it.
    llm_result = await call_llm(prompt, format_schema=MEMORY_EXTRACTION_SCHEMA)
    if isinstance(llm_result, ChatGatewayFailure):
        _log_outcome(user_id, OUTCOME_LLM_UNAVAILABLE, reason=llm_result.reason.value)
        return False

    # salvage_invalid_fields: one individually-invalid optional field no
    # longer discards an otherwise-correct extraction alongside it (an
    # unknown field is still rejected outright) - see _salvage_candidate.
    extraction = parse_memory_extraction_response(llm_result, salvage_invalid_fields=True)
    if extraction is None:
        _log_outcome(user_id, OUTCOME_UNPARSABLE)
        return False

    if not extraction.should_save:
        _log_outcome(user_id, OUTCOME_NOT_DURABLE)
        return False

    if not is_chat_memory_candidate_approved(extraction):
        _log_outcome(user_id, OUTCOME_LOW_CONFIDENCE, confidence=extraction.confidence)
        return False

    # Deterministic second opinion the model cannot override. One-directional
    # by design: it can only refuse a save, never cause one, so it can never
    # manufacture a false memory - it only stops an explicitly time-anchored
    # message ("I want to wear purple tonight") from becoming permanent when
    # the model wrongly judged it durable. See message_has_time_anchor.
    if message_has_time_anchor(message):
        _log_outcome(user_id, OUTCOME_TIME_ANCHORED)
        return False

    memory_doc = await db.memory.find_one({"user_id": user_id})
    # An explicit, first-person,
    # durable statement that already passed the should_save/confidence gate
    # above is trustworthy enough to reverse an earlier contradictory color
    # preference (see merge_chat_memory_candidate's docstring for why this
    # is opt-in and why recommendation_feedback_learning.py deliberately
    # does not pass it).
    updates = merge_chat_memory_candidate(
        memory_doc, extraction.candidate, allow_color_preference_reversal=True
    )
    if not updates:
        _log_outcome(user_id, OUTCOME_NO_CHANGE)
        return False

    updates["updated_at"] = datetime.now(timezone.utc)
    await db.memory.update_one(
        {"user_id": user_id},
        {"$set": updates, "$setOnInsert": {"user_id": user_id}},
        upsert=True,
    )
    # Field NAMES only - never the learned values themselves.
    _log_outcome(user_id, OUTCOME_LEARNED, fields=",".join(sorted(k for k in updates if k != "updated_at")))
    return True
