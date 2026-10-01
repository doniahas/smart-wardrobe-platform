import os
from pathlib import Path

MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "smartwardrobe")
# Graceful MongoDB-outage handling: PyMongo/Motor's own
# default serverSelectionTimeoutMS is 30000 (30s) - confirmed directly
# against the installed pymongo==4.7.2. A client-facing request that hits
# an unreachable Mongo would otherwise hang for up to 30 seconds before the
# new global ConnectionFailure handler (core/error_handlers.py) even gets a
# chance to turn it into a clean 503 - a "clean" failure that takes 30
# seconds is not the prompt, controlled failure this fix exists to provide.
# 5s mirrors this codebase's existing per-dependency timeout convention
# (RECOMMENDATION_SERVICE_URL/LLM_SERVICE_URL/CATALOG_SERVICE_URL each have
# their own explicit, configurable timeout below) rather than leaving
# Mongo as the one dependency with no bound at all.
MONGO_SERVER_SELECTION_TIMEOUT_MS = int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000"))
RECOMMENDATION_SERVICE_URL = os.getenv("RECOMMENDATION_SERVICE_URL", "http://recommendation:8001")
LLM_SERVICE_URL = os.getenv("LLM_SERVICE_URL", "http://llm:8002")

# Catalog service (real/mock crawled clothing products), reached
# over the Docker network by its compose service name - same pattern as
# RECOMMENDATION_SERVICE_URL/LLM_SERVICE_URL above.
CATALOG_SERVICE_URL = os.getenv("CATALOG_SERVICE_URL", "http://catalog:8003")

# Shared Redis instance (see docs/architecture.md's "Shared, replica-safe
# rate limiting" and "Cross-replica live WebSocket notification
# delivery"), reached over the Docker network by its compose service
# name - same pattern as RECOMMENDATION_SERVICE_URL/LLM_SERVICE_URL/
# CATALOG_SERVICE_URL above. Read by both core/limiter.py's shared
# rate-limit storage and services/redis_client.py's WebSocket Pub/Sub;
# each constructs its own, separate Redis client rather than sharing one
# client object.
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
# Generous relative to the other services' timeouts: the catalog service's
# own /crawl fans out to multiple adapters with its own internal retry/
# backoff (up to CATALOG_MAX_ATTEMPTS attempts x CATALOG_REQUEST_TIMEOUT_
# SECONDS per adapter, see services/catalog/app/core/config.py) before it
# ever responds, so this must comfortably exceed that worst case rather
# than racing it.
CATALOG_SERVICE_TIMEOUT_SECONDS = float(os.getenv("CATALOG_SERVICE_TIMEOUT_SECONDS", "30.0"))
# Kill-switch: catalog integration is strictly additive to the recommendation
# flow, and must be instantly disableable without a code revert if the
# catalog service or a real store misbehaves in production.
CATALOG_ENABLED = os.getenv("CATALOG_ENABLED", "true").lower() == "true"
# Hard upper bound on how many external products a client may request in one
# call, independent of (and never exceeding) the catalog service's own
# CrawlRequest.max_results<=100 limit.
CATALOG_SUGGESTIONS_MAX_RESULTS = int(os.getenv("CATALOG_SUGGESTIONS_MAX_RESULTS", "10"))
# Must stay above the llm service's own OLLAMA_GENERATE_TIMEOUT_SECONDS
# default (60s) so a slow-but-valid Ollama response isn't cut off here
# before the llm service's own 504 can fire. If that value is raised on the
# llm service, this one should be raised to match.
LLM_SERVICE_TIMEOUT_SECONDS = float(os.getenv("LLM_SERVICE_TIMEOUT_SECONDS", "65.0"))
TESTING = os.getenv("TESTING", "false").lower() == "true"

# Security: no working default outside TESTING. A weak,
# checked-in placeholder secret lets anyone forge a valid token for any
# user, so the app must refuse to start rather than silently accept one -
# TESTING mode is the only context where a convenient fallback is safe,
# since it never serves real traffic.
_INSECURE_JWT_SECRET_PLACEHOLDER = "change-this-secret"
_jwt_secret_env = os.getenv("JWT_SECRET")
if TESTING:
    JWT_SECRET = _jwt_secret_env or _INSECURE_JWT_SECRET_PLACEHOLDER
elif not _jwt_secret_env or _jwt_secret_env == _INSECURE_JWT_SECRET_PLACEHOLDER:
    raise RuntimeError(
        "JWT_SECRET must be set to a real, non-default secret outside of "
        "TESTING mode. Generate one (e.g. `python -c \"import secrets; "
        "print(secrets.token_hex(32))\"`) and set it in your environment "
        "or .env file."
    )
else:
    JWT_SECRET = _jwt_secret_env
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# Security: wardrobe images are served through a signed,
# time-limited URL (see services/image_storage.py) rather than a public
# static mount - this bounds how long a leaked URL stays valid.
WARDROBE_IMAGE_URL_TTL_SECONDS = int(os.getenv("WARDROBE_IMAGE_URL_TTL_SECONDS", "3600"))

# Rate limiting (slowapi syntax: "<count>/<period>", e.g. "100/minute").
# RATE_LIMIT_DEFAULT applies to the whole API; the others are stricter limits
# applied to specific routes that are either common abuse targets
# (auth) or costlier to serve/mutate state (recommendation calls an external
# service; wardrobe writes handle file uploads; feedback affects
# personalization; outfit and memory writes are the remaining
# state-modifying endpoints and get the same stricter treatment for
# consistency, even though they don't have a distinct cost driver of
# their own). RATE_LIMIT_POST_WRITE covers user-generated forum
# content creation/deletion, the same abuse category as feedback/outfit
# writes.
RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "100/minute")
RATE_LIMIT_AUTH = os.getenv("RATE_LIMIT_AUTH", "5/minute")
RATE_LIMIT_RECOMMENDATION = os.getenv("RATE_LIMIT_RECOMMENDATION", "20/minute")
# Separate from RATE_LIMIT_RECOMMENDATION since catalog
# suggestions are an independent, optional call a client may issue
# repeatedly (e.g. re-querying a different clothing_type) without re-running
# the full recommendation pipeline.
RATE_LIMIT_CATALOG = os.getenv("RATE_LIMIT_CATALOG", "20/minute")
RATE_LIMIT_CHAT = os.getenv("RATE_LIMIT_CHAT", "20/minute")
RATE_LIMIT_WARDROBE_WRITE = os.getenv("RATE_LIMIT_WARDROBE_WRITE", "30/minute")
RATE_LIMIT_FEEDBACK = os.getenv("RATE_LIMIT_FEEDBACK", "30/minute")
RATE_LIMIT_OUTFIT_WRITE = os.getenv("RATE_LIMIT_OUTFIT_WRITE", "30/minute")
RATE_LIMIT_MEMORY_WRITE = os.getenv("RATE_LIMIT_MEMORY_WRITE", "30/minute")
RATE_LIMIT_POST_WRITE = os.getenv("RATE_LIMIT_POST_WRITE", "30/minute")
RATE_LIMIT_REACTION_WRITE = os.getenv("RATE_LIMIT_REACTION_WRITE", "30/minute")
RATE_LIMIT_COMMENT_WRITE = os.getenv("RATE_LIMIT_COMMENT_WRITE", "30/minute")
# Direct messages are the most obvious spam vector (e.g. a user sending
# 1000 messages in a short time to overload the server).
RATE_LIMIT_MESSAGE_WRITE = os.getenv("RATE_LIMIT_MESSAGE_WRITE", "30/minute")

# Background chat worker tuning. Total concurrent in-flight LLM calls
# across a deployment is (number of web processes/replicas) x
# CHAT_WORKER_CONCURRENCY - each process runs its own independent set of
# worker tasks with no cross-process coordination beyond claim_next_job's
# atomicity, so scaling web horizontally multiplies concurrency rather than
# sharing one pool.
CHAT_WORKER_CONCURRENCY = int(os.getenv("CHAT_WORKER_CONCURRENCY", "2"))
CHAT_WORKER_POLL_INTERVAL_SECONDS = float(os.getenv("CHAT_WORKER_POLL_INTERVAL_SECONDS", "0.5"))
CHAT_WORKER_SHUTDOWN_TIMEOUT_SECONDS = float(os.getenv("CHAT_WORKER_SHUTDOWN_TIMEOUT_SECONDS", "30.0"))

# Stale "processing" job recovery (see
# chat_queue.py::reclaim_stale_jobs). A job claimed by a worker that then
# dies/is killed before recording completed/failed would otherwise stay
# "processing" forever, since complete_job/fail_job are the only other
# writers of that field. Default is 2x LLM_SERVICE_TIMEOUT_SECONDS: a
# genuinely in-flight call_llm() always resolves (success or a
# ChatGatewayFailure) within roughly LLM_SERVICE_TIMEOUT_SECONDS, so a job
# still "processing" past a comfortable multiple of that can only be one
# whose worker/process is gone - never one that's merely slow. Keep this
# above LLM_SERVICE_TIMEOUT_SECONDS with margin if that value is raised.
CHAT_JOB_STALE_TIMEOUT_SECONDS = float(
    os.getenv("CHAT_JOB_STALE_TIMEOUT_SECONDS", str(LLM_SERVICE_TIMEOUT_SECONDS * 2))
)

# Live validation demonstrated that a
# stale timeout too close to (or shorter than) LLM_SERVICE_TIMEOUT_SECONDS
# lets the reaper reclaim a job that is still genuinely, correctly
# processing - relying on a code comment ("keep this above ... with
# margin") for two independently-edited env vars to stay in the right
# relationship is not a real safeguard. This margin is deliberately a
# plain constant, not itself env-configurable: if it were also adjustable
# via env, an unsafe .env could simply raise/lower the margin to defeat the
# very check meant to catch an unsafe .env.
CHAT_JOB_STALE_TIMEOUT_MIN_MARGIN = 1.5


def validate_chat_job_stale_timeout(
    stale_timeout_seconds: float,
    llm_timeout_seconds: float,
    min_margin: float = CHAT_JOB_STALE_TIMEOUT_MIN_MARGIN,
) -> None:
    """Raise RuntimeError if CHAT_JOB_STALE_TIMEOUT_SECONDS is not a safe
    multiple of LLM_SERVICE_TIMEOUT_SECONDS.

    A genuinely in-flight call_llm() call always resolves (success or a
    classified ChatGatewayFailure) within roughly `llm_timeout_seconds` -
    see chat_queue.py::reclaim_stale_jobs's docstring. If the stale timeout
    is allowed to be shorter than that, or only marginally longer, the
    reaper can reclaim a job that is still genuinely, correctly processing.
    complete_job/fail_job's claim-fencing (see chat_queue.py) makes that
    race *safe* (no corrupted/duplicated writes), but it's still wasted
    worker capacity and a silently dropped completion for the affected job
    - this check exists to keep that from happening under normal,
    unintentional misconfiguration.

    Called unconditionally at import time below (not gated behind
    TESTING, unlike JWT_SECRET): unlike JWT_SECRET, there is always a safe
    default here, so there's no legitimate reason for this relationship to
    ever be violated at process startup, test or production alike.
    """
    min_safe = llm_timeout_seconds * min_margin
    if stale_timeout_seconds < min_safe:
        raise RuntimeError(
            f"CHAT_JOB_STALE_TIMEOUT_SECONDS ({stale_timeout_seconds}s) must be at "
            f"least {min_margin}x LLM_SERVICE_TIMEOUT_SECONDS ({llm_timeout_seconds}s) "
            f"= {min_safe}s, or the stale-job reaper can reclaim jobs that are still "
            "genuinely processing. Raise CHAT_JOB_STALE_TIMEOUT_SECONDS (or lower "
            "LLM_SERVICE_TIMEOUT_SECONDS) so the relationship holds."
        )


validate_chat_job_stale_timeout(CHAT_JOB_STALE_TIMEOUT_SECONDS, LLM_SERVICE_TIMEOUT_SECONDS)
# How many times a job may be claimed (see chat_queue.py::claim_next_job's
# attempts counter) before a stale sweep gives up on it and marks it
# permanently "failed" instead of recycling it back to "pending" again -
# bounds a job that reliably kills whatever worker touches it (e.g. a
# prompt that crashes the process) to a few tries rather than looping
# forever between "pending" and "processing".
CHAT_JOB_MAX_ATTEMPTS = int(os.getenv("CHAT_JOB_MAX_ATTEMPTS", "3"))
# How often the background reaper task (chat_worker.py::start_stale_job_reaper)
# sweeps for stale jobs. Independent of CHAT_JOB_STALE_TIMEOUT_SECONDS - this
# only needs to be frequent enough that a stale job doesn't sit recoverable-
# but-unrecovered for long, not frequent enough to matter for correctness.
CHAT_JOB_RECOVERY_INTERVAL_SECONDS = float(os.getenv("CHAT_JOB_RECOVERY_INTERVAL_SECONDS", "30.0"))

# services/web/ (three levels up from this file: core -> app -> web)
_WEB_ROOT = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(_WEB_ROOT / "uploads" / "wardrobe")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_URL_PREFIX = "/uploads/wardrobe"
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "5"))
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# Explore (posts/comments) image attachments - deliberately separate
# from the wardrobe upload settings above (even where defaults match) so
# tuning one can never silently affect the other. Public, unauthenticated
# static delivery only - NOT reused for direct-message attachments, which
# are private and use authenticated, participant-scoped retrieval of
# their own (see DM_UPLOAD_DIR below).
EXPLORE_UPLOAD_DIR = Path(os.getenv("EXPLORE_UPLOAD_DIR", str(_WEB_ROOT / "uploads" / "explore")))
EXPLORE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPLORE_UPLOAD_URL_PREFIX = "/uploads/explore"
MAX_EXPLORE_IMAGE_SIZE_MB = int(os.getenv("MAX_EXPLORE_IMAGE_SIZE_MB", "5"))
MAX_EXPLORE_IMAGE_SIZE_BYTES = MAX_EXPLORE_IMAGE_SIZE_MB * 1024 * 1024
# MP4 video attachments on posts/comments - a separate, larger cap
# than images (real video files are legitimately bigger), but still a
# conservative, hard ceiling: 20MB comfortably fits a short demo clip
# (roughly 10-20s of typical phone/browser-recorded 720p H.264) while
# keeping the worst case for one request (MAX_EXPLORE_ATTACHMENTS videos)
# bounded at a few hundred MB rather than unbounded. Enforced by a
# bounded/chunked read that aborts with 413 the
# instant this is exceeded - see services/image_storage.py's
# _read_remaining_bounded - never by reading the whole upload first.
MAX_EXPLORE_VIDEO_SIZE_MB = int(os.getenv("MAX_EXPLORE_VIDEO_SIZE_MB", "20"))
MAX_EXPLORE_VIDEO_SIZE_BYTES = MAX_EXPLORE_VIDEO_SIZE_MB * 1024 * 1024
# Shared by posts and comments - not MAX_ATTACHMENTS_PER_POST, since the
# same cap applies to both, and to both images and MP4 videos alike (no
# separate video count - see services/image_storage.py's
# _classify_and_read_bounded and routers/posts.py's _save_attachments).
MAX_EXPLORE_ATTACHMENTS = int(os.getenv("MAX_EXPLORE_ATTACHMENTS", "4"))

# Direct-message image attachments - private. Deliberately has no
# *_URL_PREFIX constant and is never passed to StaticFiles/app.mount(): the
# only way to reach a file here is the authenticated, participant-scoped
# GET /messages/conversations/{id}/attachments/{filename} route in
# routers/messages.py, not a public static path.
DM_UPLOAD_DIR = Path(os.getenv("DM_UPLOAD_DIR", str(_WEB_ROOT / "uploads" / "dm")))
DM_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_DM_IMAGE_SIZE_MB = int(os.getenv("MAX_DM_IMAGE_SIZE_MB", "5"))
MAX_DM_IMAGE_SIZE_BYTES = MAX_DM_IMAGE_SIZE_MB * 1024 * 1024
# Same reasoning/default as MAX_EXPLORE_VIDEO_SIZE_MB above, kept as
# an independently configurable value so tuning one can never silently
# affect the other (mirrors the existing MAX_EXPLORE_IMAGE_SIZE_MB /
# MAX_DM_IMAGE_SIZE_MB split).
MAX_DM_VIDEO_SIZE_MB = int(os.getenv("MAX_DM_VIDEO_SIZE_MB", "20"))
MAX_DM_VIDEO_SIZE_BYTES = MAX_DM_VIDEO_SIZE_MB * 1024 * 1024
MAX_DM_ATTACHMENTS = int(os.getenv("MAX_DM_ATTACHMENTS", "4"))

# WebSocket live delivery (routers/ws.py, services/live.py). The client
# must send its auth frame within WS_AUTH_TIMEOUT_SECONDS of connecting, or
# the socket is closed - this bounds how long an unauthenticated connection
# can sit open. MAX_WS_CONNECTIONS_PER_USER bounds concurrent tabs/devices
# per user; a new connection past that limit is rejected rather than
# evicting an existing one.
WS_AUTH_TIMEOUT_SECONDS = float(os.getenv("WS_AUTH_TIMEOUT_SECONDS", "5"))
MAX_WS_CONNECTIONS_PER_USER = int(os.getenv("MAX_WS_CONNECTIONS_PER_USER", "3"))
