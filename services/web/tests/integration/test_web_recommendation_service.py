"""Real web -> recommendation service integration test.

Every other test that touches the recommendation service
(test_recommendations_route.py etc.) mocks `httpx.AsyncClient.post` - this
is the one place in the suite where `web`'s real, completely unmodified
HTTP call in routers/recommendations.py actually leaves the process and
reaches a real, independently running `recommendation` FastAPI instance:
a genuine subprocess, listening on a real TCP socket on 127.0.0.1,
started with the exact same `uvicorn app.main:app` invocation
services/recommendation/Dockerfile uses in production.

Nothing about the HTTP call itself is touched. The only thing redirected
is the *destination URL* - from the production docker-compose hostname
(`recommendation:8001`) to this test's local port - via the same
`RECOMMENDATION_SERVICE_URL` name `routers/recommendations.py` already
imports. That's exactly how a real deployment points at a different
target per environment; it is not a mock of the call, its request, or
its response.

Placed under services/web/tests/integration/ following the exact
precedent set by services/web/tests/security/: a dedicated
subdirectory that inherits conftest.py's client/make_user/
make_wardrobe_item fixtures for free via pytest's normal parent-directory
fixture resolution, with zero duplication of existing test
infrastructure.
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import app.routers.recommendations as recommendations_router

# services/web/tests/integration/test_X.py -> parents[3] is "services"
_RECOMMENDATION_SERVICE_DIR = Path(__file__).resolve().parents[3] / "recommendation"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def real_recommendation_service(monkeypatch):
    """Start the real recommendation service as a genuine subprocess on a
    free localhost port, wait for it to actually report healthy, then
    point web's RECOMMENDATION_SERVICE_URL at it. Always terminated in
    the finally block, even if the test itself fails.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(_RECOMMENDATION_SERVICE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready = False
        for _ in range(50):
            try:
                if httpx.get(f"{base_url}/health", timeout=0.5).status_code == 200:
                    ready = True
                    break
            except httpx.RequestError:
                pass
            time.sleep(0.2)
        if not ready:
            raise RuntimeError(
                "real recommendation service did not become healthy within 10s"
            )

        monkeypatch.setattr(recommendations_router, "RECOMMENDATION_SERVICE_URL", base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_recommendation_request_reaches_a_real_recommendation_service(
    client, make_user, make_wardrobe_item, real_recommendation_service
):
    """No mock anywhere on the web side of this call. The request genuinely
    leaves the process, is scored by a real, independently running
    recommendation instance, and web then re-grounds the result against
    (mongomock) MongoDB before returning it - exactly the production
    request path.

    Asserts on the specific item *names* woven into web's own
    _build_final_explanation (routers/recommendations.py) - proof the
    real cross-service round trip actually happened, not a coincidence:
    a mocked/fake response could never know these exact names, since
    they only exist in this test's own wardrobe documents.
    """
    user = await make_user("realrecuser")
    top_id = await make_wardrobe_item(
        user["user_id"],
        name="Real Rec Top",
        clothing_type="top",
        color="blue",
        season="all",
        style="casual",
        occasion="casual",
    )
    bottom_id = await make_wardrobe_item(
        user["user_id"],
        name="Real Rec Bottom",
        clothing_type="bottom",
        color="black",
        season="all",
        style="casual",
        occasion="casual",
    )

    resp = client.post("/recommendations", json={}, headers=user["headers"])

    assert resp.status_code == 200
    body = resp.json()
    returned_ids = {item["id"] for item in body["items"]}
    assert returned_ids == {top_id, bottom_id}
    assert body["missing_types"] == []
    assert "Real Rec Top" in body["explanation"]
    assert "Real Rec Bottom" in body["explanation"]
