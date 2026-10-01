"""A global FastAPI exception handler for transient MongoDB connectivity
failures - an availability concern ("what happens when the database is
down?" - the answer must not be "it crashes"), applied specifically to
MongoDB, a required persistent dependency of every DB-backed route.

Live-testing a stopped `mongo` container showed `POST /auth/login`
previously returned a bare, unhandled-looking `500 Internal Server
Error`. Root cause: nothing in the request path ever
caught the driver's own connectivity exception, so it fell through to
FastAPI/Starlette's generic unhandled-exception handling.

Registered once in main.py, against `pymongo.errors.ConnectionFailure` -
Motor (this project's async MongoDB driver) raises PyMongo's own
exception classes directly rather than defining its own hierarchy, so
this single registration transparently covers every route that reaches
MongoDB through app.core.database.get_db() (auth, wardrobe, forum,
notifications, memory, chat queue, ...), without a try/except in each one
- the same "register one handler, cover every route" idiom this app
already uses for RateLimitExceeded (see main.py).

Deliberately scoped to ConnectionFailure and its subclasses ONLY - not a
blanket PyMongoError handler:

    PyMongoError
    +-- ConnectionFailure            <- handled here (+ every subclass)
    |   +-- AutoReconnect
    |       +-- NotPrimaryError
    |       +-- NetworkTimeout
    |           +-- ServerSelectionTimeoutError
    +-- OperationFailure             <- NOT handled here
    |   +-- WriteError
    |   |   +-- DuplicateKeyError    <- routers/auth.py already maps this
    |   |                               to 409 Conflict; a real business-
    |   |                               rule violation, not an outage
    |   +-- ExecutionTimeout
    +-- ConfigurationError           <- NOT handled here (a startup/config
    +-- InvalidOperation                bug, not a runtime outage)

(Verified directly against the installed pymongo==4.7.2's actual
`__mro__` for each class - not assumed from memory or documentation.)
OperationFailure/ConfigurationError/InvalidOperation are separate
branches of PyMongoError's hierarchy entirely - registering a handler for
ConnectionFailure cannot ever intercept them, so a duplicate-key error, a
malformed-query bug, or a bad connection-string configuration error still
surfaces exactly as it did before this change. Turning any of those into
a generic "temporarily unavailable" 503 would hide a real, actionable
defect behind a misleading, retryable-sounding response instead of
surfacing it - precisely what this module must not do.
"""
import logging

from fastapi import Request, status
from fastapi.responses import JSONResponse
from pymongo.errors import ConnectionFailure

logger = logging.getLogger(__name__)

# Deliberately fixed and generic - never the raw driver exception text
# (str(ServerSelectionTimeoutError(...)) includes the topology's
# host/port, confirmed directly against the installed driver), never the
# configured MONGO_URI, never a stack trace. Matches this codebase's
# existing logging-privacy convention (see
# services/web/tests/test_logging_privacy.py) and the wording style
# already used for other dependency-unavailable responses (routers/
# recommendations.py's "Recommendation service is unavailable.").
_SAFE_DETAIL = "Service temporarily unavailable. Please try again shortly."


async def mongo_connection_failure_handler(
    request: Request, exc: ConnectionFailure
) -> JSONResponse:
    """Map any MongoDB connectivity failure to a clean, safe 503.

    Logged server-side as a single, terse line (exception type + request
    method/path only - never `str(exc)`, which for a
    ServerSelectionTimeoutError includes the full topology description,
    and never a full traceback via logger.exception) so an operator can
    see that a Mongo outage is happening, and roughly where, without
    flooding the logs with one giant raw dump per ordinary client request
    for the entire duration of an outage - every request hitting Mongo
    during a real outage passes through this exact function.
    """
    logger.error(
        "MongoDB connectivity failure handling %s %s (%s)",
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": _SAFE_DETAIL},
    )
