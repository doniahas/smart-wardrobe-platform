import httpx
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _patch_ollama_post_capturing(monkeypatch, response):
    """Patch the outbound call to Ollama and record the JSON body sent,
    so tests can assert on model selection / images / format / keep_alive
    without a real Ollama instance."""
    calls = []

    async def fake_post(self, url, **kwargs):
        calls.append(kwargs.get("json"))
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


def test_generate_without_images_uses_text_model(monkeypatch):
    calls = _patch_ollama_post_capturing(
        monkeypatch, httpx.Response(200, json={"response": "an outfit idea"})
    )
    response = client.post("/generate", json={"prompt": "Suggest an outfit."})
    assert response.status_code == 200
    assert response.json()["model"] == "qwen2.5:3b"
    assert calls[0]["model"] == "qwen2.5:3b"
    assert "images" not in calls[0]


def test_generate_with_images_uses_vision_model(monkeypatch):
    calls = _patch_ollama_post_capturing(
        monkeypatch, httpx.Response(200, json={"response": '{"color": "blue"}'})
    )
    response = client.post(
        "/generate",
        json={"prompt": "Describe this item.", "images": ["base64bytes"]},
    )
    assert response.status_code == 200
    assert response.json()["model"] == "moondream:latest"
    assert calls[0]["model"] == "moondream:latest"


def test_generate_passes_images_field_to_ollama(monkeypatch):
    calls = _patch_ollama_post_capturing(
        monkeypatch, httpx.Response(200, json={"response": "ok"})
    )
    client.post(
        "/generate",
        json={"prompt": "Describe this item.", "images": ["abc123"]},
    )
    assert calls[0]["images"] == ["abc123"]


def test_generate_passes_format_schema_to_ollama(monkeypatch):
    schema = {"type": "object", "properties": {"color": {"type": "string"}}}
    calls = _patch_ollama_post_capturing(
        monkeypatch, httpx.Response(200, json={"response": '{"color": "blue"}'})
    )
    client.post(
        "/generate",
        json={"prompt": "Describe this item.", "images": ["abc123"], "format": schema},
    )
    assert calls[0]["format"] == schema


def test_generate_omits_format_when_not_provided(monkeypatch):
    calls = _patch_ollama_post_capturing(
        monkeypatch, httpx.Response(200, json={"response": "ok"})
    )
    client.post("/generate", json={"prompt": "Suggest an outfit."})
    assert "format" not in calls[0]


def test_generate_includes_keep_alive(monkeypatch):
    calls = _patch_ollama_post_capturing(
        monkeypatch, httpx.Response(200, json={"response": "ok"})
    )
    client.post("/generate", json={"prompt": "Suggest an outfit."})
    assert calls[0]["keep_alive"] == "30m"


def test_generate_with_images_ollama_unavailable_returns_503(monkeypatch):
    async def fake_post(self, url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    response = client.post(
        "/generate",
        json={"prompt": "Describe this item.", "images": ["abc123"]},
    )
    assert response.status_code == 503
