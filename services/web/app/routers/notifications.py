from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, status
from pymongo import ReturnDocument

from app.auth import get_current_user
from app.core.database import get_db
from app.core.pagination import Page, PageParams
from app.models import MarkAllReadResult, NotificationPublic, UnreadCountPublic

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _parse_oid(notification_id: str) -> ObjectId:
    try:
        return ObjectId(notification_id)
    except InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid notification id"
        )


def _doc_to_public(doc: dict) -> NotificationPublic:
    return NotificationPublic(
        id=str(doc["_id"]),
        type=doc["type"],
        actor_id=doc["actor_id"],
        actor_username=doc["actor_username"],
        target_type=doc["target_type"],
        target_id=str(doc["target_id"]),
        read=doc["read"],
        created_at=doc["created_at"],
    )


@router.get("", response_model=Page[NotificationPublic])
async def list_notifications(
    pagination: PageParams = Depends(),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """The caller's own notifications, newest-first."""
    user_id = str(current_user["_id"])
    cursor = (
        db.notifications.find({"recipient_id": user_id})
        .sort([("created_at", -1), ("_id", -1)])
        .skip(pagination.skip)
        .limit(pagination.page_size + 1)
    )
    docs = [doc async for doc in cursor]
    has_more = len(docs) > pagination.page_size
    docs = docs[: pagination.page_size]

    items = [_doc_to_public(doc) for doc in docs]
    return Page[NotificationPublic](
        items=items, page=pagination.page, page_size=pagination.page_size, has_more=has_more
    )


@router.get("/unread-count", response_model=UnreadCountPublic)
async def get_unread_count(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])
    count = await db.notifications.count_documents({"recipient_id": user_id, "read": False})
    return UnreadCountPublic(count=count)


@router.post("/mark-all-read", response_model=MarkAllReadResult)
async def mark_all_read(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])
    result = await db.notifications.update_many(
        {"recipient_id": user_id, "read": False}, {"$set": {"read": True}}
    )
    return MarkAllReadResult(updated_count=result.modified_count)


@router.post("/{notification_id}/read", response_model=NotificationPublic)
async def mark_read(
    notification_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(notification_id)
    user_id = str(current_user["_id"])
    doc = await db.notifications.find_one_and_update(
        {"_id": oid, "recipient_id": user_id},
        {"$set": {"read": True}},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    return _doc_to_public(doc)
