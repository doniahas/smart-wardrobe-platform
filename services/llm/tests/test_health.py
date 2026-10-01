import httpx
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _patch_ollama_get(monkeypatch, response=None, exc=None):
    """Patch the outbound call to Ollama so tests don't need it running."""

    async def fake_get(self, url, **kwargs):
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


def test_health_ollama_reachable_and_model_present(monkeypatch):
    _patch_ollama_get(
        monkeypatch,
        response=httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]}),
    )
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "llm",
        "ollama_reachable": True,
        "model_loaded": True,
    }


def test_health_ollama_reachable_but_model_not_pulled(monkeypatch):
    _patch_ollama_get(
        monkeypatch,
        response=httpx.Response(200, json={"models": []}),
    )
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "llm",
        "ollama_reachable": True,
        "model_loaded": False,
    }


def test_health_ollama_unreachable_returns_degraded_not_500(monkeypatch):
    _patch_ollama_get(monkeypatch, exc=httpx.ConnectError("connection refused"))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "service": "llm",
        "ollama_reachable": False,
        "model_loaded": False,
    }
