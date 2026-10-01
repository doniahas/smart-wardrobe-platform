import io
import re

import pytest
from PIL import Image

from app.services import image_storage
from tests.mp4_fixtures import (
    ftyp_plus_garbage_bytes,
    impossible_box_size_bytes,
    not_mp4_at_all_bytes,
    truncated_box_bytes,
    valid_mp4_bytes,
)


def _post_form(**overrides) -> dict:
    payload = {"title": "t", "body": "b"}
    payload.update(overrides)
    return payload


def _image_bytes(fmt: str, size=(20, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture()
def explore_upload_dir(tmp_path, monkeypatch):
    """Redirect saved Explore uploads to a throwaway directory instead of the real one."""
    monkeypatch.setattr(image_storage, "EXPLORE_UPLOAD_DIR", tmp_path)
    return tmp_path


# --- validation, mirroring test_image_storage.py's wardrobe coverage ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fmt,ext,content_type",
    [
        ("JPEG", ".jpg", "image/jpeg"),
        ("PNG", ".png", "image/png"),
        ("WEBP", ".webp", "image/webp"),
    ],
)
async def test_valid_post_image_is_accepted_and_saved(
    client, make_user, explore_upload_dir, fmt, ext, content_type
):
    user = await make_user(f"postimg_{fmt.lower()}")
    data = _image_bytes(fmt)

    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": (f"photo{ext}", data, content_type)},
        headers=user["headers"],
    )

    assert resp.status_code == 201
    body = resp.json()
    assert len(body["attachments"]) == 1
    attachment = body["attachments"][0]
    assert attachment["media_type"] == "image"
    assert attachment["content_type"] == content_type
    assert attachment["size_bytes"] == len(data)
    assert attachment["url"].startswith("/uploads/explore/")

    filename = attachment["url"].rsplit("/", 1)[-1]
    assert filename.endswith(ext)
    assert re.fullmatch(rf"[0-9a-f]{{32}}\{ext}", filename)
    saved = explore_upload_dir / filename
    assert saved.is_file()
    assert saved.read_bytes() == data


@pytest.mark.asyncio
async def test_text_file_renamed_to_jpg_is_rejected(client, make_user, explore_upload_dir):
    user = await make_user("postimg_faketext")
    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("totally_a_photo.jpg", b"just plain text, not an image", "image/jpeg")},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_corrupted_image_bytes_are_rejected(client, make_user, explore_upload_dir):
    user = await make_user("postimg_corrupted")
    truncated = _image_bytes("PNG", size=(50, 50))[:100]

    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("broken.png", truncated, "image/png")},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_unsupported_real_image_format_is_rejected(client, make_user, explore_upload_dir):
    user = await make_user("postimg_bmp")
    data = _image_bytes("BMP")

    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("photo.bmp", data, "image/bmp")},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_oversized_post_image_rejected_with_413(client, make_user, explore_upload_dir, monkeypatch):
    monkeypatch.setattr(image_storage, "MAX_EXPLORE_IMAGE_SIZE_BYTES", 10)
    user = await make_user("postimg_oversized")
    data = _image_bytes("PNG")
    assert len(data) > 10

    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("photo.png", data, "image/png")},
        headers=user["headers"],
    )
    assert resp.status_code == 413
    assert list(explore_upload_dir.iterdir()) == []


# --- attachment count limit ---


@pytest.mark.asyncio
async def test_max_attachments_is_accepted(client, make_user, explore_upload_dir):
    user = await make_user("postimg_exactmax")
    data = _image_bytes("PNG")
    files = [("images", (f"p{i}.png", data, "image/png")) for i in range(4)]

    resp = client.post("/posts", data=_post_form(), files=files, headers=user["headers"])
    assert resp.status_code == 201
    assert len(resp.json()["attachments"]) == 4


@pytest.mark.asyncio
async def test_too_many_attachments_rejected(client, make_user, explore_upload_dir):
    user = await make_user("postimg_toomany")
    data = _image_bytes("PNG")
    files = [("images", (f"p{i}.png", data, "image/png")) for i in range(5)]

    resp = client.post("/posts", data=_post_form(), files=files, headers=user["headers"])
    assert resp.status_code == 400
    assert "Too many attachments" in resp.json()["detail"]
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_partial_failure_rolls_back_earlier_saved_files(client, make_user, explore_upload_dir):
    """A rejected multi-file request must leave no files behind, including
    ones already saved before a later file in the same request fails
    validation."""
    user = await make_user("postimg_partialfail")
    good = _image_bytes("PNG")
    files = [
        ("images", ("good.png", good, "image/png")),
        ("images", ("bad.jpg", b"not a real image", "image/jpeg")),
    ]

    resp = client.post("/posts", data=_post_form(), files=files, headers=user["headers"])
    assert resp.status_code == 400
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_comment_too_many_attachments_rejected(client, make_user, explore_upload_dir):
    author = await make_user("commentimgtoomany")
    post_resp = client.post("/posts", data=_post_form(), headers=author["headers"])
    post_id = post_resp.json()["id"]
    data = _image_bytes("PNG")
    files = [("images", (f"c{i}.png", data, "image/png")) for i in range(5)]

    resp = client.post(
        f"/posts/{post_id}/comments", data={"body": "x"}, files=files, headers=author["headers"]
    )
    assert resp.status_code == 400
    assert list(explore_upload_dir.iterdir()) == []


# --- comments with attachments ---


@pytest.mark.asyncio
async def test_comment_with_image_attachment(client, make_user, explore_upload_dir):
    author = await make_user("commentimgauthor")
    post_resp = client.post("/posts", data=_post_form(), headers=author["headers"])
    post_id = post_resp.json()["id"]
    data = _image_bytes("JPEG")

    resp = client.post(
        f"/posts/{post_id}/comments",
        data={"body": "nice!"},
        files={"images": ("c.jpg", data, "image/jpeg")},
        headers=author["headers"],
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["attachments"]) == 1
    assert body["attachments"][0]["media_type"] == "image"


# --- attachments surfaced on reads ---


@pytest.mark.asyncio
async def test_feed_and_mine_include_attachments(client, make_user, explore_upload_dir):
    user = await make_user("feedimguser")
    data = _image_bytes("PNG")
    client.post(
        "/posts", data=_post_form(), files={"images": ("p.png", data, "image/png")}, headers=user["headers"]
    )

    feed_item = client.get("/posts", headers=user["headers"]).json()["items"][0]
    assert len(feed_item["attachments"]) == 1

    mine_item = client.get("/posts/mine", headers=user["headers"]).json()["items"][0]
    assert len(mine_item["attachments"]) == 1


@pytest.mark.asyncio
async def test_list_comments_includes_attachments(client, make_user, explore_upload_dir):
    author = await make_user("listcommentimguser")
    post_resp = client.post("/posts", data=_post_form(), headers=author["headers"])
    post_id = post_resp.json()["id"]
    data = _image_bytes("PNG")
    client.post(
        f"/posts/{post_id}/comments",
        data={"body": "x"},
        files={"images": ("c.png", data, "image/png")},
        headers=author["headers"],
    )

    item = client.get(f"/posts/{post_id}/comments", headers=author["headers"]).json()["items"][0]
    assert len(item["attachments"]) == 1


# --- cleanup on delete ---


@pytest.mark.asyncio
async def test_deleting_a_post_removes_its_own_image_files(client, make_user, explore_upload_dir):
    user = await make_user("postimgdeleteuser")
    data = _image_bytes("PNG")
    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("p.png", data, "image/png")},
        headers=user["headers"],
    )
    post_id = resp.json()["id"]
    assert len(list(explore_upload_dir.iterdir())) == 1

    del_resp = client.delete(f"/posts/{post_id}", headers=user["headers"])
    assert del_resp.status_code == 204
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_deleting_a_comment_removes_its_own_image_files(client, make_user, explore_upload_dir):
    author = await make_user("commentimgdeleteuser")
    post_resp = client.post("/posts", data=_post_form(), headers=author["headers"])
    post_id = post_resp.json()["id"]
    data = _image_bytes("PNG")
    comment_resp = client.post(
        f"/posts/{post_id}/comments",
        data={"body": "x"},
        files={"images": ("c.png", data, "image/png")},
        headers=author["headers"],
    )
    comment_id = comment_resp.json()["id"]
    assert len(list(explore_upload_dir.iterdir())) == 1

    del_resp = client.delete(f"/comments/{comment_id}", headers=author["headers"])
    assert del_resp.status_code == 204
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_deleting_a_post_removes_its_comments_image_files_too(client, make_user, explore_upload_dir):
    author = await make_user("cascadeimgauthor")
    post_resp = client.post("/posts", data=_post_form(), headers=author["headers"])
    post_id = post_resp.json()["id"]
    data = _image_bytes("PNG")
    client.post(
        f"/posts/{post_id}/comments",
        data={"body": "x"},
        files={"images": ("c1.png", data, "image/png")},
        headers=author["headers"],
    )
    client.post(
        f"/posts/{post_id}/comments",
        data={"body": "y"},
        files={"images": ("c2.png", data, "image/png")},
        headers=author["headers"],
    )
    assert len(list(explore_upload_dir.iterdir())) == 2

    del_resp = client.delete(f"/posts/{post_id}", headers=author["headers"])
    assert del_resp.status_code == 204
    assert list(explore_upload_dir.iterdir()) == []


# --- MP4 video attachments on posts/comments -----------------------------


@pytest.mark.asyncio
async def test_valid_post_video_is_accepted_and_saved(client, make_user, explore_upload_dir):
    user = await make_user("postvid_valid")
    data = valid_mp4_bytes()

    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("clip.mp4", data, "video/mp4")},
        headers=user["headers"],
    )

    assert resp.status_code == 201
    body = resp.json()
    assert len(body["attachments"]) == 1
    attachment = body["attachments"][0]
    assert attachment["media_type"] == "video"
    assert attachment["content_type"] == "video/mp4"
    assert attachment["size_bytes"] == len(data)
    assert attachment["url"].startswith("/uploads/explore/")

    filename = attachment["url"].rsplit("/", 1)[-1]
    assert filename.endswith(".mp4")
    assert re.fullmatch(r"[0-9a-f]{32}\.mp4", filename)
    saved = explore_upload_dir / filename
    assert saved.is_file()
    assert saved.read_bytes() == data


@pytest.mark.asyncio
async def test_random_bytes_named_mp4_rejected(client, make_user, explore_upload_dir):
    """Arbitrary bytes with a .mp4 filename and video/mp4 Content-Type -
    neither is trusted; real content sniffing rejects this."""
    user = await make_user("postvid_faketext")
    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("totally_a_video.mp4", not_mp4_at_all_bytes(), "video/mp4")},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]  # substring shared with the image path
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_content_type_spoofing_does_not_change_real_content_outcome(
    client, make_user, explore_upload_dir
):
    """Client-declared Content-Type is irrelevant in both directions: a
    real MP4 sent with a misleading image Content-Type is still accepted
    as video, and non-video bytes sent with video/mp4 are still rejected."""
    user = await make_user("postvid_spoofed_accept")
    data = valid_mp4_bytes()
    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("clip.mp4", data, "image/png")},  # wrong Content-Type on purpose
        headers=user["headers"],
    )
    assert resp.status_code == 201
    assert resp.json()["attachments"][0]["media_type"] == "video"

    user2 = await make_user("postvid_spoofed_reject")
    resp2 = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("photo.png", b"not actually video or image bytes", "video/mp4")},
        headers=user2["headers"],
    )
    assert resp2.status_code == 400


@pytest.mark.asyncio
async def test_ftyp_plus_garbage_spoof_rejected(client, make_user, explore_upload_dir):
    """A real ftyp signature followed by unrelated bytes - the trivial
    "ftyp-only spoof" the structural validator exists to catch."""
    user = await make_user("postvid_ftypspoof")
    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("clip.mp4", ftyp_plus_garbage_bytes(), "video/mp4")},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert "Unsupported video type" in resp.json()["detail"]
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_truncated_mp4_box_rejected(client, make_user, explore_upload_dir):
    user = await make_user("postvid_truncated")
    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("clip.mp4", truncated_box_bytes(), "video/mp4")},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_impossible_mp4_box_size_rejected(client, make_user, explore_upload_dir):
    user = await make_user("postvid_impossible")
    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("clip.mp4", impossible_box_size_bytes(), "video/mp4")},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_oversized_post_video_rejected_with_413(client, make_user, explore_upload_dir, monkeypatch):
    monkeypatch.setattr(image_storage, "MAX_EXPLORE_VIDEO_SIZE_BYTES", 10)
    user = await make_user("postvid_oversized")
    data = valid_mp4_bytes()
    assert len(data) > 10

    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("clip.mp4", data, "video/mp4")},
        headers=user["headers"],
    )
    assert resp.status_code == 413
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_video_size_cap_does_not_loosen_image_size_cap(
    client, make_user, explore_upload_dir, monkeypatch
):
    """Regression guard: a much larger video size bound must never let an
    oversized image slip through under it. Images are still classified and
    bounded by the image-specific cap, independent of how large the video
    cap is configured."""
    monkeypatch.setattr(image_storage, "MAX_EXPLORE_IMAGE_SIZE_BYTES", 10)
    monkeypatch.setattr(image_storage, "MAX_EXPLORE_VIDEO_SIZE_BYTES", 10_000_000)
    user = await make_user("postimg_stillbounded")
    data = _image_bytes("PNG")
    assert len(data) > 10

    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("photo.png", data, "image/png")},
        headers=user["headers"],
    )
    assert resp.status_code == 413
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_mixed_image_and_video_attachments_accepted_within_total_count(
    client, make_user, explore_upload_dir
):
    user = await make_user("postmix_exactmax")
    image_data = _image_bytes("PNG")
    video_data = valid_mp4_bytes()
    files = [
        ("images", ("p0.png", image_data, "image/png")),
        ("images", ("p1.png", image_data, "image/png")),
        ("images", ("v0.mp4", video_data, "video/mp4")),
        ("images", ("v1.mp4", video_data, "video/mp4")),
    ]

    resp = client.post("/posts", data=_post_form(), files=files, headers=user["headers"])
    assert resp.status_code == 201
    attachments = resp.json()["attachments"]
    assert len(attachments) == 4
    assert sorted(a["media_type"] for a in attachments) == ["image", "image", "video", "video"]


@pytest.mark.asyncio
async def test_total_attachment_count_enforced_across_both_media_types(
    client, make_user, explore_upload_dir
):
    """4 images + 2 videos (6 total) must be rejected by the same
    MAX_EXPLORE_ATTACHMENTS cap as 6 of any single type would be - there is
    no separate, larger allowance for mixing types."""
    user = await make_user("postmix_toomany")
    image_data = _image_bytes("PNG")
    video_data = valid_mp4_bytes()
    files = [("images", (f"p{i}.png", image_data, "image/png")) for i in range(4)]
    files += [("images", (f"v{i}.mp4", video_data, "video/mp4")) for i in range(2)]

    resp = client.post("/posts", data=_post_form(), files=files, headers=user["headers"])
    assert resp.status_code == 400
    assert "Too many attachments" in resp.json()["detail"]
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_invalid_later_attachment_rolls_back_earlier_saved_image_and_video_files(
    client, make_user, explore_upload_dir
):
    user = await make_user("postmix_partialfail")
    files = [
        ("images", ("good.png", _image_bytes("PNG"), "image/png")),
        ("images", ("good.mp4", valid_mp4_bytes(), "video/mp4")),
        ("images", ("bad.mp4", ftyp_plus_garbage_bytes(), "video/mp4")),
    ]

    resp = client.post("/posts", data=_post_form(), files=files, headers=user["headers"])
    assert resp.status_code == 400
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_comment_with_video_attachment(client, make_user, explore_upload_dir):
    author = await make_user("commentvidauthor")
    post_resp = client.post("/posts", data=_post_form(), headers=author["headers"])
    post_id = post_resp.json()["id"]
    data = valid_mp4_bytes()

    resp = client.post(
        f"/posts/{post_id}/comments",
        data={"body": "nice clip!"},
        files={"images": ("c.mp4", data, "video/mp4")},
        headers=author["headers"],
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["attachments"]) == 1
    assert body["attachments"][0]["media_type"] == "video"


@pytest.mark.asyncio
async def test_deleting_a_post_removes_its_own_video_file(client, make_user, explore_upload_dir):
    user = await make_user("postviddeleteuser")
    data = valid_mp4_bytes()
    resp = client.post(
        "/posts",
        data=_post_form(),
        files={"images": ("p.mp4", data, "video/mp4")},
        headers=user["headers"],
    )
    post_id = resp.json()["id"]
    assert len(list(explore_upload_dir.iterdir())) == 1

    del_resp = client.delete(f"/posts/{post_id}", headers=user["headers"])
    assert del_resp.status_code == 204
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_deleting_a_comment_removes_its_own_video_file(client, make_user, explore_upload_dir):
    author = await make_user("commentviddeleteuser")
    post_resp = client.post("/posts", data=_post_form(), headers=author["headers"])
    post_id = post_resp.json()["id"]
    data = valid_mp4_bytes()
    comment_resp = client.post(
        f"/posts/{post_id}/comments",
        data={"body": "x"},
        files={"images": ("c.mp4", data, "video/mp4")},
        headers=author["headers"],
    )
    comment_id = comment_resp.json()["id"]
    assert len(list(explore_upload_dir.iterdir())) == 1

    del_resp = client.delete(f"/comments/{comment_id}", headers=author["headers"])
    assert del_resp.status_code == 204
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_deleting_a_post_removes_its_comments_video_files_too(client, make_user, explore_upload_dir):
    author = await make_user("cascadevidauthor")
    post_resp = client.post("/posts", data=_post_form(), headers=author["headers"])
    post_id = post_resp.json()["id"]
    data = valid_mp4_bytes()
    client.post(
        f"/posts/{post_id}/comments",
        data={"body": "x"},
        files={"images": ("c1.mp4", data, "video/mp4")},
        headers=author["headers"],
    )
    client.post(
        f"/posts/{post_id}/comments",
        data={"body": "y"},
        files={"images": ("c2.mp4", data, "video/mp4")},
        headers=author["headers"],
    )
    assert len(list(explore_upload_dir.iterdir())) == 2

    del_resp = client.delete(f"/posts/{post_id}", headers=author["headers"])
    assert del_resp.status_code == 204
    assert list(explore_upload_dir.iterdir()) == []
