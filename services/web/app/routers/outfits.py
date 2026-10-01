from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pymongo import ReturnDocument

from app.auth import get_current_user
from app.core.config import RATE_LIMIT_OUTFIT_WRITE
from app.core.database import get_db
from app.core.limiter import limiter
from app.models import SavedOutfitCreate, SavedOutfitPatch, SavedOutfitPublic, WardrobeItemPublic
from app.services.image_storage import sign_wardrobe_image_url

router = APIRouter(prefix="/outfits", tags=["outfits"])

_MAX_ONE_PER_OUTFIT_MESSAGES = {
    "bottom": "Only one bottom is allowed per outfit.",
    "shoes": "Only one pair of shoes is allowed per outfit.",
    "dress": "Only one dress is allowed per outfit.",
}


async def _validate_outfit_items(item_ids: list[str], user_id: str, db) -> None:
    """Shared validation for an outfit's item_ids: ownership, distinctness,
    and per-type composition rules. Used by both create_outfit and
    update_outfit; kept private to this router rather than moved to a
    shared services module, since both consumers live here - revisit only
    if a third consumer outside this router ever needs it.

    Deliberately does not check for an empty list: both `SavedOutfitCreate`
    and `SavedOutfitPatch` already reject that with a 422 via their own
    `Field(min_length=1)` before this function is ever called, and every
    rule enforced here is a business rule, reported as a 400 instead.
    """
    try:
        item_oids = [ObjectId(item_id) for item_id in item_ids]
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid item id")

    if len(set(item_ids)) != len(item_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duplicate items are not allowed in an outfit.",
        )

    docs = await db.wardrobe.find({"_id": {"$in": item_oids}, "user_id": user_id}).to_list(length=None)
    if len(docs) != len(item_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more items do not exist or do not belong to you",
        )

    grouped: dict[str, list[dict]] = {}
    for doc in docs:
        grouped.setdefault(doc["clothing_type"], []).append(doc)

    for clothing_type, message in _MAX_ONE_PER_OUTFIT_MESSAGES.items():
        if len(grouped.get(clothing_type, [])) > 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

    # A dress is mutually exclusive with both tops and a bottom, matching
    # the recommendation engine's own semantics (services/rule_based.py:
    # "an outfit is either 'dress + optionals' or 'top+bottom + optionals',
    # never a dress alongside a separate top or bottom"). Checked as two
    # separate rules, each with its own message, so a request violating
    # only one of them still gets a specific, correct error.
    if grouped.get("dress") and grouped.get("top"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An outfit cannot include both a dress and a top.",
        )

    if grouped.get("dress") and grouped.get("bottom"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An outfit cannot include both a dress and a bottom.",
        )


def _reject_null_fields(updates: dict) -> None:
    """PATCH /outfits/{id} never treats an explicit null as "clear this
    field" - every field is either omitted (left unchanged, via
    exclude_unset at the call site) or a real value. A field present in
    the request body with a null value is rejected outright."""
    for field, value in updates.items():
        if value is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field} cannot be null",
            )


def _parse_oid(outfit_id: str) -> ObjectId:
    try:
        return ObjectId(outfit_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid outfit id")


def _wardrobe_doc_to_public(doc: dict) -> WardrobeItemPublic:
    return WardrobeItemPublic(
        id=str(doc["_id"]),
        user_id=doc["user_id"],
        name=doc["name"],
        clothing_type=doc["clothing_type"],
        color=doc["color"],
        season=doc["season"],
        style=doc["style"],
        occasion=doc["occasion"],
        image_url=sign_wardrobe_image_url(doc.get("image_url")),
        tags=doc.get("tags", []),
    )


async def _doc_to_public(doc: dict, db) -> SavedOutfitPublic:
    item_ids = doc["item_ids"]
    item_oids = [ObjectId(item_id) for item_id in item_ids]
    items_cursor = db.wardrobe.find({"_id": {"$in": item_oids}})
    items_by_id = {str(item["_id"]): item async for item in items_cursor}
    items = [_wardrobe_doc_to_public(items_by_id[item_id]) for item_id in item_ids if item_id in items_by_id]
    return SavedOutfitPublic(
        id=str(doc["_id"]),
        user_id=doc["user_id"],
        item_ids=item_ids,
        items=items,
        name=doc.get("name"),
        occasion=doc.get("occasion"),
        season=doc.get("season"),
        style=doc.get("style"),
        created_at=doc["created_at"],
    )


@router.post("", response_model=SavedOutfitPublic, status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_OUTFIT_WRITE)
async def create_outfit(
    request: Request,
    body: SavedOutfitCreate,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])
    await _validate_outfit_items(body.item_ids, user_id, db)

    doc = {
        "user_id": user_id,
        "item_ids": body.item_ids,
        "name": body.name,
        "occasion": body.occasion,
        "season": body.season,
        "style": body.style,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.outfits.insert_one(doc)
    doc["_id"] = result.inserted_id
    return await _doc_to_public(doc, db)


@router.get("", response_model=list[SavedOutfitPublic])
async def list_outfits(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])
    cursor = db.outfits.find({"user_id": user_id})
    return [await _doc_to_public(doc, db) async for doc in cursor]


@router.get("/{outfit_id}", response_model=SavedOutfitPublic)
async def get_outfit(
    outfit_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(outfit_id)
    user_id = str(current_user["_id"])
    doc = await db.outfits.find_one({"_id": oid, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outfit not found")
    return await _doc_to_public(doc, db)


@router.patch("/{outfit_id}", response_model=SavedOutfitPublic)
@limiter.limit(RATE_LIMIT_OUTFIT_WRITE)
async def update_outfit(
    request: Request,
    outfit_id: str,
    body: SavedOutfitPatch,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Partial update of one of the caller's own outfits. Never touches any
    Public Outfit Snapshot already frozen onto a post from this outfit -
    that snapshot carries no reference back to this outfit or its item ids
    (see models.py's SharedOutfitSnapshot), so there is structurally
    nothing here for an edit to reach."""
    oid = _parse_oid(outfit_id)
    user_id = str(current_user["_id"])

    existing = await db.outfits.find_one({"_id": oid, "user_id": user_id})
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outfit not found")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")
    _reject_null_fields(updates)

    if "item_ids" in updates:
        await _validate_outfit_items(updates["item_ids"], user_id, db)

    doc = await db.outfits.find_one_and_update(
        {"_id": oid, "user_id": user_id},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    return await _doc_to_public(doc, db)


@router.delete("/{outfit_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(RATE_LIMIT_OUTFIT_WRITE)
async def delete_outfit(
    request: Request,
    outfit_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(outfit_id)
    user_id = str(current_user["_id"])
    result = await db.outfits.delete_one({"_id": oid, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outfit not found")
    await db.feedback.delete_many({"user_id": user_id, "outfit_id": outfit_id})