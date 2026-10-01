# Architecture

## System overview

Smart Wardrobe is a Docker Compose microservices system with five
application containers plus MongoDB and Redis:

```text
Client (browser — the single-page app served by web, see "Frontend" below)
  |
  v
web  (FastAPI — the only service exposed to the host; owns persistence,
  |    auth, and ownership logic; stateless)
  |
  |----> recommendation  (FastAPI — stateless rule-based scoring, no DB access)
  |
  |----> mongo  (MongoDB 7 — persistence, named volume, startup healthcheck)
  |
  |----> llm  (FastAPI — thin wrapper, talks only to Ollama)
  |        |
  |        |----> ollama  (local model runtime: qwen2.5:3b text, moondream:latest vision)
  |
  |----> catalog  (FastAPI — stateless clothing-product crawler: mock_demo +
  |        four real Shopify stores; async, bounded concurrency, retry/backoff)
  |
  |----> redis  (shared, internal-only cache — backs web's rate-limit
           counters AND cross-replica live-notification Pub/Sub, no
           persistent volume; see "Cross-cutting design decisions" below)
```

Only `web` publishes a host port. `recommendation`, `llm`, `ollama`,
`catalog`, `mongo`, and `redis` are internal-only and reachable solely via
Docker DNS on the private `smartwardrobe-net` bridge network. `web`
depends on `mongo` being healthy, and starts after
`recommendation`/`llm`/`catalog`/`redis` have started (a slow or broken
`catalog` or `redis` never blocks `web` from starting, since `web`'s calls
to both always degrade gracefully — see "Data flow: catalog suggestions"
below and "Cross-cutting design decisions" below).

## Why the system is split this way

- **`web` vs. `recommendation`:** the recommendation engine is deliberately
  stateless and has no database access of its own, so it can be scaled or
  replaced independently of the stateful web tier. `web` re-verifies
  everything the recommendation service returns before trusting it (see
  "Grounding" below) — the split is also what makes that verification step
  a clean boundary rather than an internal call.
- **`llm`/`ollama` vs. `web`:** no hosted or external LLM API is used
  anywhere in this system. `llm` is a narrow FastAPI wrapper around a local
  Ollama container; `web` never talks to Ollama directly. This keeps the
  "local-only" boundary enforced at the service level, not just by
  configuration.
- **Only `web` exposed:** every other service is reachable only from inside
  the Docker network, so the attack surface at the host boundary is a single
  authenticated API.
- **`catalog` vs. `web`:** `catalog` owns all outbound HTTP to third-party
  clothing sites (real Shopify stores + the mock source) plus its own
  retry/backoff/concurrency concerns — a fundamentally different failure
  mode (external, rate-limited, adversarial-ish) from anything else in the
  system. `web` never talks to a third-party store directly; it calls
  `catalog` over the internal network and treats every response
  defensively (see "Data flow: catalog suggestions" below). `catalog` is
  stateless like `recommendation` — no MongoDB access, no product cache.

## Cross-cutting design decisions

**Ownership scoping.** Every router follows the same pattern:
`Depends(get_current_user)` + `Depends(get_db)`, and every query is scoped
by `{"_id": oid, "user_id": user_id}` (or `{"user_id": user_id}` for lists).
`user_id` is derived exclusively from the server-side JWT identity — no
client-supplied field ever names whose data to access. Both "not found" and
"not yours" return a uniform 404, never 403, to avoid resource-existence
probing.

**Grounding / no hallucination.** Recommendations are never trusted as
fact. Every item id the `recommendation` service returns is re-verified
against MongoDB (existence + ownership) before it reaches the client. A
failed candidate is replaced with a verified fallback from the same user's
own wardrobe if one exists; otherwise that slot is simply omitted and
reported via `missing_types` — nothing is ever invented. Long-term memory
and learned preferences can only influence *ranking or exclusion*; they can
never create or overwrite a verified wardrobe fact, enforced both
structurally (memory and wardrobe are separate collections) and at the API
model layer (response models reject unlisted fields).

**References, not snapshots — except when shared.** Saved outfits store
`item_ids` referencing live wardrobe documents, so an outfit always reflects
the current state of its items. The one deliberate exception is sharing an
outfit to a post: at share time, a sanitized `SharedOutfitSnapshot`
(categorical attributes only — no item id, name, image, or owner reference)
is frozen onto the post, independent of the source outfit or wardrobe
afterward.

**Local-only AI.** Both the chat feature and AI-assisted wardrobe image
analysis run entirely against the local Ollama models (`qwen2.5:3b` for
text, `moondream:latest` for vision) — nothing is sent to an external AI
provider, and both work fully offline once the models are pulled.

**Rate limiting and indexing as a baseline, not an afterthought.** Every
user-facing write path has its own configurable rate limit
(`RATE_LIMIT_*` env vars — auth, recommendations, chat, wardrobe writes,
outfit writes, memory writes, feedback, posts, reactions, comments,
messages), and MongoDB indexes are (re)created idempotently on every `web`
startup, covering every user-scoped query pattern including each optional
wardrobe filter.

**Shared, replica-safe rate limiting.**
`core/limiter.py`'s counters live in the shared `redis` container
(`REDIS_URL`) in normal runtime, not in a single process's memory, so
multiple `web` replicas enforce one combined ceiling per client key
instead of each replica keeping an independent one. If Redis becomes
unreachable, `web` stays fully available: slowapi's own built-in
`in_memory_fallback_enabled`/`in_memory_fallback` mechanism transparently
switches to a private, per-process in-memory limit until Redis is
reachable again (detected automatically), and `swallow_errors` is a
backstop so no ordinary request can ever fail with a 500 purely because
Redis is down. `TESTING` mode is unaffected — it keeps the original,
deterministic in-process storage every existing test already depends on.

**Cross-replica live WebSocket notification delivery.**
`services/live.py::send_to_user` does not deliver directly to
locally-registered sockets — it publishes to one shared Redis Pub/Sub
channel (`services/redis_client.py`), and every `web` replica runs its
own subscriber task (started in `main.py`'s `lifespan`) that receives
every published event and delivers it only to the sockets *that replica*
currently has registered for the exact `recipient_id`. As a result, a
user connected to one replica receives a live push even when the
triggering request (a DM, like, or dislike) is handled by a different
replica. The durable Mongo
notification write in `create_notification` is completely unchanged and
unaffected by this — it always happens first, exactly once, regardless of
how many replicas are subscribed. Redis Pub/Sub is deliberately
best-effort: a publish failure is logged and swallowed (never affecting
the Mongo write or the triggering request), and the subscriber
automatically reconnects with a bounded backoff after any connection
loss, never permanently dying. `redis_client.py`'s Redis client is
entirely independent from `core/limiter.py`'s own rate-limiting client —
both read the same `REDIS_URL` but never share one Python client object
or connection.

## Services

| Service | Role | Exposed? |
|---|---|---|
| `web` | Main API: auth, wardrobe, outfits, recommendations orchestration, memory, chat orchestration, forum/social, notifications, WebSocket live delivery. Serves the static frontend. | Yes (`:8000`) |
| `recommendation` | Stateless rule-based outfit scoring engine. | No |
| `llm` | Wrapper around Ollama's HTTP API (`/health`, `/generate`); auto-selects the vision model when a request includes an image. | No |
| `ollama` | Local model runtime container. Models are pulled manually once per machine and persist in the `ollama_data` volume. | No |
| `catalog` | Stateless clothing-product crawler: `mock_demo` + four real Shopify stores, async/bounded-concurrency, retry/backoff, `/health`, `/sources`, `/crawl`. No MongoDB access, no product cache. | No |
| `mongo` | MongoDB 7, persisted via the `mongo_data` volume, gates other services via a healthcheck. | No |
| `redis` | Shared, internal-only cache (`redis:7-alpine`, no persistent volume). Backs `web`'s rate-limit counters and cross-replica WebSocket live-event Pub/Sub. | No |
| `caddy` | Production only (`docker-compose.prod.yml`): reverse proxy, automatic HTTPS, round-robin load balancing across `web` replicas. | Yes (80/443, production) |

## Frontend

The client is a browser-based single-page app, served directly by `web`
from `services/web/app/static/` (`index.html` + `app.js`, plus dedicated
modules — `outfit-builder.js` for the Outfit Builder, `chat.js` for the AI
Chat tab, and `explore-*.js` for the forum/social surface). It talks to
`web` exclusively over the same authenticated HTTP/WebSocket API described
in this document — there is no separate frontend server or build step.

Navigation is four top-level tabs:

- **Dashboard** — wardrobe grid + add/edit, the recommendation request
  form, My Outfits, and the Outfit Builder (opened either empty or
  pre-filled from a recommendation result).
- **Explore** — the forum feed, post creation (with optional anonymity and
  "share an outfit"), comments, reactions, and Recreate-with-my-wardrobe.
- **Messages** — direct-message inbox and conversation view.
- **AI Chat** (`chat.js`) — an open-ended chat with the local LLM. Submits
  exclusively through the asynchronous `POST /chat/jobs` queue endpoint and
  polls `GET /chat/jobs/{id}` until the job reaches a terminal state
  (`completed`/`failed`), rendering a waiting state in between and a plain
  per-message error on failure — it never calls the synchronous `POST
  /chat` endpoint (see "Data flow: a chat message" below and
  `docs/features.md`'s "Local LLM & Chat" section for why that endpoint
  still exists).

These four sections carry real browser navigation/history state:
`explore-core.js` reflects the active one as a URL fragment
(`#dashboard`/`#explore`/`#messages`/`#chat`) via `history.pushState`/
`replaceState`, with a `popstate` listener restoring the right section on
Back/Forward and `boot()`/login reading the fragment back on a fresh load
or refresh — no backend route change, since `/ui/` still serves the same
`index.html` for every request and a fragment is never sent to the
server. Subtabs within a section (e.g. Dashboard's wardrobe/add-item
views, AI Chat's chat/recommendations views) are deliberately not part of
this history state, only the four sections above are.

A floating notifications panel and a live WebSocket connection sit outside
those tabs. Wardrobe photo analysis is surfaced as an ordinary UI action
embedded in the Dashboard's "Add a Clothing Item" form rather than as its
own tab — from the user's point of view there's no visible seam between
"regular" and "AI-assisted" functionality there; the distinction only
exists on the backend.

**Styling** (`style.css`) uses a curated system-font stack (an editorial
serif for display headings, a standard sans stack for everything
functional) rather than a webfont service — the app must work without
internet access except where actual functionality needs it (the local LLM,
image uploads), and decorative typography doesn't qualify.

## Data flow: a recommendation request

1. The user fills in the occasion/season/style filter form on the Dashboard
   and clicks "Generate Outfit"; the frontend calls `POST /recommendations`
   on `web`.
2. `web` loads the caller's wardrobe and builds a preferences profile from
   their saved outfits + feedback (learned-preference ranking) and their
   long-term memory profile.
3. `web` calls `recommendation`, which scores and selects candidate items
   from the wardrobe snapshot it was given (rule-based; a dress is treated
   as mutually exclusive with top+bottom by construction).
4. `web` re-verifies every returned item id against MongoDB (grounding),
   replacing any failed candidate with a verified fallback from the same
   user's wardrobe, or dropping the slot and recording it in
   `missing_types`.
5. `web` returns `{ items, explanation, missing_types }`, which the
   frontend renders in the Outfit Builder. The user either saves it as-is
   ("Save recommended outfit") or customizes it first — both write to the
   same Builder state and the same backend outfit model, and saving is a
   separate, later action from the recommendation call itself.

## Data flow: catalog suggestions

A separate, additive flow — never part of the recommendation request/
response above, and never required for it to succeed. The same
`catalog_gateway.py` client described here is also reused by chat's
product-question grounding — see "Data flow: a chat message" below.

1. If the recommendation response's `missing_types` is non-empty, the
   frontend calls `GET /recommendations/catalog-suggestions?clothing_type=
   <type>` on `web`, once per missing type — passing the same `season` the
   user's current recommendation request used (via `#r-season`'s value,
   `''` if none was selected, in which case no `season` param is sent at
   all — see "Season verification" below). See step 4a below for what the
   backend does with it.
2. `web` (via `services/catalog_gateway.py`) calls `catalog`'s
   `POST /crawl` over the internal Docker network with a timeout well
   above `catalog`'s own worst-case retry/backoff duration.
3. `catalog` fans out concurrently (bounded by a semaphore) to `mock_demo`
   and the four real Shopify-store adapters, retrying transient failures
   and isolating one source's failure from the others, and returns
   normalized products + per-source status. Each real Shopify adapter also
   independently, best-effort enriches every product with a trusted
   `season` classification — see "Season verification" below; a failure to
   fetch that enrichment never fails the crawl itself.
4. `web` validates each returned product defensively (allowlisted fields
   only, including `season`) and drops anything malformed, individually,
   without failing the request.
4a. **Season verification, when `season` was requested.** `web`
   (`catalog_gateway.py::season_matches`) narrows `products` to only those
   with trusted season evidence confirming the request — either an exact
   match, or the documented `spring→summer`/`autumn→winter` fallback
   equivalence (winter/summer requests have no fallback). If at least one
   such product exists, `season_verified: true` and `products` is that
   verified subset only. If none exist, `web` falls back to the full,
   unfiltered clothing-type-only `products` list with `season_verified:
   false` — the two are never mixed in one response.
5. `web` returns `{ clothing_type, products, catalog_available,
   season_requested, season_verified }` to the client. If `catalog` was
   unreachable, timed out, or returned something unusable at any point
   above, this becomes a controlled `200` with `catalog_available: false`
   and `products: []` — never an error, and with **no effect whatsoever**
   on the wardrobe/recommendation data already shown to the user.
6. The frontend renders a small product row if `catalog_available` is true
   and `products` is non-empty; otherwise it renders nothing extra — the
   missing-type banner from step 1 is unaffected either way. The row's
   label depends on `season_verified` (`app.js::shopSuggestionsLabel`):
   `season_requested` truthy and `season_verified` true → seasonal wording
   ("Winter top"); `season_requested` truthy and `season_verified` false →
   the original generic "Shop for a &lt;type&gt;" wording plus a small
   "(season not verified)" hint; no `season_requested` at all (unaffected,
   original behavior) → the plain generic wording with no hint. Verified
   and unverified results are never labeled the same way.

Nothing here touches MongoDB, the `recommendation` service, or `llm` —
products are never generated, described, or invented by the LLM; they come
only from `catalog`'s own crawled/normalized data, re-validated by `web`.

### Season verification

Trusted season data comes exclusively from a manually reviewed, per-store
allowlist of real Shopify collection handles
(`services/catalog/app/services/adapters/shopify_stores.py::
ShopifyStoreConfig.season_collections`) — never from title/tag/description
keyword guessing. `ShopifyAdapter` fetches each configured season
collection's product membership (best-effort; a single collection's
failure only leaves that season's evidence empty, never fails the whole
store's crawl) and tags each product `winter`/`summer`/`spring`/`autumn`/
`all`/`unknown` accordingly; a product found in more than one trusted
season collection is reported `unknown` rather than guessed. As of this
writing only two of the four real stores have any reviewed season
collections at all (Shtaim: winter/summer/autumn; Kahiko: winter only) —
PIPA and ATA have none configured (ATA's season-named collections are
sale/clearance events, not curated seasonal assortments, and were
deliberately excluded), so every one of their products is `unknown`. The
`GET /recommendations/catalog-suggestions?season=` query param, the
`season_requested`/`season_verified` response fields, and the frontend's
use of both (step 1 and step 6 above) are all wired end to end and
covered by tests. Only Shtaim and Kahiko can currently produce a
`season_verified: true` result — a season-filtered request against PIPA/
ATA-only results always falls back to the unverified, generic wording.

## Data flow: a chat message

1. The user sends a message from the AI Chat tab (`app/static/chat.js`);
   the frontend always submits through the asynchronous `POST /chat/jobs`
   queue endpoint — never the synchronous `POST /chat` endpoint, which
   stays in the API for direct/API-level callers and tests only (see
   `docs/features.md`'s "Local LLM & Chat" section). Steps 2–6 below build
   the prompt the same way regardless of which endpoint is used; steps 7–8
   describe the job/worker pipeline that both endpoints now go through —
   see "Both endpoints share one job/worker pipeline" below for exactly how
   they differ.
2. `web` loads the caller's long-term memory profile.
3. `web` runs deterministic, rule-based catalog-relevance detection
   (`services/chat_catalog_grounding.py`) against the raw message — no
   extra LLM call is used for this step. It recognizes a clothing type
   (top/bottom/dress/outerwear/shoes/accessory) plus either one of the four
   allowlisted store names or a purchase/existence-intent phrase
   ("does X sell...", "do you have...", "can I buy...", "real
   shoes/shirts/...", price/product/shop/store wording). Ordinary
   conversation and general style questions (no clothing type, or a
   clothing type with no store/purchase signal) never match, and the
   catalog service is **not** called for them.
4. **Only when relevant:** `web` calls the same `catalog_gateway.py` used
   by the recommendation flow (see "Data flow: catalog suggestions" above)
   — restricted to the named store if one was mentioned, or all four real
   allowlisted stores otherwise (never the `mock_demo` source, so a
   product-verification chat answer is never grounded in demo data). The
   returned products are filtered locally by any color/material terms in
   the message, then rendered into a bounded, sanitized `[CATALOG_CONTEXT]`
   prompt block with one of three explicit, distinct outcomes: `verified`
   (real matching products listed), `no_match` (the catalog was checked and
   nothing matched — the LLM is told not to invent a product or confirm the
   user's claim), or `unavailable` (the catalog couldn't be reached/timed
   out/is disabled — the LLM is told it cannot make a specific availability
   claim right now). Third-party product text is treated as data, never as
   instructions, and catalog-context delimiter tokens are stripped from it
   so a crawled product title can't forge a fake block boundary. The exact
   structured products retrieved here (`CatalogGrounding.products`, possibly
   empty) are kept for step 8's reply-side check below — not re-fetched
   later, so that check always validates against what the prompt was
   actually built from.
5. `web` builds the final prompt (`services/chat_context.py`), folding in
   the catalog-context block (if any) ahead of the memory-preference block,
   with prompt-injection hardening on every block.
6. `web` persists the built prompt, the original user message, and the
   step-4 catalog products as a real `chat_jobs` document
   (`chat_queue.py::create_chat_job`) — for **both** endpoints, not only
   `POST /chat/jobs`. See "Both endpoints share one job/worker pipeline"
   immediately below for what happens to that document next.

### Both endpoints share one job/worker pipeline

`POST /chat` and `POST /chat/jobs` differ only in how the job created in
step 6 is claimed and awaited — from `chat_worker.py::run_claimed_job`
onward, every job (regardless of which endpoint created it) goes through
the identical LLM-call → reply-normalize → hallucination-guardrail →
complete-or-fail → memory-learn pipeline, under the same
`CHAT_WORKER_CONCURRENCY` shared concurrency accounting:

- **`POST /chat/jobs`** returns `202` immediately after step 6 and does
  **not** wait for the job to be processed. The job sits in the shared
  priority/FIFO queue until one of `CHAT_WORKER_CONCURRENCY` background
  worker loops (running inside the `web` process) atomically claims it —
  claims are ordered `priority` DESC, `created_at` ASC; every public job
  shares one priority class today, see `docs/features.md`'s "Local LLM &
  Chat" section. The frontend then polls `GET /chat/jobs/{id}` until the
  job reaches a terminal state.
- **`POST /chat`** does *not* wait in that shared queue. It immediately
  calls `chat_queue.py::claim_job_by_id` to claim the *one specific job it
  just created in step 6* — never anyone else's queued job — then calls
  `chat_worker.py::run_claimed_job` on it synchronously, in the same HTTP
  request, and returns the result directly. It still goes through every
  stage of the same pipeline (including step 8's reply validation below)
  and the same shared concurrency limit as a background-processed job; the
  only thing it skips is standing in line behind other pending jobs.

Either way, `run_claimed_job` (step 7 below) is the one place the LLM is
actually called and the reply is actually validated — there is no code
path where a chat reply reaches the client without going through it.

7. `run_claimed_job` calls `llm` (via a shared gateway that classifies
   failures into a stable reason code), which forwards the already-built
   prompt to `ollama`, and normalizes the raw reply
   (`chat_text_formatting.py`).
8. **Chat catalog-reply validation (code-level, not just a prompt
   instruction).** Before the reply is stored as the job's result,
   `chat_catalog_grounding.py::validate_grounded_reply` checks it against
   the exact catalog products retrieved in step 4 and stored on the job in
   step 6. If step 4 never ran (an ordinary, non-product message), the
   reply is returned unchanged. If step 4 ran and found real, verified
   products (`STATUS=verified`), the reply is **unconditionally replaced**
   with a code-rendered listing built only from those products' own
   trusted fields (name, color, material, season, price/currency,
   availability, `source_url`) — never the model's own generated text, even
   when it happens to cite the correct URL. This closes a narrower gap than
   URL-matching alone: a reply can carry the right link while still
   misstating a color or price, and nothing previously checked that: this
   step means an LLM never originates a real product's facts at all,
   mirroring how the Dashboard's own catalog cards (`GET
   /recommendations/catalog-suggestions`) never involve an LLM for product
   facts either. Any conversational framing the model wrote is discarded
   along with it, deliberately — splicing in only part of a generated reply
   would still let the model misstate a fact in the part kept. If step 4
   ran but found nothing verified (`no_match`/`unavailable`/
   `needs_clarification`), the reply is **always** replaced with an
   honest, deterministic, status-specific message ("I don't have a
   verified product…", "the store catalog couldn't be checked…", or a
   request to clarify the item type). Free text can't be reliably
   classified as "safe general advice" versus "an unsupported product
   claim" (a fabricated claim need not contain a URL at all), so no model
   text is passed through in this state.
9. A separate background reaper task recovers any job left stuck
   "processing" by a killed/crashed worker (see `docs/features.md`) so a
   job can never be stranded permanently.
10. After a reply is generated, the user's own message (never the
    assistant's reply) is reviewed for an explicit, durable preference; if
    one passes validation, it's merged non-destructively into the user's
    memory profile.

## Deployment, scaling & persistence

Which state must persist (`mongo_data`, the uploads bind mount, `.env`),
the backup/restore scripts, and recovery procedures are documented
separately in `docs/deployment.md`, since that document covers operational
procedure, not system design.

In production (`docker-compose.prod.yml`, not used locally), a `caddy`
reverse proxy is the only container publishing host ports (80/443) and
terminates HTTPS; `web` is not reachable directly, only via `caddy`. See
`docs/deployment.md` for the full HTTPS/reverse-proxy design.

**Horizontal `web` scaling.** `web` has no fixed `container_name` (unlike
every other service), so `docker compose -f docker-compose.yml -f
docker-compose.prod.yml up -d --scale web=N` genuinely runs N independent
`web` processes — each fully stateless (see "Shared, replica-safe rate
limiting" and "Cross-replica live WebSocket notification delivery" above,
plus the central MongoDB persistence). `docker-compose.prod.yml` also
declares `scale: 2` under `services.web`, so two replicas are the
*default* for a normal production `up -d` with no `--scale` flag (an
explicit `--scale web=N` still overrides it for a one-off run). See
`docs/deployment.md`'s "Horizontal `web` scaling" section for the
operational detail.

`Caddyfile`'s `reverse_proxy` uses Caddy's built-in `dynamic a web 8000`
upstream source rather than a literal `web:8000` address: a literal
address was verified live to resolve the `web` DNS name exactly once and
keep proxying to that single resolved replica forever, even with several
replicas healthy — `dynamic a` instead re-resolves Docker's embedded DNS
for the `web` service every `refresh` interval (5s) and load-balances
(`lb_policy round_robin`) across every A record it returns — one per
currently-healthy replica. This is the only component that had to change
for horizontal scaling; nothing in `services/web`'s Python code needed to,
since the shared-Redis and shared-Mongo design already made every replica
safe to run concurrently. The multi-replica topology was verified live on
a Docker host: two simultaneous healthy replicas, traffic round-robining
between them, a JWT issued via one replica staying valid on the other, one
Redis-backed rate-limit ceiling enforced across both, and a live WebSocket
notification delivered to a socket on one replica when the triggering
action was handled by the other.
