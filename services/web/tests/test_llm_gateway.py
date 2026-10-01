import httpx
import pytest

from app.services.llm_gateway import (
    ChatGatewayFailure,
    ChatGatewayFailureReason,
    VisionGenerateResult,
    call_llm,
    call_llm_vision,
)


def _patch_post(monkeypatch, response: httpx.Response = None, exc=None):
    async def fake_post(self, url, json=None, **kwargs):
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _patch_post_capturing(monkeypatch, response: httpx.Response):
    calls = []

    async def fake_post(self, url, json=None, **kwargs):
        calls.append(json)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


@pytest.mark.asyncio
async def test_call_llm_success_returns_response_text(monkeypatch):
    _patch_post(
        monkeypatch,
        response=httpx.Response(200, json={"response": "Try a red top.", "model": "qwen2.5:3b"}),
    )

    result = await call_llm("What should I wear?")

    assert result == "Try a red top."


@pytest.mark.asyncio
async def test_call_llm_unreachable_maps_to_llm_unavailable(monkeypatch):
    _patch_post(monkeypatch, exc=httpx.ConnectError("connection refused"))

    result = await call_llm("Hi")

    assert isinstance(result, ChatGatewayFailure)
    assert result.reason == ChatGatewayFailureReason.LLM_UNAVAILABLE
    assert result.status_code == 503


@pytest.mark.asyncio
async def test_call_llm_passthrough_503_maps_to_llm_unavailable(monkeypatch):
    _patch_post(
        monkeypatch,
        response=httpx.Response(503, json={"detail": "Ollama service is unavailable."}),
    )

    result = await call_llm("Hi")

    assert result.reason == ChatGatewayFailureReason.LLM_UNAVAILABLE
    assert result.status_code == 503
    assert result.detail == "Ollama service is unavailable."


@pytest.mark.asyncio
async def test_call_llm_passthrough_504_maps_to_llm_timeout(monkeypatch):
    _patch_post(
        monkeypatch,
        response=httpx.Response(504, json={"detail": "Ollama did not respond in time."}),
    )

    result = await call_llm("Hi")

    assert result.reason == ChatGatewayFailureReason.LLM_TIMEOUT
    assert result.status_code == 504
    assert result.detail == "Ollama did not respond in time."


@pytest.mark.asyncio
async def test_call_llm_passthrough_502_maps_to_llm_invalid_response(monkeypatch):
    _patch_post(
        monkeypatch,
        response=httpx.Response(502, json={"detail": "Ollama returned an unexpected response."}),
    )

    result = await call_llm("Hi")

    assert result.reason == ChatGatewayFailureReason.LLM_INVALID_RESPONSE
    assert result.status_code == 502


@pytest.mark.asyncio
async def test_call_llm_unexpected_status_maps_to_llm_invalid_response(monkeypatch):
    _patch_post(monkeypatch, response=httpx.Response(500, text="internal error"))

    result = await call_llm("Hi")

    assert result.reason == ChatGatewayFailureReason.LLM_INVALID_RESPONSE
    assert result.status_code == 502


@pytest.mark.asyncio
async def test_call_llm_malformed_json_maps_to_llm_invalid_response(monkeypatch):
    _patch_post(monkeypatch, response=httpx.Response(200, text="not json"))

    result = await call_llm("Hi")

    assert result.reason == ChatGatewayFailureReason.LLM_INVALID_RESPONSE
    assert result.status_code == 502


@pytest.mark.asyncio
async def test_call_llm_missing_response_field_maps_to_llm_invalid_response(monkeypatch):
    _patch_post(monkeypatch, response=httpx.Response(200, json={"model": "qwen2.5:3b"}))

    result = await call_llm("Hi")

    assert result.reason == ChatGatewayFailureReason.LLM_INVALID_RESPONSE
    assert result.status_code == 502


# --- call_llm_vision (Analyze Item) ---


@pytest.mark.asyncio
async def test_call_llm_vision_success_returns_response_and_model(monkeypatch):
    _patch_post(
        monkeypatch,
        response=httpx.Response(
            200, json={"response": '{"color": "blue"}', "model": "moondream:latest"}
        ),
    )

    result = await call_llm_vision("Describe this item.", b"fake-image-bytes", {"type": "object"})

    assert isinstance(result, VisionGenerateResult)
    assert result.response == '{"color": "blue"}'
    assert result.model == "moondream:latest"


@pytest.mark.asyncio
async def test_call_llm_vision_sends_base64_images_and_format(monkeypatch):
    import base64

    calls = _patch_post_capturing(
        monkeypatch,
        httpx.Response(200, json={"response": "{}", "model": "moondream:latest"}),
    )
    schema = {"type": "object", "properties": {"color": {"type": "string"}}}

    await call_llm_vision("Describe this item.", b"fake-image-bytes", schema)

    assert calls[0]["images"] == [base64.b64encode(b"fake-image-bytes").decode("ascii")]
    assert calls[0]["format"] == schema
    assert calls[0]["prompt"] == "Describe this item."


@pytest.mark.asyncio
async def test_call_llm_vision_unreachable_maps_to_llm_unavailable(monkeypatch):
    _patch_post(monkeypatch, exc=httpx.ConnectError("connection refused"))

    result = await call_llm_vision("Describe this item.", b"bytes", {})

    assert isinstance(result, ChatGatewayFailure)
    assert result.reason == ChatGatewayFailureReason.LLM_UNAVAILABLE
    assert result.status_code == 503


@pytest.mark.asyncio
async def test_call_llm_vision_passthrough_504_maps_to_llm_timeout(monkeypatch):
    _patch_post(
        monkeypatch,
        response=httpx.Response(504, json={"detail": "Ollama did not respond in time."}),
    )

    result = await call_llm_vision("Describe this item.", b"bytes", {})

    assert result.reason == ChatGatewayFailureReason.LLM_TIMEOUT
    assert result.status_code == 504


@pytest.mark.asyncio
async def test_call_llm_vision_missing_model_field_maps_to_llm_invalid_response(monkeypatch):
    """Unlike call_llm, the vision gateway also requires a `model` field in
    the llm service's response - it's needed to report which model produced
    the suggestion, so a response missing it is treated as malformed."""
    _patch_post(
        monkeypatch,
        response=httpx.Response(200, json={"response": "{}"}),
    )

    result = await call_llm_vision("Describe this item.", b"bytes", {})

    assert result.reason == ChatGatewayFailureReason.LLM_INVALID_RESPONSE
    assert result.status_code == 502
