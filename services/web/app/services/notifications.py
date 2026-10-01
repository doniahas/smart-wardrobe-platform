from datetime import datetime, timezone

from bson import ObjectId

from app.models import NotificationPublic
from app.services.live import send_to_user


async def create_notification(
    db,
    recipient_id: str,
    actor_id: str,
    actor_username: str,
    notif_type: str,
    target_type: str,
    target_id: ObjectId,
) -> None:
    """Create a notification, unless the recipient is also the actor, and
    push it live to any of the recipient's currently-open WebSocket
    connections (services/live.py::send_to_user).

    Never notify a user about their own action - centralized here so this
    rule can't be missed at an individual call site (message send, post/
    comment reactions). Callers are expected to wrap this in a best-effort
    try/except, matching routers/chat.py's existing pattern for automatic
    memory learning: a notification failure must never break the primary
    action. The document is always persisted before the live push is
    attempted, and send_to_user never raises, so a delivery failure (or no
    open connection at all) can never undo or block the fact that this
    notification already durably exists.
    """
    if recipient_id == actor_id:
        return

    now = datetime.now(timezone.utc)
    doc = {
        "recipient_id": recipient_id,
        "actor_id": actor_id,
        "actor_username": actor_username,
        "type": notif_type,
        "target_type": target_type,
        "target_id": target_id,
        "read": False,
        "created_at": now,
    }
    result = await db.notifications.insert_one(doc)

    notification = NotificationPublic(
        id=str(result.inserted_id),
        type=notif_type,
        actor_id=actor_id,
        actor_username=actor_username,
        target_type=target_type,
        target_id=str(target_id),
        read=False,
        created_at=now,
    )
    await send_to_user(
        recipient_id,
        {"event": "notification", "notification": notification.model_dump(mode="json")},
    )
