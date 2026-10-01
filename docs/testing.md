# Testing

Smart Wardrobe is tested at several levels: fast per-service suites that
need no infrastructure, integration tests across real service and database
boundaries, opt-in end-to-end tests against running containers, and a live
load test. Automated coverage is backend-focused (API, business logic, data
layer, served frontend content); frontend *behavior* in a real browser is
verified manually — see [Known gaps](#known-gaps).

## Contents

- [Running the suites](#running-the-suites)
- [Coverage by service](#coverage-by-service)
- [Test categories](#test-categories)
  - [Service unit tests](#service-unit-tests)
  - [Integration tests](#integration-tests)
  - [System / end-to-end tests](#system--end-to-end-tests)
  - [Security tests](#security-tests)
  - [Concurrency and stress tests](#concurrency-and-stress-tests)
  - [Failure-path and degradation tests](#failure-path-and-degradation-tests)
  - [Chat load test (live stack)](#chat-load-test-live-stack)
- [CI coverage](#ci-coverage)
- [Manual validation pending](#manual-validation-pending)
- [Known gaps](#known-gaps)

## Running the suites

Each backend service has its own independent pytest suite. `web` and
`recommendation` fake MongoDB with `mongomock-motor`; `web` fakes Redis
with `fakeredis[lua]`; `llm` and `catalog` mock all outbound HTTP. **No
running database, Redis, model, or application stack is required.**

**Recommended — Docker only.** Each service runs in a disposable
`python:3.11-slim` container (`--rm`), installing its own
`requirements.txt` and running its own `pytest` suite:

```bash
# Linux / macOS / Git Bash:
./scripts/run_tests_docker.sh                 # all four services
./scripts/run_tests_docker.sh web catalog     # or just specific ones
```

```powershell
# Windows PowerShell:
.\scripts\run_tests_docker.ps1
.\scripts\run_tests_docker.ps1 web catalog
```

**Optional — host Python.** With Python 3.11 available:

```bash
cd services/web            # or recommendation, llm, catalog
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest
```

(On Windows use `python` and `.venv\Scripts\...`; on Debian/Ubuntu,
`python3 -m venv` may first need `sudo apt install python3-venv`.)

## Coverage by service

| Service | Tests | Files | What's covered |
|---|---|---|---|
| `web` | 1,306 | 86 | Auth and JWT validation, ownership isolation, wardrobe CRUD + filters, AI photo analysis (mocked vision model), outfits + Outfit Builder integrity rules, recommendations + grounding + fallback + `missing_types`, personalization and long-term memory (manual, chat-learned, preference reversal, generic preferences), recommendation feedback, catalog gateway and suggestions route, chat catalog grounding and reply guardrail, chat route/queue/workers/claim fencing/stale-job recovery, forum (posts, comments, reactions, Recreate), direct messages, notifications, WebSocket live delivery (incl. cross-replica Pub/Sub via fakeredis), image + MP4 media, rate limiting (incl. shared Redis storage), pagination, indexes, config validation, logging privacy, MongoDB-outage handling, served-frontend content checks, Compose/Caddy/demo-seed configuration checks, plus the `integration/` and `security/` subdirectories |
| `catalog` | 177 | 14 | Product model validation, mock source, async crawler (bounded concurrency, retry/backoff, failure isolation, real elapsed-time timeouts), Shopify adapter parsing and normalization (fixture-based, no live network), season classification, crawl/sources routes, `CATALOG_ENABLED_SOURCES` allowlisting |
| `recommendation` | 30 | 1 | Rule-based scoring, hard filters vs. soft preference bonuses, dress vs. top+bottom exclusivity, memory exclusions, learned-score ranking |
| `llm` | 20 | 3 | `/health`, `/generate` (text), vision-model routing, Ollama failure classification |
| **Total** | **1,533** | | |

`tests/system/` adds 5 further files (live-MongoDB and end-to-end tests),
described below.

## Test categories

### Service unit tests

The bulk of each suite: route-level tests through FastAPI's `TestClient`
against an in-memory database, plus pure-function tests for scoring,
grounding, prompt building, memory extraction parsing, MP4 validation, and
crawler normalization. Outbound calls (to `recommendation`, `llm`,
`catalog`, Ollama, or real stores) are patched at the `httpx` seam, so these
tests are deterministic and offline.

### Integration tests

Two tiers exercise real boundaries that mocks cannot:

- **Real cross-service HTTP** (`services/web/tests/integration/`) — each
  test starts the real sibling service (`recommendation`, `llm`, or
  `catalog`) as a `uvicorn` subprocess on a real TCP port and points
  `web`'s `*_SERVICE_URL` at it, plus a real static-media serving test.
  These run as part of the normal `web` suite.
- **Real MongoDB** (`tests/system/test_live_mongo_smoke.py`,
  `tests/system/test_live_mongo_integration.py`) — the real app against a
  real `mongo:7` server: unique-index enforcement, duplicate registration /
  duplicate feedback mapped to `409`, the wardrobe-delete cascade into
  outfits and feedback, and `claim_next_job` atomicity under genuinely
  concurrent round-trips. They need `MONGO_URI`:

  ```bash
  docker run -d --rm --name sw-test-mongo -p 27017:27017 mongo:7
  cd tests/system
  MONGO_URI=mongodb://localhost:27017 TESTING=true \
    pytest test_live_mongo_smoke.py test_live_mongo_integration.py -v
  docker stop sw-test-mongo
  ```

  (Requires the `web` requirements installed; the smoke test also runs in
  CI against a `mongo:7` service container.)

### System / end-to-end tests

`tests/system/test_e2e_*.py` are true system tests: a plain `httpx` client
(and, for the forum flow, a real WebSocket client) talking to real running
containers over real sockets — never `TestClient`, `ASGITransport`, or
mongomock. They are **opt-in** (`RUN_E2E_TESTS=1`) and skipped by every
ordinary `pytest` run.

| File | Flow | Needs |
|---|---|---|
| `test_e2e_wardrobe_flow.py` | Register → login → add items → recommend → save outfit → rate → retrieve → second-user isolation | `web`, `mongo`, `recommendation` |
| `test_e2e_catalog_flow.py` | Catalog suggestions through a real `catalog` container (only real-store sources requested, so `mock_demo` data never reaches the client), plus an unreachable-catalog failure path that degrades to `catalog_available: false` without affecting recommendations | Two `web` instances: one with a `catalog` restricted to `CATALOG_ENABLED_SOURCES=mock_demo`, one whose `CATALOG_SERVICE_URL` is unreachable |
| `test_e2e_forum_flow.py` | Three users: anonymous post with image + MP4, comment, reaction, DM with media, live WebSocket notification delivery, private DM-media isolation | `web`, `mongo`, `redis` |

None of them depends on Ollama or the live Internet. Two configuration
details matter:

- The forum flow's `web` **must not** run with `TESTING=true` — that flag
  disables the Redis live-notification subscriber, so the WebSocket client
  would wait for a notification that is never delivered.
- The flows register several users from one client IP, so the `web`
  instances need a generous `RATE_LIMIT_AUTH` (e.g. `100/minute`).

**Disposable recipe (all three suites).** Everything runs on a private
Docker network with uniquely named containers and no published host ports;
the test runner itself runs inside the `web` image (which already contains
`pytest`, `httpx`, and `websockets`). From the repository root, in a
Linux/macOS shell or Git Bash:

```bash
NET=sw-e2e
SECRET=$(docker run --rm python:3.11-slim python -c "import secrets; print(secrets.token_hex(32))")

docker network create "$NET"
docker build -t sw-e2e-web services/web
docker build -t sw-e2e-recommendation services/recommendation
docker build -t sw-e2e-catalog services/catalog

docker run -d --name sw-e2e-mongo --network "$NET" mongo:7
docker run -d --name sw-e2e-redis --network "$NET" redis:7-alpine
docker run -d --name sw-e2e-recommendation --network "$NET" sw-e2e-recommendation
docker run -d --name sw-e2e-catalog --network "$NET" \
  -e CATALOG_ENABLED_SOURCES=mock_demo sw-e2e-catalog

WEB_ENV=(-e JWT_SECRET="$SECRET" -e MONGO_URI=mongodb://sw-e2e-mongo:27017
         -e RECOMMENDATION_SERVICE_URL=http://sw-e2e-recommendation:8001
         -e REDIS_URL=redis://sw-e2e-redis:6379/0 -e RATE_LIMIT_AUTH=100/minute)
docker run -d --name sw-e2e-web --network "$NET" "${WEB_ENV[@]}" \
  -e CATALOG_SERVICE_URL=http://sw-e2e-catalog:8003 sw-e2e-web
docker run -d --name sw-e2e-web-catalog-down --network "$NET" "${WEB_ENV[@]}" \
  -e CATALOG_SERVICE_URL=http://sw-e2e-no-such-host:8003 sw-e2e-web

docker run --rm --network "$NET" -v "$PWD:/repo" -w /repo/tests/system \
  -e TESTING=true -e RUN_E2E_TESTS=1 \
  -e E2E_BASE_URL=http://sw-e2e-web:8000 \
  -e E2E_CATALOG_DOWN_BASE_URL=http://sw-e2e-web-catalog-down:8000 \
  --entrypoint python sw-e2e-web -m pytest -p no:cacheprovider -v \
  test_e2e_wardrobe_flow.py test_e2e_catalog_flow.py test_e2e_forum_flow.py

# Clean up everything the recipe created:
docker rm -f -v sw-e2e-web sw-e2e-web-catalog-down sw-e2e-catalog \
  sw-e2e-recommendation sw-e2e-redis sw-e2e-mongo
docker network rm "$NET"
```

`TESTING=true` on the *runner* container is correct and harmless: the
runner never imports or runs the application itself; it only lets the
shared `tests/system/conftest.py` load without a real `JWT_SECRET`. On
Windows Git Bash, prefix the runner command with `MSYS_NO_PATHCONV=1` and
use `-v "$(pwd -W):/repo"` so the volume path is passed through unchanged.

The wardrobe flow can also run against a normal local stack
(`docker compose up -d`, then
`RUN_E2E_TESTS=1 pytest tests/system/test_e2e_wardrobe_flow.py -v` with the
`web` requirements installed), but note the default `RATE_LIMIT_AUTH`
(`5/minute`) and that E2E users (`e2e_`-prefixed) are left in that
database by design — the tests never delete data they didn't create in a
disposable database.

### Security tests

`services/web/tests/security/` is a dedicated adversarial suite (78 tests)
covering path traversal and unsafe filenames, malformed JSON bodies,
NoSQL-operator and injection-style input, cross-user authorization for the
memory profile and DM history, and MP4 attachment attacks (MIME/extension
spoofing, malformed or incomplete containers, oversized uploads, private
DM-video access, rate limiting on video uploads). Security-relevant checks
also run throughout the rest of the suite: auth-required checks on nearly
every route, JWT expiry/tampering, per-resource ownership isolation,
content-sniffed upload validation, and rate limiting.

See [`security-testing-report.md`](security-testing-report.md) for the
full breakdown and residual risks.

### Concurrency and stress tests

| Area | Test file | What it proves |
|---|---|---|
| Chat queue/workers | `test_chat_concurrency.py`, `test_chat_queue_unification.py` | Worker concurrency bound, no double-processing, ordering, error isolation, ownership |
| Claim fencing | `test_chat_queue.py`, `test_chat_worker.py` | A stale, reclaimed job's late write (and its memory-learning side effect) is fenced out |
| Mixed multi-user operations | `test_multi_user_concurrency.py` | Wardrobe write + recommendation + forum post + DM fired concurrently by different users all succeed, with per-user isolation intact |
| WebSocket fan-out | `test_websocket_concurrency.py` | Five simultaneous real sockets receive exactly their own mixed notifications |
| Upload volume | `test_upload_stress.py` | Repeated near-limit uploads and a burst of oversized uploads leave state consistent |
| Rate limiting | `test_rate_limiting.py`, `test_rate_limiting_shared_storage.py` | Per-route limits, `429` behavior, one combined counter across two app instances sharing Redis, fallback when Redis is unreachable |
| Crawler | `catalog/tests/test_crawler_concurrency.py`, `test_real_delayed_timeout.py` | Semaphore-bounded concurrency; a genuinely delayed local TCP server trips the real `httpx` timeout |
| Races | `test_reactions.py`, `test_notification_triggers.py`, `test_messages_route.py` | Reaction/notification dedup and concurrent conversation creation |

### Failure-path and degradation tests

- **MongoDB outage** (`test_mongo_outage_handling.py`, `test_health.py`) —
  driver connectivity errors become a clean `503` on any DB-backed route,
  while `GET /health/full` keeps reporting the degraded dependency.
- **Catalog unavailable/malformed** — the suggestions route returns a
  controlled `200` with `catalog_available: false`; chat grounding renders an
  explicit `unavailable` status.
- **LLM failures** (`test_llm_gateway.py`, `test_chat_worker.py`) —
  unavailable/timeout/invalid responses map to stable error reasons, and
  failed chat jobs reach a terminal state.
- **Redis unavailable** — rate limiting falls back to per-process limits
  without ever producing a `500`; Pub/Sub publish failures never affect the
  durable notification write; the subscriber reconnects automatically.
- **Malformed model output** — memory extraction and photo analysis degrade
  to "nothing learned" / all-`null` suggestions instead of erroring.
- **Configuration safety** (`test_config.py`) — startup refuses a missing or
  placeholder `JWT_SECRET` and an unsafe stale-job timeout.

### Chat load test (live stack)

`scripts/chat_load_test.py` drives the real asynchronous chat queue
(`POST /chat/jobs` + `GET /chat/jobs/{id}`) over HTTP with several
concurrent authenticated users against a running stack and a real model. It
reports client-observed and server end-to-end latency, throughput, and
terminal-state counts; with `--mongo-uri` it also measures queue wait time
and flags any job processed more than once.

```bash
docker compose up -d
python scripts/chat_load_test.py                                   # 5 users x 2 jobs
python scripts/chat_load_test.py --users 10 --jobs-per-user 3      # raise RATE_LIMIT_CHAT/AUTH first
```

Rate limits are per client IP, so a large run from one machine needs higher
`RATE_LIMIT_CHAT`/`RATE_LIMIT_AUTH` values (see the script's docstring). Not
part of CI: it needs the live stack and a real, slow model round-trip per
job.

## CI coverage

`.github/workflows/ci.yml` runs on every push and on pull requests to
`main`:

| Job | What it does |
|---|---|
| `unit-tests` | Matrix over `web`, `recommendation`, `llm`, `catalog`: install requirements on Python 3.11 and run the service's full pytest suite with `TESTING=true` |
| `docker-build` | Matrix over the same four services: `docker build` each image |
| `live-mongo-smoke` | Runs `tests/system/test_live_mongo_smoke.py` against a `mongo:7` service container |

CI does not run the end-to-end suites, the live-MongoDB integration file,
or the chat load test, and it does not deploy anything.

## Manual validation pending

The automated suites mock the LLM and vision model and have no browser
tier, so the following behaviors still need a real `docker compose up`
stack (with both models pulled) and a real browser. These are pending
checks, not known bugs.

| # | Check | Why automation can't cover it |
|---|---|---|
| 1 | Analyze Item with a real single-garment photo | Vision call is mocked |
| 2 | Real vision latency / cold-start behavior (~30–45s on CPU) | Mocked calls return instantly |
| 3 | Recreate on a post with `shared_outfit` makes no vision call | Backend-asserted; UI path unverified |
| 4 | Partial wardrobe match → Builder opens with real matches, missing items still listed | No browser tier |
| 5 | Zero wardrobe match → no Builder button, explanation only | No browser tier |
| 6 | "Customize this outfit" → Outfit Builder pre-filled with owned items | No browser tier |
| 7 | Saving from the Builder saves only owned wardrobe items | No browser tier |
| 8 | Missing Recreate components → catalog suggestions render inside the Builder | No browser tier |
| 9 | Full wardrobe match → no catalog requests fire (DevTools Network) | No browser tier |
| 10 | Catalog stopped (`docker compose stop catalog`) → Builder still works, honest "unavailable" message | No browser tier |
| 11 | External product links open the store and never add to wardrobe/outfit | No browser tier |
| 12 | Dashboard catalog suggestions still work (regression) | No browser tier |
| 13 | Modal/layout behavior at normal and narrow widths | No browser tier |
| 14 | Logout → login clears stale Recreate/Builder state | No browser tier |
| 15 | A photo-only Explore post shows no recreate affordance | No browser tier |
| 16 | AI Chat: a queued job polls to completion and the reply renders | Served markup/endpoints are checked; live round-trip is not |
| 17 | AI Chat with a real model: reply quality, latency, plain-text rendering | LLM is mocked |
| 18 | Chat-driven memory: state a preference, then a contradicting one, and confirm later replies respect it | Backend is tested end-to-end; browser round-trip is not |
| 19 | All four nav tabs reachable and styled; dashboard stat tiles show real counts | Served-content check only |

## Known gaps

- **No JavaScript test execution.** Frontend behavior is manual-QA only.
  Several tests assert on the *served* static content (e.g. that the chat UI
  only uses the queued `/chat/jobs` endpoints, that there is no external
  font dependency), and changed JS is syntax-checked with `node --check`,
  but no test clicks through the UI.
- **Model behavior is mocked.** Automated tests verify request handling,
  parsing, grounding, and error mapping — not real model quality or latency.
- **Load testing is manual.** `scripts/chat_load_test.py` needs a live
  stack; deterministic concurrency *correctness* is covered by the regular
  suite.
- **One load-sensitive test.**
  `test_rate_limiting.py::test_catalog_suggestions_requests_are_rate_limited`
  uses a 20-per-minute moving window; on a heavily loaded machine the first
  requests can age out of the window before the final one, letting it
  through legitimately. It passes on re-run and in isolation.
- **No fuzzing / property-based testing.** Adversarial payloads are
  hand-picked representative cases.
- **Executable bits.** `test_demo_seed_wiring.py` asserts
  `scripts/start_demo.sh` is executable; a checkout that doesn't preserve
  the executable bit (e.g. some Windows setups) will fail that one check.
