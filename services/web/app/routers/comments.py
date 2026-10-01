import logging

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import get_current_user
from app.core.config import RATE_LIMIT_COMMENT_WRITE, RATE_LIMIT_REACTION_WRITE
from app.core.database import get_db
from app.core.limiter import limiter
from app.models import ReactionPublic, ReactionRequest
from app.services.image_storage import delete_explore_image
from app.services.notifications import create_notification
from app.services.reactions import ReactionConflict, apply_reaction, count_target_reactions

router = APIRouter(prefix="/comments", tags=["comments"])
logger = logging.getLogger(__name__)


def _parse_oid(comment_id: str) -> ObjectId:
    try:
        return ObjectId(comment_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid comment id")


@router.delete("/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(RATE_LIMIT_COMMENT_WRITE)
async def delete_comment(
    request: Request,
    comment_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Delete one of the caller's own comments, along with its images and reactions."""
    oid = _parse_oid(comment_id)
    user_id = str(current_user["_id"])
    doc = await db.comments.find_one_and_delete({"_id": oid, "author_id": user_id})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")

    for attachment in doc.get("attachments", []):
        delete_explore_image(attachment["url"])

    await db.reactions.delete_many({"target_type": "comment", "target_id": oid})


@router.post("/{comment_id}/reactions", response_model=ReactionPublic)
@limiter.limit(RATE_LIMIT_REACTION_WRITE)
async def react_to_comment(
    request: Request,
    comment_id: str,
    payload: ReactionRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(comment_id)
    comment_doc = await db.comments.find_one({"_id": oid})
    if comment_doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")

    user_id = str(current_user["_id"])
    try:
        my_reaction = await apply_reaction(db, user_id, "comment", oid, payload.value)
    except ReactionConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not process this reaction due to concurrent updates. Please try again.",
        )

    if my_reaction is not None:
        try:
            await create_notification(
                db,
                recipient_id=comment_doc["author_id"],
                actor_id=user_id,
                actor_username=current_user["username"],
                notif_type="like_comment" if my_reaction == 1 else "dislike_comment",
                target_type="comment",
                target_id=oid,
            )
        except Exception:
            logger.exception("react_to_comment: unexpected error creating notification")

    likes_count, dislikes_count = await count_target_reactions(db, "comment", oid)
    return ReactionPublic(my_reaction=my_reaction, likes_count=likes_count, dislikes_count=dislikes_count)
