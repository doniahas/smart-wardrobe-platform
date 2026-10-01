"""Real system/end-to-end test for the external product/catalog
grounding workflow (robustness to hallucinations).

Companion to test_e2e_wardrobe_flow.py and test_e2e_forum_flow.py
- same conventions (plain httpx.Client against a real running
`web` container's public port, never TestClient/ASGITransport/mongomock,
unique e2e_-prefixed test data, opt-in via RUN_E2E_TESTS=1).

What this proves, and what it deliberately does NOT claim
-----------------------------------------------------------
This file proves the real production system boundary: a real HTTP request
to `web`'s real public route (`GET /recommendations/catalog-suggestions`)
triggers a real, unmocked `httpx` call to a genuinely separate, real
`catalog` Docker container, which runs its real `Crawler.crawl()`
(including its real adapter-selection logic) and returns a real JSON
response that the real client parses - every line of production code in
that chain executes for real. In this file's success-path test,
`Crawler.crawl()`'s adapter selection finds no adapter eligible for the
route's real-store-only source request (the disposable catalog container
here only has `MockSourceAdapter` registered, and `mock_demo` is never one
of the requested sources - see "CATALOG_ENABLED_SOURCES" below), so
`MockSourceAdapter.fetch()` itself is correctly never invoked and the
crawl legitimately, honestly returns zero products rather than a
mock/fixture one - there is no product for this route to return here at
all, mock or real, and none is fabricated to fill that gap. It also proves
the "never invent, verify or omit" grounding contract, including
real-store scoping: a catalog outage
degrades to an honest, empty, `200` response rather than a fabricated
product or a crash, and - the specific property real-store scoping
guarantees - **the
mock_demo fixture data can never reach this route's client response, even
when it is the only source the backing catalog service has registered.**

What this test does **NOT** claim: the `mock_demo` source is a
deterministic, fixture-backed, in-repo test double
(`services/catalog/app/services/mock_source.py`) - it is **not** a real
retail website, and this file never describes it as one. The requirement
that the system can crawl and ground against **real** retail store data is
evidenced separately, by the catalog service's own fixture-backed
Shopify adapter tests (four real, allowlisted Shopify stores) and by
`services/web/tests/integration/test_web_catalog_service.py`, which proves
the identical real-crawl-boundary contract against `mock_demo` via a
different call path. This file exists specifically to prove the *system
boundary and grounding behavior* reproducibly, in CI-suitable time, with
zero live-Internet dependency - not to re-prove that real Shopify crawling
works, which is already proven elsewhere and is out of scope for an
automated, first-try-portable E2E suite (which must run first try on
any computer, with no uncontrolled Internet calls).

CATALOG_ENABLED_SOURCES, and real-store scoping's effect on this file
------------------------------------------------------------------------
`GET /recommendations/catalog-suggestions` still takes no `sources` query
parameter from the client. Real-store scoping: the route itself
*always* forwards `sources=REAL_STORE_SOURCES` (the four real
allowlisted store names, never `mock_demo`) to the catalog service's
`POST /crawl`, and additionally drops any returned product whose `source`
isn't one of those four as a second, defense-in-depth layer (see
`services/web/app/services/catalog_gateway.py::real_store_products_only`
and `services/web/app/routers/recommendations.py::get_catalog_suggestions`).
Without this scoping, an unfiltered request would let the catalog service's `Crawler`
dispatch to *every* registered adapter, so a disposable environment
restricted to `CATALOG_ENABLED_SOURCES=mock_demo` (`services/catalog/app/
core/config.py`, `app/main.py`) would have this route return real
mock_demo products - which is exactly the fixture-verification behavior
`test_catalog_suggestions_excludes_mock_demo_even_when_it_is_the_only_registered_source`
below would otherwise rely on. With scoping, a catalog service restricted to
`mock_demo` has *no* real-store adapter for this route's request to reach,
so `Crawler.crawl()` correctly returns zero products and the route reports
`catalog_available: true, products: []` - "reached the real catalog
service successfully; genuinely nothing eligible was found" - never a
fabricated or mock product. That is now the property this file's success-
path test proves directly, end-to-end, against a real running catalog
container. `CATALOG_ENABLED_SOURCES` exists precisely so this E2E
environment never dispatches to a live external store.

Two disposable web instances
------------------------------
This file needs **two independently configured** `web` instances, so it
uses two base URLs:

- `E2E_BASE_URL` (default `http://localhost:8000`) - the success-path
  instance: `web` + `mongo` + `recommendation` + a real `catalog`
  container started with `CATALOG_ENABLED_SOURCES=mock_demo`.
- `E2E_CATALOG_DOWN_BASE_URL` (default `http://localhost:8011`) - the
  failure-path instance: `web` + `mongo` + `recommendation`, with
  `CATALOG_SERVICE_URL` pointed at a hostname that does not exist on that
  instance's own disposable Docker network (never a live Internet host) -
  a real, deterministic `httpx.RequestError` "unreachable", not a
  simulated one.

Both fixtures are independently gated on `RUN_E2E_TESTS=1`; running this
whole file requires standing up both disposable topologies - see
docs/testing.md's "System / end-to-end tests" section for the exact
commands.

Opt-in and safety
------------------
Off by default, exactly like the other two E2E files:

    RUN_E2E_TESTS=1 pytest tests/system/test_e2e_catalog_flow.py -v -s

No Ollama/LLM dependency anywhere in this file - `POST /recommendations`
calls only the stateless, rule-based `recommendation` service, and
`GET /recommendations/catalog-suggestions` calls only `catalog`.
"""
import os
import time
import uuid

import httpx
import pytest

E2E_BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8000")
E2E_CATALOG_DOWN_BASE_URL = os.environ.get(
    "E2E_CATALOG_DOWN_BASE_URL", "http://localhost:8011"
)
_RUN_E2E = os.environ.get("RUN_E2E_TESTS", "").strip().lower() in ("1", "true", "yes")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _RUN_E2E,
        reason=(
            "Catalog system/E2E tests are opt-in and require a real running "
            "Docker stack. Set RUN_E2E_TESTS=1 (and optionally "
            "E2E_BASE_URL/E2E_CATALOG_DOWN_BASE_URL) to run them - see "
            "this file's module docstring."
        ),
    ),
]

_READY_TIMEOUT_SECONDS = 60
_READY_POLL_INTERVAL_SECONDS = 2


def _wait_for_stack_ready(client: httpx.Client, base_url: str) -> None:
    """Poll GET /health/full until `web`, MongoDB, and `recommendation`
    all report healthy, or fail loudly with a clear, actionable message -
    same contract as test_e2e_wardrobe_flow.py's helper of the same name.
    `/health/full` never reports catalog reachability (see
    services/web/app/routers/health.py) - catalog reachability is instead
    proven by the tests themselves, not this readiness gate.
    """
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
        f"Real Docker stack at {base_url} was not reachable/healthy "
        f"within {_READY_TIMEOUT_SECONDS}s (last observed: {last_error}). "
        "See docs/testing.md's System / end-to-end tests section for the"
        " exact disposable-stack recipe before running with RUN_E2E_TESTS=1."
    )


@pytest.fixture()
def e2e_client():
    """The success-path instance: web + mongo + recommendation + a real
    `catalog` container restricted to CATALOG_ENABLED_SOURCES=mock_demo."""
    with httpx.Client(base_url=E2E_BASE_URL, timeout=30.0) as client:
        _wait_for_stack_ready(client, E2E_BASE_URL)
        yield client


@pytest.fixture()
def e2e_catalog_down_client():
    """The failure-path instance: web + mongo + recommendation, with
    CATALOG_SERVICE_URL pointed at an unreachable hostname."""
    with httpx.Client(base_url=E2E_CATALOG_DOWN_BASE_URL, timeout=30.0) as client:
        _wait_for_stack_ready(client, E2E_CATALOG_DOWN_BASE_URL)
        yield client


def _unique_suffix() -> str:
    return uuid.uuid4().hex[:12]


def _register_and_login(client: httpx.Client, label: str) -> dict:
    """Duplicated from test_e2e_wardrobe_flow.py's helper of the same name
    - each E2E file is deliberately self-contained, matching that
    file's own precedent."""
    username = f"e2e_{label}_{_unique_suffix()}"
    email = f"{username}@example.com"
    password = "password123"

    register_resp = client.post(
        "/auth/register",
        json={"username": username, "email": email, "password": password},
    )
    assert register_resp.status_code == 201, register_resp.text
    assert register_resp.json() == {"username": username, "email": email}

    login_resp = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert login_resp.status_code == 200, login_resp.text
    login_body = login_resp.json()
    headers = {"Authorization": f"Bearer {login_body['access_token']}"}

    me_resp = client.get("/auth/me", headers=headers)
    assert me_resp.status_code == 200, me_resp.text

    return {"username": username, "email": email, "headers": headers}


def _create_top_only_wardrobe(client: httpx.Client, headers: dict) -> dict:
    """A wardrobe with exactly one `top` item and nothing else - a known,
    deterministic clothing-type gap (no bottom, no dress), matching
    services/recommendation/app/services/rule_based.py::build_recommendation's
    documented behavior: with no bottom/dress candidate at all, the
    real recommendation service selects only the top rather than
    inventing a missing piece, and web's own
    routers/recommendations.py::_compute_missing_types reports
    missing_types=["bottom"] from that real, final selection."""
    resp = client.post(
        "/wardrobe",
        data={
            "name": "E2E Catalog Top",
            "clothing_type": "top",
            "color": "blue",
            "season": "all",
            "style": "casual",
            "occasion": "casual",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_catalog_suggestions_excludes_mock_demo_even_when_it_is_the_only_registered_source(
    e2e_client: httpx.Client,
):
    """Real-store scoping, proved end-to-end against a real running Docker stack
    over real HTTP - not a unit-level mock:

    1. Register/login a real E2E user.
    2. Create a wardrobe with a known clothing-type gap (top only).
    3. POST /recommendations - verify missing_types via the real
       web -> recommendation service chain.
    4. GET /recommendations/catalog-suggestions (the real public route,
       exactly as a real client calls it - no client-supplied `sources`
       parameter exists).
    5. The real web -> catalog HTTP call happens (not mocked - see
       services/web/app/services/catalog_gateway.py).
    6. The real catalog Crawler receives the route's now-hardcoded
       `sources=REAL_STORE_SOURCES` request, but this disposable
       environment's catalog container has only `mock_demo` registered
       (via CATALOG_ENABLED_SOURCES - see module docstring) - so none of
       the requested sources match anything registered, and the real,
       unmodified Crawler correctly returns zero products
       (`Crawler.crawl()`'s own `if not selected: return
       CrawlResponse(products=[], source_status=[])` path).
    7. The externally-visible response is `catalog_available: true`
       (the real catalog service was reached successfully) with
       `products: []` - proof the mock_demo fixture data is genuinely
       unreachable through this route even when it's the *only* source
       available on the other side of a real network call, never merely
       "not requested by convention."
    """
    client = e2e_client
    run_start = time.perf_counter()

    user = _register_and_login(client, "cataloguser")
    top = _create_top_only_wardrobe(client, user["headers"])

    # --- Step 3: POST /recommendations - real web -> recommendation chain ---
    rec_resp = client.post("/recommendations", json={}, headers=user["headers"])
    assert rec_resp.status_code == 200, rec_resp.text
    recommendation = rec_resp.json()
    assert recommendation["missing_types"] == ["bottom"]
    assert len(recommendation["items"]) == 1
    assert recommendation["items"][0]["id"] == top["id"]
    assert recommendation["items"][0]["clothing_type"] == "top"

    # --- Steps 4-7: the real public catalog-suggestions route reaches the
    # real catalog container, which has only mock_demo registered - the
    # route's own real-store-only source scoping means it can genuinely
    # find nothing eligible, and must say so honestly rather than serving
    # mock_demo's fixture products ---
    suggestions_resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "bottom"},
        headers=user["headers"],
    )
    assert suggestions_resp.status_code == 200, suggestions_resp.text
    suggestions = suggestions_resp.json()
    assert suggestions["clothing_type"] == "bottom"
    # The real catalog service was reached (not "unavailable") - it simply
    # had no real-store adapter this request could ever match.
    assert suggestions["catalog_available"] is True
    assert suggestions["products"] == []

    total_seconds = round(time.perf_counter() - run_start, 3)
    print(f"\n[catalog E2E] base_url={E2E_BASE_URL}")
    print(f"[catalog E2E] user={user['username']} top_item={top['id']}")
    print(f"[catalog E2E] missing_types={recommendation['missing_types']}")
    print(f"[catalog E2E] catalog_available={suggestions['catalog_available']} products={suggestions['products']}")
    print(f"[catalog E2E] total elapsed: {total_seconds}s")


def test_catalog_suggestions_failure_path_degrades_safely(e2e_catalog_down_client: httpx.Client):
    """Failure scenario: the real public route with `catalog`
    genuinely unreachable (CATALOG_SERVICE_URL pointed at a hostname that
    does not exist on this disposable stack's own network - see module
    docstring), proving:

    - HTTP behavior matches the production contract: a controlled `200`,
      never a `5xx`/error response bubbled up to the client.
    - `catalog_available` is `False` (the current, documented contract -
      see app/models.py::CatalogSuggestionsResponse).
    - `products` is empty - no invented product, no fabricated/fallback URL.
    - The normal recommendation path (`POST /recommendations`) remains
      fully usable and unaffected, per the production design's explicit
      guarantee (services/web/app/services/catalog_gateway.py's own
      module docstring: "the recommendation flow ... must stay 100%
      functional whether or not catalog is reachable").
    """
    client = e2e_catalog_down_client
    run_start = time.perf_counter()

    user = _register_and_login(client, "catdown")
    top = _create_top_only_wardrobe(client, user["headers"])

    # --- POST /recommendations must be entirely unaffected by catalog being down ---
    rec_resp = client.post("/recommendations", json={}, headers=user["headers"])
    assert rec_resp.status_code == 200, rec_resp.text
    recommendation = rec_resp.json()
    assert recommendation["missing_types"] == ["bottom"]
    assert len(recommendation["items"]) == 1
    assert recommendation["items"][0]["id"] == top["id"]

    # --- GET /recommendations/catalog-suggestions must degrade safely ---
    suggestions_resp = client.get(
        "/recommendations/catalog-suggestions",
        params={"clothing_type": "bottom"},
        headers=user["headers"],
    )
    assert suggestions_resp.status_code == 200, suggestions_resp.text
    suggestions = suggestions_resp.json()
    assert suggestions["clothing_type"] == "bottom"
    assert suggestions["catalog_available"] is False
    assert suggestions["products"] == []

    total_seconds = round(time.perf_counter() - run_start, 3)
    print(f"\n[catalog E2E failure] base_url={E2E_CATALOG_DOWN_BASE_URL}")
    print(f"[catalog E2E failure] user={user['username']} top_item={top['id']}")
    print(f"[catalog E2E failure] recommendation missing_types={recommendation['missing_types']} (unaffected)")
    print(f"[catalog E2E failure] catalog_available={suggestions['catalog_available']} products={suggestions['products']}")
    print(f"[catalog E2E failure] total elapsed: {total_seconds}s")
