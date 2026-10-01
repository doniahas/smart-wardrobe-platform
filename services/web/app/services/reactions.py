from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

_MAX_REACTION_RETRIES = 5

# Bounds any single $in query used for a grand total across a user's own
# targets (see count_reactions_for_targets) - a prolific poster could have
# an unbounded number of posts, so that total is summed across chunks
# instead of one $in over every id at once. Not needed for
# batch_reaction_summary, which only ever covers a single page (already
# bounded by core/pagination.py's MAX_PAGE_SIZE).
_TARGET_ID_CHUNK_SIZE = 500


class ReactionConflict(Exception):
    """Raised when apply_reaction exhausts its CAS retry budget without
    converging (see routers/posts.py for how this becomes an HTTP 409).

    Not a routine code path - a safety net against pathological contention,
    never expected to fire in normal operation.
    """


async def apply_reaction(
    db, user_id: str, target_type: str, target_id: ObjectId, requested_value: int
) -> Optional[int]:
    """Atomically toggle/switch/remove a user's reaction to a target.

    Returns the caller's resulting reaction value (1 or -1), or None if the
    reaction was just removed (clicking the currently-active reaction
    again). Every state transition is a single compare-and-swap Mongo
    operation guarded by the value just read in that same attempt - if a
    concurrent request changes the document first, the CAS matches nothing
    and the loop retries with a fresh read, so the outcome always reflects
    genuinely current state rather than a stale snapshot. A brand new
    reaction relies on the unique index (user_id, target_type, target_id)
    to reject a concurrent duplicate insert the same way.
    """
    for _ in range(_MAX_REACTION_RETRIES):
        existing = await db.reactions.find_one(
            {"user_id": user_id, "target_type": target_type, "target_id": target_id}
        )

        if existing is None:
            try:
                await db.reactions.insert_one(
                    {
                        "user_id": user_id,
                        "target_type": target_type,
                        "target_id": target_id,
                        "value": requested_value,
                        "created_at": datetime.now(timezone.utc),
                    }
                )
                return requested_value
            except DuplicateKeyError:
                continue

        if existing["value"] == requested_value:
            result = await db.reactions.delete_one(
                {"_id": existing["_id"], "value": existing["value"]}
            )
            if result.deleted_count == 1:
                return None
            continue

        result = await db.reactions.update_one(
            {"_id": existing["_id"], "value": existing["value"]},
            {"$set": {"value": requested_value}},
        )
        if result.matched_count == 1:
            return requested_value

    raise ReactionConflict()


async def count_target_reactions(db, target_type: str, target_id: ObjectId) -> tuple[int, int]:
    """Likes/dislikes for a single target, derived from stored reactions."""
    likes = await db.reactions.count_documents(
        {"target_type": target_type, "target_id": target_id, "value": 1}
    )
    dislikes = await db.reactions.count_documents(
        {"target_type": target_type, "target_id": target_id, "value": -1}
    )
    return likes, dislikes


async def batch_reaction_summary(
    db, target_type: str, target_ids: list[ObjectId], viewer_user_id: str
) -> dict[ObjectId, dict]:
    """likes/dislikes/my_reaction for a page of targets in one query.

    Always bounded to a single page of ids by every current caller (see
    core/pagination.py's MAX_PAGE_SIZE), so - unlike
    count_reactions_for_targets - this never needs chunking.
    """
    summary = {tid: {"likes": 0, "dislikes": 0, "my_reaction": None} for tid in target_ids}
    if not target_ids:
        return summary

    cursor = db.reactions.find({"target_type": target_type, "target_id": {"$in": target_ids}})
    async for doc in cursor:
        tid = doc["target_id"]
        if doc["value"] == 1:
            summary[tid]["likes"] += 1
        else:
            summary[tid]["dislikes"] += 1
        if doc["user_id"] == viewer_user_id:
            summary[tid]["my_reaction"] = doc["value"]
    return summary


async def count_reactions_for_targets(
    db, target_type: str, target_ids: list[ObjectId]
) -> tuple[int, int]:
    """Grand total likes/dislikes across a set of targets, e.g. every post a
    user has ever authored - processed in bounded chunks rather than one
    unbounded $in query, since this set (unlike batch_reaction_summary's)
    isn't page-limited.
    """
    total_likes = 0
    total_dislikes = 0
    for start in range(0, len(target_ids), _TARGET_ID_CHUNK_SIZE):
        chunk = target_ids[start : start + _TARGET_ID_CHUNK_SIZE]
        total_likes += await db.reactions.count_documents(
            {"target_type": target_type, "target_id": {"$in": chunk}, "value": 1}
        )
        total_dislikes += await db.reactions.count_documents(
            {"target_type": target_type, "target_id": {"$in": chunk}, "value": -1}
        )
    return total_likes, total_dislikes
