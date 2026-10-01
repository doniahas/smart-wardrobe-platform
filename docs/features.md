# Features

## Status legend

| Status | Meaning |
|---|---|
| ✅ Complete | Implemented, working, and has automated test coverage. |
| 🟡 Partial | Implemented but with a known, specific gap (missing piece named below). |
| 🔵 Planned | Not implemented yet; there is a concrete design or decision for it. |
| ⚪ Deferred | Not implemented; explicitly postponed with no current timeline. |
| ⚫ Removed | Existed at some point but intentionally dropped. |

### Implementation status vs. validation status

The icons above describe **implementation** status and **automated** test
coverage only. They never imply that a feature has been exercised by hand,
in a real browser, against real running services and a real model.

Where a feature is implemented and automated-tested but has not yet had
that manual pass, it also carries **⏳ Manual validation pending**. That
tag is not a known bug or a suspected defect — it means a specific check
has not been performed yet. The exact outstanding checks are listed in
`docs/testing.md`'s "Manual validation pending" section.

---

## Authentication & Authorization — ✅ Complete

The user registers and logs in through a form on the landing page; every
other screen (wardrobe, outfits, recommendations, chat, forum) is only
reachable once logged in, and each user only ever sees their own data.

- `POST /auth/register`, `POST /auth/login`, `GET /auth/me`.
- Passwords hashed with bcrypt (`passlib`); never stored or returned in
  plaintext.
- JWT (HS256, `python-jose`) with an `exp` claim; a single `except JWTError`
  handler covers expired/malformed/wrong-signature tokens uniformly, and the
  user is re-fetched from the DB on every request (so a token for a deleted
  user is rejected).
- Every protected resource is scoped to the authenticated user server-side;
  cross-user access and "not found" both return 404.

## Wardrobe — ✅ Complete

On the Dashboard, the user builds their digital closet: an "Add a Clothing
Item" form (with optional photo) adds items to a grid, each item can be
edited or deleted in place, and the grid can be filtered by type/color/
season/style/occasion.

- `POST /wardrobe`, `GET /wardrobe` (filterable by type/color/season/style/
  occasion), `GET /wardrobe/{id}`, `PUT /wardrobe/{id}` (partial update,
  optional image replace), `DELETE /wardrobe/{id}` (cascades: removes the
  item from any outfit referencing it, deletes any outfit left with zero
  items plus its feedback docs).

- **Analyze Item — AI-assisted single-item analysis — ✅ Complete · ⏳
  Manual validation pending.** When adding an item, the user can upload a
  photo and have the form pre-filled automatically instead of typing
  everything by hand.

  ```text
  user uploads one clothing photo
    → POST /wardrobe/analyze-image           (image held in memory only)
    → local moondream vision model            (via services/llm → ollama)
    → structured attributes suggested         (WardrobeAiSuggestion)
    → add-item form is pre-filled
    → user reviews and edits every field
    → user submits POST /wardrobe             ← the only thing that saves
  ```

  - Suggested fields: `name`, `clothing_type`, `color`, `season`, `style`,
    `occasion`, plus the `model` that produced them.
  - **The AI never persists an item.** `POST /wardrobe/analyze-image`
    writes nothing to disk or the database — the photo is validated and
    held in memory for the duration of the request only. An item exists
    solely because the user submitted the form themselves.
  - An unrecognized enum value or a blank string from the model becomes
    `null` rather than being guessed, and the UI labels the more
    subjective fields (season/style/occasion) as suggestions to
    double-check.
  - Malformed model output is not an error — it returns all-`null`
    suggestions, since the call itself succeeded. Only a genuine
    llm-service failure surfaces as an HTTP error (503/504/502).
  - ~30–45s per analysis on CPU-only hardware (measured during
    development).
  - Only single-item photos are supported — the former multi-garment
    "Analyze Full Outfit" path and its photo-based Recreate
    counterpart were removed; see "Outfit Recreation" below for
    the current, structured-data-only recreation contract.

## Outfits & Outfit Builder — ✅ Complete

"My Outfits" is where every outfit a user has saved lives, however it was
created. A "+ New Outfit" button opens the Outfit Builder, a single screen
where the user picks items from their own wardrobe (grouped by clothing
type) and sees a flat-lay preview as they go; the same screen also opens
pre-filled when customizing a recommendation, or when editing an
already-saved outfit.

- `POST /outfits`, `GET /outfits`, `GET /outfits/{id}`,
  `PATCH /outfits/{id}` (partial edit), `DELETE /outfits/{id}`.
- Server-side integrity validation backs the Builder's on-screen rules: no
  duplicate item ids within an outfit, per-clothing-type cardinality caps,
  dress mutually exclusive with top+bottom.
- Recommendations and manual outfit management share this one creation/edit
  surface and one backend model rather than two disconnected flows — see
  `architecture.md`'s recommendation data-flow for how a generated result
  lands here.

## Recommendation Engine — ✅ Complete

The user picks an occasion/season/style on the Dashboard and clicks
"Generate Outfit"; the result renders as a complete outfit in the Outfit
Builder with a plain-language explanation of why it was picked, ready to
save as-is or customize first.

- `POST /recommendations` — rule-based scoring (`services/recommendation`)
  on occasion/season/style match, with a dress treated as mutually
  exclusive with top+bottom by construction.
- **Grounding / hallucination robustness:** every returned item id is
  re-verified against MongoDB (existence + ownership) before being
  returned. A failed candidate is auto-replaced with a verified fallback
  from the same user's wardrobe matching the request + their preferences;
  if none exists, the slot is omitted, never invented, and reported via
  `missing_types` (`[]`, `["top"]`, `["bottom"]`, or `["top","bottom"]`).
- ⚪ **Not implemented:** grounding recommendations in real-world weather
  data is a possible future enhancement. (Clothing-site crawling *is*
  implemented — see "Catalog / Store Product Suggestions" below.)

## Personalization & Long-Term Memory — ✅ Complete

From the user's side, this shows up as recommendations that quietly get
better the more the app is used — saving/rating outfits, liking or
disliking a suggestion, filling in a preferences form, or just chatting
about what they like all feed the same underlying profile, with no separate
"train the AI" step. Four layers, all scoped strictly to the authenticated
user:

1. **Learned-preference ranking** — a per-request ranking boost derived
   from the user's own saved outfits and feedback (color/style/occasion/
   season/type), applied on top of the base rule-based score. Never
   overrides the requested filters; a user with no history gets the
   original unpersonalized ranking.
2. **Explicit long-term memory** — `GET`/`PUT`/`PATCH /memory`: preferred
   styles, liked/disliked colors, disliked item types, preferred occasions,
   fit, communication style, a bounded list of generic `additional_
   preferences` (see below), free-text notes. Disliked colors/types are
   hard exclusions in recommendations; preferred styles/occasions are soft
   ranking bonuses. A color can never be both liked and disliked at once
   (validated on write, including across a `PATCH` merge).
3. **Automatic learning from chat** — after each chat exchange, the user's
   own message (never the assistant's reply) is reviewed for an explicit,
   durable preference (e.g. "I hate black clothes"), as opposed to a
   one-off situational request (e.g. "I need a jacket today"). A validated
   extraction is merged into the user's memory — every list only ever
   grows (nothing already saved is silently cleared), and a single
   extraction can never itself create a liked/disliked conflict (validated
   the same way as a manual `PUT`/`PATCH`). **Preference reversal:** when
   a later, separate durable statement contradicts an
   earlier one for the same color (e.g. "I hate black" followed, in a
   later message, by "I've changed my style, I like black clothes now"),
   the newer statement wins — the color moves from `disliked_colors` to
   `liked_colors` (or vice versa) instead of the older value silently
   overriding it forever (`services/personalization.py::
   merge_chat_memory_candidate`'s opt-in `allow_color_preference_reversal`,
   used only by chat-driven learning — the separate, lower-trust
   recommendation-feedback learning path below keeps its original,
   conservative never-overwrite behavior for a single item's implicit
   signal).
   **Generic durable preferences:**
   a durable preference that genuinely doesn't fit any structured field
   (e.g. "From now on I prefer modest, more covered clothing", "I prefer
   clothes that are easy to put on with one hand") is captured via
   `additional_preferences` (`app/models.py::GenericPreference`) - a
   bounded list of short, topic-keyed `{topic, preference}` entries
   (max 10 entries, max 60/200 characters, control-character-stripped,
   `extra="forbid"`), never unrestricted free text. A later durable
   statement about the *same* topic replaces its value (the same
   "later statement wins" principle as colors/fit/communication style);
   an unrelated topic is left untouched. Folded into the chat prompt by
   `chat_context.py::build_chat_prompt` (re-sanitized/delimiter-stripped
   again at render time, defense-in-depth) alongside every other memory
   field, so it actually reaches later conversations - not just stored and
   forgotten. Deliberately **not** folded into recommendation scoring, for
   the same reason `preferred_fit`/`communication_style` already aren't
   (no corresponding wardrobe-item attribute to score against).
4. **Recommendation-level feedback** — `POST /recommendation-feedback`
   lets a user like/dislike a *fresh, unsaved* recommendation directly
   (distinct from feedback on an already-saved outfit), with structured
   reason-scoping, feeding its own learning path back into the memory
   profile.

## Robustness to Hallucinations — ✅ Complete (wardrobe grounding + catalog-grounded chat)

In practice this means two things. First, a recommendation the user sees
will never reference an item they don't actually own (or no longer own) —
if the engine's pick turns out to be stale, the app quietly substitutes a
real item from their own wardrobe instead of showing something fictional,
or tells them what type of item is missing rather than making one up.
Second, when the user asks the chat assistant about a real external
product ("Does ATA sell a black linen shirt?", "I saw a red silk skirt at
PIPA for 100 NIS — is it a good choice?"), the assistant no longer answers
from an unverified guess — it's grounded in the same real, crawled catalog
data the recommendation flow uses.

- Wardrobe-fact grounding (see Recommendation Engine above) is fully
  implemented and tested.
- **Chat catalog grounding (`POST /chat` and `POST /chat/jobs`) is
  implemented and tested.** A deterministic, rule-based check
  (`services/chat_catalog_grounding.py` — no extra LLM call) decides
  whether a chat message concerns a real external product; only then does
  `web` call the catalog service (via the same gateway the recommendation
  flow uses) and fold a bounded, sanitized `CATALOG_CONTEXT` block into the
  prompt before it reaches the LLM, with four explicit, distinct outcomes:
  - **verified** — real, matching products from the catalog are listed;
    the LLM is instructed to answer only from what's listed.
  - **no_match** — the catalog was checked and nothing matched; the LLM is
    instructed to say so plainly, not invent a product or confirm the
    user's own claim about one.
  - **unavailable** — the catalog couldn't be reached/timed out/is
    disabled; the LLM is instructed to say availability/price/material
    couldn't be verified right now, not to make a specific claim either
    way.
  - **needs_clarification** — the message
    names one of the four real stores and uses clear purchase/existence-
    intent wording (e.g. "I'm pretty sure PIPA sells this red silk piece —
    is that true?"), but no clothing-type keyword was recognized, so no
    catalog call was made at all (the catalog service requires an item
    type to crawl for). The LLM is told plainly that nothing was checked,
    instructed not to confirm the user's claim, and asked to request the
    item type instead of guessing.

  Ordinary conversation and general style questions ("What colors work
  well with navy?", "Hello, how are you?") never trigger a catalog call at
  all. **Known limitation:** clothing-type detection is deterministic
  keyword/phrase matching over a fixed vocabulary (six clothing types with
  simple pluralization) — it reliably covers the supported product phrasing
  this feature targets, but it is not a general-purpose NLP fact-checker
  for every possible noun a user might use for a garment. This does not
  mean a real-store product claim can reach the LLM completely
  ungrounded: a named store plus purchase/existence intent — or a strong,
  store-less shopping phrase such as "where can I buy a kaftan?" —
  always produces at least a needs_clarification block, even when the
  item type itself isn't recognized.
- **Code-enforced reply validation, not just a prompt instruction.** The
  four outcomes above only ever *ask* the model (via the prompt) to answer
  from what's listed. `chat_catalog_grounding.py::validate_grounded_reply`
  is the actual enforcement, checked against the exact catalog products
  retrieved for that request (never re-fetched, so it validates against the
  same crawl the prompt was built from). When real, verified products were
  retrieved, the reply is **unconditionally replaced** with a code-rendered
  listing built only from those products' own trusted fields (name, color,
  material, season, price/currency, availability, `source_url`) — never the
  model's free text, even when it correctly cites the right URL, since a
  reply can carry the right link while still misstating another fact.
  When grounding ran but nothing was verified (`no_match`/`unavailable`/
  `needs_clarification`), the reply is always replaced with an honest,
  status-specific "nothing verified" / "catalog unavailable" /
  "please clarify the item type" message — a fabricated claim need not
  contain a URL, so no model text is passed through in that state. Either
  way, a replacement is never edited in place — the whole reply is swapped.
  Applied identically to both `POST /chat` and `POST /chat/jobs`, since
  both now run through the same `chat_worker.py::run_claimed_job` pipeline
  (see "Chat-queue unification" below and `docs/architecture.md`'s "Data
  flow: a chat message").
- ⚪ **Not implemented:** grounding chat/recommendations in live weather
  data (see above).

## Catalog / Store Product Suggestions — ✅ Complete

Clothing-site web crawling, demonstrating asynchronous HTTP, bounded
parallelism, and defensive handling of third-party data. From the user's
side: after generating a recommendation, if the
wardrobe is missing a top or bottom (`missing_types` non-empty), the app
can show real store products for that missing type alongside the
recommendation, each linking out to the actual product page. The same
crawled catalog data also grounds product-specific AI chat answers — see
"Robustness to Hallucinations" above.

- A dedicated `catalog` service crawls product data from a deterministic
  mock source (`mock_demo`) and four real, allowlisted Shopify stores
  (Shtaim, PIPA Fashion, Kahiko Swimwear, ATA), asynchronously and with
  bounded concurrency, retry/backoff, and per-source health tracking
  (`GET /sources`).
- `GET /recommendations/catalog-suggestions` — a separate, authenticated,
  rate-limited endpoint in `web` that calls `catalog` over the internal
  Docker network and returns validated products for one `clothing_type` at
  a time. Deliberately **not** a field on `POST /recommendations`'s own
  response, so that endpoint's contract stays byte-for-byte unchanged.
  Two frontend surfaces now consume this one endpoint: the Dashboard
  recommendation panel (driven by `missing_types`) and the Outfit Builder
  when opened from Recreate (driven by unmatched components — see "Outfit
  Recreation" below). Each owns its own container and state; the shared code is only
  the fetch/render helpers in `app.js`.
  **Real-store scoping:** this
  route always restricts the catalog request to the four real
  allowlisted stores (`REAL_STORE_SOURCES`, `catalog_gateway.py`),
  mirroring the same scoping chat catalog grounding already used, and
  additionally drops any returned product whose source isn't one of those
  four as a second, defense-in-depth layer
  (`catalog_gateway.py::real_store_products_only`) — the catalog service's
  deterministic `mock_demo` fixture source can never reach a real user
  through this route, even though `mock_demo` itself remains registered
  and available (e.g. for the disposable, offline E2E environment
  described below and in `docs/testing.md`).
- **Graceful degradation:** if `catalog` is unreachable, times out, or
  returns something malformed, the endpoint returns a controlled `200`
  (`catalog_available: false`, `products: []`) — never an error, and with
  zero effect on the recommendation flow itself.
- **Stateless, no caching:** both `catalog` and `web`'s integration with it
  are stateless — every request live-queries the configured sources. There
  is **no MongoDB-backed product cache or TTL revalidation** anywhere in
  this system; each call reflects a fresh crawl, never a stored result.
- Products are never generated or described by the LLM — `catalog`'s
  crawled/normalized data is the only source of a product's name, price, or
  link; `services/llm` has no involvement in this feature.
- **Season verification.** Each real Shopify adapter best-effort classifies
  every product's `season` (`winter`/`summer`/`spring`/`autumn`/`all`/
  `unknown`) from a manually reviewed, per-store allowlist of real
  collection handles (`shopify_stores.py::ShopifyStoreConfig.
  season_collections`) — never from title/tag/description keyword
  guessing. `GET /recommendations/catalog-suggestions` accepts an optional
  `season` query param: when given, it prefers products with trusted
  evidence confirming that season (an exact match, or the documented
  `spring→summer`/`autumn→winter` fallback — see `docs/architecture.md`'s
  "Season verification"), and returns `season_verified: true` only for
  that confirmed subset. If no verified match exists, it falls back to the
  unfiltered clothing-type-only result with `season_verified: false` —
  never presented as a confirmed seasonal match. As of this writing only
  Shtaim (winter/summer/autumn) and Kahiko (winter) have any reviewed
  season collections; PIPA and ATA have none configured, so their products
  are always `unknown`.
  The Dashboard's recommendation flow (`app.js`) passes the user's own
  current recommendation-request season straight through to every
  catalog-suggestions call it makes (`''`/unselected → no `season` param
  at all, unchanged from before this feature) and labels the result
  accordingly: `season_verified: true` → seasonal wording (e.g. "Winter
  top"); `season_verified: false` with a season requested → the original
  generic "Shop for a &lt;type&gt;" wording plus a small "(season not
  verified)" hint; no season requested → the original generic wording,
  unchanged. The Outfit Builder's own catalog-suggestions call (driven by
  Recreate's unmatched components, not a recommendation-request season) is
  untouched by this and keeps requesting exactly the pre-existing
  clothing-type-only shape.

## Local LLM & Chat — ✅ Complete

The user has an open-ended chat conversation with the app (advice, "what
goes with X", general questions), fully offline-capable since it never
leaves the local machine/network. The assistant already knows the user's
stated style preferences without being told them again each time.

- `services/llm` talks only to a local Ollama container — no hosted/
  external LLM API anywhere in the system.
- **Frontend:** the browser app has a
  dedicated "AI Chat" tab (`services/web/app/static/chat.js`) that is the
  normal, user-facing way to chat. It submits exclusively through
  `POST /chat/jobs` and polls `GET /chat/jobs/{job_id}` until the job
  reaches a terminal state, showing a waiting/processing state in between,
  a plain per-message error on `failed` (mapped from `error_reason` to a
  short human sentence, never a raw code), and disabling the input while a
  job is in flight to prevent accidental duplicate submissions. It never
  calls the synchronous endpoint below.
- **Plain-text replies:** the frontend renders a
  reply with `el.textContent` (safe against HTML/script injection - no
  `innerHTML`, no Markdown parser), so `PLAIN_TEXT_STYLE_INSTRUCTION`
  (`services/chat_context.py`) always leads the prompt for both `POST /chat`
  and `POST /chat/jobs`, asking the model to avoid Markdown
  (`**bold**`/`__bold__`/`#`/`##` headings) in favor of plain sentences,
  numbered lists, or `-` bullets. `services/chat_text_formatting.py::
  normalize_chat_reply` is a conservative fallback applied to the model's
  actual reply at both call sites (never inside the shared
  `llm_gateway.py::call_llm`, so `chat_memory_learning.py`'s structured-JSON
  extraction call is untouched) - it strips only `**`/`__` emphasis markers
  and leading `#` heading markers, never fenced code, list markers, or
  ordinary punctuation.
- **Chat-queue unification:** `POST /chat` and `POST /chat/jobs` both
  persist a real `chat_jobs` document and both run through the exact same
  `chat_worker.py::run_claimed_job` pipeline (LLM call → reply normalize →
  hallucination guardrail, see below → complete-or-fail → memory learn),
  under the same shared `CHAT_WORKER_CONCURRENCY` concurrency accounting —
  see `docs/architecture.md`'s "Data flow: a chat message" for the full
  step-by-step. **Neither endpoint bypasses that pipeline.** They differ
  only in whether the job waits its turn:
  - `POST /chat` — synchronous, memory-aware (folds in the caller's
    long-term memory profile with prompt-injection hardening),
    rate-limited. Creates its job, then immediately claims *that specific
    job* (`chat_queue.py::claim_job_by_id`) and runs it synchronously in
    the same HTTP request — it never waits in the shared priority queue
    behind other pending jobs, but every stage of the pipeline above still
    runs. Kept in the API for backward compatibility and for tests/direct
    API callers that want an immediate reply without polling (e.g.
    `test_chat_route.py`, `test_chat_catalog_grounding_route.py`,
    `test_chat_queue_unification.py`) — the browser UI never calls it (see
    the frontend bullet above and `test_chat_frontend_route.py`, which
    asserts `chat.js` contains no call to it).
  - `POST /chat/jobs` / `GET /chat/jobs/{job_id}` — asynchronous: persists
    a job to `chat_jobs`, then returns immediately; the job is later picked
    up by whichever of the `CHAT_WORKER_CONCURRENCY` background worker
    loops per `web` process claims it (no two workers can claim the same
    job), waiting in the shared priority/FIFO line described below. Graceful
    shutdown (in-flight jobs finish or reach a terminal state before the
    process exits, within `CHAT_WORKER_SHUTDOWN_TIMEOUT_SECONDS`) is
    validated with real in-flight jobs across a `docker compose
    stop`/restart cycle.
- **Priority-queue policy** (`services/chat_queue.py`): claims are ordered
  by `priority` DESC, then `created_at` ASC (FIFO within a priority).
  `POST /chat/jobs` never accepts a client-supplied priority — `ChatRequest`
  has no such field — so every public job is the same
  `DEFAULT_JOB_PRIORITY` (0) class; a validated, bounded `priority` argument
  on `create_chat_job` exists only as a server-controlled hook for internal
  callers, not for end users or manufactured tiers. A job the stale-job
  reaper recovers (see below) is requeued at a fixed, lower
  `RETRY_JOB_PRIORITY` (a single non-cumulative demotion step, never
  promoting an already-lower-priority job) so one problematic retry doesn't
  sit ahead of fresh interactive requests — a real, bounded policy, not
  manufactured user/product tiers.
- **Stale/interrupted job recovery:** a separate
  background reaper task (`start_stale_job_reaper`) periodically recovers
  jobs left stuck "processing" by a killed/crashed worker —
  `CHAT_JOB_STALE_TIMEOUT_SECONDS` (default 2x `LLM_SERVICE_TIMEOUT_SECONDS`,
  and app startup refuses to run if it's set below 1.5x
  `LLM_SERVICE_TIMEOUT_SECONDS` — see `validate_chat_job_stale_timeout` in
  `services/web/app/core/config.py`) past `started_at` and still under
  `CHAT_JOB_MAX_ATTEMPTS` claims, a job is returned to "pending"; past the
  attempt limit, it's marked permanently "failed"
  (`error_reason="stale_abandoned"`) instead of recycling forever.
  `reclaim_stale_jobs`'s own pending/failed transitions are atomic and
  filtered on the job's current `status`, so it can never race itself into
  a double transition — but status alone is **not** sufficient to protect
  `complete_job`/`fail_job` once a job can be reclaimed: after a reclaim,
  status cycles back to "processing" under a *newer* claim, so a late write
  from the *original* (now-obsolete) claim would still match a status-only
  filter. `complete_job`/`fail_job` therefore also require the job's stored
  `attempts` counter to equal the exact value the caller's own
  `claim_next_job` call returned (its **claim-fencing token**) — an
  obsolete claim's write becomes a safe no-op instead of corrupting a
  newer claim's result. `complete_job`/`fail_job` return whether *this*
  call actually performed the transition, and the worker only runs its
  post-completion memory-learning side effect (layer 3 of Personalization
  above) when that's `True` — fencing therefore also stops a stale,
  fenced-out claim from learning memory for a job it no longer owns, not
  just from corrupting the stored result. See `services/chat_queue.py`'s
  module docstring ("Claim fencing" section) and `test_chat_queue.py`'s /
  `test_chat_worker.py`'s dedicated fencing tests.
- Jobs are ownership-scoped like every other resource.

## Online Forum / Explore & Social — ✅ Complete

- The forum includes posts, comments, reactions, image **and MP4 video**
  attachments, direct messages, notifications, live updates, user
  discovery, and Recreate with My Wardrobe.
- ✅ **Video attachments** — MP4 only, on posts, comments, and direct
  messages, with a bounded MP4/ISO-BMFF structural
  validator that requires a valid `ftyp` box plus both a `moov` and an
  `mdat` box genuinely present (order-independent) — a syntactically
  valid but non-media box such as `free`/`skip` is not enough by itself
  (rejects spoofed/malformed containers, never trusts filename or
  client-declared Content-Type) — and independently configurable size
  caps. See "Media" below and `docs/security-testing-report.md`.
- ✅ **Cold-seed demo data (automatic)** — `scripts/seed_demo_data.py`
  populates a few deterministic demo users, posts (anonymous and non-anonymous), comments, and reactions
  via the real HTTP API, idempotently and safely re-runnable after a
  partial/interrupted run. A one-shot `seed` service in `docker-compose.yml`
  now runs it automatically on every local/dev `docker compose up -d`
  (gated by `SEED_DEMO_DATA`, hard-disabled in `docker-compose.prod.yml`),
  so a fresh local install is populated with **no manual step
  required**. Verified via automated tests, an isolated real-Mongo/
  real-HTTP stack (idempotency and partial-run-recovery), and a
  real-browser pass confirming the seeded content renders in the Explore
  feed. See "Cold-seed demo data" below.
- ✅ **User discovery for direct messages** — a `GET /users`/
  `GET /users?search=` authenticated user-discovery route plus a "Start a
  new conversation" picker in the Messages tab, so a client can message
  any other registered client (not only someone who has authored a
  visible, non-anonymous post). `scripts/start_demo.sh` is a one-command
  local demo startup wrapper that leaves ordinary `docker compose up -d`/
  production/CI behavior unchanged.

The Explore tab is a small social feed: users post (optionally
anonymously), optionally attach a saved outfit or photos, comment, and
react; the Messages tab holds one-to-one direct conversations; a
notification bell and live WebSocket updates surface new likes, comments,
and messages without a page refresh. "Recreate with My Wardrobe" lets any
viewer see how well a shared outfit matches their own closet.

- **Posts** (`routers/posts.py`) — create/list/personal-area, optional
  anonymity (real author retained server-side for ownership checks only,
  never exposed publicly).
- **Reactions** (`services/reactions.py`) — like/dislike on posts and
  comments, one per user per target, toggle/switch semantics; counts always
  derived from stored `reactions` documents, never a denormalized counter.
- **Comments** (`routers/comments.py`) — flat, non-anonymous, immutable,
  author-owned delete, cascading cleanup when a post is deleted.
- **Media** (`services/image_storage.py`) — up to 4 attachments per
  post/comment (images and/or MP4 video, freely mixed, one shared count
  cap), content-sniffed and size-bounded, served from a dedicated public
  Explore upload path separate from wardrobe images. Video is
  MP4-only, validated by a bounded MP4/ISO-BMFF structural check requiring
  a valid `ftyp` plus both `moov` and `mdat` genuinely present — real
  container-format validation, not a bare `ftyp` signature check, not
  "any two recognized box names," and not filename/Content-Type trust —
  with its own, independently configurable size cap
  (`MAX_EXPLORE_VIDEO_SIZE_MB`/`MAX_DM_VIDEO_SIZE_MB`, default 20MB)
  alongside the existing image caps.
- **Direct messages** (`routers/messages.py`) — persistent one-to-one
  messaging; a conversation is get-or-created atomically from the unordered
  user pair; paginated history and inbox.
- **Private DM media** — image and MP4 video attachments on direct
  messages, retrieved only through an authenticated, participant-scoped
  endpoint, never a public static mount.
- **Notifications** (`routers/notifications.py`) — persistent,
  recipient-scoped notifications for new direct messages and for likes/
  dislikes on a user's posts/comments; self-notifications suppressed;
  removing a reaction creates no notification; switching a reaction
  notifies only the final state.
- **Live delivery** (`routers/ws.py`) — a single authenticated `/ws`
  endpoint pushes each notification, best-effort, to every open connection
  for its recipient. Delivery is cross-replica-safe:
  `services/live.py::send_to_user` publishes to a shared Redis Pub/Sub
  channel rather than delivering directly, so a recipient's push still
  arrives even when the triggering action (a DM, like, or dislike) is
  handled by a different `web` replica than the one holding their
  connection.
- **Share & Recreate** — sharing a Personal Outfit to a post freezes a
  sanitized `SharedOutfitSnapshot`; `GET /posts/{id}/recreate` matches that
  snapshot against the *viewing* user's own wardrobe, regardless of who
  authored the post.
- **Search People / View Profile** (`explore-profile.js`) — a "Search
  People" sub-tab lets a user find any other registered client by username
  (reusing the same authenticated `GET /users`/`GET /users?search=` route
  the Messages "Start a new conversation" picker already uses — it
  returns only `{id, username}` per user, never email or password data,
  always excludes the caller, and never reveals who authored an anonymous
  post), and a
  single shared, lightweight `#profile-modal` shows a public profile view —
  reachable from an Explore post's "View Profile" button, a Search People
  result, or a Messages thread's "View Profile" button. It shows only what
  the backend already exposes publicly: `{id, username}` from `GET /users`
  and that user's own non-anonymous posts from `GET /posts?author_id=`
  (reusing the existing `postCard()` renderer `explore-posts.js` already
  uses for Feed/My Posts — no second post-rendering implementation), plus
  the existing Message action (`startThreadWith`, already defined in
  `explore-messages.js`). No followers, presence, bio, or profile-editing —
  none of that data exists on the backend and none is added by this
  feature. Closing the modal never navigates anywhere: it's an overlay on
  whichever tab was already open.
- Frontend (`explore-posts.js`, `explore-messages.js`,
  `explore-notifications.js`, `explore-live.js`, `explore-profile.js`)
  covers the full workflow: feed browsing, post creation (with
  anonymity/outfit-share/media options), commenting, reacting, the DM inbox
  and thread view, the notification bell/panel, live WebSocket updates,
  Search People / View Profile, and the Recreate UI.
- **Cold-seed demo data** — `scripts/seed_demo_data.py` populates
  a fresh install with 5 deterministic demo users, 6 text-only posts
  (mixing anonymous and non-anonymous), 5 comments (always from a user
  other than the post's real author), and 8 likes/dislikes, entirely
  through the real API routes above (no direct MongoDB writes). A one-shot
  `seed` compose service now runs it automatically on every local/dev
  `docker compose up -d` (`SEED_DEMO_DATA`, hard-disabled in
  `docker-compose.prod.yml` — see root `README.md`); it can also still be
  run explicitly (`docker compose exec web python ...` or
  `scripts/start_demo.sh`) as an on-demand reconciliation helper. Every
  object is checked individually before being created or changed, so
  running it once, twice, or after being interrupted partway through never
  duplicates anything already seeded, and a reaction already in its
  desired state is never blindly re-POSTed (which would toggle it off).

## Outfit Recreation — ✅ Complete (structured-data-only)

**The multi-garment "Analyze Full Outfit" feature and the photo-based
Recreate path were removed.** This project no longer supports detecting multiple garments
from an arbitrary photo, and no longer offers "Recreate from Photo" on an
Explore post. The removed surface: `POST /wardrobe/analyze-outfit-image`,
`POST /posts/{post_id}/recreate-from-photo`, the "Analyze full outfit"
button and its multi-item review modal on the Add Item form, and the
`OutfitImageAnalysis`/`PhotoRecreateRequest`/`PhotoRecreateResponse`
models. Analyze Item (single garment) is unaffected.

**The rule going forward is conceptually simple: recreation is only ever
offered when the source outfit already has structured component data.**
A post's photo, by itself, is never a valid recreation source.

```text
post has shared_outfit
  → existing SharedOutfitSnapshot           (frozen at post-creation time,
                                              from a real saved outfit's
                                              real wardrobe items)
  → match_snapshot_to_wardrobe()
  → requesting user's real wardrobe documents
  → GET /posts/{post_id}/recreate  →  RecreateResponse
```

- **No vision call is ever made.** The snapshot is exact structured data
  captured when the outfit was shared (`routers/posts.py::
  _build_shared_outfit_snapshot`), so recreation is instant and never
  approximates what the outfit contained.
- A post with no `shared_outfit` has **no recreate affordance at all** —
  not a degraded AI-derived fallback, just nothing to offer.
- **Hallucination protection** (unchanged): the AI is not involved in this
  path at all, but the same grounding discipline applies to the matcher
  itself — `match_snapshot_to_wardrobe()` selects every match from real
  MongoDB `wardrobe` documents scoped to the requesting user, never the
  post author's; a missing clothing type yields `matched_item: null` with
  an explanation, never an invented substitute; only real `matched_item.id`
  values are ever handed to the Outfit Builder.

### Outfit Builder handoff

```text
Recreate results
  → matched real wardrobe item ids   (matched_item.id only)
  → "Customize this outfit"
  → existing Outfit Builder, pre-selected with those items
  → user customizes / names / saves — or doesn't
```

- **Partial recreations still open the Builder**, pre-selected with
  whichever real items matched. The "Missing from your wardrobe: …" line
  stays visible, so the partiality remains clear.
- **Zero-match recreations do not open the Builder** — the button is not
  rendered at all, and the handoff function refuses an empty list. The
  match rows still explain why.
- **Nothing is automatically saved.** The handoff only prefills a
  selection; the Builder's own Save button (POST/PATCH `/outfits`) remains
  the only write, and the user explicitly chooses whether to customize and
  save.

### Catalog / web-crawling suggestions inside the Builder

```text
unmatched Recreate components  (matched_item == null)
  → their clothing_type values, deduplicated
  → GET /recommendations/catalog-suggestions?clothing_type=<type>&max_results=4
  → real external products rendered inside the Builder
```

- **Only missing clothing types trigger catalog requests**, taken solely
  from rows where `matched_item` is `null` — never from AI free text, a
  product name, or any other source.
- **Duplicate missing types are deduplicated** (first-seen order
  preserved), so one request per distinct type.
- **A full match triggers no catalog requests at all** and renders no
  section.
- **Catalog failure never blocks the Builder.** The request is fired
  without being awaited, after the modal is already open. An outage,
  timeout, or malformed response shows "Shop suggestions unavailable.";
  a successful call with nothing matching shows "No suggestions found for
  the missing items." The two are distinguished via the endpoint's
  `catalog_available` flag rather than collapsed together.
- **External products are suggestions only.** They are never treated as
  owned wardrobe items, never added to the outfit or the wardrobe, and
  never counted in the Builder's selection. The only thing a user can do
  with one is follow its link.
- **Product links use validated HTTP(S) URLs.** `source_url` and
  `image_url` are crawled third-party data, so they are restricted to
  `http(s)` schemes and HTML-escaped before rendering; a product with no
  usable URL is dropped rather than rendered as a dead or unsafe link.
- **The Dashboard's catalog behavior is separate and unchanged.** The
  Builder owns its own `#builder-shop-suggestions` container and state and
  never writes into the Dashboard's `#rec-shop-suggestions`. The two share
  only the fetch/render helpers in `app.js` and the one backend endpoint.

## Not implemented / out of scope

- ⚪ **Weather integration** — not implemented. A possible future
  enhancement to ground recommendations/chat in real-world weather.
- ⚫ **AI-derived (photo-based) outfit recreation** — deliberately removed
  after being implemented. Recreation now only works from structured
  saved/shared outfit data; see "Outfit Recreation" above for the
  current, simplified contract. Analyze Item is unaffected.
- ⚪ Typing indicators, presence, read receipts, group conversations
  (forum/messaging) — out of scope.
