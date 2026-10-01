from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pymongo import ReturnDocument

from app.auth import get_current_user
from app.core.config import RATE_LIMIT_MEMORY_WRITE
from app.core.database import get_db
from app.core.limiter import limiter
from app.models import Occasion, Style, UserMemoryPatch, UserMemoryPublic, UserMemoryUpdate, _sanitize_legacy_enum_list

router = APIRouter(prefix="/memory", tags=["memory"])


def _doc_to_public(doc: dict | None) -> UserMemoryPublic:
    if doc is None:
        return UserMemoryPublic()
    return UserMemoryPublic(
        preferred_styles=_sanitize_legacy_enum_list(doc.get("preferred_styles", []), Style),
        liked_colors=doc.get("liked_colors", []),
        disliked_colors=doc.get("disliked_colors", []),
        disliked_item_types=doc.get("disliked_item_types", []),
        preferred_occasions=_sanitize_legacy_enum_list(doc.get("preferred_occasions", []), Occasion),
        preferred_fit=doc.get("preferred_fit"),
        communication_style=doc.get("communication_style"),
        additional_preferences=doc.get("additional_preferences", []),
        notes=doc.get("notes"),
        updated_at=doc.get("updated_at"),
    )


def _check_color_overlap(liked: list[str], disliked: list[str]) -> None:
    overlap = set(liked) & set(disliked)
    if overlap:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Colors cannot be both liked and disliked: {sorted(overlap)}",
        )


@router.get("", response_model=UserMemoryPublic)
async def get_memory(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return the current user's long-term memory profile.

    If nothing has been saved yet, returns the default (empty) profile
    rather than 404 - a memory profile conceptually always exists.
    """
    user_id = str(current_user["_id"])
    doc = await db.memory.find_one({"user_id": user_id})
    return _doc_to_public(doc)


@router.put("", response_model=UserMemoryPublic)
@limiter.limit(RATE_LIMIT_MEMORY_WRITE)
async def put_memory(
    request: Request,
    body: UserMemoryUpdate,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Create or fully replace the current user's memory profile."""
    user_id = str(current_user["_id"])
    fields = body.model_dump()
    fields["updated_at"] = datetime.now(timezone.utc)

    doc = await db.memory.find_one_and_update(
        {"user_id": user_id},
        {"$set": fields, "$setOnInsert": {"user_id": user_id}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return _doc_to_public(doc)


@router.patch("", response_model=UserMemoryPublic)
@limiter.limit(RATE_LIMIT_MEMORY_WRITE)
async def patch_memory(
    request: Request,
    body: UserMemoryPatch,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Partially update the current user's memory profile.

    Requires a profile to already exist (created via PUT) since only the
    fields explicitly sent are changed.
    """
    user_id = str(current_user["_id"])
    existing = await db.memory.find_one({"user_id": user_id})
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No memory profile yet. Use PUT /memory to create one first.",
        )

    updates = body.model_dump(exclude_unset=True)
    if updates:
        merged_liked = updates.get("liked_colors", existing.get("liked_colors", []))
        merged_disliked = updates.get("disliked_colors", existing.get("disliked_colors", []))
        _check_color_overlap(merged_liked, merged_disliked)

        updates["updated_at"] = datetime.now(timezone.utc)
        await db.memory.update_one({"user_id": user_id}, {"$set": updates})
        existing.update(updates)

    return _doc_to_public(existing)
