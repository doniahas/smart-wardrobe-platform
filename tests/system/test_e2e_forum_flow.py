"""Real system/end-to-end test for the forum workflow.

Companion to test_e2e_wardrobe_flow.py - same conventions (plain
httpx.Client against a real running `web` container's public port, never
FastAPI's TestClient/ASGITransport/mongomock, unique e2e_-prefixed test
data, opt-in via RUN_E2E_TESTS=1), extended with one genuinely new piece of
infrastructure this file introduces: a **real WebSocket client** connecting
to `web`'s real `/ws` endpoint over a real socket, to prove live
notification delivery actually works end-to-end - not just that the
in-process WebSocket *protocol* is implemented correctly (that's already
proven by services/web/tests/test_live_notifications.py via Starlette's
TestClient.websocket_connect, which never leaves the test process or
reaches a separately-running container).

Real-WebSocket-client dependency decision
------------------------------------------
`httpx` itself does not speak the WebSocket protocol. Before adding any
new dependency, the actual `web`/`catalog` Docker images were inspected
directly (`docker run --rm --entrypoint pip smart-wardrobe-web:latest
list`): both already carry `websockets==17.0.1`, installed transitively as
part of `uvicorn[standard]==0.29.0`'s own "standard" extras (uvicorn's
`standard` extra bundle includes `websockets>=10.4` for its own dev-server
support) - already declared in `services/web/requirements.txt`/
`services/catalog/requirements.txt`. **No new package was added anywhere**:
this file simply imports the `websockets` package that already ships
inside the same `web` image the E2E tests run from. The import is guarded (see
`_WEBSOCKETS_AVAILABLE` below) purely so a bare local `pytest` run outside
that image - or any environment where `websockets` genuinely isn't
installed - gets a clear, actionable skip reason instead of a collection
error, mirroring the exact lesson `tests/system/conftest.py` already
applies to `MONGO_URI` for `test_live_mongo_smoke.py`/
`test_live_mongo_integration.py`.

Scope
-----
The forum/WebSocket flow only. The core wardrobe flow and the external
product/catalog flow are separate files. No application code change was
needed for this file: every forum/media/notification/WebSocket
capability it exercises already existed.

What this test does NOT re-prove (to avoid duplicating existing
coverage): MP4/image adversarial edge cases
(spoofed content-type, malformed containers, oversized uploads, path
traversal - already proven in
`services/web/tests/security/test_video_attachment_security.py` and
`test_path_traversal.py`), WebSocket protocol-level correctness (auth
timeout, malformed first frame, connection-limit enforcement - already
proven in-process by `test_live_notifications.py`), or "Recreate with My
Wardrobe" (already proven by `test_recreate.py`; unrelated to the forum
requirement this file targets). This file exercises exactly one real,
valid, end-to-end media/notification flow through the real system - that
is sufficient to prove the system boundary; the edge cases above are
correctly the job of the existing route/security-tier suites.

Opt-in and safety
------------------
Off by default (`pytestmark` below), exactly like test_e2e_wardrobe_flow.py
- an ordinary `pytest` run never attempts to reach a Docker stack that may
not exist:

    docker compose up -d   # or an isolated disposable stack, see
                            # docs/testing.md's "System / end-to-end
                            # tests" section for the exact recipe
    RUN_E2E_TESTS=1 pytest tests/system/test_e2e_forum_flow.py -v -s

`E2E_BASE_URL` (default `http://localhost:8000`) can point this at a
different reachable `web` instance if needed.

Required services: `web`, MongoDB, and Redis. `recommendation`, `llm`,
`catalog`, and Ollama are not required - the forum feature set (posts,
comments, reactions, direct messages, notifications, Explore/DM media) is
self-contained within `web` and never calls them. Redis is required
specifically for live WebSocket notification delivery:
`services/web/app/services/live.py::send_to_user` publishes exclusively
via Redis Pub/Sub, with no same-process fallback. `_wait_for_stack_ready`
below calls `GET /health/full`, which may probe services other than
Redis and does **not** validate Redis reachability - a disposable
topology for this file should separately confirm Redis readiness (e.g.
`redis-cli ping`) before running it.

Rate limiting note: this flow registers/logs in three users (6 real
`POST /auth/*` calls), which exceeds the production default
`RATE_LIMIT_AUTH=5/minute` in a single run. This is not a code change -
`RATE_LIMIT_AUTH` is already a supported environment-configurable setting
(`services/web/app/core/config.py`) - the disposable container used to
*validate* this file sets a generous override (e.g.
`-e RATE_LIMIT_AUTH=100/minute`) for that one container only, exactly like
`-e TESTING=true` would be set for other disposable containers. (Note:
this file's own `web` container must NOT use TESTING=true - it
disables the Redis live-notification subscriber.)
`docker-compose.yml`/`.env.example` (production defaults) are never
touched.

Recipient-id discovery note: `POST /messages/{other_user_id}` needs the
recipient's real MongoDB id, but no endpoint exposes "look up a user's id
by username" (matching real production - the frontend only ever learns an
id from context: a non-anonymous post/comment's `author_id`, or a
notification's `actor_id`). Since User A's post here is deliberately
anonymous (to exercise that feature), A's own id is instead discovered the
same way a real client would: A adds one comment to their own post -
comments are never anonymous (`CommentPublic.author_id` is always
populated, see `app/models.py`) - and User B later discovers User A's
username from the like-notification (this test doesn't need it) while User A
discovers User B's id from the "message" notification's own `actor_id`,
exactly like a real client using notifications to open a conversation
would. No test-only backdoor or hack is used anywhere in this file.

Test-data isolation: every user/post/message this test creates uses a
freshly generated, randomly suffixed `e2e_...` identifier - collision-free
across runs and against any pre-existing data, exactly like
test_e2e_wardrobe_flow.py. Nothing here reads, modifies, or deletes a
record it didn't itself create, and there is no automatic teardown against
a real persistent database (same documented, intentional trade-off as
test_e2e_wardrobe_flow.py - see that file's own docstring).
"""
import asyncio
import io
import json
import os
import struct
import time
import uuid

import httpx
import pytest
from PIL import Image

try:
    import websockets
    _WEBSOCKETS_AVAILABLE = True
except ImportError:  # pragma: no cover - only hit outside the web/catalog images
    _WEBSOCKETS_AVAILABLE = False

E2E_BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8000")
_RUN_E2E = os.environ.get("RUN_E2E_TESTS", "").strip().lower() in ("1", "true", "yes")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _RUN_E2E,
        reason=(
            "Forum system/E2E tests are opt-in and require a real running "
            "Docker stack. Set RUN_E2E_TESTS=1 (and optionally "
            "E2E_BASE_URL) to run them - see this file's module docstring."
        ),
    ),
    pytest.mark.skipif(
        not _WEBSOCKETS_AVAILABLE,
        reason=(
            "The `websockets` package is required for this file's real "
            "WebSocket client and was not importable. It already ships "
            "inside the web/catalog Docker images (a transitive dependency "
            "of uvicorn[standard]) - run this file from inside one of "
            "those images (see the module docstring), or `pip install "
            "websockets` for a bare local run."
        ),
    ),
]

_READY_TIMEOUT_SECONDS = 60
_READY_POLL_INTERVAL_SECONDS = 2
_WS_RECV_TIMEOUT_SECONDS = 10.0


def _wait_for_stack_ready(client: httpx.Client) -> None:
    """Poll GET /health/full until `web` and MongoDB report healthy, or
    fail loudly with a clear, actionable message.

    Deliberately does NOT require `recommendation` (unlike
    test_e2e_wardrobe_flow.py's own check) - this file's disposable stack
    legitimately never runs a `recommendation` container (see module
    docstring), so that field is expected to report an error here.
    """
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    last_error = "no attempt made"
    while time.monotonic() < deadline:
        try:
            resp = client.get("/health/full", timeout=5.0)
            if resp.status_code == 200:
                body = resp.json()
                if body.get("web") == "ok" and body.get("mongo") == "ok":
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
        "Start it with `docker compose up -d` (or the disposable recipe in "
        "docs/testing.md) before running with "
        "RUN_E2E_TESTS=1."
    )


@pytest.fixture()
def e2e_client():
    """A genuine HTTP client against a real running `web` container - same
    contract as test_e2e_wardrobe_flow.py's fixture of the same name."""
    with httpx.Client(base_url=E2E_BASE_URL, timeout=30.0) as client:
        _wait_for_stack_ready(client)
        yield client


def _unique_suffix() -> str:
    return uuid.uuid4().hex[:12]


def _register_and_login(client: httpx.Client, label: str) -> dict:
    """Register a brand-new, uniquely-named E2E test user over real HTTP
    and log in. Duplicated from test_e2e_wardrobe_flow.py's helper of the
    same name rather than imported/shared - each E2E file is
    deliberately self-contained, matching that file's own precedent."""
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
    assert login_body["token_type"] == "bearer"
    headers = {"Authorization": f"Bearer {login_body['access_token']}"}

    me_resp = client.get("/auth/me", headers=headers)
    assert me_resp.status_code == 200, me_resp.text
    assert me_resp.json() == {"username": username, "email": email}

    return {"username": username, "email": email, "headers": headers}


def _tiny_jpeg_bytes() -> bytes:
    """A tiny, genuinely valid JPEG - same Pillow idiom already used by
    services/web/tests/test_explore_media.py and friends."""
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _box(box_type: bytes, payload: bytes = b"") -> bytes:
    """One ordinary (32-bit size) ISO-BMFF box: 4-byte size + 4-byte type
    + payload - mirrors services/web/tests/mp4_fixtures.py::_box."""
    size = 8 + len(payload)
    return struct.pack(">I", size) + box_type + payload


def _tiny_valid_mp4_bytes() -> bytes:
    """A minimal, genuinely well-formed MP4/ISO-BMFF container: a real
    `ftyp` box plus an empty `moov` box and a small `mdat` box - passes
    services/image_storage.py's `_detect_mp4` structural validator.
    Deliberately duplicated (not imported) from
    services/web/tests/mp4_fixtures.py::valid_mp4_bytes(): tests/system is
    a separate top-level test suite from services/web/tests (see this
    directory's own pytest.ini `pythonpath`, which does not wire up
    services/web/tests as an importable package), so a handful of
    duplicated lines is simpler and more robust than cross-suite import
    wiring for one small, stable byte sequence."""
    ftyp_payload = b"isom" + b"\x00\x00\x00\x00" + b"isom"
    ftyp = _box(b"ftyp", ftyp_payload)
    moov = _box(b"moov")
    mdat = _box(b"mdat", b"\x00" * 16)
    return ftyp + moov + mdat


def _ws_url() -> str:
    """Translate E2E_BASE_URL's http(s) scheme into the matching ws(s)
    scheme for a real WebSocket connection to the same host/port."""
    if E2E_BASE_URL.startswith("https://"):
        return "wss://" + E2E_BASE_URL[len("https://"):] + "/ws"
    if E2E_BASE_URL.startswith("http://"):
        return "ws://" + E2E_BASE_URL[len("http://"):] + "/ws"
    raise ValueError(f"E2E_BASE_URL must start with http:// or https://, got {E2E_BASE_URL!r}")


async def _recv_notification(ws) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout=_WS_RECV_TIMEOUT_SECONDS)
    return json.loads(raw)


@pytest.mark.asyncio
async def test_forum_e2e_flow(e2e_client: httpx.Client):
    """Forum workflow, chained end-to-end against a real running
    Docker stack over real HTTP and a real WebSocket connection:

    1-3.   register/login User A, User B, User C over real HTTP
    4-7.   User A creates an anonymous post with a real image + real MP4
           attachment
    (A also adds one non-anonymous comment to their own post, purely to
    discover their own real user id the same way a real client would -
    see the module docstring's "Recipient-id discovery note")
    8-9.   User B does a fresh GET and sees the post; anonymity and
           attachment metadata verified from that fresh response
    10.    both attachment URLs are fetched over real HTTP and are
           genuinely retrievable
    11.    User A connects to /ws with a real WebSocket client and a real JWT
    12-13. User B comments and reacts; User A receives the resulting
           live notification over the real WebSocket
    14.    the same notification is verified persisted via a fresh
           GET /notifications call
    15-18. User B sends User A a DM (with real image + MP4 attachments);
           User A (still connected) receives the live "message"
           notification, verified live and via a fresh GET /notifications
    19.    DM history fetched via a fresh HTTP request
    20.    both DM attachments fetched by an authenticated participant (A)
    21.    User C (non-participant) gets the production 404 for the same
           DM attachment
    """
    client = e2e_client
    run_start = time.perf_counter()
    timings: dict[str, float] = {}

    def _mark(step: str) -> None:
        timings[step] = round(time.perf_counter() - run_start, 3)

    # --- Steps 1-3: register/login three users over real HTTP ---
    user_a = _register_and_login(client, "usera")
    user_b = _register_and_login(client, "userb")
    user_c = _register_and_login(client, "userc")
    _mark("1-3_register_login_three_users")

    # --- Steps 4-7: User A creates an anonymous post with a real image + real MP4 ---
    jpeg_bytes = _tiny_jpeg_bytes()
    mp4_bytes = _tiny_valid_mp4_bytes()

    create_post_resp = client.post(
        "/posts",
        data={"title": "E2E forum post", "body": "Real system E2E body", "is_anonymous": "true"},
        files=[
            ("images", ("e2e_photo.jpg", jpeg_bytes, "image/jpeg")),
            ("images", ("e2e_clip.mp4", mp4_bytes, "video/mp4")),
        ],
        headers=user_a["headers"],
    )
    assert create_post_resp.status_code == 201, create_post_resp.text
    created_post = create_post_resp.json()
    assert created_post["is_anonymous"] is True
    assert created_post["author_id"] is None
    assert created_post["author_username"] is None
    post_id = created_post["id"]
    _mark("4-7_user_a_creates_anonymous_post_with_media")

    # A's own real user id, discovered via a comment on their own post -
    # comments are never anonymous (see module docstring). Needed below so
    # User B can DM User A.
    a_self_comment_resp = client.post(
        f"/posts/{post_id}/comments",
        data={"body": "E2E: commenting on my own post"},
        headers=user_a["headers"],
    )
    assert a_self_comment_resp.status_code == 201, a_self_comment_resp.text
    a_user_id = a_self_comment_resp.json()["author_id"]
    assert a_self_comment_resp.json()["author_username"] == user_a["username"]

    # --- Steps 8-9: User B does a FRESH GET and independently verifies ---
    list_resp = client.get("/posts", headers=user_b["headers"])
    assert list_resp.status_code == 200, list_resp.text
    fetched_post = next(p for p in list_resp.json()["items"] if p["id"] == post_id)
    assert fetched_post["is_anonymous"] is True
    assert fetched_post["author_id"] is None
    assert fetched_post["author_username"] is None
    assert fetched_post["title"] == "E2E forum post"

    attachments = fetched_post["attachments"]
    assert len(attachments) == 2
    media_types = {a["media_type"] for a in attachments}
    assert media_types == {"image", "video"}
    image_attachment = next(a for a in attachments if a["media_type"] == "image")
    video_attachment = next(a for a in attachments if a["media_type"] == "video")
    assert image_attachment["content_type"] == "image/jpeg"
    assert video_attachment["content_type"] == "video/mp4"
    assert image_attachment["size_bytes"] == len(jpeg_bytes)
    assert video_attachment["size_bytes"] == len(mp4_bytes)
    _mark("8-9_user_b_fresh_get_verifies_anonymity_and_attachments")

    # --- Step 10: both attachment URLs are actually retrievable over real HTTP ---
    image_fetch = client.get(image_attachment["url"])
    assert image_fetch.status_code == 200
    assert image_fetch.headers["content-type"] == "image/jpeg"
    assert image_fetch.content == jpeg_bytes

    video_fetch = client.get(video_attachment["url"])
    assert video_fetch.status_code == 200
    assert video_fetch.headers["content-type"] == "video/mp4"
    assert video_fetch.content == mp4_bytes
    _mark("10_explore_media_urls_retrievable")

    # --- Step 11: User A connects to /ws with a real WebSocket client and a real JWT ---
    a_token = user_a["headers"]["Authorization"].split(" ", 1)[1]
    async with websockets.connect(_ws_url()) as ws:
        await ws.send(json.dumps({"token": a_token}))
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=_WS_RECV_TIMEOUT_SECONDS))
        assert ack == {"type": "connected"}
        _mark("11_user_a_real_websocket_connected")

        # --- Steps 12-13: User B comments (no notification by design - see
        # app/models.py's NotificationPublic docstring) and reacts (like) to
        # the post - User A receives the live push for the reaction only ---
        comment_resp = client.post(
            f"/posts/{post_id}/comments",
            data={"body": "E2E comment from User B"},
            headers=user_b["headers"],
        )
        assert comment_resp.status_code == 201, comment_resp.text

        reaction_resp = client.post(
            f"/posts/{post_id}/reactions", json={"value": 1}, headers=user_b["headers"]
        )
        assert reaction_resp.status_code == 200, reaction_resp.text
        assert reaction_resp.json()["likes_count"] == 1

        like_event = await _recv_notification(ws)
        assert like_event["event"] == "notification"
        like_notif = like_event["notification"]
        assert like_notif["type"] == "like_post"
        assert like_notif["target_type"] == "post"
        assert like_notif["target_id"] == post_id
        assert like_notif["actor_username"] == user_b["username"]
        assert "body" not in like_notif  # identifiers-only, no content inlined
        _mark("12-13_comment_and_reaction_notification_received_live")

        # --- Step 14: same notification verified persisted via a fresh GET ---
        notifs_resp = client.get("/notifications", headers=user_a["headers"])
        assert notifs_resp.status_code == 200, notifs_resp.text
        persisted_like = next(
            n for n in notifs_resp.json()["items"] if n["id"] == like_notif["id"]
        )
        assert persisted_like["type"] == "like_post"
        assert persisted_like["target_id"] == post_id
        _mark("14_like_notification_persisted")

        # --- Steps 15/17: User B sends User A a real DM with a real image +
        # real MP4 attachment (the "real DM flow" is established implicitly:
        # get_or_create_conversation runs transparently on first send) ---
        dm_jpeg_bytes = _tiny_jpeg_bytes()
        dm_mp4_bytes = _tiny_valid_mp4_bytes()
        send_dm_resp = client.post(
            f"/messages/{a_user_id}",
            data={"body": "E2E DM from User B"},
            files=[
                ("images", ("e2e_dm_photo.jpg", dm_jpeg_bytes, "image/jpeg")),
                ("images", ("e2e_dm_clip.mp4", dm_mp4_bytes, "video/mp4")),
            ],
            headers=user_b["headers"],
        )
        assert send_dm_resp.status_code == 201, send_dm_resp.text
        sent_dm = send_dm_resp.json()
        assert len(sent_dm["attachments"]) == 2
        _mark("15_17_dm_sent_with_media")

        # --- Steps 16/18: User A (still connected) receives the live
        # "message" notification, and User B's real id is discovered from
        # its actor_id - exactly like a real client opening a conversation
        # from a notification would (see module docstring) ---
        message_event = await _recv_notification(ws)
        assert message_event["event"] == "notification"
        message_notif = message_event["notification"]
        assert message_notif["type"] == "message"
        assert message_notif["target_type"] == "message"
        assert message_notif["target_id"] == sent_dm["id"]
        assert message_notif["actor_username"] == user_b["username"]
        assert "body" not in message_notif
        b_user_id = message_notif["actor_id"]
        assert sent_dm["sender_id"] == b_user_id

        message_notifs_resp = client.get("/notifications", headers=user_a["headers"])
        assert message_notifs_resp.status_code == 200, message_notifs_resp.text
        persisted_message = next(
            n for n in message_notifs_resp.json()["items"] if n["id"] == message_notif["id"]
        )
        assert persisted_message["type"] == "message"
        _mark("16_18_dm_notification_received_live_and_persisted")

    # WebSocket portion complete - the `async with` block above closed it cleanly.

    # --- Step 19: DM history fetched via a fresh HTTP request ---
    history_resp = client.get(f"/messages/{b_user_id}", headers=user_a["headers"])
    assert history_resp.status_code == 200, history_resp.text
    history_items = history_resp.json()["items"]
    fetched_dm = next(m for m in history_items if m["id"] == sent_dm["id"])
    assert fetched_dm["sender_id"] == b_user_id
    assert fetched_dm["body"] == "E2E DM from User B"
    assert len(fetched_dm["attachments"]) == 2
    _mark("19_dm_history_fetched_fresh")

    # --- Step 20: both DM attachments fetched by an authenticated participant (A) ---
    dm_image_attachment = next(a for a in fetched_dm["attachments"] if a["media_type"] == "image")
    dm_video_attachment = next(a for a in fetched_dm["attachments"] if a["media_type"] == "video")

    a_image_fetch = client.get(dm_image_attachment["url"], headers=user_a["headers"])
    assert a_image_fetch.status_code == 200
    assert a_image_fetch.content == dm_jpeg_bytes

    a_video_fetch = client.get(dm_video_attachment["url"], headers=user_a["headers"])
    assert a_video_fetch.status_code == 200
    assert a_video_fetch.content == dm_mp4_bytes
    _mark("20_participant_fetches_dm_attachments")

    # --- Step 21: User C (a non-participant) gets the production
    # "not found" result for the same protected DM attachment ---
    c_image_fetch = client.get(dm_image_attachment["url"], headers=user_c["headers"])
    assert c_image_fetch.status_code == 404
    assert c_image_fetch.json()["detail"] == "Attachment not found"

    c_video_fetch = client.get(dm_video_attachment["url"], headers=user_c["headers"])
    assert c_video_fetch.status_code == 404
    assert c_video_fetch.json()["detail"] == "Attachment not found"
    _mark("21_non_participant_denied_dm_media")

    total_seconds = round(time.perf_counter() - run_start, 3)
    print(f"\n[forum E2E] base_url={E2E_BASE_URL}")
    print(f"[forum E2E] users: {user_a['username']} (A), {user_b['username']} (B), {user_c['username']} (C)")
    print(f"[forum E2E] post={post_id} dm={sent_dm['id']}")
    print(f"[forum E2E] step timings (seconds since test start): {timings}")
    print(f"[forum E2E] total elapsed: {total_seconds}s")
