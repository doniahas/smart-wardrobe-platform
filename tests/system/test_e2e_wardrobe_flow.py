"""Real system/end-to-end test for the core wardrobe workflow.

This is the first true system/E2E test in the repository. Every other test
under tests/system/ and services/*/tests/ either talks to a real MongoDB
through an in-process ASGI app (test_live_mongo_smoke.py,
test_live_mongo_integration.py), spins up a real sibling service as a
subprocess while `web` itself stays in-process (services/web/tests/
integration/*.py), or drives one route in-process with a mocked/mongomock
database. None of them leave the test process over a real network socket
to reach `web`'s own public port.

This file does exactly that: it is a plain `httpx.Client` making real
TCP/HTTP requests against a real running `web` Docker container
(`E2E_BASE_URL`, default `http://localhost:8000` - the only externally
exposed port in docker-compose.yml; `recommendation`/`llm`/`catalog`/
`mongo` are internal-network-only, so entering through `web`'s public API
is the only correct entry point for a real client, and also the only one
this file uses). It deliberately never imports `app.main.app`, never uses
FastAPI's `TestClient`, never uses `ASGITransport`, and never touches
mongomock - the workflow below only exists because a real `web` process,
talking to a real MongoDB and a real `recommendation` service over the
real Docker network, produced it.

Scope: the core wardrobe flow only. The external product/catalog flow
and the forum/WebSocket flow have their own files
(test_e2e_catalog_flow.py, test_e2e_forum_flow.py).

Opt-in and safety
------------------
This tier is off by default (`pytestmark` below) so that an ordinary
`pytest` run in this directory - including CI's `live-mongo-smoke` job,
which never starts docker-compose - never attempts to reach a Docker stack
that may not exist. Set `RUN_E2E_TESTS=1` to opt in, after starting the
real stack yourself:

    docker compose up -d
    RUN_E2E_TESTS=1 pytest tests/system/test_e2e_wardrobe_flow.py -v -s

`E2E_BASE_URL` (default `http://localhost:8000`) can point this at a
different reachable `web` instance if needed.

No dependency on Ollama, the catalog crawler, or live Internet: wardrobe
items are created without an image (skips Moondream), and
`POST /recommendations` calls only the stateless, rule-based
`recommendation` service - never the `llm`/`catalog` services.

Test-data isolation: every user this test creates is registered with a
freshly-generated, randomly-suffixed `e2e_...` username, so repeated runs
never collide with each other or with any pre-existing developer data, and
nothing here ever reads, modifies, or deletes a record it didn't itself
create. This file deliberately does
not attempt automatic teardown of the real users/wardrobe/outfit/feedback
documents it creates against the real database (unlike `live_db`'s
drop-database pattern, which owns its own dedicated, disposable database -
this test runs against the same database a real developer stack uses, so
dropping it would be unsafe). The small number of clearly `e2e_`-prefixed
records left behind per run is the intentional, documented trade-off.
"""
import os
import time
import uuid

import httpx
import pytest

E2E_BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8000")
_RUN_E2E = os.environ.get("RUN_E2E_TESTS", "").strip().lower() in ("1", "true", "yes")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _RUN_E2E,
        reason=(
            "Wardrobe system/E2E tests are opt-in and require a real running "
            "Docker stack. Set RUN_E2E_TESTS=1 (and optionally "
            "E2E_BASE_URL) to run them - see this file's module docstring."
        ),
    ),
]

_READY_TIMEOUT_SECONDS = 60
_READY_POLL_INTERVAL_SECONDS = 2


def _wait_for_stack_ready(client: httpx.Client) -> None:
    """Poll GET /health/full until `web`, MongoDB, and `recommendation` all
    report healthy, or fail loudly with a clear, actionable message.

    Deliberately fails the test (rather than silently skipping) once
    RUN_E2E_TESTS=1 has been explicitly set - the caller asked for a real
    run, so an unreachable stack should be reported as a blocker, not
    swallowed as a false "not run at all"."""
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    last_error = "no attempt made"
    while time.monotonic() < deadline:
        try:
            resp = client.get("/health/full", timeout=5.0)
            if resp.status_code == 200:
                body = resp.json()
                if (
                    body.get("web") == "ok"
                    and body.get("mongo") == "ok"
                    and body.get("recommendation") == "ok"
                ):
                    return
                last_error = f"stack not fully healthy yet: {body}"
            else:
                last_error = f"/health/full returned {resp.status_code}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(_READY_POLL_INTERVAL_SECONDS)
    pytest.fail(
        f"Real Docker stack at {E2E_BASE_URL} was not reachable/healthy "
        f"within {_READY_TIMEOUT_SECONDS}s (last observed: {last_error}). "
        "Start it with `docker compose up -d` before running with "
        "RUN_E2E_TESTS=1."
    )


@pytest.fixture()
def e2e_client():
    """A genuine HTTP client against a real running `web` container.

    Plain `httpx.Client(base_url=...)`, making real requests over a real
    socket - never FastAPI's TestClient, never ASGITransport, never an
    imported `app` object. See the module docstring for why this
    distinction is the entire point of this file."""
    with httpx.Client(base_url=E2E_BASE_URL, timeout=30.0) as client:
        _wait_for_stack_ready(client)
        yield client


def _unique_suffix() -> str:
    return uuid.uuid4().hex[:12]


def _register_and_login(client: httpx.Client, label: str) -> dict:
    """Register a brand-new, uniquely-named E2E test user over real HTTP,
    log in, and verify GET /auth/me.

    The `e2e_` prefix plus a random suffix keeps every record this test
    creates clearly identifiable and collision-free across repeated runs
    against the same real database (no fixture/teardown truncates it -
    see the module docstring's "Test-data isolation" section)."""
    username = f"e2e_{label}_{_unique_suffix()}"
    email = f"{username}@example.com"
    password = "password123"

    register_resp = client.post(
        "/auth/register",
        json={"username": username, "email": email, "password": password},
    )
    assert register_resp.status_code == 201, register_resp.text
    registered = register_resp.json()
    assert registered == {"username": username, "email": email}

    login_resp = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert login_resp.status_code == 200, login_resp.text
    login_body = login_resp.json()
    assert login_body["token_type"] == "bearer"
    headers = {"Authorization": f"Bearer {login_body['access_token']}"}

    # Step 3: verify authenticated current-user access - the API exposes
    # GET /auth/me for exactly this.
    me_resp = client.get("/auth/me", headers=headers)
    assert me_resp.status_code == 200, me_resp.text
    assert me_resp.json() == {"username": username, "email": email}

    return {"username": username, "email": email, "headers": headers}


def _create_wardrobe_item(client: httpx.Client, headers: dict, **overrides) -> dict:
    """POST /wardrobe as real multipart/form-data (matching the route's
    actual Form(...) signature - see services/web/app/routers/wardrobe.py),
    with no `image` part at all, so no vision-model/Moondream/Ollama call
    is ever triggered."""
    form = {
        "name": "E2E Item",
        "color": "blue",
        "season": "all",
        "style": "casual",
        "occasion": "casual",
    }
    form.update(overrides)
    resp = client.post("/wardrobe", data=form, headers=headers)
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["image_url"] is None
    return item


def test_core_wardrobe_e2e_flow(e2e_client: httpx.Client):
    """Core wardrobe workflow, chained end-to-end against a real
    running Docker stack over real HTTP:

    1-3. register/login/verify User A
    4.   add wardrobe items (no image -> no Ollama/Moondream)
    5.   request a recommendation via the real public API
    6.   verify the returned items are User A's real wardrobe items
    7.   save/customize the recommendation as an outfit
    8.   rate the saved outfit
    9.   verify the saved/rated result is retrievable
    10-11. register/login User B and verify User B cannot access
           User A's wardrobe item or saved outfit (same not-found
           convention already established by
           services/web/tests/test_ownership_isolation.py)
    """
    client = e2e_client
    run_start = time.perf_counter()
    timings: dict[str, float] = {}

    def _mark(step: str) -> None:
        timings[step] = round(time.perf_counter() - run_start, 3)

    # --- Steps 1-3: register, log in, verify current-user access (User A) ---
    user_a = _register_and_login(client, "usera")
    _mark("1-3_register_login_verify_user_a")

    # --- Step 4: add enough wardrobe items for a valid outfit ---
    top = _create_wardrobe_item(client, user_a["headers"], name="E2E Top", clothing_type="top")
    bottom = _create_wardrobe_item(
        client, user_a["headers"], name="E2E Bottom", clothing_type="bottom"
    )
    created_item_ids = {top["id"], bottom["id"]}
    assert top["user_id"] == bottom["user_id"]
    _mark("4_add_wardrobe_items")

    # --- Step 5: request a recommendation through the real public web API ---
    # An empty body applies no season/occasion/style filter, so with
    # exactly one top and one bottom in the wardrobe, the rule-based
    # recommendation service (services/recommendation/app/services/
    # rule_based.py) deterministically selects both - no flakiness from
    # scoring/filter interactions.
    rec_resp = client.post("/recommendations", json={}, headers=user_a["headers"])
    assert rec_resp.status_code == 200, rec_resp.text
    recommendation = rec_resp.json()
    assert recommendation["missing_types"] == []
    _mark("5_request_recommendation")

    # --- Step 6: returned item ids really are User A's real wardrobe items ---
    returned_ids = {item["id"] for item in recommendation["items"]}
    assert returned_ids == created_item_ids
    for item in recommendation["items"]:
        assert item["user_id"] == top["user_id"]
    _mark("6_validate_recommendation_ownership")

    # --- Step 7: save/customize the recommendation as an outfit ---
    outfit_resp = client.post(
        "/outfits",
        json={
            "item_ids": [item["id"] for item in recommendation["items"]],
            "name": "E2E customized outfit",
        },
        headers=user_a["headers"],
    )
    assert outfit_resp.status_code == 201, outfit_resp.text
    outfit = outfit_resp.json()
    assert set(outfit["item_ids"]) == created_item_ids
    assert outfit["name"] == "E2E customized outfit"
    _mark("7_save_outfit")

    # --- Step 8: rate the saved outfit via the real feedback API ---
    feedback_resp = client.post(
        "/feedback",
        json={
            "outfit_id": outfit["id"],
            "rating": 5,
            "liked": True,
            "comment": "E2E rating",
        },
        headers=user_a["headers"],
    )
    assert feedback_resp.status_code == 201, feedback_resp.text
    feedback = feedback_resp.json()
    assert feedback["outfit_id"] == outfit["id"]
    assert feedback["rating"] == 5
    _mark("8_rate_outfit")

    # --- Step 9: verify the saved/rated result is retrievable ---
    get_outfit_resp = client.get(f"/outfits/{outfit['id']}", headers=user_a["headers"])
    assert get_outfit_resp.status_code == 200, get_outfit_resp.text
    assert get_outfit_resp.json()["id"] == outfit["id"]

    get_feedback_resp = client.get(f"/feedback/{feedback['id']}", headers=user_a["headers"])
    assert get_feedback_resp.status_code == 200, get_feedback_resp.text
    assert get_feedback_resp.json()["id"] == feedback["id"]
    _mark("9_verify_retrievable")

    # --- Steps 10-11: second user, second-user ownership isolation ---
    user_b = _register_and_login(client, "userb")
    _mark("10_register_login_user_b")

    cross_item_resp = client.get(f"/wardrobe/{top['id']}", headers=user_b["headers"])
    assert cross_item_resp.status_code == 404
    assert cross_item_resp.json()["detail"] == "Item not found"

    cross_outfit_resp = client.get(f"/outfits/{outfit['id']}", headers=user_b["headers"])
    assert cross_outfit_resp.status_code == 404
    assert cross_outfit_resp.json()["detail"] == "Outfit not found"
    _mark("11_ownership_isolation_verified")

    total_seconds = round(time.perf_counter() - run_start, 3)
    print(f"\n[wardrobe E2E] base_url={E2E_BASE_URL}")
    print(f"[wardrobe E2E] users: {user_a['username']} (A), {user_b['username']} (B)")
    print(f"[wardrobe E2E] wardrobe items: top={top['id']} bottom={bottom['id']}")
    print(f"[wardrobe E2E] outfit={outfit['id']} feedback={feedback['id']}")
    print(f"[wardrobe E2E] step timings (seconds since test start): {timings}")
    print(f"[wardrobe E2E] total elapsed: {total_seconds}s")
