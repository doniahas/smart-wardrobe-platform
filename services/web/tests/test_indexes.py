import pytest

from app.core.database import _ensure_indexes


@pytest.mark.asyncio
async def test_ensure_indexes_creates_expected_indexes(db):
    await _ensure_indexes(db)

    users_indexes = await db.users.index_information()
    assert users_indexes["username_1"]["unique"] is True
    assert users_indexes["email_1"]["unique"] is True

    wardrobe_indexes = await db.wardrobe.index_information()
    assert "user_id_1" in wardrobe_indexes
    for field in ("clothing_type", "season", "style", "occasion", "color"):
        assert f"user_id_1_{field}_1" in wardrobe_indexes

    outfits_indexes = await db.outfits.index_information()
    assert "user_id_1_item_ids_1" in outfits_indexes

    feedback_indexes = await db.feedback.index_information()
    assert feedback_indexes["user_id_1_outfit_id_1"]["unique"] is True

    posts_indexes = await db.posts.index_information()
    assert "created_at_-1" in posts_indexes
    assert "author_id_1_created_at_-1" in posts_indexes

    reactions_indexes = await db.reactions.index_information()
    assert reactions_indexes["user_id_1_target_type_1_target_id_1"]["unique"] is True
    assert "target_type_1_target_id_1_value_1" in reactions_indexes

    comments_indexes = await db.comments.index_information()
    assert "post_id_1_created_at_1" in comments_indexes

    conversations_indexes = await db.conversations.index_information()
    assert conversations_indexes["pair_key_1"]["unique"] is True
    assert "participants.user_id_1_last_message_at_-1__id_-1" in conversations_indexes

    direct_messages_indexes = await db.direct_messages.index_information()
    assert "conversation_id_1_created_at_-1__id_-1" in direct_messages_indexes

    notifications_indexes = await db.notifications.index_information()
    assert "recipient_id_1_created_at_-1__id_-1" in notifications_indexes
    assert "recipient_id_1_read_1" in notifications_indexes

    recommendation_feedback_indexes = await db.recommendation_feedback.index_information()
    assert "user_id_1_liked_1_reason_scope_1" in recommendation_feedback_indexes


@pytest.mark.asyncio
async def test_ensure_indexes_is_idempotent(db):
    """Startup calls this on every boot - running it repeatedly must not raise."""
    await _ensure_indexes(db)
    await _ensure_indexes(db)
    await _ensure_indexes(db)

    wardrobe_indexes = await db.wardrobe.index_information()
    assert "user_id_1" in wardrobe_indexes