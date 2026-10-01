from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pymongo.errors import DuplicateKeyError

from app.auth import get_current_user
from app.core.config import RATE_LIMIT_FEEDBACK
from app.core.database import get_db
from app.core.limiter import limiter
from app.models import FeedbackCreate, FeedbackPublic, FeedbackUpdate
from app.services.personalization import update_memory_from_feedback

router = APIRouter(prefix="/feedback", tags=["feedback"])


async def _learn_from_feedback(
    db, user_id: str, outfit: dict, rating: int | None, liked: bool | None
) -> None:
    """Fold this feedback signal into the user's persisted memory profile."""
    item_oids = [ObjectId(item_id) for item_id in outfit.get("item_ids", [])]
    outfit_items = await db.wardrobe.find({"_id": {"$in": item_oids}}).to_list(length=None)

    memory_doc = await db.memory.find_one({"user_id": user_id})
    updates = update_memory_from_feedback(memory_doc, outfit_items, rating, liked)
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc)
        await db.memory.update_one(
            {"user_id": user_id},
            {"$set": updates, "$setOnInsert": {"user_id": user_id}},
            upsert=True,
        )


def _parse_oid(feedback_id: str) -> ObjectId:
    try:
        return ObjectId(feedback_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid feedback id")


def _doc_to_public(doc: dict) -> FeedbackPublic:
    return FeedbackPublic(
        id=str(doc["_id"]),
        user_id=doc["user_id"],
        outfit_id=doc["outfit_id"],
        rating=doc["rating"],
        liked=doc.get("liked"),
        comment=doc.get("comment"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


@router.post("", response_model=FeedbackPublic, status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_FEEDBACK)
async def create_feedback(
    request: Request,
    body: FeedbackCreate,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])

    try:
        outfit_oid = ObjectId(body.outfit_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid outfit id")

    outfit = await db.outfits.find_one({"_id": outfit_oid, "user_id": user_id})
    if outfit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outfit not found")

    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user_id,
        "outfit_id": body.outfit_id,
        "rating": body.rating,
        "liked": body.liked,
        "comment": body.comment,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = await db.feedback.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Feedback already exists for this outfit. Use PUT to update it.",
        )
    doc["_id"] = result.inserted_id
    await _learn_from_feedback(db, user_id, outfit, body.rating, body.liked)
    return _doc_to_public(doc)


@router.get("", response_model=list[FeedbackPublic])
async def list_feedback(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])
    cursor = db.feedback.find({"user_id": user_id})
    return [_doc_to_public(doc) async for doc in cursor]


@router.get("/{feedback_id}", response_model=FeedbackPublic)
async def get_feedback(
    feedback_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(feedback_id)
    user_id = str(current_user["_id"])
    doc = await db.feedback.find_one({"_id": oid, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
    return _doc_to_public(doc)


@router.put("/{feedback_id}", response_model=FeedbackPublic)
@limiter.limit(RATE_LIMIT_FEEDBACK)
async def update_feedback(
    request: Request,
    feedback_id: str,
    body: FeedbackUpdate,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(feedback_id)
    user_id = str(current_user["_id"])
    doc = await db.feedback.find_one({"_id": oid, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")

    updates = body.model_dump(exclude_unset=True)
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc)
        await db.feedback.update_one({"_id": oid}, {"$set": updates})
        doc.update(updates)

        if "rating" in updates or "liked" in updates:
            outfit = await db.outfits.find_one({"_id": ObjectId(doc["outfit_id"]), "user_id": user_id})
            if outfit is not None:
                await _learn_from_feedback(db, user_id, outfit, doc.get("rating"), doc.get("liked"))
    return _doc_to_public(doc)


@router.delete("/{feedback_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(RATE_LIMIT_FEEDBACK)
async def delete_feedback(
    request: Request,
    feedback_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(feedback_id)
    user_id = str(current_user["_id"])
    result = await db.feedback.delete_one({"_id": oid, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")