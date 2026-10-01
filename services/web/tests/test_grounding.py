import logging

import pytest

from app.services.grounding import (
    GroundedWardrobeItem,
    VerificationFailure,
    VerificationFailureReason,
    WARDROBE_VERIFIED_FIELDS,
    ground_recommended_items,
    ground_wardrobe_item,
)


@pytest.mark.asyncio
async def test_ground_wardrobe_item_returns_verified_item_for_owner(db, make_user, make_wardrobe_item):
    user = await make_user("alice")
    item_id = await make_wardrobe_item(user["user_id"], color="red", name="Red Shirt")

    result = await ground_wardrobe_item(item_id, user["user_id"], db)

    assert isinstance(result, GroundedWardrobeItem)
    assert result.source_type == "wardrobe"
    assert result.source_id == item_id
    assert result.item.id == item_id
    assert result.item.color == "red"
    assert result.item.name == "Red Shirt"
    assert result.verified_at is not None


@pytest.mark.asyncio
async def test_ground_wardrobe_item_rejects_nonexistent_id(db, make_user):
    user = await make_user("bob")
    # A well-formed ObjectId (24 hex chars) that was never inserted.
    fake_id = "507f1f77bcf86cd799439011"

    result = await ground_wardrobe_item(fake_id, user["user_id"], db)

    assert isinstance(result, VerificationFailure)
    assert result.requested_id == fake_id
    assert result.reason == VerificationFailureReason.NOT_FOUND


@pytest.mark.asyncio
async def test_ground_wardrobe_item_rejects_another_users_item(db, make_user, make_wardrobe_item):
    owner = await make_user("carol")
    attacker = await make_user("mallory")
    owner_item_id = await make_wardrobe_item(owner["user_id"])

    result = await ground_wardrobe_item(owner_item_id, attacker["user_id"], db)

    assert isinstance(result, VerificationFailure)
    assert result.requested_id == owner_item_id
    assert result.reason == VerificationFailureReason.NOT_OWNED


@pytest.mark.asyncio
async def test_ground_wardrobe_item_rejects_malformed_id(db, make_user):
    user = await make_user("dave")

    result = await ground_wardrobe_item("not-an-object-id", user["user_id"], db)

    assert isinstance(result, VerificationFailure)
    assert result.requested_id == "not-an-object-id"
    assert result.reason == VerificationFailureReason.INVALID_ID


@pytest.mark.asyncio
async def test_ground_wardrobe_item_verified_fields_are_not_invented(db, make_user, make_wardrobe_item):
    """Verified fields must be exactly the known wardrobe attributes - the
    grounding layer must never claim to have verified something it didn't
    actually check."""
    user = await make_user("erin")
    item_id = await make_wardrobe_item(user["user_id"])

    result = await ground_wardrobe_item(item_id, user["user_id"], db)

    assert isinstance(result, GroundedWardrobeItem)
    assert set(result.verified_fields) == set(WARDROBE_VERIFIED_FIELDS)
    # No extra unsupported attributes (e.g. "brand", "price") on the model.
    assert set(result.item.model_dump().keys()) == {
        "name", "clothing_type", "color", "color_hex", "season", "style", "occasion",
        "image_url", "tags", "id", "user_id",
    }


@pytest.mark.asyncio
async def test_ground_recommended_items_splits_success_and_failure_preserving_order(
    db, make_user, make_wardrobe_item
):
    user = await make_user("frank")
    other = await make_user("grace")

    item_1 = await make_wardrobe_item(user["user_id"], name="First")
    other_item = await make_wardrobe_item(other["user_id"])
    item_2 = await make_wardrobe_item(user["user_id"], name="Second")

    grounded, failures = await ground_recommended_items(
        [item_1, other_item, "bad-id", item_2], user["user_id"], db
    )

    assert [g.source_id for g in grounded] == [item_1, item_2]
    assert [f.requested_id for f in failures] == [other_item, "bad-id"]
    assert failures[0].reason == VerificationFailureReason.NOT_OWNED
    assert failures[1].reason == VerificationFailureReason.INVALID_ID


@pytest.mark.asyncio
async def test_ground_recommended_items_empty_list_returns_empty_results(db, make_user):
    user = await make_user("henry")

    grounded, failures = await ground_recommended_items([], user["user_id"], db)

    assert grounded == []
    assert failures == []


@pytest.mark.asyncio
async def test_ground_wardrobe_item_deleted_item_is_rejected(db, make_user, make_wardrobe_item):
    user = await make_user("ivan")
    item_id = await make_wardrobe_item(user["user_id"])

    from bson import ObjectId
    await db.wardrobe.delete_one({"_id": ObjectId(item_id)})

    result = await ground_wardrobe_item(item_id, user["user_id"], db)

    assert isinstance(result, VerificationFailure)
    assert result.reason == VerificationFailureReason.NOT_FOUND


@pytest.mark.asyncio
async def test_ground_wardrobe_item_logs_failure_without_exposing_document(
    db, make_user, make_wardrobe_item, caplog
):
    owner = await make_user("julia")
    attacker = await make_user("kim")
    owner_item_id = await make_wardrobe_item(owner["user_id"], name="Secret Jacket", color="purple")

    with caplog.at_level(logging.WARNING, logger="app.services.grounding"):
        await ground_wardrobe_item(owner_item_id, attacker["user_id"], db)

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert owner_item_id in message
    assert attacker["user_id"] in message
    assert "not_owned" in message
    # The rejected item's actual attributes must never leak into the log.
    assert "Secret Jacket" not in message
    assert "purple" not in message
