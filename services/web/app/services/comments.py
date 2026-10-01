from bson import ObjectId

from app.services.image_storage import delete_explore_image

# Mirrors services/reactions.py's _TARGET_ID_CHUNK_SIZE rationale (same
# value, kept as a separate local constant rather than imported, since that
# module is meant to stay unchanged and this isn't reaction logic): a single
# post could accumulate an unbounded number of comments, so the cascade
# delete below chunks its $in query instead of using one unbounded list.
_COMMENT_ID_CHUNK_SIZE = 500


async def batch_comment_counts(db, post_ids: list[ObjectId]) -> dict[ObjectId, int]:
    """comments_count for a page of posts in one query.

    Always bounded to a single page of ids by every current caller (see
    core/pagination.py's MAX_PAGE_SIZE), so - unlike delete_comments_for_post
    below - this never needs chunking.
    """
    counts = {pid: 0 for pid in post_ids}
    if not post_ids:
        return counts

    cursor = db.comments.find({"post_id": {"$in": post_ids}}, {"post_id": 1})
    async for doc in cursor:
        counts[doc["post_id"]] += 1
    return counts


async def delete_comments_for_post(db, post_id: ObjectId) -> None:
    """Cascade-delete every comment on a post - and each comment's own
    attachment files and reactions - when the post itself is deleted. The
    post's own attachments/reactions are deleted separately by the caller
    (routers/posts.py::delete_post).
    """
    comments = [
        doc async for doc in db.comments.find({"post_id": post_id}, {"_id": 1, "attachments": 1})
    ]
    comment_oids = [doc["_id"] for doc in comments]

    for doc in comments:
        for attachment in doc.get("attachments", []):
            delete_explore_image(attachment["url"])

    for start in range(0, len(comment_oids), _COMMENT_ID_CHUNK_SIZE):
        chunk = comment_oids[start : start + _COMMENT_ID_CHUNK_SIZE]
        await db.reactions.delete_many({"target_type": "comment", "target_id": {"$in": chunk}})

    await db.comments.delete_many({"post_id": post_id})
