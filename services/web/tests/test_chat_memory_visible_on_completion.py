"""Immediate-next-turn memory visibility (2026-08-28).

Manual browser testing found that a preference stated in one chat turn was
missing from the very next turn, even though it was correctly persisted a
moment later. Root cause: `complete_job` - the point at which the browser's
poll of `GET /chat/jobs/{id}` renders the reply and re-enables the input -
ran BEFORE `learn_memory_from_chat`, leaving a window as wide as the whole
extraction LLM call (5-20s measured) in which the next turn's prompt was
built from not-yet-written memory.

The invariant these tests pin:

    Once a chat job is reported COMPLETE to the client, any durable memory
    learned from that message is already committed and visible to the next
    request.

No test here sleeps or waits on wall-clock time. The ordering is asserted
directly: either by observing DB state at the exact moment of completion,
or by holding the extraction call open on an explicit asyncio.Event and
checking that the job has NOT been published while it is held.
"""
import asyncio
import json

import httpx
import pytest
from bson import ObjectId
from pymongo import ReturnDocument

from app.services.chat_queue import complete_job, create_chat_job, still_owns_claim
from app.services.chat_worker import process_one_job

_EXTRACTION_MARKER = "[MEMORY_EXTRACTION_USER_MESSAGE]"


def _extraction_payload(colour="pink", sentiment="like"):
    return {
        "candidate": {"colors": [{"color": colour, "sentiment": sentiment}]},
        "reasoning": "The user stated a lasting colour preference.",
        "is_lasting_preference": True,
        "confidence": 0.95,
    }


def _mock_llm(monkeypatch, reply_text="Sounds lovely.", payload=None, on_extraction=None):
    """Route the reply prompt and the extraction prompt to different
    answers, exactly as a real Ollama call would. `on_extraction` is an
    optional async hook invoked while the extraction call is in flight -
    the point at which memory has NOT yet been written."""
    payload = payload if payload is not None else _extraction_payload()

    async def fake_post(self, url, **kwargs):
        prompt = kwargs["json"]["prompt"]
        if _EXTRACTION_MARKER in prompt:
            if on_extraction is not None:
                await on_extraction()
            return httpx.Response(200, json={"response": json.dumps(payload), "model": "qwen2.5:3b"})
        return httpx.Response(200, json={"response": reply_text, "model": "qwen2.5:3b"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


# --- The invariant, asserted at the exact moment of publication ---


@pytest.mark.asyncio
async def test_memory_is_already_committed_when_the_job_is_published(db, monkeypatch):
    """The core regression. Snapshots the user's memory document at the
    instant complete_job is called - i.e. the instant the browser could
    first see the reply. Before the fix this snapshot was empty."""
    await create_chat_job(db, "user-1", "prompt", message="I love wearing pink")
    _mock_llm(monkeypatch)

    seen_at_completion = {}
    real_complete_job = complete_job

    async def spying_complete_job(db_, job_id, result, claim_attempts):
        doc = await db_.memory.find_one({"user_id": "user-1"})
        seen_at_completion["liked_colors"] = (doc or {}).get("liked_colors")
        return await real_complete_job(db_, job_id, result=result, claim_attempts=claim_attempts)

    monkeypatch.setattr("app.services.chat_worker.complete_job", spying_complete_job)

    assert await process_one_job(db) is True
    assert seen_at_completion["liked_colors"] == ["pink"], (
        "memory must already be committed when the job is published to the client"
    )


@pytest.mark.asyncio
async def test_job_is_not_published_while_memory_learning_is_still_in_flight(db, monkeypatch):
    """Ordering proven with an explicit synchronization point rather than a
    delay: the extraction call is held open on an Event, and while it is
    held the job must NOT yet read as completed to a polling client."""
    doc = await create_chat_job(db, "user-1", "prompt", message="I love wearing pink")
    release = asyncio.Event()
    observed = {}

    async def on_extraction():
        # The extraction call is in flight => memory is not written yet.
        job = await db.chat_jobs.find_one({"_id": doc["_id"]})
        observed["status_while_learning"] = job["status"]
        observed["result_while_learning"] = job.get("result")
        release.set()

    _mock_llm(monkeypatch, on_extraction=on_extraction)

    await process_one_job(db)

    assert release.is_set(), "the extraction call must actually have run"
    assert observed["status_while_learning"] != "completed", (
        "the job must not be visible as completed while its memory learning is still running"
    )
    assert observed["result_while_learning"] is None

    final = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert final["status"] == "completed"
    assert (await db.memory.find_one({"user_id": "user-1"}))["liked_colors"] == ["pink"]


# --- The real user sequence, end to end through the routes ---


async def _run_pending_jobs(db):
    while await process_one_job(db):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("colour,sentiment,statement,question,memory_field", [
    ("pink", "like", "I love wearing pink", "What colors do I like to wear?", "liked_colors"),
    ("black", "dislike", "I hate black clothes", "What colors should I avoid?", "disliked_colors"),
])
async def test_immediate_next_turn_prompt_contains_the_just_learned_preference(
    client, db, make_user, monkeypatch, colour, sentiment, statement, question, memory_field
):
    """The manual browser sequence, with no artificial delay: submit turn 1,
    let the worker finish it, poll until the client would see it completed,
    then IMMEDIATELY submit turn 2 and inspect the prompt that was actually
    stored for it."""
    user = await make_user("racer")
    headers = user["headers"]
    _mock_llm(monkeypatch, payload=_extraction_payload(colour, sentiment))

    first = client.post("/chat/jobs", json={"message": statement}, headers=headers)
    assert first.status_code == 202
    job_id = first.json()["job_id"]

    await _run_pending_jobs(db)

    # This is exactly what chat.js does before re-enabling the input.
    polled = client.get(f"/chat/jobs/{job_id}", headers=headers)
    assert polled.json()["status"] == "completed"

    # No sleep here - the next turn goes in the moment the client sees the reply.
    second = client.post("/chat/jobs", json={"message": question}, headers=headers)
    assert second.status_code == 202

    stored = await db.chat_jobs.find_one({"_id": ObjectId(second.json()["job_id"])})
    assert colour in stored["prompt"].lower(), (
        f"the immediate next turn's prompt must already carry {colour!r}"
    )
    assert (await db.memory.find_one({"user_id": user["user_id"]}))[memory_field] == [colour]


# --- Fencing must survive the reordering ---


@pytest.mark.asyncio
async def test_still_owns_claim_is_true_for_the_current_claim(db):
    doc = await create_chat_job(db, "user-1", "prompt", message="I love wearing pink")
    await db.chat_jobs.update_one(
        {"_id": doc["_id"]}, {"$set": {"status": "processing"}, "$inc": {"attempts": 1}}
    )
    assert await still_owns_claim(db, doc["_id"], 1) is True


@pytest.mark.asyncio
async def test_still_owns_claim_is_false_after_a_reclaim_bumped_attempts(db):
    doc = await create_chat_job(db, "user-1", "prompt", message="I love wearing pink")
    await db.chat_jobs.update_one(
        {"_id": doc["_id"]}, {"$set": {"status": "processing"}, "$inc": {"attempts": 2}}
    )
    assert await still_owns_claim(db, doc["_id"], 1) is False


@pytest.mark.asyncio
async def test_still_owns_claim_is_false_once_the_job_is_resolved(db):
    doc = await create_chat_job(db, "user-1", "prompt", message="I love wearing pink")
    await db.chat_jobs.update_one(
        {"_id": doc["_id"]}, {"$set": {"status": "completed"}, "$inc": {"attempts": 1}}
    )
    assert await still_owns_claim(db, doc["_id"], 1) is False


@pytest.mark.asyncio
async def test_still_owns_claim_is_read_only(db):
    doc = await create_chat_job(db, "user-1", "prompt", message="I love wearing pink")
    before = await db.chat_jobs.find_one({"_id": doc["_id"]})
    await still_owns_claim(db, doc["_id"], 0)
    await still_owns_claim(db, doc["_id"], 99)
    assert await db.chat_jobs.find_one({"_id": doc["_id"]}) == before


@pytest.mark.asyncio
async def test_a_reclaimed_job_does_not_learn_twice_from_the_stale_attempt(db, monkeypatch):
    """The claim-fencing guarantee, re-proven against the new ordering: a
    stale attempt whose job was reclaimed and completed by a newer attempt
    must still not learn memory. Previously this was enforced by
    complete_job's return value; it is now enforced by still_owns_claim."""
    doc = await create_chat_job(db, "user-1", "prompt", message="I love wearing pink")

    async def fake_call_llm(prompt, format_schema=None):
        # While this attempt's reply call is in flight, the reaper reclaims
        # the job and a newer attempt completes it.
        await db.chat_jobs.update_one(
            {"_id": doc["_id"]}, {"$set": {"status": "pending", "started_at": None}}
        )
        newer = await db.chat_jobs.find_one_and_update(
            {"_id": doc["_id"], "status": "pending"},
            {"$set": {"status": "processing"}, "$inc": {"attempts": 1}},
            return_document=ReturnDocument.AFTER,
        )
        assert await complete_job(
            db, doc["_id"], result="newer attempt result", claim_attempts=newer["attempts"]
        ) is True
        return "stale result"

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)

    learned = []

    async def fake_learn(db_, user_id, message):
        learned.append((user_id, message))

    monkeypatch.setattr("app.services.chat_worker.learn_memory_from_chat", fake_learn)

    assert await process_one_job(db) is True
    assert learned == [], "a fenced-out stale attempt must not learn memory"
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["result"] == "newer attempt result"


@pytest.mark.asyncio
async def test_learning_failure_still_lets_the_job_complete(db, monkeypatch):
    """Learning now runs first, so it must not be able to block completion -
    the reply is already computed and the user must still receive it."""
    doc = await create_chat_job(db, "user-1", "prompt", message="I love wearing pink")

    async def fake_call_llm(prompt, format_schema=None):
        return "Sounds lovely."

    async def exploding_learn(db_, user_id, message):
        raise RuntimeError("extraction blew up")

    monkeypatch.setattr("app.services.chat_worker.call_llm", fake_call_llm)
    monkeypatch.setattr("app.services.chat_worker.learn_memory_from_chat", exploding_learn)

    assert await process_one_job(db) is True
    stored = await db.chat_jobs.find_one({"_id": doc["_id"]})
    assert stored["status"] == "completed"
    assert stored["result"] == "Sounds lovely."


@pytest.mark.asyncio
async def test_synchronous_chat_route_keeps_the_same_invariant(client, db, make_user, monkeypatch):
    """The synchronous route shares run_claimed_job, so it must keep the
    guarantee too - it already did, but the ordering change must not have
    silently inverted it."""
    user = await make_user("syncracer")
    _mock_llm(monkeypatch)

    resp = client.post("/chat", json={"message": "I love wearing pink"},
                       headers=user["headers"])
    assert resp.status_code == 200
    assert (await db.memory.find_one({"user_id": user["user_id"]}))["liked_colors"] == ["pink"]
