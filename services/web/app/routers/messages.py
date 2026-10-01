import logging
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse

from app.auth import get_current_user
from app.core.config import MAX_DM_ATTACHMENTS, RATE_LIMIT_MESSAGE_WRITE
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.pagination import Page, PageParams
from app.models import AttachmentPublic, ConversationPublic, DirectMessagePublic
from app.services import image_storage
from app.services.conversations import get_or_create_conversation, pair_key
from app.services.image_storage import delete_dm_image, save_dm_image
from app.services.notifications import create_notification

router = APIRouter(prefix="/messages", tags=["messages"])
logger = logging.getLogger(__name__)

_MESSAGE_BODY_MAX_LENGTH = 2000


def _parse_user_oid(user_id: str) -> ObjectId:
    try:
        return ObjectId(user_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id")


def _parse_conversation_oid(conversation_id: str) -> ObjectId:
    try:
        return ObjectId(conversation_id)
    except InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid conversation id"
        )


def _reject_blank(value: str, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{field} cannot be blank"
        )
    return stripped


async def _save_dm_attachments(images: list[UploadFile], conversation_id: str) -> list[dict]:
    """Validate and save each attachment via save_dm_image, in order.

    Despite its name, save_dm_image accepts either an image or an
    MP4 video per file, detected from real content - every file still
    counts toward the same MAX_DM_ATTACHMENTS cap below regardless of
    which kind it turns out to be (mirrors routers/posts.py's
    _save_attachments).

    If any attachment fails validation, every attachment already saved
    earlier in this same call is deleted before the failure propagates.
    Kept local to this module rather than shared with routers/posts.py's
    near-identical helper - if a third identical call site appears later,
    that's when a shared utility is warranted, not before.
    """
    non_empty = [f for f in images if f.filename]
    if len(non_empty) > MAX_DM_ATTACHMENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many attachments (max {MAX_DM_ATTACHMENTS}).",
        )

    saved: list[dict] = []
    try:
        for file in non_empty:
            saved.append(await save_dm_image(file, conversation_id))
    except Exception:
        for attachment in saved:
            delete_dm_image(attachment["url"])
        raise
    return saved


def _message_doc_to_public(doc: dict) -> DirectMessagePublic:
    return DirectMessagePublic(
        id=str(doc["_id"]),
        conversation_id=str(doc["conversation_id"]),
        sender_id=doc["sender_id"],
        body=doc["body"],
        attachments=[AttachmentPublic(**a) for a in doc.get("attachments", [])],
        created_at=doc["created_at"],
    )


def _conversation_doc_to_public(doc: dict, viewer_user_id: str) -> ConversationPublic:
    other = next(p for p in doc["participants"] if p["user_id"] != viewer_user_id)
    return ConversationPublic(
        conversation_id=str(doc["_id"]),
        other_user_id=other["user_id"],
        other_username=other["username"],
        last_message_at=doc["last_message_at"],
    )


@router.post("/{other_user_id}", response_model=DirectMessagePublic, status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_MESSAGE_WRITE)
async def send_message(
    request: Request,
    other_user_id: str,
    body: str = Form(..., min_length=1, max_length=_MESSAGE_BODY_MAX_LENGTH),
    images: list[UploadFile] = File(default=[]),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    other_oid = _parse_user_oid(other_user_id)
    current_user_id = str(current_user["_id"])

    if str(other_oid) == current_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot send a message to yourself"
        )

    other_user_doc = await db.users.find_one({"_id": other_oid})
    if other_user_doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    body = _reject_blank(body, "body")

    conversation = await get_or_create_conversation(
        db,
        {"user_id": current_user_id, "username": current_user["username"]},
        {"user_id": str(other_user_doc["_id"]), "username": other_user_doc["username"]},
    )

    attachments = await _save_dm_attachments(images, str(conversation["_id"]))

    doc = {
        "conversation_id": conversation["_id"],
        "sender_id": current_user_id,
        "body": body,
        "attachments": attachments,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        result = await db.direct_messages.insert_one(doc)
    except Exception:
        # The images are already safely on disk (previous step), but the
        # message document that was going to reference them never got
        # created - clean them up rather than leaving orphaned files.
        for attachment in attachments:
            delete_dm_image(attachment["url"])
        raise
    doc["_id"] = result.inserted_id

    # Best-effort: a notification failure must never break the send itself.
    try:
        await create_notification(
            db,
            recipient_id=str(other_user_doc["_id"]),
            actor_id=current_user_id,
            actor_username=current_user["username"],
            notif_type="message",
            target_type="message",
            target_id=doc["_id"],
        )
    except Exception:
        logger.exception("send_message: unexpected error creating notification")

    # The message is already persisted and retrievable at this point. This
    # activity-timestamp bump is a second, separate write - on this
    # standalone (non-replica-set) MongoDB there's no transaction spanning
    # both, so a crash between them could leave the inbox's
    # last_message_at stale relative to the true last message. The message
    # itself (and its attachments) are never lost either way.
    await db.conversations.update_one(
        {"_id": conversation["_id"]},
        {"$set": {"last_message_at": doc["created_at"]}},
    )

    return _message_doc_to_public(doc)


@router.get("/conversations", response_model=Page[ConversationPublic])
async def list_conversations(
    pagination: PageParams = Depends(),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """The caller's own conversations, most recently active first."""
    user_id = str(current_user["_id"])
    cursor = (
        db.conversations.find({"participants.user_id": user_id})
        .sort([("last_message_at", -1), ("_id", -1)])
        .skip(pagination.skip)
        .limit(pagination.page_size + 1)
    )
    docs = [doc async for doc in cursor]
    has_more = len(docs) > pagination.page_size
    docs = docs[: pagination.page_size]

    items = [_conversation_doc_to_public(doc, user_id) for doc in docs]
    return Page[ConversationPublic](
        items=items, page=pagination.page, page_size=pagination.page_size, has_more=has_more
    )


@router.get("/conversations/{conversation_id}/attachments/{filename}")
async def get_dm_attachment(
    conversation_id: str,
    filename: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Serve a single DM image to a participant of its conversation only.

    The disk path is derived solely from the trusted, stored attachment
    metadata - never directly from the raw `filename` path parameter. That
    parameter is only ever used to build a lookup key which either matches
    a real, owned attachment's stored URL exactly, or 404s; it is never
    passed to Path()/the filesystem directly.
    """
    conv_oid = _parse_conversation_oid(conversation_id)
    user_id = str(current_user["_id"])

    conversation = await db.conversations.find_one(
        {"_id": conv_oid, "participants.user_id": user_id}
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")

    expected_url = f"/messages/conversations/{conversation_id}/attachments/{filename}"
    message = await db.direct_messages.find_one(
        {"conversation_id": conv_oid, "attachments.url": expected_url}
    )
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")

    attachment = next((a for a in message["attachments"] if a["url"] == expected_url), None)
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")

    trusted_filename = Path(attachment["url"]).name
    file_path = image_storage.DM_UPLOAD_DIR / trusted_filename
    if not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")

    return FileResponse(file_path, media_type=attachment["content_type"])


@router.get("/{other_user_id}", response_model=Page[DirectMessagePublic])
async def get_message_history(
    other_user_id: str,
    pagination: PageParams = Depends(),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Message history with a specific user, newest-first.

    Returns an empty page (not 404) if the two of you have never exchanged
    a message - that's a normal state, not an error. Never creates a
    conversation as a side effect of reading.
    """
    other_oid = _parse_user_oid(other_user_id)
    current_user_id = str(current_user["_id"])

    other_user_doc = await db.users.find_one({"_id": other_oid})
    if other_user_doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    key = pair_key(current_user_id, str(other_user_doc["_id"]))
    conversation = await db.conversations.find_one({"pair_key": key})
    if conversation is None:
        return Page[DirectMessagePublic](
            items=[], page=pagination.page, page_size=pagination.page_size, has_more=False
        )

    cursor = (
        db.direct_messages.find({"conversation_id": conversation["_id"]})
        .sort([("created_at", -1), ("_id", -1)])
        .skip(pagination.skip)
        .limit(pagination.page_size + 1)
    )
    docs = [doc async for doc in cursor]
    has_more = len(docs) > pagination.page_size
    docs = docs[: pagination.page_size]

    items = [_message_doc_to_public(doc) for doc in docs]
    return Page[DirectMessagePublic](
        items=items, page=pagination.page, page_size=pagination.page_size, has_more=has_more
    )
