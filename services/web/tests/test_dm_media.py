import io
import re

import pytest
from PIL import Image

from app.services import image_storage
from tests.mp4_fixtures import (
    ftyp_plus_garbage_bytes,
    not_mp4_at_all_bytes,
    valid_mp4_bytes,
)


def _image_bytes(fmt: str, size=(20, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture()
def dm_upload_dir(tmp_path, monkeypatch):
    """Redirect saved DM uploads to a throwaway directory instead of the real one."""
    monkeypatch.setattr(image_storage, "DM_UPLOAD_DIR", tmp_path)
    return tmp_path


def _send(client, headers, other_user_id, body="hi", files=None):
    kwargs = {"data": {"body": body}, "headers": headers}
    if files is not None:
        kwargs["files"] = files
    return client.post(f"/messages/{other_user_id}", **kwargs)


# --- validation, mirroring test_explore_media.py's coverage ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fmt,ext,content_type",
    [
        ("JPEG", ".jpg", "image/jpeg"),
        ("PNG", ".png", "image/png"),
        ("WEBP", ".webp", "image/webp"),
    ],
)
async def test_valid_dm_image_is_accepted_and_saved(
    client, make_user, dm_upload_dir, fmt, ext, content_type
):
    sender = await make_user(f"dmimg_{fmt.lower()}_sender")
    recipient = await make_user(f"dmimg_{fmt.lower()}_recipient")
    data = _image_bytes(fmt)

    resp = _send(
        client,
        sender["headers"],
        recipient["user_id"],
        files={"images": (f"photo{ext}", data, content_type)},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert len(body["attachments"]) == 1
    attachment = body["attachments"][0]
    assert attachment["media_type"] == "image"
    assert attachment["content_type"] == content_type
    assert attachment["size_bytes"] == len(data)

    conversation_id = body["conversation_id"]
    expected_prefix = f"/messages/conversations/{conversation_id}/attachments/"
    assert attachment["url"].startswith(expected_prefix)

    filename = attachment["url"].rsplit("/", 1)[-1]
    assert filename.endswith(ext)
    assert re.fullmatch(rf"[0-9a-f]{{32}}\{ext}", filename)
    saved = dm_upload_dir / filename
    assert saved.is_file()
    assert saved.read_bytes() == data


@pytest.mark.asyncio
async def test_text_file_renamed_to_jpg_is_rejected(client, make_user, dm_upload_dir):
    sender = await make_user("dmimg_faketext_sender")
    recipient = await make_user("dmimg_faketext_recipient")

    resp = _send(
        client,
        sender["headers"],
        recipient["user_id"],
        files={"images": ("totally_a_photo.jpg", b"just plain text, not an image", "image/jpeg")},
    )
    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]
    assert list(dm_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_corrupted_image_bytes_are_rejected(client, make_user, dm_upload_dir):
    sender = await make_user("dmimg_corrupted_sender")
    recipient = await make_user("dmimg_corrupted_recipient")
    truncated = _image_bytes("PNG", size=(50, 50))[:100]

    resp = _send(
        client,
        sender["headers"],
        recipient["user_id"],
        files={"images": ("broken.png", truncated, "image/png")},
    )
    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]
    assert list(dm_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_unsupported_real_image_format_is_rejected(client, make_user, dm_upload_dir):
    sender = await make_user("dmimg_bmp_sender")
    recipient = await make_user("dmimg_bmp_recipient")
    data = _image_bytes("BMP")

    resp = _send(
        client, sender["headers"], recipient["user_id"], files={"images": ("photo.bmp", data, "image/bmp")}
    )
    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]
    assert list(dm_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_oversized_dm_image_rejected_with_413(client, make_user, dm_upload_dir, monkeypatch):
    monkeypatch.setattr(image_storage, "MAX_DM_IMAGE_SIZE_BYTES", 10)
    sender = await make_user("dmimg_oversized_sender")
    recipient = await make_user("dmimg_oversized_recipient")
    data = _image_bytes("PNG")
    assert len(data) > 10

    resp = _send(
        client, sender["headers"], recipient["user_id"], files={"images": ("photo.png", data, "image/png")}
    )
    assert resp.status_code == 413
    assert list(dm_upload_dir.iterdir()) == []


# --- attachment count limit ---


@pytest.mark.asyncio
async def test_max_dm_attachments_is_accepted(client, make_user, dm_upload_dir):
    sender = await make_user("dmimg_exactmax_sender")
    recipient = await make_user("dmimg_exactmax_recipient")
    data = _image_bytes("PNG")
    files = [("images", (f"p{i}.png", data, "image/png")) for i in range(4)]

    resp = _send(client, sender["headers"], recipient["user_id"], files=files)
    assert resp.status_code == 201
    assert len(resp.json()["attachments"]) == 4


@pytest.mark.asyncio
async def test_too_many_dm_attachments_rejected(client, make_user, dm_upload_dir):
    sender = await make_user("dmimg_toomany_sender")
    recipient = await make_user("dmimg_toomany_recipient")
    data = _image_bytes("PNG")
    files = [("images", (f"p{i}.png", data, "image/png")) for i in range(5)]

    resp = _send(client, sender["headers"], recipient["user_id"], files=files)
    assert resp.status_code == 400
    assert "Too many attachments" in resp.json()["detail"]
    assert list(dm_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_partial_failure_rolls_back_earlier_saved_files(client, make_user, dm_upload_dir):
    """A rejected multi-file request must leave no files behind, including
    ones already saved before a later file in the same request fails
    validation."""
    sender = await make_user("dmimg_partialfail_sender")
    recipient = await make_user("dmimg_partialfail_recipient")
    good = _image_bytes("PNG")
    files = [
        ("images", ("good.png", good, "image/png")),
        ("images", ("bad.jpg", b"not a real image", "image/jpeg")),
    ]

    resp = _send(client, sender["headers"], recipient["user_id"], files=files)
    assert resp.status_code == 400
    assert list(dm_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_failed_message_insert_leaves_no_orphaned_dm_files(
    client, make_user, db, dm_upload_dir, monkeypatch
):
    """Required by the send-flow ordering: images are saved before the
    message document is inserted, so a failure in that insert must roll
    back every image already saved for this request - not just failures
    within the image-validation loop itself."""
    sender = await make_user("dminsertfail_sender")
    recipient = await make_user("dminsertfail_recipient")
    data = _image_bytes("PNG")

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated message insert failure")

    monkeypatch.setattr(type(db.direct_messages), "insert_one", _boom)

    with pytest.raises(RuntimeError):
        _send(
            client,
            sender["headers"],
            recipient["user_id"],
            files={"images": ("p.png", data, "image/png")},
        )

    assert list(dm_upload_dir.iterdir()) == []
    assert await db.direct_messages.count_documents({}) == 0


# --- authenticated, participant-scoped retrieval ---


@pytest.mark.asyncio
async def test_sender_can_retrieve_attachment(client, make_user, dm_upload_dir):
    sender = await make_user("dmretrieve_sender")
    recipient = await make_user("dmretrieve_recipient")
    data = _image_bytes("PNG")
    resp = _send(
        client, sender["headers"], recipient["user_id"], files={"images": ("p.png", data, "image/png")}
    )
    url = resp.json()["attachments"][0]["url"]

    get_resp = client.get(url, headers=sender["headers"])
    assert get_resp.status_code == 200
    assert get_resp.content == data
    assert get_resp.headers["content-type"] == "image/png"


@pytest.mark.asyncio
async def test_recipient_can_retrieve_attachment(client, make_user, dm_upload_dir):
    sender = await make_user("dmretrieve2_sender")
    recipient = await make_user("dmretrieve2_recipient")
    data = _image_bytes("JPEG")
    resp = _send(
        client, sender["headers"], recipient["user_id"], files={"images": ("p.jpg", data, "image/jpeg")}
    )
    url = resp.json()["attachments"][0]["url"]

    get_resp = client.get(url, headers=recipient["headers"])
    assert get_resp.status_code == 200
    assert get_resp.content == data


@pytest.mark.asyncio
async def test_unrelated_user_cannot_retrieve_attachment(client, make_user, dm_upload_dir):
    sender = await make_user("dmretrieve3_sender")
    recipient = await make_user("dmretrieve3_recipient")
    outsider = await make_user("dmretrieve3_outsider")
    data = _image_bytes("PNG")
    resp = _send(
        client, sender["headers"], recipient["user_id"], files={"images": ("p.png", data, "image/png")}
    )
    url = resp.json()["attachments"][0]["url"]

    get_resp = client.get(url, headers=outsider["headers"])
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_unauthenticated_cannot_retrieve_attachment(client, make_user, dm_upload_dir):
    sender = await make_user("dmretrieve4_sender")
    recipient = await make_user("dmretrieve4_recipient")
    data = _image_bytes("PNG")
    resp = _send(
        client, sender["headers"], recipient["user_id"], files={"images": ("p.png", data, "image/png")}
    )
    url = resp.json()["attachments"][0]["url"]

    get_resp = client.get(url)
    assert get_resp.status_code == 401


@pytest.mark.asyncio
async def test_retrieve_nonexistent_filename_in_real_conversation_returns_404(
    client, make_user, dm_upload_dir
):
    sender = await make_user("dmretrieve5_sender")
    recipient = await make_user("dmretrieve5_recipient")
    _send(client, sender["headers"], recipient["user_id"])  # no attachments

    conversation_id = client.get(
        f"/messages/{recipient['user_id']}", headers=sender["headers"]
    ).json()["items"][0]["conversation_id"]

    resp = client.get(
        f"/messages/conversations/{conversation_id}/attachments/does-not-exist.png",
        headers=sender["headers"],
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_retrieve_invalid_conversation_id_returns_400(client, make_user):
    user = await make_user("dmretrieve6_user")
    resp = client.get(
        "/messages/conversations/not-an-id/attachments/x.png", headers=user["headers"]
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cross_conversation_attachment_is_not_leaked(client, make_user, dm_upload_dir):
    """A participant of conversation A must not be able to fetch a file
    that actually belongs to conversation B by swapping in A's
    conversation_id against B's filename - the stored URL embeds B's own
    conversation_id, so the exact-match lookup scoped to A's id must fail."""
    a1 = await make_user("crossconvo_a1")
    a2 = await make_user("crossconvo_a2")
    b1 = await make_user("crossconvo_b1")
    b2 = await make_user("crossconvo_b2")

    data = _image_bytes("PNG")
    # Conversation A: a1 <-> a2, with an attachment.
    resp_a = _send(client, a1["headers"], a2["user_id"], files={"images": ("p.png", data, "image/png")})
    conversation_a_id = resp_a.json()["conversation_id"]

    # Conversation B: b1 <-> b2, no attachment - a2 is not a participant of B.
    _send(client, b1["headers"], b2["user_id"])
    conversation_b_id = client.get(
        f"/messages/{b2['user_id']}", headers=b1["headers"]
    ).json()["items"][0]["conversation_id"]

    a_filename = resp_a.json()["attachments"][0]["url"].rsplit("/", 1)[-1]

    # b1 (participant of B, not A) tries to fetch A's file by addressing it
    # through B's conversation_id.
    resp = client.get(
        f"/messages/conversations/{conversation_b_id}/attachments/{a_filename}",
        headers=b1["headers"],
    )
    assert resp.status_code == 404

    # Sanity: the same filename genuinely exists and is fetchable via the
    # correct conversation_id by an actual participant of A.
    resp = client.get(
        f"/messages/conversations/{conversation_a_id}/attachments/{a_filename}",
        headers=a1["headers"],
    )
    assert resp.status_code == 200


# --- MP4 video attachments in direct messages ----------------------------


@pytest.mark.asyncio
async def test_valid_dm_video_is_accepted_and_saved(client, make_user, dm_upload_dir):
    sender = await make_user("dmvid_valid_sender")
    recipient = await make_user("dmvid_valid_recipient")
    data = valid_mp4_bytes()

    resp = _send(
        client, sender["headers"], recipient["user_id"], files={"images": ("clip.mp4", data, "video/mp4")}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert len(body["attachments"]) == 1
    attachment = body["attachments"][0]
    assert attachment["media_type"] == "video"
    assert attachment["content_type"] == "video/mp4"
    assert attachment["size_bytes"] == len(data)

    conversation_id = body["conversation_id"]
    expected_prefix = f"/messages/conversations/{conversation_id}/attachments/"
    assert attachment["url"].startswith(expected_prefix)

    filename = attachment["url"].rsplit("/", 1)[-1]
    assert filename.endswith(".mp4")
    assert re.fullmatch(r"[0-9a-f]{32}\.mp4", filename)
    saved = dm_upload_dir / filename
    assert saved.is_file()
    assert saved.read_bytes() == data


@pytest.mark.asyncio
async def test_dm_random_bytes_named_mp4_rejected(client, make_user, dm_upload_dir):
    sender = await make_user("dmvid_faketext_sender")
    recipient = await make_user("dmvid_faketext_recipient")

    resp = _send(
        client,
        sender["headers"],
        recipient["user_id"],
        files={"images": ("totally_a_video.mp4", not_mp4_at_all_bytes(), "video/mp4")},
    )
    assert resp.status_code == 400
    assert list(dm_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_dm_ftyp_plus_garbage_spoof_rejected(client, make_user, dm_upload_dir):
    sender = await make_user("dmvid_ftypspoof_sender")
    recipient = await make_user("dmvid_ftypspoof_recipient")

    resp = _send(
        client,
        sender["headers"],
        recipient["user_id"],
        files={"images": ("clip.mp4", ftyp_plus_garbage_bytes(), "video/mp4")},
    )
    assert resp.status_code == 400
    assert "Unsupported video type" in resp.json()["detail"]
    assert list(dm_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_oversized_dm_video_rejected_with_413(client, make_user, dm_upload_dir, monkeypatch):
    monkeypatch.setattr(image_storage, "MAX_DM_VIDEO_SIZE_BYTES", 10)
    sender = await make_user("dmvid_oversized_sender")
    recipient = await make_user("dmvid_oversized_recipient")
    data = valid_mp4_bytes()
    assert len(data) > 10

    resp = _send(
        client, sender["headers"], recipient["user_id"], files={"images": ("clip.mp4", data, "video/mp4")}
    )
    assert resp.status_code == 413
    assert list(dm_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_dm_mixed_image_and_video_rollback_on_partial_failure(client, make_user, dm_upload_dir):
    sender = await make_user("dmmix_partialfail_sender")
    recipient = await make_user("dmmix_partialfail_recipient")
    files = [
        ("images", ("good.png", _image_bytes("PNG"), "image/png")),
        ("images", ("good.mp4", valid_mp4_bytes(), "video/mp4")),
        ("images", ("bad.mp4", ftyp_plus_garbage_bytes(), "video/mp4")),
    ]

    resp = _send(client, sender["headers"], recipient["user_id"], files=files)
    assert resp.status_code == 400
    assert list(dm_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_sender_can_retrieve_video_attachment(client, make_user, dm_upload_dir):
    sender = await make_user("dmvidretrieve_sender")
    recipient = await make_user("dmvidretrieve_recipient")
    data = valid_mp4_bytes()
    resp = _send(
        client, sender["headers"], recipient["user_id"], files={"images": ("clip.mp4", data, "video/mp4")}
    )
    url = resp.json()["attachments"][0]["url"]

    get_resp = client.get(url, headers=sender["headers"])
    assert get_resp.status_code == 200
    assert get_resp.content == data
    assert get_resp.headers["content-type"] == "video/mp4"


@pytest.mark.asyncio
async def test_recipient_can_retrieve_video_attachment(client, make_user, dm_upload_dir):
    sender = await make_user("dmvidretrieve2_sender")
    recipient = await make_user("dmvidretrieve2_recipient")
    data = valid_mp4_bytes()
    resp = _send(
        client, sender["headers"], recipient["user_id"], files={"images": ("clip.mp4", data, "video/mp4")}
    )
    url = resp.json()["attachments"][0]["url"]

    get_resp = client.get(url, headers=recipient["headers"])
    assert get_resp.status_code == 200
    assert get_resp.content == data


@pytest.mark.asyncio
async def test_unrelated_user_cannot_retrieve_video_attachment(client, make_user, dm_upload_dir):
    """Covers the video-specific security area named in
    docs/security-testing-report.md ("private DM video access by a
    non-participant") - proves it directly, mirroring
    test_unrelated_user_cannot_retrieve_attachment's image coverage above.
    """
    sender = await make_user("dmvidretrieve3_sender")
    recipient = await make_user("dmvidretrieve3_recipient")
    outsider = await make_user("dmvidretrieve3_outsider")
    data = valid_mp4_bytes()
    resp = _send(
        client, sender["headers"], recipient["user_id"], files={"images": ("clip.mp4", data, "video/mp4")}
    )
    url = resp.json()["attachments"][0]["url"]

    get_resp = client.get(url, headers=outsider["headers"])
    assert get_resp.status_code == 404
