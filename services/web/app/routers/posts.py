import logging
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status

from app.auth import get_current_user
from app.core.config import (
    MAX_EXPLORE_ATTACHMENTS,
    RATE_LIMIT_COMMENT_WRITE,
    RATE_LIMIT_POST_WRITE,
    RATE_LIMIT_REACTION_WRITE,
    RATE_LIMIT_RECOMMENDATION,
)
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.pagination import Page, PageParams
from app.models import (
    AttachmentPublic,
    CommentPublic,
    MyPostsPage,
    PostPublic,
    ReactionPublic,
    ReactionRequest,
    RecreateResponse,
    SharedOutfitComponentSnapshot,
    SharedOutfitSnapshot,
    _first_or_scalar,
)
from app.services.comments import batch_comment_counts, delete_comments_for_post
from app.services.image_storage import (
    delete_explore_image,
    save_explore_image,
)
from app.services.notifications import create_notification
from app.services.reactions import (
    ReactionConflict,
    apply_reaction,
    batch_reaction_summary,
    count_reactions_for_targets,
    count_target_reactions,
)
from app.services.recreate import match_snapshot_to_wardrobe

router = APIRouter(prefix="/posts", tags=["posts"])
logger = logging.getLogger(__name__)

_POST_TITLE_MAX_LENGTH = 150
_POST_BODY_MAX_LENGTH = 2000
_COMMENT_BODY_MAX_LENGTH = 1000


def _parse_oid(post_id: str) -> ObjectId:
    try:
        return ObjectId(post_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid post id")


def _reject_blank(value: str, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{field} cannot be blank"
        )
    return stripped


async def _save_attachments(images: list[UploadFile]) -> list[dict]:
    """Validate and save each attachment via save_explore_image, in order.

    Despite its name, save_explore_image accepts either an image or
    an MP4 video per file, detected from real content - not from this
    field's name or any per-file type hint - so a single request may mix
    images and videos freely; every file still counts toward the same
    MAX_EXPLORE_ATTACHMENTS cap below regardless of which kind it turns
    out to be.

    If any attachment fails validation, every attachment already saved
    earlier in this same call is deleted before the failure propagates - a
    rejected multi-file request must never leave partial attachments
    behind on disk.
    """
    non_empty = [f for f in images if f.filename]
    if len(non_empty) > MAX_EXPLORE_ATTACHMENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many attachments (max {MAX_EXPLORE_ATTACHMENTS}).",
        )

    saved: list[dict] = []
    try:
        for file in non_empty:
            saved.append(await save_explore_image(file))
    except Exception:
        for attachment in saved:
            delete_explore_image(attachment["url"])
        raise
    return saved


def _doc_to_public(
    doc: dict,
    likes_count: int = 0,
    dislikes_count: int = 0,
    my_reaction: int | None = None,
    comments_count: int = 0,
) -> PostPublic:
    is_anonymous = doc["is_anonymous"]
    shared_outfit_doc = doc.get("shared_outfit")
    return PostPublic(
        id=str(doc["_id"]),
        author_id=None if is_anonymous else doc["author_id"],
        author_username=None if is_anonymous else doc.get("author_username"),
        is_anonymous=is_anonymous,
        title=doc["title"],
        body=doc["body"],
        attachments=[AttachmentPublic(**a) for a in doc.get("attachments", [])],
        shared_outfit=SharedOutfitSnapshot(**shared_outfit_doc) if shared_outfit_doc else None,
        likes_count=likes_count,
        dislikes_count=dislikes_count,
        comments_count=comments_count,
        my_reaction=my_reaction,
        created_at=doc["created_at"],
    )


async def _build_shared_outfit_snapshot(shared_outfit_id: str, author_id: str, db) -> SharedOutfitSnapshot:
    """Verify the outfit belongs to the post's author, then build an
    immutable, sanitized snapshot from it - never a reference to the outfit
    or wardrobe item ids (see models.py's SharedOutfitSnapshot note)."""
    try:
        oid = ObjectId(shared_outfit_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid outfit id")

    outfit_doc = await db.outfits.find_one({"_id": oid, "user_id": author_id})
    if outfit_doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outfit not found")

    item_oids = [ObjectId(item_id) for item_id in outfit_doc["item_ids"]]
    items_cursor = db.wardrobe.find({"_id": {"$in": item_oids}, "user_id": author_id})
    # style/occasion: _first_or_scalar is a temporary compatibility shim -
    # see its docstring in models.py. This snapshot type stays scalar
    # while the outfit-sharing feature it feeds is being simplified
    # separately; only the source item's style/occasion may now be a list.
    components = [
        SharedOutfitComponentSnapshot(
            clothing_type=item["clothing_type"],
            color=item["color"],
            season=item["season"],
            style=_first_or_scalar(item["style"]),
            occasion=_first_or_scalar(item["occasion"]),
        )
        async for item in items_cursor
    ]
    if not components:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This outfit no longer has any items and cannot be shared",
        )

    return SharedOutfitSnapshot(
        outfit_name=outfit_doc.get("name"),
        outfit_occasion=outfit_doc.get("occasion"),
        outfit_season=outfit_doc.get("season"),
        outfit_style=outfit_doc.get("style"),
        components=components,
    )


def _comment_doc_to_public(
    doc: dict, likes_count: int = 0, dislikes_count: int = 0, my_reaction: int | None = None
) -> CommentPublic:
    return CommentPublic(
        id=str(doc["_id"]),
        post_id=str(doc["post_id"]),
        author_id=doc["author_id"],
        author_username=doc["author_username"],
        body=doc["body"],
        attachments=[AttachmentPublic(**a) for a in doc.get("attachments", [])],
        likes_count=likes_count,
        dislikes_count=dislikes_count,
        my_reaction=my_reaction,
        created_at=doc["created_at"],
    )


async def _paginated_posts(query: dict, pagination: PageParams, db, viewer_user_id: str) -> Page[PostPublic]:
    cursor = (
        db.posts.find(query)
        .sort("created_at", -1)
        .skip(pagination.skip)
        .limit(pagination.page_size + 1)
    )
    docs = [doc async for doc in cursor]
    has_more = len(docs) > pagination.page_size
    docs = docs[: pagination.page_size]

    post_oids = [doc["_id"] for doc in docs]
    reaction_summary = await batch_reaction_summary(db, "post", post_oids, viewer_user_id)
    comment_counts = await batch_comment_counts(db, post_oids)

    items = [
        _doc_to_public(
            doc,
            likes_count=reaction_summary[doc["_id"]]["likes"],
            dislikes_count=reaction_summary[doc["_id"]]["dislikes"],
            my_reaction=reaction_summary[doc["_id"]]["my_reaction"],
            comments_count=comment_counts[doc["_id"]],
        )
        for doc in docs
    ]
    return Page[PostPublic](
        items=items,
        page=pagination.page,
        page_size=pagination.page_size,
        has_more=has_more,
    )


@router.post("", response_model=PostPublic, status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_POST_WRITE)
async def create_post(
    request: Request,
    title: str = Form(..., min_length=1, max_length=_POST_TITLE_MAX_LENGTH),
    body: str = Form(..., min_length=1, max_length=_POST_BODY_MAX_LENGTH),
    is_anonymous: bool = Form(False),
    shared_outfit_id: str | None = Form(default=None),
    images: list[UploadFile] = File(default=[]),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    title = _reject_blank(title, "title")
    body = _reject_blank(body, "body")
    author_id = str(current_user["_id"])

    shared_outfit = None
    if shared_outfit_id:
        shared_outfit = await _build_shared_outfit_snapshot(shared_outfit_id, author_id, db)

    attachments = await _save_attachments(images)

    doc = {
        "author_id": author_id,
        "author_username": current_user["username"],
        "is_anonymous": is_anonymous,
        "title": title,
        "body": body,
        "attachments": attachments,
        "shared_outfit": shared_outfit.model_dump() if shared_outfit else None,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.posts.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _doc_to_public(doc)


@router.get("", response_model=Page[PostPublic])
async def list_posts(
    author_id: str | None = Query(default=None),
    pagination: PageParams = Depends(),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """The public Explore feed - visible to any authenticated user, not just
    the author.

    `author_id`, when given, narrows the feed to a lightweight public-profile
    view of that one user's own posts - reusing this same route rather than
    adding a second one, since the serialization, pagination, reaction
    counts and comment counts are otherwise identical to the plain feed.
    Their anonymous posts are always excluded from this filtered view: an
    author who chose to post anonymously must not have that choice undone
    by the mere existence of a filter that groups posts by author, even
    though `is_anonymous=False` is the only difference from a raw
    `{"author_id": ...}` query and no anonymous field is ever exposed either
    way (see `_doc_to_public`). A nonexistent or garbage `author_id` simply
    matches nothing, like every other id-shaped filter in this app - never
    a 404, since "this user has no public posts" is a normal empty state,
    not an error.
    """
    query: dict = {} if author_id is None else {"author_id": author_id, "is_anonymous": False}
    return await _paginated_posts(query, pagination, db, str(current_user["_id"]))


@router.get("/mine", response_model=MyPostsPage)
async def list_my_posts(
    pagination: PageParams = Depends(),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """The personal-area post list, including aggregate likes/dislikes
    received across every post this user has ever authored - not just the
    current page."""
    user_id = str(current_user["_id"])
    page = await _paginated_posts({"author_id": user_id}, pagination, db, user_id)

    all_post_oids = [doc["_id"] async for doc in db.posts.find({"author_id": user_id}, {"_id": 1})]
    total_likes, total_dislikes = await count_reactions_for_targets(db, "post", all_post_oids)

    return MyPostsPage(
        items=page.items,
        page=page.page,
        page_size=page.page_size,
        has_more=page.has_more,
        total_likes_received=total_likes,
        total_dislikes_received=total_dislikes,
    )


@router.post("/{post_id}/reactions", response_model=ReactionPublic)
@limiter.limit(RATE_LIMIT_REACTION_WRITE)
async def react_to_post(
    request: Request,
    post_id: str,
    payload: ReactionRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(post_id)
    post_doc = await db.posts.find_one({"_id": oid})
    if post_doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    user_id = str(current_user["_id"])
    try:
        my_reaction = await apply_reaction(db, user_id, "post", oid, payload.value)
    except ReactionConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not process this reaction due to concurrent updates. Please try again.",
        )

    if my_reaction is not None:
        try:
            await create_notification(
                db,
                recipient_id=post_doc["author_id"],
                actor_id=user_id,
                actor_username=current_user["username"],
                notif_type="like_post" if my_reaction == 1 else "dislike_post",
                target_type="post",
                target_id=oid,
            )
        except Exception:
            logger.exception("react_to_post: unexpected error creating notification")

    likes_count, dislikes_count = await count_target_reactions(db, "post", oid)
    return ReactionPublic(my_reaction=my_reaction, likes_count=likes_count, dislikes_count=dislikes_count)


@router.post("/{post_id}/comments", response_model=CommentPublic, status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_COMMENT_WRITE)
async def create_comment(
    request: Request,
    post_id: str,
    body: str = Form(..., min_length=1, max_length=_COMMENT_BODY_MAX_LENGTH),
    images: list[UploadFile] = File(default=[]),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    oid = _parse_oid(post_id)
    exists = await db.posts.count_documents({"_id": oid}, limit=1)
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    body = _reject_blank(body, "body")
    attachments = await _save_attachments(images)

    doc = {
        "post_id": oid,
        "author_id": str(current_user["_id"]),
        "author_username": current_user["username"],
        "body": body,
        "attachments": attachments,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.comments.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _comment_doc_to_public(doc)


@router.get("/{post_id}/comments", response_model=Page[CommentPublic])
async def list_comments(
    post_id: str,
    pagination: PageParams = Depends(),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Comments on a post, oldest-first, with each comment's own like/dislike
    counts and the caller's own reaction populated via a single batched
    reaction-summary query for the comment ids on this page - never a
    per-comment query."""
    oid = _parse_oid(post_id)
    exists = await db.posts.count_documents({"_id": oid}, limit=1)
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    cursor = (
        db.comments.find({"post_id": oid})
        .sort("created_at", 1)
        .skip(pagination.skip)
        .limit(pagination.page_size + 1)
    )
    docs = [doc async for doc in cursor]
    has_more = len(docs) > pagination.page_size
    docs = docs[: pagination.page_size]

    comment_oids = [doc["_id"] for doc in docs]
    viewer_user_id = str(current_user["_id"])
    summary = await batch_reaction_summary(db, "comment", comment_oids, viewer_user_id)

    items = [
        _comment_doc_to_public(
            doc,
            likes_count=summary[doc["_id"]]["likes"],
            dislikes_count=summary[doc["_id"]]["dislikes"],
            my_reaction=summary[doc["_id"]]["my_reaction"],
        )
        for doc in docs
    ]
    return Page[CommentPublic](
        items=items,
        page=pagination.page,
        page_size=pagination.page_size,
        has_more=has_more,
    )


@router.get("/{post_id}/recreate", response_model=RecreateResponse)
@limiter.limit(RATE_LIMIT_RECOMMENDATION)
async def recreate_post_outfit(
    request: Request,
    post_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Match the post's shared outfit snapshot against the caller's own
    wardrobe. Read-only - never modifies the wardrobe. Works identically
    whether the caller is the post's original author or any other user,
    since matching only ever reads the caller's own wardrobe (see
    services/recreate.py) - never the original author's."""
    oid = _parse_oid(post_id)
    doc = await db.posts.find_one({"_id": oid})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    shared_outfit_doc = doc.get("shared_outfit")
    if not shared_outfit_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="This post has no shared outfit to recreate"
        )

    snapshot = SharedOutfitSnapshot(**shared_outfit_doc)
    user_id = str(current_user["_id"])
    matches = await match_snapshot_to_wardrobe(snapshot, user_id, db)
    return RecreateResponse(post_id=post_id, matches=matches)


# The former "Recreate from one of a post's own photos" route
# (POST /{post_id}/recreate-from-photo) was removed - a post with no
# shared_outfit snapshot now has no recreate affordance at all, rather
# than falling back to an AI-derived reading of its photo.


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(RATE_LIMIT_POST_WRITE)
async def delete_post(
    request: Request,
    post_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Delete one of the caller's own posts, along with its own images, its
    reactions, its comments (and their images), and the reactions on each
    of those comments."""
    oid = _parse_oid(post_id)
    user_id = str(current_user["_id"])
    doc = await db.posts.find_one_and_delete({"_id": oid, "author_id": user_id})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    for attachment in doc.get("attachments", []):
        delete_explore_image(attachment["url"])

    await delete_comments_for_post(db, oid)
    await db.reactions.delete_many({"target_type": "post", "target_id": oid})
