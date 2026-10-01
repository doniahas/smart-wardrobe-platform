import httpx
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _patch_ollama_post(monkeypatch, response=None, exc=None):
    """Patch the outbound call to Ollama so tests don't need it running."""

    async def fake_post(self, url, **kwargs):
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def test_generate_success(monkeypatch):
    _patch_ollama_post(
        monkeypatch,
        response=httpx.Response(200, json={"response": "some generated text", "done": True}),
    )
    response = client.post("/generate", json={"prompt": "Suggest an outfit."})
    assert response.status_code == 200
    assert response.json() == {"response": "some generated text", "model": "qwen2.5:3b"}


def test_generate_empty_prompt_returns_422():
    response = client.post("/generate", json={"prompt": ""})
    assert response.status_code == 422


def test_generate_whitespace_only_prompt_returns_422():
    response = client.post("/generate", json={"prompt": "   "})
    assert response.status_code == 422


def test_generate_missing_prompt_field_returns_422():
    response = client.post("/generate", json={})
    assert response.status_code == 422


def test_generate_ollama_unavailable_returns_503(monkeypatch):
    _patch_ollama_post(monkeypatch, exc=httpx.ConnectError("connection refused"))
    response = client.post("/generate", json={"prompt": "Suggest an outfit."})
    assert response.status_code == 503


def test_generate_ollama_timeout_returns_504(monkeypatch):
    _patch_ollama_post(monkeypatch, exc=httpx.ReadTimeout("timed out"))
    response = client.post("/generate", json={"prompt": "Suggest an outfit."})
    assert response.status_code == 504


def test_generate_ollama_non_200_returns_502(monkeypatch):
    _patch_ollama_post(monkeypatch, response=httpx.Response(500, text="internal error"))
    response = client.post("/generate", json={"prompt": "Suggest an outfit."})
    assert response.status_code == 502


def test_generate_ollama_malformed_json_returns_502(monkeypatch):
    _patch_ollama_post(monkeypatch, response=httpx.Response(200, text="not json"))
    response = client.post("/generate", json={"prompt": "Suggest an outfit."})
    assert response.status_code == 502


def test_generate_ollama_missing_response_field_returns_502(monkeypatch):
    _patch_ollama_post(monkeypatch, response=httpx.Response(200, json={"done": True}))
    response = client.post("/generate", json={"prompt": "Suggest an outfit."})
    assert response.status_code == 502


def test_generate_ollama_blank_response_returns_502(monkeypatch):
    _patch_ollama_post(monkeypatch, response=httpx.Response(200, json={"response": "   "}))
    response = client.post("/generate", json={"prompt": "Suggest an outfit."})
    assert response.status_code == 502
