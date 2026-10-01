"""Integration of learn_memory_from_chat into POST /chat,
POST /chat/jobs, and the chat worker.

Both the sync /chat route and the async worker now make TWO real LLM
calls per successful chat turn: the assistant reply, and (internally,
inside learn_memory_from_chat) the memory-extraction call. Both go through
the same llm_gateway.call_llm -> httpx.AsyncClient.post path, so a single
fake_post is used here, branching on the outgoing prompt's content -
mirroring tests/test_chat_route.py's own _fake_llm_response technique,
per the task's "mock only llm_gateway" instruction.
"""
import json

import httpx
import pytest
from bson import ObjectId

from app.services.chat_worker import process_one_job

_EXTRACTION_MARKER = "[MEMORY_EXTRACTION_USER_MESSAGE]"


def _extraction_payload(should_save=True, confidence=0.95, **candidate_overrides) -> dict:
    candidate = {
        "preferred_styles": [],
        "liked_colors": [],
        "disliked_colors": [],
        "disliked_item_types": [],
        "preferred_occasions": [],
        "preferred_fit": None,
        "communication_style": None,
    }
    candidate.update(candidate_overrides)
    return {"should_save": should_save, "confidence": confidence, "candidate": candidate}


def _mock_llm(monkeypatch, reply_text: str, extraction_payload: dict, extraction_status: int = 200):
    """Route the reply prompt to `reply_text` and the extraction prompt
    (identified by its dedicated delimiter marker) to `extraction_payload`,
    exactly like a real Ollama call would return two different answers for
    two different prompts. Note: the patched function deliberately avoids a
    parameter literally named `json` (call_llm passes it as a keyword arg)
    since this helper needs the real `json` module for json.dumps below."""

    async def fake_post(self, url, **kwargs):
        prompt = kwargs["json"]["prompt"]
        if _EXTRACTION_MARKER in prompt:
            body = {"response": json.dumps(extraction_payload), "model": "qwen2.5:3b"}
            return httpx.Response(extraction_status, json=body)
        return httpx.Response(200, json={"response": reply_text, "model": "qwen2.5:3b"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _mock_llm_reply_only(monkeypatch, reply_text: str, exc=None, extraction_exc=None):
    """Reply succeeds; the extraction call (identified by its marker) can
    independently fail (exc) to simulate learning-pipeline failures without
    touching the reply path at all."""

    async def fake_post(self, url, **kwargs):
        prompt = kwargs["json"]["prompt"]
        if _EXTRACTION_MARKER in prompt:
            if extraction_exc is not None:
                raise extraction_exc
            return httpx.Response(200, json={"response": "not json at all", "model": "qwen2.5:3b"})
        if exc is not None:
            raise exc
        return httpx.Response(200, json={"response": reply_text, "model": "qwen2.5:3b"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


@pytest.mark.asyncio
async def test_successful_chat_still_returns_the_normal_assistant_response(client, make_user, monkeypatch):
    user = await make_user("alice")
    _mock_llm(monkeypatch, "Try a red top.", _extraction_payload(liked_colors=["blue"]))

    resp = client.post("/chat", json={"message": "I like blue."}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json() == {"response": "Try a red top."}


@pytest.mark.asyncio
async def test_successful_chat_triggers_memory_learning(client, db, make_user, monkeypatch):
    user = await make_user("bob")
    _mock_llm(monkeypatch, "Sure!", _extraction_payload(liked_colors=["blue"]))

    resp = client.post("/chat", json={"message": "I like blue."}, headers=user["headers"])

    assert resp.status_code == 200
    doc = await db.memory.find_one({"user_id": user["user_id"]})
    assert doc is not None
    assert doc["liked_colors"] == ["blue"]


@pytest.mark.asyncio
async def test_memory_learning_failure_does_not_change_the_assistant_response(
    client, db, make_user, monkeypatch
):
    user = await make_user("carol")
    _mock_llm_reply_only(monkeypatch, "Sure!", extraction_exc=httpx.ConnectError("refused"))

    resp = client.post("/chat", json={"message": "I hate black."}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json() == {"response": "Sure!"}
    assert await db.memory.find_one({"user_id": user["user_id"]}) is None


@pytest.mark.asyncio
async def test_low_confidence_extraction_leaves_memory_unchanged(client, db, make_user, monkeypatch):
    user = await make_user("dave")
    _mock_llm(monkeypatch, "Sure!", _extraction_payload(confidence=0.4, liked_colors=["blue"]))

    resp = client.post("/chat", json={"message": "Maybe I like blue?"}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json() == {"response": "Sure!"}
    assert await db.memory.find_one({"user_id": user["user_id"]}) is None


@pytest.mark.asyncio
async def test_malformed_extraction_output_leaves_memory_unchanged(client, db, make_user, monkeypatch):
    user = await make_user("erin")
    _mock_llm_reply_only(monkeypatch, "Sure!")  # extraction call returns non-JSON text

    resp = client.post("/chat", json={"message": "I hate black."}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json() == {"response": "Sure!"}
    assert await db.memory.find_one({"user_id": user["user_id"]}) is None


@pytest.mark.asyncio
async def test_async_chat_worker_also_invokes_the_orchestrator(client, db, make_user, monkeypatch):
    user = await make_user("frank")
    _mock_llm(monkeypatch, "Try navy.", _extraction_payload(liked_colors=["green"]))

    submit_resp = client.post(
        "/chat/jobs", json={"message": "I like green."}, headers=user["headers"]
    )
    assert submit_resp.status_code == 202
    job_id = submit_resp.json()["job_id"]

    processed = await process_one_job(db)

    assert processed is True
    stored_job = await db.chat_jobs.find_one({"_id": ObjectId(job_id)})
    assert stored_job["status"] == "completed"
    assert stored_job["result"] == "Try navy."

    doc = await db.memory.find_one({"user_id": user["user_id"]})
    assert doc is not None
    assert doc["liked_colors"] == ["green"]


@pytest.mark.asyncio
async def test_memory_learned_in_one_chat_turn_is_used_in_a_later_turns_prompt(
    client, db, make_user, monkeypatch
):
    """Preference reversal, downstream-use proof: a
    durable preference learned from one message must actually reach the
    prompt built for a LATER, separate chat turn - without the user
    repeating themselves - not just sit unused in the database."""
    user = await make_user("ivan")

    captured_reply_prompts = []

    async def fake_post(self, url, **kwargs):
        prompt = kwargs["json"]["prompt"]
        if _EXTRACTION_MARKER in prompt:
            body = {
                "response": json.dumps(_extraction_payload(disliked_colors=["black"])),
                "model": "qwen2.5:3b",
            }
            return httpx.Response(200, json=body)
        captured_reply_prompts.append(prompt)
        return httpx.Response(200, json={"response": "Sure!", "model": "qwen2.5:3b"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    first = client.post(
        "/chat", json={"message": "I really hate black clothing."}, headers=user["headers"]
    )
    assert first.status_code == 200
    assert (await db.memory.find_one({"user_id": user["user_id"]}))["disliked_colors"] == ["black"]

    second = client.post(
        "/chat", json={"message": "What should I wear today?"}, headers=user["headers"]
    )
    assert second.status_code == 200

    # The first call's own reply prompt had no memory yet; the second turn's
    # reply prompt must reflect what was just learned from the first turn.
    assert "Disliked colors: black" not in captured_reply_prompts[0]
    assert "Disliked colors: black" in captured_reply_prompts[1]


@pytest.mark.asyncio
async def test_generic_preference_learned_in_one_turn_is_used_in_a_later_turns_prompt(
    client, db, make_user, monkeypatch
):
    """Long-term memory follow-up completion, downstream-use proof for the
    generic durable-preference fallback: exactly the corrected requirement's
    scenario - Turn 1 states a durable preference that has no dedicated
    structured field ('From now on I prefer modest, more covered
    clothing'); Turn 2 ('What should I wear to dinner?') must receive that
    saved preference in its own prompt without the user repeating it."""
    user = await make_user("judy")

    captured_reply_prompts = []

    async def fake_post(self, url, **kwargs):
        prompt = kwargs["json"]["prompt"]
        if _EXTRACTION_MARKER in prompt:
            body = {
                "response": json.dumps(_extraction_payload(additional_preferences=[
                    {"topic": "clothing coverage", "preference": "prefers more covered, modest clothing"}
                ])),
                "model": "qwen2.5:3b",
            }
            return httpx.Response(200, json=body)
        captured_reply_prompts.append(prompt)
        return httpx.Response(200, json={"response": "Sure!", "model": "qwen2.5:3b"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    first = client.post(
        "/chat",
        json={"message": "From now on I prefer modest, more covered clothing."},
        headers=user["headers"],
    )
    assert first.status_code == 200
    stored = await db.memory.find_one({"user_id": user["user_id"]})
    assert stored["additional_preferences"] == [
        {"topic": "clothing coverage", "preference": "prefers more covered, modest clothing"}
    ]

    second = client.post(
        "/chat", json={"message": "What should I wear to dinner?"}, headers=user["headers"]
    )
    assert second.status_code == 200

    assert "Other stated preference (clothing coverage)" not in captured_reply_prompts[0]
    assert (
        "Other stated preference (clothing coverage): prefers more covered, modest clothing"
        in captured_reply_prompts[1]
    )


@pytest.mark.asyncio
async def test_one_users_chat_learning_never_updates_another_users_memory(
    client, db, make_user, monkeypatch
):
    alice = await make_user("grace")
    bob = await make_user("heidi")
    _mock_llm(monkeypatch, "Sure!", _extraction_payload(liked_colors=["purple"]))

    resp = client.post("/chat", json={"message": "I like purple."}, headers=alice["headers"])
    assert resp.status_code == 200

    alice_doc = await db.memory.find_one({"user_id": alice["user_id"]})
    bob_doc = await db.memory.find_one({"user_id": bob["user_id"]})

    assert alice_doc is not None
    assert alice_doc["liked_colors"] == ["purple"]
    assert bob_doc is None
