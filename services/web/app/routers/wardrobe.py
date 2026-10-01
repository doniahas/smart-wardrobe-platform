from typing import List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from pymongo import ReturnDocument

from app.auth import get_current_user
from app.core.config import RATE_LIMIT_WARDROBE_WRITE
from app.core.database import get_db
from app.core.limiter import limiter
from app.models import (
    ClothingType,
    Occasion,
    Season,
    Style,
    WardrobeAiSuggestion,
    WardrobeItemPublic,
    _normalize_color_hex,
)
from app.services.image_storage import (
    delete_wardrobe_image,
    save_wardrobe_image,
    sign_wardrobe_image_url,
    validate_wardrobe_image_bytes,
)
from app.services.llm_gateway import ChatGatewayFailure
from app.services.wardrobe_ai import analyze_wardrobe_image

router = APIRouter(prefix="/wardrobe", tags=["wardrobe"])


def _parse_oid(item_id: str) -> ObjectId:
    try:
        return ObjectId(item_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid item id")


def _parse_tags(tags: Optional[str]) -> list[str]:
    if not tags:
        return []
    return [t.strip() for t in tags.split(",") if t.strip()]


def _parse_color_hex(color_hex: Optional[str]) -> Optional[str]:
    """Reuses WardrobeItemCreate/Update's own normalizer (see models.py) so
    create and update apply the exact same #RRGGBB rule, surfaced here as
    a normal 422 rather than an unhandled 500 - color_hex only ever comes
    from the Color picker's own swatch data or the AI suggestion's
    category-default lookup, never free typing, so a malformed value means
    a client bug, not a user typo to tolerate."""
    try:
        return _normalize_color_hex(color_hex)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def _doc_to_public(doc: dict) -> WardrobeItemPublic:
    return WardrobeItemPublic(
        id=str(doc["_id"]),
        user_id=doc["user_id"],
        name=doc["name"],
        clothing_type=doc["clothing_type"],
        color=doc["color"],
        color_hex=doc.get("color_hex"),
        season=doc["season"],
        style=doc["style"],
        occasion=doc["occasion"],
        image_url=sign_wardrobe_image_url(doc.get("image_url")),
        tags=doc.get("tags", []),
    )


@router.post("", response_model=WardrobeItemPublic, status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_WARDROBE_WRITE)
async def create_item(
    request: Request,
    name: str = Form(..., min_length=1, max_length=100),
    clothing_type: ClothingType = Form(...),
    color: str = Form(...),
    color_hex: Optional[str] = Form(None),
    season: Season = Form(...),
    style: List[Style] = Form(...),
    occasion: List[Occasion] = Form(...),
    image_url: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])

    if not style:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one style is required")
    if not occasion:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one occasion is required")

    final_image_url = image_url
    if image is not None and image.filename:
        final_image_url = await save_wardrobe_image(image)

    doc = {
        "user_id": user_id,
        "name": name,
        "clothing_type": clothing_type,
        "color": color,
        "color_hex": _parse_color_hex(color_hex),
        "season": season,
        "style": style,
        "occasion": occasion,
        "image_url": final_image_url,
        "tags": _parse_tags(tags),
    }
    result = await db.wardrobe.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _doc_to_public(doc)


@router.post("/analyze-image", response_model=WardrobeAiSuggestion)
@limiter.limit(RATE_LIMIT_WARDROBE_WRITE)
async def analyze_image(
    request: Request,
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Suggest wardrobe fields from a single clothing-item photo.

    Never writes to disk or the database - the image is validated and held
    in memory only for the duration of this request, and the response is
    suggestions only for the user to review; nothing is persisted here.
    """
    data = await validate_wardrobe_image_bytes(image)
    result = await analyze_wardrobe_image(data)
    if isinstance(result, ChatGatewayFailure):
        raise HTTPException(status_code=result.status_code, detail=result.detail)
    return result


@router.get("", response_model=list[WardrobeItemPublic])
async def list_items(
    clothing_type: Optional[ClothingType] = Query(None),
    color: Optional[str] = Query(None),
    season: Optional[Season] = Query(None),
    style: Optional[Style] = Query(None),
    occasion: Optional[Occasion] = Query(None),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])
    query: dict = {"user_id": user_id}
    if clothing_type is not None:
        query["clothing_type"] = clothing_type
    if color is not None:
        query["color"] = color
    if season is not None:
        query["season"] = season
    if style is not None:
        query["style"] = style
    if occasion is not None:
        query["occasion"] = occasion
    cursor = db.wardrobe.find(query)
    return [_doc_to_public(doc) async for doc in cursor]


@router.get("/{item_id}", response_model=WardrobeItemPublic)
async def get_item(
    item_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(item_id)
    user_id = str(current_user["_id"])
    doc = await db.wardrobe.find_one({"_id": oid, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return _doc_to_public(doc)


@router.put("/{item_id}", response_model=WardrobeItemPublic)
@limiter.limit(RATE_LIMIT_WARDROBE_WRITE)
async def update_item(
    request: Request,
    item_id: str,
    name: Optional[str] = Form(None, min_length=1, max_length=100),
    clothing_type: Optional[ClothingType] = Form(None),
    color: Optional[str] = Form(None),
    color_hex: Optional[str] = Form(None),
    season: Optional[Season] = Form(None),
    # Deliberately `List[Style] = Form(None)`, not `Optional[List[Style]] =
    # Form(None)`: FastAPI 0.111's Form-list binding only calls
    # FormData.getlist() (collecting every repeated `style=` value) when it
    # recognizes the parameter's annotation as a sequence shape - wrapping
    # it in Optional/Union breaks that recognition and silently falls back
    # to taking a single (last-wins) value instead, even when the client
    # sent several. The bare `List[Style]` annotation still works fine as
    # an optional field at runtime (Form(None) is still the actual default
    # when the client omits it) - this is purely about what FastAPI's
    # shape-detection sees, not the field's real optionality.
    style: List[Style] = Form(None),
    occasion: List[Occasion] = Form(None),
    image_url: Optional[str] = Form(None),
    # An explicit boolean, not an empty `image_url=""`. FastAPI/
    # Starlette parses an empty-content multipart field as the parameter
    # being *absent* (-> None) for an Optional[str] = Form(None) parameter,
    # not as an explicit "" - confirmed live (curl -F "image_url=" and
    # -F "color_hex=" both produce the same "No fields to update" 400 this
    # route already had), so image_url="" can never actually reach here
    # over real HTTP. A dedicated flag sidesteps that ambiguity entirely.
    remove_image: bool = Form(False),
    tags: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(item_id)
    user_id = str(current_user["_id"])

    existing = await db.wardrobe.find_one({"_id": oid, "user_id": user_id})
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    updates: dict = {}
    if name is not None:
        updates["name"] = name
    if clothing_type is not None:
        updates["clothing_type"] = clothing_type
    if color is not None:
        updates["color"] = color
    if color_hex is not None:
        updates["color_hex"] = _parse_color_hex(color_hex)
    if season is not None:
        updates["season"] = season
    if style is not None:
        if not style:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one style is required")
        updates["style"] = style
    if occasion is not None:
        if not occasion:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one occasion is required")
        updates["occasion"] = occasion
    if tags is not None:
        updates["tags"] = _parse_tags(tags)

    new_image_url: Optional[str] = None
    if image is not None and image.filename:
        # A chosen replacement always takes precedence over a removal
        # request, regardless of remove_image's value.
        new_image_url = await save_wardrobe_image(image)
    elif remove_image:
        new_image_url = ""
    elif image_url is not None:
        new_image_url = image_url
    if new_image_url is not None:
        updates["image_url"] = new_image_url

    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    doc = await db.wardrobe.find_one_and_update(
        {"_id": oid, "user_id": user_id},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    old_image_url = existing.get("image_url")
    if new_image_url is not None and old_image_url and old_image_url != new_image_url:
        delete_wardrobe_image(old_image_url)

    return _doc_to_public(doc)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(RATE_LIMIT_WARDROBE_WRITE)
async def delete_item(
    request: Request,
    item_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(item_id)
    user_id = str(current_user["_id"])

    doc = await db.wardrobe.find_one({"_id": oid, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    await db.wardrobe.delete_one({"_id": oid, "user_id": user_id})
    delete_wardrobe_image(doc.get("image_url"))

    # Keep this user's saved outfits consistent: drop the reference to the
    # deleted item, and remove any outfit that ends up with no items left.
    await db.outfits.update_many(
        {"user_id": user_id, "item_ids": item_id},
        {"$pull": {"item_ids": item_id}},
    )
    empty_outfits = await db.outfits.find(
        {"user_id": user_id, "item_ids": {"$size": 0}}, {"_id": 1}
    ).to_list(length=None)
    if empty_outfits:
        empty_oids = [o["_id"] for o in empty_outfits]
        empty_ids = [str(o["_id"]) for o in empty_outfits]
        await db.outfits.delete_many({"_id": {"$in": empty_oids}})
        await db.feedback.delete_many({"user_id": user_id, "outfit_id": {"$in": empty_ids}})