from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.core.config import DATABASE_NAME, MONGO_SERVER_SELECTION_TIMEOUT_MS, MONGO_URI

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None

async def _ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    """Create all indexes required by the query patterns used across the routers.

    Kept idempotent and separate from connect_db() so index creation can be
    reasoned about (and tested) independently of connection management.
    """
    # auth.py / routers/auth.py: lookup by username on every login, and both
    # fields must reject duplicates on register.
    await db.users.create_index("username", unique=True)
    await db.users.create_index("email", unique=True)

    # routers/wardrobe.py: every wardrobe endpoint scopes its query to the
    # current user's items.
    await db.wardrobe.create_index("user_id")

    # routers/wardrobe.py::list_items also supports optional filtering by
    # these fields, always combined with the owning user_id.
    await db.wardrobe.create_index([("user_id", 1), ("clothing_type", 1)])
    await db.wardrobe.create_index([("user_id", 1), ("season", 1)])
    await db.wardrobe.create_index([("user_id", 1), ("style", 1)])
    await db.wardrobe.create_index([("user_id", 1), ("occasion", 1)])
    await db.wardrobe.create_index([("user_id", 1), ("color", 1)])

    # routers/outfits.py: list/get/delete all scope to the current user's
    # saved outfits. Compound with item_ids so it also covers
    # wardrobe.py::delete_item's cascade lookup
    # (outfits.find/update_many({"user_id": ..., "item_ids": item_id})).
    await db.outfits.create_index([("user_id", 1), ("item_ids", 1)])

    # routers/feedback.py: one feedback per outfit per user (uniqueness), and
    # this compound index's user_id prefix also serves list_feedback's
    # find({"user_id": ...}) without a separate index.
    await db.feedback.create_index([("user_id", 1), ("outfit_id", 1)], unique=True)

    # routers/memory.py: one long-term memory profile per user.
    await db.memory.create_index("user_id", unique=True)

    # services/recommendation_feedback.py::count_matching_implicit_events:
    # counts this user's implicit (bare/combination-scope) events sharing
    # one liked value and one attribute value, per distinct attribute.
    await db.recommendation_feedback.create_index(
        [("user_id", 1), ("liked", 1), ("reason_scope", 1)]
    )

    # services/chat_queue.py::claim_next_job: atomically claims the
    # highest-priority, oldest pending job across all users.
    await db.chat_jobs.create_index([("status", 1), ("priority", -1), ("created_at", 1)])
    # services/chat_queue.py::reclaim_stale_jobs scans
    # for status == "processing" jobs whose started_at is past the staleness
    # cutoff - a separate access pattern from claim_next_job's above (that
    # one never filters/sorts on started_at), so it gets its own index.
    await db.chat_jobs.create_index([("status", 1), ("started_at", 1)])

    # routers/posts.py: the public feed lists all posts newest-first, and
    # GET /posts/mine scopes to the current user's own posts, also
    # newest-first.
    await db.posts.create_index([("created_at", -1)])
    await db.posts.create_index([("author_id", 1), ("created_at", -1)])

    # services/conversations.py: at most one conversation per unordered
    # pair of users (pair_key is a scalar, so - unlike a unique index on the
    # participants array itself - this correctly enforces pair uniqueness
    # rather than per-element uniqueness across the whole collection).
    await db.conversations.create_index([("pair_key", 1)], unique=True)
    # routers/messages.py::list_conversations: the caller's own
    # conversations, most-recently-active first, with _id as a
    # deterministic tiebreaker.
    await db.conversations.create_index(
        [("participants.user_id", 1), ("last_message_at", -1), ("_id", -1)]
    )
    # routers/messages.py::get_message_history: paginated, newest-first,
    # deterministic via the _id tiebreaker.
    await db.direct_messages.create_index(
        [("conversation_id", 1), ("created_at", -1), ("_id", -1)]
    )

    # services/notifications.py: paginated, newest-first notification list.
    await db.notifications.create_index(
        [("recipient_id", 1), ("created_at", -1), ("_id", -1)]
    )
    # routers/notifications.py: unread count and mark-all-read both scope
    # by (recipient_id, read).
    await db.notifications.create_index([("recipient_id", 1), ("read", 1)])

    # services/reactions.py: one reaction per user per target. The same
    # collection holds both post and comment reactions (target_type
    # discriminates); target_id is stored as the target's own ObjectId.
    await db.reactions.create_index(
        [("user_id", 1), ("target_type", 1), ("target_id", 1)], unique=True
    )
    # services/reactions.py: likes/dislikes for a target are always derived
    # by counting/filtering these documents, never denormalized elsewhere.
    await db.reactions.create_index([("target_type", 1), ("target_id", 1), ("value", 1)])

    # routers/posts.py::list_comments lists a post's comments oldest-first;
    # services/comments.py uses the post_id prefix for both the batch count
    # and the cascade delete on post removal.
    await db.comments.create_index([("post_id", 1), ("created_at", 1)])


async def connect_db() -> None:
    """Open one MongoDB connection when the FastAPI app starts."""
    global _client, _db
    # Bounded server-selection timeout (see core/config.py's own
    # comment) - without this, a request hitting an unreachable Mongo would
    # hang on the driver's 30s default before app/core/error_handlers.py's
    # global ConnectionFailure handler ever gets a chance to respond.
    _client = AsyncIOMotorClient(
        MONGO_URI, serverSelectionTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS
    )
    _db = _client[DATABASE_NAME]
    await _db.command("ping")
    await _ensure_indexes(_db)

async def close_db() -> None:
    """Close MongoDB connection when the FastAPI app shuts down."""
    global _client, _db
    if _client is not None:
        _client.close()
    _client = None
    _db = None

def get_db() -> AsyncIOMotorDatabase:
    """Return the active database object for routers/services."""
    if _db is None:
        raise RuntimeError("Database is not connected yet")
    return _db
