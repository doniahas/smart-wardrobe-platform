"""WebSocket live delivery, including cross-replica live notification
delivery.

_connections stays a plain in-process registry - it is, correctly, the
set of sockets THIS replica physically owns; nothing about that needed to
change (register/unregister are untouched). What changed is how a
notification actually reaches a socket: send_to_user no longer delivers
directly (which only ever worked when the caller and the recipient's
socket happened to be registered in the same process) - it publishes to
the shared Redis channel instead (see services/redis_client.py), and
delivery to THIS replica's own locally-registered sockets happens only
through _deliver_locally, invoked by the subscriber task main.py starts
at startup. This is deliberately the ONLY delivery path (no direct local
shortcut inside send_to_user) so a socket owned by the same replica that
triggered the event is never delivered to twice - once directly, once
again via its own subscription to the very channel it just published on.
See docs/architecture.md's "Cross-replica live WebSocket notification
delivery" for the design.

A single-replica deployment's observable behavior is unchanged in effect
(the publish/subscribe round trip still ends up delivering to the same
process, just via Redis rather than a direct in-process call) while a
genuinely multi-replica deployment now works correctly for the first
time.
"""
from fastapi import WebSocket

from app.core.config import MAX_WS_CONNECTIONS_PER_USER
from app.services import redis_client

# In-process registry of sockets THIS replica physically holds - never
# shared with, or visible to, any other replica directly. Cross-replica
# awareness of "does some other replica own a socket for this user" is
# never needed: every replica only ever asks "do *I* own one", which is
# exactly what this dict answers, regardless of which replica the
# triggering event came from - see _deliver_locally below.
_connections: dict[str, set[WebSocket]] = {}


class ConnectionLimitExceeded(Exception):
    """Raised when a user already has MAX_WS_CONNECTIONS_PER_USER active
    connections. See routers/ws.py, which closes the new socket with code
    1008 rather than evicting an existing one."""


def register(user_id: str, websocket: WebSocket) -> None:
    connections = _connections.setdefault(user_id, set())
    if len(connections) >= MAX_WS_CONNECTIONS_PER_USER:
        raise ConnectionLimitExceeded()
    connections.add(websocket)


def unregister(user_id: str, websocket: WebSocket) -> None:
    connections = _connections.get(user_id)
    if connections is None:
        return
    connections.discard(websocket)
    if not connections:
        del _connections[user_id]


async def _deliver_locally(user_id: str, payload: dict) -> None:
    """Send payload to every socket THIS replica currently has registered
    for user_id.

    Kept as its own, private, directly-testable function - the exact body
    send_to_user had before cross-replica delivery was added - so it can
    be exercised
    deterministically (register a fake socket, call this, assert it was
    sent to) without going through Redis/publish at all, and so the
    subscriber task (services/redis_client.py, wired in main.py's
    lifespan) has a single, well-defined local-delivery target to invoke
    for every event it receives, regardless of which replica published
    it.

    Never raises - a failed send on one socket only unregisters that
    socket and never blocks delivery to the user's other active sockets,
    exactly as with single-process delivery.
    """
    for websocket in list(_connections.get(user_id, ())):
        try:
            await websocket.send_json(payload)
        except Exception:
            unregister(user_id, websocket)


async def send_to_user(user_id: str, payload: dict) -> None:
    """Best-effort push to every currently-open connection for this user,
    on ANY replica - not just this process.

    Publishes to the shared Redis channel (services/redis_client.py)
    rather than delivering directly: every replica's own subscriber task
    receives the same published event and calls _deliver_locally against
    ITS OWN _connections - including this replica, for its own locally-
    registered sockets. See this module's docstring for why a direct
    local shortcut here would risk double delivery.

    Never raises - services/notifications.py::create_notification has
    already durably persisted the Mongo notification document before
    calling this, and redis_client.publish_live_event's own contract is
    to swallow any Redis failure (logged) rather than propagate it, so a
    Redis outage can never turn into an error for the caller here.
    """
    await redis_client.publish_live_event(user_id, payload)


def reset() -> None:
    """Test-only: clear the entire registry between tests."""
    _connections.clear()
