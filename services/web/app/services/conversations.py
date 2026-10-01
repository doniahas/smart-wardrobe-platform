from datetime import datetime, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError


def pair_key(user_id_a: str, user_id_b: str) -> str:
    """Deterministic key for an unordered pair of user ids.

    Used both for the conversations.pair_key unique index and for looking
    up an existing conversation without creating one (see
    routers/messages.py::get_message_history, which must never create a
    conversation as a side effect of a read).
    """
    a, b = sorted([user_id_a, user_id_b])
    return f"{a}:{b}"


async def get_or_create_conversation(db, user_a: dict, user_b: dict) -> dict:
    """Atomically get-or-create the 1:1 conversation between two users.

    user_a/user_b: {"user_id": str, "username": str}.

    find_one_and_update with upsert=True is itself atomic per document, so
    this single call covers the common case with no race. A
    DuplicateKeyError here only happens in the narrow window where two
    concurrent upserts for the same pair_key both decide to insert before
    the unique index rejects the second one - in that case the loser simply
    fetches what the winner already created. This is deliberately simpler
    than services/reactions.py's retry loop: there is only ever one
    possible outcome to converge on here (the single conversation for this
    pair), not a range of valid state transitions to re-evaluate.
    """
    key = pair_key(user_a["user_id"], user_b["user_id"])
    participants = sorted([user_a, user_b], key=lambda p: p["user_id"])
    now = datetime.now(timezone.utc)

    try:
        return await db.conversations.find_one_and_update(
            {"pair_key": key},
            {
                "$setOnInsert": {
                    "pair_key": key,
                    "participants": participants,
                    "created_at": now,
                    "last_message_at": now,
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return await db.conversations.find_one({"pair_key": key})
