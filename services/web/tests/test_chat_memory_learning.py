import json
from datetime import datetime, timezone

import httpx
import pytest

from app.services.chat_memory_learning import learn_memory_from_chat


def _fake_llm_response(monkeypatch, response: httpx.Response = None, exc=None):
    """Patch the outbound call to the llm microservice, matching the same
    technique used by tests/test_chat_route.py."""

    async def fake_post(self, url, json=None, **kwargs):
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _extraction_payload(**candidate_overrides) -> dict:
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
    return {"should_save": True, "confidence": 0.95, "candidate": candidate}


def _llm_response_for(payload: dict) -> httpx.Response:
    """Wrap an extraction payload as the llm service's own /generate
    response shape: {"response": "<generated text>", "model": ...}. The
    generated text itself is the JSON the extraction parser must handle."""
    return httpx.Response(200, json={"response": json.dumps(payload), "model": "qwen2.5:3b"})


@pytest.mark.asyncio
async def test_durable_preference_is_persisted(db, monkeypatch):
    _fake_llm_response(monkeypatch, response=_llm_response_for(_extraction_payload(liked_colors=["blue"])))

    result = await learn_memory_from_chat(db, "user1", "I like blue.")

    assert result is True
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["liked_colors"] == ["blue"]
    assert doc["updated_at"] is not None


@pytest.mark.asyncio
async def test_existing_memory_is_merged_non_destructively(db, monkeypatch):
    await db.memory.insert_one(
        {"user_id": "user1", "liked_colors": ["red"], "preferred_styles": ["formal"]}
    )
    _fake_llm_response(monkeypatch, response=_llm_response_for(_extraction_payload(liked_colors=["blue"])))

    result = await learn_memory_from_chat(db, "user1", "I also like blue.")

    assert result is True
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["liked_colors"] == ["red", "blue"]
    assert doc["preferred_styles"] == ["formal"]


@pytest.mark.asyncio
async def test_should_save_false_performs_no_write(db, monkeypatch):
    payload = _extraction_payload(liked_colors=["blue"])
    payload["should_save"] = False
    _fake_llm_response(monkeypatch, response=_llm_response_for(payload))

    result = await learn_memory_from_chat(db, "user1", "I need a jacket today.")

    assert result is False
    assert await db.memory.find_one({"user_id": "user1"}) is None


@pytest.mark.asyncio
async def test_confidence_below_threshold_performs_no_write(db, monkeypatch):
    payload = _extraction_payload(liked_colors=["blue"])
    payload["confidence"] = 0.5
    _fake_llm_response(monkeypatch, response=_llm_response_for(payload))

    result = await learn_memory_from_chat(db, "user1", "Maybe I like blue?")

    assert result is False
    assert await db.memory.find_one({"user_id": "user1"}) is None


@pytest.mark.asyncio
async def test_empty_candidate_performs_no_write(db, monkeypatch):
    _fake_llm_response(monkeypatch, response=_llm_response_for(_extraction_payload()))

    result = await learn_memory_from_chat(db, "user1", "Hi there.")

    assert result is False
    assert await db.memory.find_one({"user_id": "user1"}) is None


@pytest.mark.asyncio
async def test_malformed_llm_output_performs_no_write(db, monkeypatch):
    _fake_llm_response(
        monkeypatch,
        response=httpx.Response(200, json={"response": "not json at all", "model": "qwen2.5:3b"}),
    )

    result = await learn_memory_from_chat(db, "user1", "I hate black.")

    assert result is False
    assert await db.memory.find_one({"user_id": "user1"}) is None


@pytest.mark.asyncio
async def test_unknown_field_output_performs_no_write(db, monkeypatch):
    payload = _extraction_payload(liked_colors=["blue"])
    payload["user_id"] = "someone-elses-id"
    _fake_llm_response(monkeypatch, response=_llm_response_for(payload))

    result = await learn_memory_from_chat(db, "user1", "I like blue.")

    assert result is False
    assert await db.memory.find_one({"user_id": "user1"}) is None


@pytest.mark.asyncio
async def test_llm_unavailable_returns_false_and_no_write(db, monkeypatch):
    _fake_llm_response(monkeypatch, exc=httpx.ConnectError("connection refused"))

    result = await learn_memory_from_chat(db, "user1", "I hate black.")

    assert result is False
    assert await db.memory.find_one({"user_id": "user1"}) is None


@pytest.mark.asyncio
async def test_llm_timeout_returns_false_and_no_write(db, monkeypatch):
    _fake_llm_response(
        monkeypatch,
        response=httpx.Response(504, json={"detail": "Ollama did not respond in time."}),
    )

    result = await learn_memory_from_chat(db, "user1", "I hate black.")

    assert result is False
    assert await db.memory.find_one({"user_id": "user1"}) is None


@pytest.mark.asyncio
async def test_invalid_llm_response_returns_false_and_no_write(db, monkeypatch):
    _fake_llm_response(
        monkeypatch,
        response=httpx.Response(502, json={"detail": "Ollama returned an unexpected response."}),
    )

    result = await learn_memory_from_chat(db, "user1", "I hate black.")

    assert result is False
    assert await db.memory.find_one({"user_id": "user1"}) is None


@pytest.mark.asyncio
async def test_changed_mind_about_a_color_updates_memory_end_to_end(db, monkeypatch):
    """Preference reversal, end-to-end through the real
    pipeline: the canonical example ('I really hate black clothing
    and prefer colorful clothing from now on') establishes a dislike, then
    a later durable statement reversing it ('I've changed my style, I like
    black clothes now') must actually update the stored memory - the AI
    must not keep silently assuming the user still hates black forever."""
    _fake_llm_response(
        monkeypatch,
        response=_llm_response_for(_extraction_payload(disliked_colors=["black"])),
    )
    first = await learn_memory_from_chat(db, "user1", "I really hate black clothing.")
    assert first is True
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["disliked_colors"] == ["black"]
    assert doc["liked_colors"] == []

    _fake_llm_response(
        monkeypatch,
        response=_llm_response_for(_extraction_payload(liked_colors=["black", "green"])),
    )
    second = await learn_memory_from_chat(db, "user1", "I've changed my style, I like black clothes now.")

    assert second is True
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["liked_colors"] == ["black", "green"]
    assert doc["disliked_colors"] == []


@pytest.mark.asyncio
async def test_temporary_situational_statement_does_not_overwrite_a_durable_dislike(db, monkeypatch):
    """A one-off situational statement ('Today I want to wear black') must
    not silently reverse an existing durable dislike - the extraction
    prompt already instructs the model to return should_save=false for
    exactly this kind of message (see memory_extraction.py's temporary
    examples); this proves the pipeline honors that and leaves the
    existing durable value untouched rather than treating every mention of
    a color as an implicit preference change."""
    await db.memory.insert_one({"user_id": "user1", "disliked_colors": ["black"]})
    payload = _extraction_payload(liked_colors=["black"])
    payload["should_save"] = False
    payload["confidence"] = 0.1
    _fake_llm_response(monkeypatch, response=_llm_response_for(payload))

    result = await learn_memory_from_chat(db, "user1", "Today I feel like wearing black.")

    assert result is False
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["disliked_colors"] == ["black"]
    assert doc.get("liked_colors", []) == []


@pytest.mark.asyncio
async def test_preferred_fit_can_update(db, monkeypatch):
    await db.memory.insert_one({"user_id": "user1", "preferred_fit": "regular"})
    _fake_llm_response(
        monkeypatch, response=_llm_response_for(_extraction_payload(preferred_fit="oversized"))
    )

    result = await learn_memory_from_chat(db, "user1", "I always wear oversized hoodies.")

    assert result is True
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["preferred_fit"] == "oversized"


@pytest.mark.asyncio
async def test_communication_style_can_update(db, monkeypatch):
    await db.memory.insert_one({"user_id": "user1", "communication_style": "formal"})
    _fake_llm_response(
        monkeypatch, response=_llm_response_for(_extraction_payload(communication_style="concise"))
    )

    result = await learn_memory_from_chat(db, "user1", "I prefer concise answers.")

    assert result is True
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["communication_style"] == "concise"


@pytest.mark.asyncio
async def test_existing_notes_remain_untouched(db, monkeypatch):
    await db.memory.insert_one({"user_id": "user1", "notes": "prefers neutral tones"})
    _fake_llm_response(monkeypatch, response=_llm_response_for(_extraction_payload(liked_colors=["blue"])))

    result = await learn_memory_from_chat(db, "user1", "I like blue.")

    assert result is True
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["notes"] == "prefers neutral tones"


@pytest.mark.asyncio
async def test_first_memory_creation_uses_supplied_user_id(db, monkeypatch):
    _fake_llm_response(monkeypatch, response=_llm_response_for(_extraction_payload(liked_colors=["blue"])))

    result = await learn_memory_from_chat(db, "brand-new-user", "I like blue.")

    assert result is True
    doc = await db.memory.find_one({"user_id": "brand-new-user"})
    assert doc is not None
    assert doc["user_id"] == "brand-new-user"


@pytest.mark.asyncio
async def test_one_users_learning_never_updates_another_users_memory(db, monkeypatch):
    await db.memory.insert_one({"user_id": "other-user", "liked_colors": ["red"]})
    _fake_llm_response(monkeypatch, response=_llm_response_for(_extraction_payload(liked_colors=["blue"])))

    result = await learn_memory_from_chat(db, "user1", "I like blue.")

    assert result is True
    other_doc = await db.memory.find_one({"user_id": "other-user"})
    assert other_doc["liked_colors"] == ["red"]
    mine = await db.memory.find_one({"user_id": "user1"})
    assert mine["liked_colors"] == ["blue"]


@pytest.mark.asyncio
async def test_no_database_write_when_merge_produces_no_changes(db, monkeypatch):
    """A pre-existing document's updated_at only changes if a $set actually
    ran - checking it stays frozen proves no write was issued at all, not
    just that no new document was created."""
    frozen_updated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await db.memory.insert_one(
        {"user_id": "user1", "liked_colors": ["red"], "updated_at": frozen_updated_at}
    )
    _fake_llm_response(monkeypatch, response=_llm_response_for(_extraction_payload()))

    result = await learn_memory_from_chat(db, "user1", "Hi there.")

    assert result is False
    doc = await db.memory.find_one({"user_id": "user1"})
    # mongomock round-trips datetimes as naive (UTC) - compare on equal footing.
    assert doc["updated_at"].replace(tzinfo=timezone.utc) == frozen_updated_at
    assert doc["liked_colors"] == ["red"]


@pytest.mark.asyncio
async def test_returns_true_only_when_a_write_actually_occurred(db, monkeypatch):
    # No-op path: empty candidate -> no write, returns False, no document created.
    _fake_llm_response(monkeypatch, response=_llm_response_for(_extraction_payload()))
    result = await learn_memory_from_chat(db, "user1", "Hi there.")
    assert result is False
    assert await db.memory.count_documents({"user_id": "user1"}) == 0

    # Real update path: durable preference -> a write happens, returns True.
    _fake_llm_response(monkeypatch, response=_llm_response_for(_extraction_payload(liked_colors=["blue"])))
    result = await learn_memory_from_chat(db, "user1", "I like blue.")
    assert result is True
    assert await db.memory.count_documents({"user_id": "user1"}) == 1
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["liked_colors"] == ["blue"]


# --- Long-term memory follow-up completion: generic durable preferences
# end-to-end through the real extraction/merge/persist pipeline -----------


@pytest.mark.asyncio
async def test_generic_durable_preference_is_extracted_and_stored(db, monkeypatch):
    """The corrected-requirement scenario, end-to-end: 'From now on I
    prefer modest, more covered clothing' can be remembered without a
    dedicated coverage field."""
    _fake_llm_response(
        monkeypatch,
        response=_llm_response_for(_extraction_payload(
            additional_preferences=[
                {"topic": "clothing coverage", "preference": "prefers more covered, modest clothing"}
            ]
        )),
    )

    result = await learn_memory_from_chat(db, "user1", "From now on I prefer modest, more covered clothing.")

    assert result is True
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["additional_preferences"] == [
        {"topic": "clothing coverage", "preference": "prefers more covered, modest clothing"}
    ]


@pytest.mark.asyncio
async def test_a_second_unrelated_generic_topic_also_extracts_correctly(db, monkeypatch):
    """Proves genericity beyond coverage - any short topic/preference
    pair the model proposes is handled the same way."""
    _fake_llm_response(
        monkeypatch,
        response=_llm_response_for(_extraction_payload(
            additional_preferences=[
                {"topic": "ease of dressing", "preference": "prefers clothes that are easy to put on with one hand"}
            ]
        )),
    )

    result = await learn_memory_from_chat(
        db, "user1", "I prefer clothes that are easy to put on with one hand."
    )

    assert result is True
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["additional_preferences"] == [
        {"topic": "ease of dressing", "preference": "prefers clothes that are easy to put on with one hand"}
    ]


@pytest.mark.asyncio
async def test_mixed_message_saves_structured_and_generic_fields_together(db, monkeypatch):
    """Known preferences still go into their existing structured fields
    even when the same message also contains a generic one."""
    _fake_llm_response(
        monkeypatch,
        response=_llm_response_for(_extraction_payload(
            disliked_colors=["black"],
            additional_preferences=[
                {"topic": "clothing coverage", "preference": "prefers more covered, modest clothing"}
            ],
        )),
    )

    result = await learn_memory_from_chat(
        db, "user1", "I hate black clothing and prefer modest, covered outfits."
    )

    assert result is True
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["disliked_colors"] == ["black"]
    assert doc["additional_preferences"] == [
        {"topic": "clothing coverage", "preference": "prefers more covered, modest clothing"}
    ]


@pytest.mark.asyncio
async def test_temporary_generic_style_statement_is_not_persisted(db, monkeypatch):
    """'For tonight I want something covered' is situational - the
    extraction prompt's own temporary examples cover exactly this
    wording; this proves the pipeline honors a should_save=false verdict
    for it rather than persisting a fabricated generic preference."""
    payload = _extraction_payload(
        additional_preferences=[{"topic": "clothing coverage", "preference": "wants something covered"}]
    )
    payload["should_save"] = False
    payload["candidate"] = {}  # a well-behaved model also returns an empty candidate
    _fake_llm_response(monkeypatch, response=_llm_response_for(payload))

    result = await learn_memory_from_chat(db, "user1", "For tonight I want something covered.")

    assert result is False
    assert await db.memory.find_one({"user_id": "user1"}) is None


@pytest.mark.asyncio
async def test_changed_mind_about_a_generic_topic_updates_memory_end_to_end(db, monkeypatch):
    """The corrected-requirement's change-of-mind scenario, end-to-end,
    for a generic (non-structured) topic."""
    _fake_llm_response(
        monkeypatch,
        response=_llm_response_for(_extraction_payload(
            additional_preferences=[
                {"topic": "clothing coverage", "preference": "prefers more covered, modest clothing"}
            ]
        )),
    )
    first = await learn_memory_from_chat(db, "user1", "From now on I prefer modest, more covered clothing.")
    assert first is True

    _fake_llm_response(
        monkeypatch,
        response=_llm_response_for(_extraction_payload(
            additional_preferences=[
                {"topic": "clothing coverage", "preference": "prefers more open clothing now"}
            ]
        )),
    )
    second = await learn_memory_from_chat(db, "user1", "My style changed, I prefer more open clothing now.")

    assert second is True
    doc = await db.memory.find_one({"user_id": "user1"})
    assert doc["additional_preferences"] == [
        {"topic": "clothing coverage", "preference": "prefers more open clothing now"}
    ]


@pytest.mark.asyncio
async def test_one_users_generic_preference_learning_never_updates_another_users_memory(db, monkeypatch):
    await db.memory.insert_one({
        "user_id": "other-user",
        "additional_preferences": [{"topic": "fabric", "preference": "prefers cotton"}],
    })
    _fake_llm_response(
        monkeypatch,
        response=_llm_response_for(_extraction_payload(
            additional_preferences=[{"topic": "fabric", "preference": "prefers wool"}]
        )),
    )

    result = await learn_memory_from_chat(db, "user1", "I prefer wool fabrics.")

    assert result is True
    other_doc = await db.memory.find_one({"user_id": "other-user"})
    assert other_doc["additional_preferences"] == [{"topic": "fabric", "preference": "prefers cotton"}]
    mine = await db.memory.find_one({"user_id": "user1"})
    assert mine["additional_preferences"] == [{"topic": "fabric", "preference": "prefers wool"}]
