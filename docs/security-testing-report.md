# Security Testing Report

This report describes how Smart Wardrobe's security-relevant behavior is
tested: what the dedicated adversarial suite covers, how it complements the
security checks spread across the rest of the test suite, the results, and
the residual risks that remain. It is a testing report, not a formal threat
model or penetration test.

All tests described here are automated and run as part of the `web`
service suite (see [`testing.md`](testing.md)). They use `TestClient` with
an in-memory MongoDB fake; selected behaviors were additionally confirmed
against a real running stack (see [Real-stack confirmation](#real-stack-confirmation)).

## Scope

The `web` service is the only component with an authentication, ownership,
or upload surface, and the only application service reachable from outside
the Docker network. `recommendation`, `llm`, and `catalog` are
internal-only and have no auth or user-data surface of their own, so this
report covers `web`.

## Security controls under test

| Control | Implementation |
|---|---|
| Authentication | bcrypt password hashing; JWT (HS256) with expiry; user re-fetched on every request; startup refuses a missing/placeholder `JWT_SECRET` |
| Authorization | Every query scoped by the JWT-derived `user_id`; "not found" and "not yours" both return `404` |
| Input validation | Pydantic models with strict types; response models reject unlisted fields |
| Upload validation | Content-sniffed (Pillow for images, ISO-BMFF structure for MP4) — never filename or client `Content-Type`; size caps enforced during a bounded, chunked read |
| Media access | Wardrobe images via signed, expiring URLs; DM media only through an authenticated, participant-scoped route; filenames resolved safely against the upload directory |
| Abuse limits | Configurable per-route rate limits on every user-facing write path, shared across replicas via Redis |
| Prompt safety | Memory and third-party product text treated as data: sanitized, length-bounded, delimiter tokens stripped before entering a prompt |

## Coverage across the suite

Much of the security-relevant coverage lives alongside each feature's own
tests:

| Area | Where |
|---|---|
| Protected-route access without login | `*_requires_auth` tests across nearly every route test file |
| Expired, malformed, and wrong-signature JWTs | `test_jwt_validation.py` |
| Insecure configuration rejected at startup | `test_config.py` (missing/placeholder `JWT_SECRET`) |
| Cross-user access: wardrobe, feedback | `test_ownership_isolation.py` |
| Cross-user access: outfits, posts, comments, notifications, chat jobs, DM attachments | Each resource's own route tests (`test_outfits_route.py`, `test_posts_route.py`, `test_comments.py`, `test_notifications_route.py`, `test_chat_jobs_route.py`, `test_dm_media.py`) |
| Image content spoofing (e.g. text renamed `.jpg`) | `test_image_storage.py`, `test_explore_media.py`, `test_dm_media.py` |
| MP4 container validation | `test_mp4_validation.py` (19 unit tests of the structural validator) |
| Oversized uploads (`413`) | `test_image_storage.py`, `test_explore_media.py`, `test_dm_media.py` |
| Repeated / abusive requests | `test_rate_limiting.py` (every upload-carrying endpoint plus the rest of the write surface), `test_rate_limiting_shared_storage.py` |
| Invalid resource IDs (`400`) | Many route test files |
| Sensitive values kept out of logs | `test_logging_privacy.py` |
| Signed wardrobe image URLs | `test_image_storage.py` |
| Prompt-injection hardening | `test_chat_context.py`, `test_chat_catalog_grounding.py` (delimiter forging, oversized/malformed memory entries) |

## Dedicated adversarial suite

`services/web/tests/security/` contains 78 tests written specifically to
attack the API rather than exercise features:

| File | Tests | Focus |
|---|---|---|
| `test_path_traversal.py` | 13 | Path traversal and unsafe filenames |
| `test_malformed_json.py` | 42 | Syntactically broken JSON bodies |
| `test_injection_and_invalid_ids.py` | 8 | NoSQL-operator payloads and injection-like input |
| `test_cross_user_authorization.py` | 5 | Memory profile and DM-history isolation |
| `test_video_attachment_security.py` | 10 | MP4 spoofing, malformed containers, oversized uploads, private DM video |

### Path traversal and unsafe filenames

Encoded traversal attempts (e.g. `%2e%2e%2f`) against the two routes that
read a client-influenced filename from disk —
`GET /uploads/wardrobe/{filename}` and
`GET /messages/conversations/{id}/attachments/{filename}`. One test places a
real secret file just outside the upload directory and confirms that even a
validly *signed* traversal filename cannot reach it, so the defense does not
depend solely on signature checking.

### Malformed JSON

Seven genuinely invalid payloads (truncated, trailing comma, unquoted keys,
single quotes, trailing garbage, raw binary, empty body) against six
JSON-accepting endpoints (`/auth/register`, `/auth/login`, `POST /outfits`,
`PATCH /outfits/{id}`, `POST /feedback`, `PUT /memory`). Each must produce a
clean `4xx`, leak no traceback, and change no state. These exercise
FastAPI's JSON *decoding* step, which runs before Pydantic validation.

### Injection-like input

- Structured operator payloads (e.g. `{"$ne": null}`) where a string is
  expected are rejected by Pydantic type validation before any query runs.
- Injection-looking strings in free-text fields (feedback comments, memory
  notes) and in a wardrobe list filter are stored and matched as inert
  literal data — pymongo only treats real dict/BSON structures as operators.
- A username containing MongoDB-special characters (`$`, `.`) works as
  ordinary data.

### Cross-user authorization

- The memory profile (`GET`/`PUT`/`PATCH /memory`) is isolated, verified both
  through the API and directly in the database.
- DM history never leaks another pair's conversation to a third user who
  shares one participant, and returns a genuinely empty page for a user who
  was never contacted.

### MP4 attachments

The MP4 validator walks the ISO-BMFF top-level box chain and requires a
valid `ftyp` box plus both a `moov` and an `mdat` box (in either order). The
dedicated tests cover:

- **MIME / extension spoofing in both directions** — a real MP4 is accepted
  despite a wrong declared type, and non-video bytes are rejected despite a
  claimed `video/mp4`.
- **Malformed containers** — a bare `ftyp` signature, a syntactically valid
  but non-media box alone (`free`), a missing `moov` or `mdat`, and a
  truncated container are all rejected.
- **Oversized uploads** — rejected with `413` without buffering the whole
  file.
- **Private DM media isolation** — a non-participant cannot retrieve a DM
  video (`404`).
- **Path traversal** on a video attachment's filename.
- **Rate limiting** — video-bearing requests remain subject to the normal
  write limiter; there is no separate, unlimited upload path.

Wardrobe uploads are separately confirmed to reject a genuinely valid MP4,
since video is accepted only for forum and DM attachments.

## Results

- Dedicated security suite: **78 passed, 0 failed.**
- Full `web` suite (which includes it): **1,306 passed, 0 failed.**

No test in these suites demonstrated an authorization bypass, path
traversal, injection, unsafe error leakage, unintended state change, or
content-spoofing bypass in the areas covered. This means the tested
behaviors hold for the tested inputs; it is not a guarantee that no
vulnerabilities exist.

## Real-stack confirmation

Beyond the in-process suite, the attachment behaviors were exercised over
real HTTP against a disposable stack (real `web` container, real `mongo:7`,
isolated Docker network): real users posted, commented, and messaged with a
real MP4; the video was retrieved byte-identical as `video/mp4`; DM privacy
held (participants succeed, an unrelated user gets `404`, an
unauthenticated request gets `401`); a spoofed and an oversized MP4 were
rejected; and image attachments continued to work. The opt-in end-to-end
forum test (`tests/system/test_e2e_forum_flow.py`) re-checks real
authenticated DM-media retrieval and cross-user isolation over real HTTP.

## Residual risks and limitations

- **No fuzzing or property-based testing.** Payloads are hand-picked,
  representative adversarial cases, not an exhaustive search.
- **Container validity, not codec decodability.** The MP4 check proves
  structural validity and rejects smuggled non-video content, but never
  decodes frames; a structurally valid file with a codec browsers can't
  play is accepted. No decoder (e.g. ffmpeg) runs on uploads.
- **Representative, not exhaustive, injection coverage.** The protection
  mechanism (strict typing plus pymongo's handling of strings) is uniform,
  so a representative sample was tested rather than every field.
- **Rate limits are per client IP.** Users behind a shared IP share a bucket,
  and limits are an abuse-mitigation measure, not a DoS defense. If Redis is
  unavailable, limits fall back to per-replica counters.
- **Uploaded media is stored on the host.** There is no malware scanning of
  uploaded files.
- **No external penetration test or dependency-vulnerability scanning** is
  part of the current CI pipeline.
- **Small LLM, prompt-level instructions.** Prompt-injection hardening
  sanitizes inputs and product facts are enforced in code, but the model's
  free conversational text is not otherwise constrained.
