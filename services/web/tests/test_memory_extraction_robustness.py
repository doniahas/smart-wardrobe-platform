"""Long-term-memory extraction reliability fix.

Regression coverage for a defect reproduced against the real local
qwen2.5:3b model: a durable preference
stated in chat ("I love wearing pink", "I hate black clothes") was
frequently never persisted at all, so a later question about the user's
colours got a generic answer.

These tests pin the *deterministic* parts of the fix - the wire schema, the
colour mapping, field-level salvage, and the time-anchor veto. They
deliberately do NOT assert anything about what the model itself decides;
real-model behaviour is measured separately, against the live model.

Every payload below is a real shape observed in a recorded qwen2.5:3b run,
not an invented one.
"""
import json

import httpx
import pytest

from app.models import ChatMemoryExtractionResult
from app.services.chat_memory_learning import learn_memory_from_chat
from app.services.memory_extraction import (
    MEMORY_EXTRACTION_SCHEMA,
    build_memory_extraction_prompt,
    message_has_time_anchor,
    parse_memory_extraction_response,
)


# --- The structured-output schema itself ---


def test_schema_enum_values_are_derived_from_the_real_enums():
    """The schema must never drift from the model it has to validate
    against - the whole point of deriving it from app.models."""
    from app.models import ClothingType, CommunicationStyle, FitPreference, Occasion, Style

    props = MEMORY_EXTRACTION_SCHEMA["properties"]["candidate"]["properties"]
    assert props["preferred_styles"]["items"]["enum"] == [e.value for e in Style]
    assert props["disliked_item_types"]["items"]["enum"] == [e.value for e in ClothingType]
    assert props["preferred_occasions"]["items"]["enum"] == [e.value for e in Occasion]
    assert props["preferred_fit"]["enum"] == [e.value for e in FitPreference] + [None]
    assert props["communication_style"]["enum"] == [e.value for e in CommunicationStyle] + [None]


def test_schema_generates_the_candidate_before_the_durability_judgement():
    """Field order is load-bearing: the model must write down what the
    message states BEFORE deciding whether it is durable. With the
    judgement first, the real model emitted should_save=false while
    filling the candidate correctly."""
    required = MEMORY_EXTRACTION_SCHEMA["required"]
    assert required.index("candidate") < required.index("is_lasting_preference")
    assert required.index("reasoning") < required.index("is_lasting_preference")
    order = list(MEMORY_EXTRACTION_SCHEMA["properties"])
    assert order.index("candidate") < order.index("is_lasting_preference")


def test_schema_forbids_unknown_top_level_and_candidate_keys():
    assert MEMORY_EXTRACTION_SCHEMA["additionalProperties"] is False
    assert MEMORY_EXTRACTION_SCHEMA["properties"]["candidate"]["additionalProperties"] is False


def test_schema_requires_only_colors_inside_candidate():
    """Requiring EVERY field made the real model pad preferred_styles with
    a value the user never stated; requiring NONE let it skip `colors`
    entirely for a dislike and file it under additional_preferences, so the
    colour never became a recommendation exclusion. Exactly one required
    field is the balance point."""
    assert MEMORY_EXTRACTION_SCHEMA["properties"]["candidate"]["required"] == ["colors"]


def test_prompt_lists_the_allowed_enum_values():
    prompt = build_memory_extraction_prompt("Hi")
    for value in ("streetwear", "oversized", "concise", "university", "outerwear"):
        assert value in prompt


def test_prompt_teaches_the_durable_versus_temporary_contrast():
    prompt = build_memory_extraction_prompt("Hi")
    assert '"I love wearing pink."' in prompt
    assert '"My favorite clothing color is pink."' in prompt
    assert '"I am wearing pink today."' in prompt
    assert '"I want to wear purple tonight."' in prompt
    # A dislike must be presented as just as durable as a like.
    assert '"I hate black clothes."' in prompt
    assert '"I do not like wearing red."' in prompt


# --- Wire `colors` list -> liked_colors/disliked_colors ---


def _wire(candidate: dict, lasting: bool = True, confidence: float = 0.95) -> str:
    return json.dumps({
        "candidate": candidate,
        "reasoning": "The user stated a lasting preference.",
        "is_lasting_preference": lasting,
        "confidence": confidence,
    })


def test_colors_list_maps_onto_liked_and_disliked():
    raw = _wire({"colors": [{"color": "pink", "sentiment": "like"},
                            {"color": "black", "sentiment": "dislike"}]})
    result = parse_memory_extraction_response(raw)
    assert result.candidate.liked_colors == ["pink"]
    assert result.candidate.disliked_colors == ["black"]


def test_dislike_only_message_still_produces_a_usable_candidate():
    """The canonical example: "the client tells the AI it really
    hates black clothing ... The AI should remember to offer no black
    clothes from that point forward"."""
    result = parse_memory_extraction_response(
        _wire({"colors": [{"color": "black", "sentiment": "dislike"}]})
    )
    assert result.should_save is True
    assert result.candidate.disliked_colors == ["black"]
    assert result.candidate.liked_colors == []


def test_colors_are_normalized_to_lowercase():
    result = parse_memory_extraction_response(
        _wire({"colors": [{"color": "  PINK  ", "sentiment": "like"}]})
    )
    assert result.candidate.liked_colors == ["pink"]


def test_a_colour_claimed_with_both_sentiments_is_dropped_not_guessed():
    """The real model sometimes emitted black as both liked and disliked.
    There is no honest way to know which was meant, so it is dropped from
    both lists - but the rest of the extraction survives."""
    result = parse_memory_extraction_response(
        _wire({"colors": [{"color": "black", "sentiment": "like"},
                          {"color": "black", "sentiment": "dislike"},
                          {"color": "green", "sentiment": "like"}]})
    )
    assert "black" not in result.candidate.liked_colors
    assert "black" not in result.candidate.disliked_colors
    assert result.candidate.liked_colors == ["green"]


def test_literal_null_string_is_not_stored_as_a_colour():
    """Observed verbatim in a real run: colors contained the string
    "null" instead of the entry simply being omitted."""
    result = parse_memory_extraction_response(
        _wire({"colors": [{"color": "null", "sentiment": "like"},
                          {"color": "black", "sentiment": "dislike"}]})
    )
    assert result.candidate.liked_colors == []
    assert result.candidate.disliked_colors == ["black"]


def test_malformed_colour_entries_are_skipped_without_losing_the_good_ones():
    result = parse_memory_extraction_response(
        _wire({"colors": ["pink", {"color": "red"}, {"sentiment": "like"},
                          {"color": "blue", "sentiment": "maybe"},
                          {"color": "green", "sentiment": "like"}]})
    )
    assert result.candidate.liked_colors == ["green"]


def test_legacy_two_list_wire_shape_still_parses_identically():
    """The plain ChatMemoryExtractionResult shape must keep working - it is
    what every pre-existing test and the recommendation-feedback path use."""
    raw = json.dumps({
        "should_save": True, "confidence": 0.95,
        "candidate": {"liked_colors": ["blue"], "disliked_colors": ["black"]},
    })
    result = parse_memory_extraction_response(raw)
    assert result.candidate.liked_colors == ["blue"]
    assert result.candidate.disliked_colors == ["black"]


def test_reasoning_is_never_returned_as_memory_content():
    result = parse_memory_extraction_response(_wire({"colors": []}, lasting=False, confidence=0.0))
    assert not hasattr(result, "reasoning")
    assert result.should_save is False


# --- Field-level salvage (opt-in) ---


def test_invalid_optional_enum_no_longer_discards_a_valid_preference():
    """The core field-level robustness case. The real model emitted
    preferred_fit="" and preferred_styles=["black"] alongside a correct
    disliked_colors=["black"]; strict validation threw away all of it."""
    raw = _wire({
        "colors": [{"color": "black", "sentiment": "dislike"}],
        "preferred_styles": ["black"],
        "preferred_fit": "",
        "communication_style": "",
    })
    assert parse_memory_extraction_response(raw) is None  # unchanged strict default
    result = parse_memory_extraction_response(raw, salvage_invalid_fields=True)
    assert result is not None
    assert result.candidate.disliked_colors == ["black"]
    assert result.candidate.preferred_styles == []
    assert result.candidate.preferred_fit is None
    assert result.candidate.communication_style is None


def test_salvage_keeps_valid_enum_members_and_drops_only_the_invalid_ones():
    result = parse_memory_extraction_response(
        _wire({"preferred_styles": ["casual", "avoid_black", "elegant"]}),
        salvage_invalid_fields=True,
    )
    assert [s.value for s in result.candidate.preferred_styles] == ["casual", "elegant"]


def test_salvage_never_coerces_an_invalid_value_into_a_valid_enum_member():
    """Dropped, never guessed - mirrors wardrobe_ai.py::_valid_enum_or_none."""
    result = parse_memory_extraction_response(
        _wire({"preferred_fit": "super-baggy"}), salvage_invalid_fields=True
    )
    assert result.candidate.preferred_fit is None


def test_salvage_still_rejects_an_unknown_candidate_field():
    """Security boundary, not a tidiness rule: the LLM must never be able
    to smuggle in a field the candidate model does not define."""
    raw = _wire({"colors": [{"color": "pink", "sentiment": "like"}], "notes": "sneaky"})
    assert parse_memory_extraction_response(raw, salvage_invalid_fields=True) is None


def test_salvage_still_rejects_an_unknown_top_level_field():
    payload = json.loads(_wire({"colors": []}))
    payload["user_id"] = "some-other-user"
    assert parse_memory_extraction_response(json.dumps(payload), salvage_invalid_fields=True) is None


def test_salvage_still_rejects_an_out_of_range_confidence():
    raw = _wire({"colors": []}, confidence=1.5)
    assert parse_memory_extraction_response(raw, salvage_invalid_fields=True) is None


def test_salvage_drops_only_the_malformed_generic_preference_entry():
    result = parse_memory_extraction_response(
        _wire({"additional_preferences": [
            {"topic": "fabric", "preference": "prefers cotton"},
            {"topic": "coverage", "preference": "modest", "confidence": 0.9},
            "not-an-object",
            {"topic": "fit"},
        ]}),
        salvage_invalid_fields=True,
    )
    assert [p.topic for p in result.candidate.additional_preferences] == ["fabric"]


def test_salvage_resolves_a_liked_disliked_overlap_in_the_legacy_shape():
    raw = json.dumps({
        "should_save": True, "confidence": 0.95,
        "candidate": {"liked_colors": ["black", "green"], "disliked_colors": ["black"]},
    })
    assert parse_memory_extraction_response(raw) is None  # _check_color_overlap
    result = parse_memory_extraction_response(raw, salvage_invalid_fields=True)
    assert result.candidate.liked_colors == ["green"]
    assert result.candidate.disliked_colors == []


# --- Deterministic time-anchor veto ---


@pytest.mark.parametrize("message", [
    "I am wearing black today",
    "I want to wear purple tonight",
    "Show me something red for the party on Saturday",
    "I need a jacket TODAY",
    "I am going to a wedding tomorrow",
    "I don't want black tonight",
])
def test_time_anchored_messages_are_detected(message):
    assert message_has_time_anchor(message) is True


@pytest.mark.parametrize("message", [
    "I love wearing pink",
    "My favorite clothing color is pink",
    "I hate black clothes",
    "I really hate black clothing and prefer colorful clothes",
    "I never wear yellow",
    "I adore green sweaters",
])
def test_durable_messages_carry_no_time_anchor(message):
    assert message_has_time_anchor(message) is False


def test_time_anchor_matching_is_word_bounded():
    """Must not fire on a longer word that merely contains an anchor."""
    assert message_has_time_anchor("I like todays-special-edition jackets") is False
    assert message_has_time_anchor("I prefer nowhere-to-hide designs") is False


def test_time_anchor_check_tolerates_a_non_string():
    assert message_has_time_anchor(None) is False


# --- End-to-end through learn_memory_from_chat ---


def _mock_extraction(monkeypatch, payload_json: str):
    async def fake_post(self, url, **kwargs):
        return httpx.Response(200, json={"response": payload_json, "model": "qwen2.5:3b"})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


@pytest.mark.asyncio
async def test_durable_dislike_reaches_memory(db, monkeypatch):
    _mock_extraction(monkeypatch, _wire({"colors": [{"color": "black", "sentiment": "dislike"}]}))
    assert await learn_memory_from_chat(db, "u1", "I hate black clothes") is True
    doc = await db.memory.find_one({"user_id": "u1"})
    assert doc["disliked_colors"] == ["black"]


@pytest.mark.asyncio
async def test_durable_like_reaches_memory(db, monkeypatch):
    _mock_extraction(monkeypatch, _wire({"colors": [{"color": "pink", "sentiment": "like"}]}))
    assert await learn_memory_from_chat(db, "u1", "I love wearing pink") is True
    doc = await db.memory.find_one({"user_id": "u1"})
    assert doc["liked_colors"] == ["pink"]


@pytest.mark.asyncio
async def test_time_anchored_message_is_never_persisted_even_if_the_model_says_durable(
    db, monkeypatch
):
    """The veto is the safety net for exactly this real-model failure:
    "Show me something red for the party on Saturday" was judged durable
    6/6 by qwen2.5:3b."""
    _mock_extraction(monkeypatch, _wire({"colors": [{"color": "red", "sentiment": "like"}]}))
    assert await learn_memory_from_chat(
        db, "u1", "Show me something red for the party on Saturday"
    ) is False
    assert await db.memory.find_one({"user_id": "u1"}) is None


@pytest.mark.asyncio
async def test_model_judged_temporary_is_not_persisted(db, monkeypatch):
    _mock_extraction(monkeypatch, _wire(
        {"colors": [{"color": "purple", "sentiment": "like"}]}, lasting=False, confidence=0.0))
    assert await learn_memory_from_chat(db, "u1", "I want to wear purple please") is False
    assert await db.memory.find_one({"user_id": "u1"}) is None


@pytest.mark.asyncio
async def test_salvaged_extraction_still_reaches_memory(db, monkeypatch):
    """A response the OLD parser would have discarded entirely now still
    persists the valid part."""
    _mock_extraction(monkeypatch, _wire({
        "colors": [{"color": "black", "sentiment": "dislike"}],
        "preferred_styles": ["not-a-real-style"],
        "preferred_fit": "",
    }))
    assert await learn_memory_from_chat(db, "u1", "I hate black clothes") is True
    doc = await db.memory.find_one({"user_id": "u1"})
    assert doc["disliked_colors"] == ["black"]
    assert doc["preferred_styles"] == []


@pytest.mark.asyncio
async def test_extraction_call_is_schema_constrained(db, monkeypatch):
    """The structured-output constraint must actually be sent - this is
    what stops the malformed-JSON class of failure at the decoder."""
    seen = {}

    async def fake_post(self, url, **kwargs):
        seen.update(kwargs["json"])
        return httpx.Response(200, json={
            "response": _wire({"colors": [{"color": "pink", "sentiment": "like"}]}),
            "model": "qwen2.5:3b"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await learn_memory_from_chat(db, "u1", "I love wearing pink")
    assert seen["format"] == MEMORY_EXTRACTION_SCHEMA


@pytest.mark.asyncio
async def test_one_users_learning_never_reaches_another_user(db, monkeypatch):
    await db.memory.insert_one({"user_id": "other", "liked_colors": ["blue"]})
    _mock_extraction(monkeypatch, _wire({"colors": [{"color": "pink", "sentiment": "like"}]}))
    await learn_memory_from_chat(db, "u1", "I love wearing pink")
    assert (await db.memory.find_one({"user_id": "other"}))["liked_colors"] == ["blue"]
    assert (await db.memory.find_one({"user_id": "u1"}))["liked_colors"] == ["pink"]


@pytest.mark.asyncio
async def test_a_later_contradicting_statement_moves_the_colour(db, monkeypatch):
    """Contradiction/update behaviour must survive the fix."""
    _mock_extraction(monkeypatch, _wire({"colors": [{"color": "black", "sentiment": "dislike"}]}))
    await learn_memory_from_chat(db, "u1", "I hate black clothes")
    _mock_extraction(monkeypatch, _wire({"colors": [{"color": "black", "sentiment": "like"}]}))
    await learn_memory_from_chat(db, "u1", "I changed my mind, I like black now")
    doc = await db.memory.find_one({"user_id": "u1"})
    assert doc["liked_colors"] == ["black"]
    assert doc["disliked_colors"] == []


# --- Diagnostic logging ---


@pytest.mark.asyncio
async def test_rejections_are_logged_with_a_stable_reason_code(db, monkeypatch, caplog):
    import logging
    _mock_extraction(monkeypatch, _wire({"colors": []}, lasting=False, confidence=0.0))
    with caplog.at_level(logging.INFO):
        await learn_memory_from_chat(db, "u1", "It is cold outside")
    assert "model_judged_not_durable" in caplog.text


@pytest.mark.asyncio
async def test_logs_never_contain_the_chat_message_or_the_models_reasoning(
    db, monkeypatch, caplog
):
    """Privacy rule from tests/test_logging_privacy.py applies here too:
    diagnostics must be reason codes and field names, never content."""
    import logging
    sentinel_message = "SENTINEL-CHAT-BODY-9f8e7d6c-do-not-log-me"
    sentinel_reasoning = "SENTINEL-REASONING-1a2b3c-do-not-log-me"
    payload = json.dumps({
        "candidate": {"colors": [{"color": "pink", "sentiment": "like"}]},
        "reasoning": sentinel_reasoning,
        "is_lasting_preference": True,
        "confidence": 0.95,
    })
    _mock_extraction(monkeypatch, payload)
    with caplog.at_level(logging.DEBUG):
        await learn_memory_from_chat(db, "u1", sentinel_message)
    assert sentinel_message not in caplog.text
    assert sentinel_reasoning not in caplog.text
    for record in caplog.records:
        assert sentinel_message not in record.getMessage()
        assert sentinel_reasoning not in record.getMessage()


@pytest.mark.asyncio
async def test_successful_learning_logs_field_names_but_not_values(db, monkeypatch, caplog):
    import logging
    _mock_extraction(monkeypatch, _wire({"colors": [{"color": "chartreuse", "sentiment": "like"}]}))
    with caplog.at_level(logging.INFO):
        await learn_memory_from_chat(db, "u1", "I love wearing chartreuse")
    assert "learned" in caplog.text
    assert "liked_colors" in caplog.text
    assert "chartreuse" not in caplog.text
