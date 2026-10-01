import asyncio
import json

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.auth import get_user_from_token
from app.core.config import WS_AUTH_TIMEOUT_SECONDS
from app.core.database import get_db
from app.services.live import ConnectionLimitExceeded, register, unregister

router = APIRouter()

_POLICY_VIOLATION = 1008


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, db=Depends(get_db)):
    """One connection per authenticated client (tab/device), grouped by
    user in services/live.py's in-process registry.

    The client must send {"token": "<jwt>"} as its first text frame within
    WS_AUTH_TIMEOUT_SECONDS - verified via the same JWT logic HTTP auth
    uses (auth.py::get_user_from_token). A timeout, malformed frame,
    invalid/expired token, or the user already being at
    MAX_WS_CONNECTIONS_PER_USER all close the socket with code 1008
    without ever registering it. Once authenticated, this endpoint is
    push-only - no further protocol is defined; incoming frames are simply
    read and discarded until disconnect.
    """
    await websocket.accept()

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await websocket.close(code=_POLICY_VIOLATION)
        return

    try:
        token = json.loads(raw)["token"]
    except (ValueError, KeyError, TypeError):
        await websocket.close(code=_POLICY_VIOLATION)
        return

    user = await get_user_from_token(token, db)
    if user is None:
        await websocket.close(code=_POLICY_VIOLATION)
        return

    user_id = str(user["_id"])
    try:
        register(user_id, websocket)
    except ConnectionLimitExceeded:
        await websocket.close(code=_POLICY_VIOLATION)
        return

    await websocket.send_json({"type": "connected"})

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        unregister(user_id, websocket)
