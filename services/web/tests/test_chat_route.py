import httpx
import pytest

from app.services.chat_context import PLAIN_TEXT_STYLE_INSTRUCTION


def _fake_llm_response(monkeypatch, captured_payloads: list, response: httpx.Response = None, exc=None):
    """Patch the outbound call to the llm microservice so tests don't need
    it (or Ollama) running, and record the payload the web service builds."""

    async def fake_post(self, url, json=None, **kwargs):
        captured_payloads.append(json)
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def test_chat_requires_auth(client):
    resp = client.post("/chat", json={"message": "Hi"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_chat_success_returns_llm_response(client, make_user, monkeypatch):
    user = await make_user("alice")

    captured = []
    _fake_llm_response(
        monkeypatch, captured,
        response=httpx.Response(200, json={"response": "Try a red top.", "model": "qwen2.5:3b"}),
    )

    resp = client.post("/chat", json={"message": "What should I wear?"}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json() == {"response": "Try a red top."}
    assert captured[0] == {
        "prompt": f"{PLAIN_TEXT_STYLE_INSTRUCTION}\n\nWhat should I wear?"
    }


@pytest.mark.asyncio
async def test_chat_empty_message_returns_422(client, make_user):
    user = await make_user("bob")
    resp = client.post("/chat", json={"message": ""}, headers=user["headers"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_whitespace_message_returns_422(client, make_user):
    user = await make_user("carol")
    resp = client.post("/chat", json={"message": "   "}, headers=user["headers"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_llm_service_unreachable_returns_503(client, make_user, monkeypatch):
    user = await make_user("dave")
    _fake_llm_response(monkeypatch, [], exc=httpx.ConnectError("connection refused"))

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_chat_llm_reports_unavailable_passes_through_503(client, make_user, monkeypatch):
    user = await make_user("erin")
    _fake_llm_response(
        monkeypatch, [],
        response=httpx.Response(503, json={"detail": "Ollama service is unavailable."}),
    )

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_chat_llm_timeout_passes_through_504(client, make_user, monkeypatch):
    user = await make_user("frank")
    _fake_llm_response(
        monkeypatch, [],
        response=httpx.Response(504, json={"detail": "Ollama did not respond in time."}),
    )

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])
    assert resp.status_code == 504


@pytest.mark.asyncio
async def test_chat_llm_invalid_response_passes_through_502(client, make_user, monkeypatch):
    user = await make_user("grace")
    _fake_llm_response(
        monkeypatch, [],
        response=httpx.Response(502, json={"detail": "Ollama returned an unexpected response."}),
    )

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_chat_llm_unexpected_status_returns_502(client, make_user, monkeypatch):
    user = await make_user("heidi")
    _fake_llm_response(monkeypatch, [], response=httpx.Response(500, text="internal error"))

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_chat_llm_malformed_json_returns_502(client, make_user, monkeypatch):
    user = await make_user("ivan")
    _fake_llm_response(monkeypatch, [], response=httpx.Response(200, text="not json"))

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_chat_llm_missing_response_field_returns_502(client, make_user, monkeypatch):
    user = await make_user("judy")
    _fake_llm_response(monkeypatch, [], response=httpx.Response(200, json={"model": "qwen2.5:3b"}))

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_chat_includes_sanitized_memory_context_when_present(client, make_user, monkeypatch):
    user = await make_user("kim")
    put_resp = client.put(
        "/memory",
        json={"liked_colors": ["blue"], "preferred_fit": "relaxed"},
        headers=user["headers"],
    )
    assert put_resp.status_code == 200

    captured = []
    _fake_llm_response(
        monkeypatch, captured,
        response=httpx.Response(200, json={"response": "Try blue.", "model": "qwen2.5:3b"}),
    )

    resp = client.post("/chat", json={"message": "What should I wear?"}, headers=user["headers"])

    assert resp.status_code == 200
    prompt = captured[0]["prompt"]
    assert "[WARDROBE_PREFERENCE_CONTEXT]" in prompt
    assert "Liked colors: blue" in prompt
    assert "Preferred fit: relaxed" in prompt
    assert "[USER_MESSAGE]\nWhat should I wear?\n[/USER_MESSAGE]" in prompt


@pytest.mark.asyncio
async def test_chat_raw_passthrough_when_memory_all_empty(client, make_user, monkeypatch):
    """"Raw passthrough" here means no [WARDROBE_PREFERENCE_CONTEXT] block is
    added when memory is empty - PLAIN_TEXT_STYLE_INSTRUCTION still always
    leads every conversational prompt (see chat_context.py)."""
    user = await make_user("liam")
    put_resp = client.put("/memory", json={}, headers=user["headers"])
    assert put_resp.status_code == 200

    captured = []
    _fake_llm_response(
        monkeypatch, captured,
        response=httpx.Response(200, json={"response": "Sure.", "model": "qwen2.5:3b"}),
    )

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])

    assert resp.status_code == 200
    assert captured[0] == {"prompt": f"{PLAIN_TEXT_STYLE_INSTRUCTION}\n\nHi"}


@pytest.mark.asyncio
async def test_chat_response_has_markdown_bold_and_headings_stripped(client, make_user, monkeypatch):
    """The conservative normalization fallback
    (chat_text_formatting.py) is applied to the synchronous route's reply."""
    user = await make_user("mallory")
    _fake_llm_response(
        monkeypatch, [],
        response=httpx.Response(
            200,
            json={
                "response": "# Style Recommendations\n**Try a red top** with __dark jeans__.",
                "model": "qwen2.5:3b",
            },
        ),
    )

    resp = client.post("/chat", json={"message": "What should I wear?"}, headers=user["headers"])

    assert resp.status_code == 200
    assert resp.json() == {
        "response": "Style Recommendations\nTry a red top with dark jeans."
    }
