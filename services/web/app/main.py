import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pymongo.errors import ConnectionFailure
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import EXPLORE_UPLOAD_DIR, TESTING
from app.core.database import close_db, connect_db, get_db
from app.core.error_handlers import mongo_connection_failure_handler
from app.core.limiter import limiter
from app.routers import (
    auth,
    chat,
    comments,
    feedback,
    health,
    memory,
    messages,
    notifications,
    outfits,
    posts,
    recommendation_feedback,
    recommendations,
    uploads,
    users,
    wardrobe,
    ws,
)
from app.services import live
from app.services.chat_worker import (
    start_chat_workers,
    start_stale_job_reaper,
    stop_chat_workers,
    stop_stale_job_reaper,
)
from app.services.redis_client import start_live_subscriber, stop_live_subscriber

@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    workers: list[asyncio.Task] = []
    stop_event: asyncio.Event | None = None
    reaper_task: asyncio.Task | None = None
    reaper_stop_event: asyncio.Event | None = None
    live_subscriber_task: asyncio.Task | None = None
    live_subscriber_stop_event: asyncio.Event | None = None
    try:
        if not TESTING:
            # get_db() is a plain accessor (no yield/request-scope - see
            # core/database.py) that returns the already-connected db, so
            # it's safe to call directly here, outside of a request.
            workers, stop_event = start_chat_workers(get_db())
            # Separate task/stop_event pair from the
            # worker pool above - see chat_worker.py's module docstring.
            reaper_task, reaper_stop_event = start_stale_job_reaper(get_db())
            # Cross-replica live delivery: the single, process-
            # wide Redis Pub/Sub subscriber for cross-replica live
            # notification delivery (services/redis_client.py). A third,
            # independent task/stop_event pair - not folded into either
            # pool above - so each concern's start/stop is reasoned about
            # on its own, same principle as the reaper being separate from
            # the chat worker pool. Never blocks startup even if Redis is
            # unreachable - see redis_client.start_live_subscriber's own
            # docstring.
            live_subscriber_task, live_subscriber_stop_event = start_live_subscriber(
                live._deliver_locally
            )
        yield
    finally:
        if live_subscriber_stop_event is not None:
            await stop_live_subscriber(live_subscriber_task, live_subscriber_stop_event)
        if reaper_stop_event is not None:
            await stop_stale_job_reaper(reaper_task, reaper_stop_event)
        if stop_event is not None:
            await stop_chat_workers(workers, stop_event)
        await close_db()

app = FastAPI(
    title="SmartWardrobe API",
    description="Backend API for a personalized outfit recommendation system",
    version="0.1.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# Graceful MongoDB-outage handling: one centralized handler covering
# every DB-backed route (see app/core/error_handlers.py's own docstring
# for the exact exception-classification reasoning) - same "register once,
# cover every route" idiom as RateLimitExceeded above.
app.add_exception_handler(ConnectionFailure, mongo_connection_failure_handler)
app.add_middleware(SlowAPIMiddleware)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(wardrobe.router)
app.include_router(recommendations.router)
app.include_router(recommendation_feedback.router)
app.include_router(outfits.router)
app.include_router(feedback.router)
app.include_router(memory.router)
app.include_router(chat.router)
app.include_router(posts.router)
app.include_router(comments.router)
app.include_router(messages.router)
app.include_router(notifications.router)
app.include_router(users.router)
app.include_router(uploads.router)
app.include_router(ws.router)

@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/ui/")


_STATIC = Path(__file__).parent / "static"
app.mount("/ui", StaticFiles(directory=_STATIC, html=True), name="ui")
# Public, unauthenticated Explore (post/comment) image delivery - never
# reused for direct-message attachments, which need authenticated,
# participant-scoped retrieval instead of a plain static mount.
app.mount("/uploads/explore", StaticFiles(directory=EXPLORE_UPLOAD_DIR), name="uploads_explore")