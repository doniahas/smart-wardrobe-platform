"""Real web -> llm service integration test.

Mirrors test_web_recommendation_service.py's pattern exactly: a genuine
`llm` subprocess (the same `uvicorn app.main:app` invocation
services/llm/Dockerfile uses), reached over a real TCP socket, with only
the *destination URL* redirected (LLM_SERVICE_URL) - never the HTTP call
itself. Driven through the real POST /chat route (routers/chat.py), not
just llm_gateway.call_llm() directly, so the router's own
ChatGatewayFailure -> HTTPException translation is exercised too.

This deliberately never reaches a real Ollama/model: that's Ollama's own
integration, and this suite's standing rule is to
never touch live LLM functionality. Instead, the real llm subprocess is
started with OLLAMA_URL pointed at a closed local port, so
services/llm/app/services/ollama_client.py's own, already-implemented
"Ollama unreachable" path (-> 503, reason=llm_unavailable) is reached
deterministically over loopback (within OLLAMA_TIMEOUT_SECONDS, ~2s) -
proving the real web<->llm HTTP contract (status code, JSON error body,
gateway parsing) without any live model dependency, network egress, or
non-determinism.

The chat message ("Hi") is the same one already used throughout
test_chat_route.py specifically because it does not trip
chat_catalog_grounding's local keyword-based relevance detection - this
test would otherwise also reach the (real, unmocked) catalog gateway,
which is out of scope here (covered separately in
test_web_catalog_service.py).
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import app.services.llm_gateway as llm_gateway

# services/web/tests/integration/test_X.py -> parents[3] is "services"
_LLM_SERVICE_DIR = Path(__file__).resolve().parents[3] / "llm"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def real_llm_service(monkeypatch):
    """Start the real llm service as a genuine subprocess, with its own
    OLLAMA_URL pointed at a closed local port (nothing ever listens there
    for the lifetime of this fixture) so its "Ollama unreachable" failure
    path is reached deterministically, without a slow/flaky DNS lookup for
    the production `ollama` compose hostname and without a real model.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    closed_ollama_port = _free_port()

    env = {**os.environ, "OLLAMA_URL": f"http://127.0.0.1:{closed_ollama_port}"}

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(_LLM_SERVICE_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready = False
        for _ in range(50):
            try:
                # Unlike recommendation/catalog's instant /health, this
                # service's /health calls check_ollama_status(), which
                # itself waits up to OLLAMA_TIMEOUT_SECONDS (2.0s) trying
                # to reach the deliberately-unreachable OLLAMA_URL above
                # before responding - so the per-request timeout here must
                # comfortably exceed that, not match the other two fixtures'
                # 0.5s.
                if httpx.get(f"{base_url}/health", timeout=3.0).status_code == 200:
                    ready = True
                    break
            except httpx.RequestError:
                pass
            time.sleep(0.2)
        if not ready:
            raise RuntimeError("real llm service did not become healthy within 10s")

        monkeypatch.setattr(llm_gateway, "LLM_SERVICE_URL", base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_chat_route_reaches_a_real_llm_service_and_surfaces_its_failure(
    client, make_user, real_llm_service
):
    """No mock anywhere on the web side of this call: the request genuinely
    leaves the process and is handled by a real, independently running llm
    FastAPI instance. Because that instance's own Ollama backend is
    unreachable (by construction, see real_llm_service above), it returns
    its real 503 "unavailable" response, and this asserts that
    routers/chat.py::chat correctly receives and translates that real
    response into the expected HTTP failure - proof of the actual
    cross-service contract, not a stand-in for it."""
    user = await make_user("reallmuser")

    resp = client.post("/chat", json={"message": "Hi"}, headers=user["headers"])

    # 503 is reached (not raised locally): the real llm service responded,
    # and its own real detail string ("Ollama service is unavailable.",
    # from services/llm/app/services/ollama_client.py::generate_text) is
    # passed through unchanged by llm_gateway.py's _PASSTHROUGH_STATUSES
    # handling - proof this round-tripped through both real services,
    # rather than web's own "LLM service is unavailable." fallback string
    # for when `llm` itself can't be reached at all (a different failure
    # mode, not exercised here since `llm` is reachable and responding).
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Ollama service is unavailable."
