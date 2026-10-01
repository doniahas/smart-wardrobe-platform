"""Adversarial video-attachment security tests.

Covers the video-specific security areas listed in
`docs/security-testing-report.md` ("MIME/content spoofing for video",
"private DM video access by a non-participant"). The individual media test
files (test_explore_media.py, test_dm_media.py, test_mp4_validation.py)
already cover the same validator/route mechanics for both normal and
adversarial input as part of their own feature coverage - this file exists
specifically to give the named security requirement its own explicit,
easy-to-point-to proof, the same way test_path_traversal.py already does
for path traversal rather than relying solely on test_dm_media.py's own
cross-conversation test.
"""
import pytest

from app.core.config import RATE_LIMIT_POST_WRITE
from app.services import image_storage
from tests.mp4_fixtures import (
    ftyp_plus_free_only_bytes,
    ftyp_plus_garbage_bytes,
    missing_mdat_bytes,
    missing_moov_bytes,
    not_mp4_at_all_bytes,
    truncated_box_bytes,
    valid_mp4_bytes,
)


def _limit_count(limit_str: str) -> int:
    return int(limit_str.split("/")[0])


@pytest.fixture()
def explore_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(image_storage, "EXPLORE_UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def dm_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(image_storage, "DM_UPLOAD_DIR", tmp_path)
    return tmp_path


# --- MIME / Content-Type / extension spoofing ------------------------------


@pytest.mark.asyncio
async def test_content_type_and_extension_are_never_trusted_for_video(
    client, make_user, explore_upload_dir
):
    """Plain non-video bytes, with both a .mp4 filename AND a
    video/mp4 Content-Type header - a real attacker's best-effort spoof.
    Rejected purely because the real content signature/structure fails,
    never because of a filename or header check we don't even have."""
    user = await make_user("secvid_spoof")
    resp = client.post(
        "/posts",
        data={"title": "t", "body": "b"},
        files={"images": ("legit_looking_video.mp4", not_mp4_at_all_bytes(), "video/mp4")},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_real_video_bytes_accepted_despite_wrong_content_type_header(
    client, make_user, explore_upload_dir
):
    """The inverse proof: a genuinely valid MP4 sent with a misleading
    Content-Type must still be accepted as video - confirming rejection
    above is driven by real content, not merely by disagreeing with
    whatever header the client sent."""
    user = await make_user("secvid_realdespiteheader")
    resp = client.post(
        "/posts",
        data={"title": "t", "body": "b"},
        files={"images": ("clip.mp4", valid_mp4_bytes(), "application/octet-stream")},
        headers=user["headers"],
    )
    assert resp.status_code == 201
    assert resp.json()["attachments"][0]["media_type"] == "video"


# --- Malformed / malicious MP4 containers -----------------------------------


@pytest.mark.asyncio
async def test_ftyp_only_spoof_rejected_on_posts(client, make_user, explore_upload_dir):
    """The specific attack the structural validator (not a bare signature
    check) exists to catch: a real `ftyp` box followed by unrelated bytes."""
    user = await make_user("secvid_ftyponly_posts")
    resp = client.post(
        "/posts",
        data={"title": "t", "body": "b"},
        files={"images": ("clip.mp4", ftyp_plus_garbage_bytes(), "video/mp4")},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_truncated_container_rejected_on_dm(client, make_user, dm_upload_dir):
    sender = await make_user("secvid_truncated_sender")
    recipient = await make_user("secvid_truncated_recipient")
    resp = client.post(
        f"/messages/{recipient['user_id']}",
        data={"body": "hi"},
        files={"images": ("clip.mp4", truncated_box_bytes(), "video/mp4")},
        headers=sender["headers"],
    )
    assert resp.status_code == 400
    assert list(dm_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_syntactically_valid_non_media_box_alone_is_not_accepted_as_video(
    client, make_user, explore_upload_dir
):
    """The specific correction this security pass exists to prove end to
    end: a real, well-formed ISO-BMFF box (`free`) following a valid ftyp
    is genuinely valid box *syntax*, but is neither `moov` nor `mdat` - it
    must not be enough, by itself, to turn an arbitrary file into an
    accepted video."""
    user = await make_user("secvid_freebox_only")
    resp = client.post(
        "/posts",
        data={"title": "t", "body": "b"},
        files={"images": ("clip.mp4", ftyp_plus_free_only_bytes(), "video/mp4")},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert list(explore_upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_missing_moov_or_mdat_rejected_on_posts(client, make_user, explore_upload_dir):
    """A valid ftyp plus only one of moov/mdat is still rejected - both
    must genuinely be present."""
    user = await make_user("secvid_missingpart")
    resp1 = client.post(
        "/posts",
        data={"title": "t", "body": "b"},
        files={"images": ("clip.mp4", missing_moov_bytes(), "video/mp4")},
        headers=user["headers"],
    )
    assert resp1.status_code == 400

    resp2 = client.post(
        "/posts",
        data={"title": "t", "body": "b"},
        files={"images": ("clip.mp4", missing_mdat_bytes(), "video/mp4")},
        headers=user["headers"],
    )
    assert resp2.status_code == 400
    assert list(explore_upload_dir.iterdir()) == []


# --- Oversized upload --------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_video_upload_rejected_without_full_buffering(
    client, make_user, explore_upload_dir, monkeypatch
):
    """Proves the bound is enforced *while reading*, not after: the
    request body genuinely exceeds the (monkeypatched, tiny) cap, and the
    server responds 413 with nothing written to disk - reusing the exact
    _read_remaining_bounded early-abort mechanism images already relied on
    (see test_oversized_upload_still_rejected_with_413 in
    test_image_storage.py for the original image-side proof)."""
    monkeypatch.setattr(image_storage, "MAX_EXPLORE_VIDEO_SIZE_BYTES", 10)
    user = await make_user("secvid_oversized")
    data = valid_mp4_bytes()
    assert len(data) > 10

    resp = client.post(
        "/posts",
        data={"title": "t", "body": "b"},
        files={"images": ("clip.mp4", data, "video/mp4")},
        headers=user["headers"],
    )
    assert resp.status_code == 413
    assert list(explore_upload_dir.iterdir()) == []


# --- Non-participant private DM video access --------------------------------


@pytest.mark.asyncio
async def test_non_participant_cannot_retrieve_dm_video(client, make_user, dm_upload_dir):
    sender = await make_user("secvid_dm_sender")
    recipient = await make_user("secvid_dm_recipient")
    outsider = await make_user("secvid_dm_outsider")

    send_resp = client.post(
        f"/messages/{recipient['user_id']}",
        data={"body": "hi"},
        files={"images": ("clip.mp4", valid_mp4_bytes(), "video/mp4")},
        headers=sender["headers"],
    )
    url = send_resp.json()["attachments"][0]["url"]

    outsider_resp = client.get(url, headers=outsider["headers"])
    assert outsider_resp.status_code == 404

    unauthenticated_resp = client.get(url)
    assert unauthenticated_resp.status_code == 401

    # Sanity: the real participant genuinely can retrieve it - proves the
    # 404 above is a real authorization boundary, not a broken route.
    recipient_resp = client.get(url, headers=recipient["headers"])
    assert recipient_resp.status_code == 200


# --- Path traversal via a crafted DM attachment filename --------------------


@pytest.mark.asyncio
async def test_dm_video_attachment_path_traversal_rejected(client, make_user, dm_upload_dir):
    """Mirrors test_path_traversal.py's DM-image coverage for a real video
    attachment specifically - the lookup is against trusted stored
    metadata regardless of media type, so this was already covered by
    construction, but is proven directly here rather than only inferred."""
    sender = await make_user("secvid_traversal_sender")
    recipient = await make_user("secvid_traversal_recipient")
    send_resp = client.post(
        f"/messages/{recipient['user_id']}",
        data={"body": "hi"},
        files={"images": ("clip.mp4", valid_mp4_bytes(), "video/mp4")},
        headers=sender["headers"],
    )
    conversation_id = send_resp.json()["conversation_id"]

    resp = client.get(
        f"/messages/conversations/{conversation_id}/attachments/..%2f..%2fconftest.py",
        headers=sender["headers"],
    )
    assert resp.status_code == 404
    assert "conftest" not in resp.text.lower()


# --- Rate limiting still applies to video-bearing requests ------------------


@pytest.mark.asyncio
async def test_video_bearing_post_requests_still_rate_limited(client, make_user):
    """Video attachments don't create a separate, unlimited upload path -
    the same RATE_LIMIT_POST_WRITE bucket that already governs every
    /posts write governs these too."""
    user = await make_user("secvid_ratelimit")
    limit = _limit_count(RATE_LIMIT_POST_WRITE)
    data = valid_mp4_bytes()

    for _ in range(limit):
        resp = client.post(
            "/posts",
            data={"title": "t", "body": "b"},
            files={"images": ("clip.mp4", data, "video/mp4")},
            headers=user["headers"],
        )
        assert resp.status_code == 201

    resp = client.post(
        "/posts",
        data={"title": "t", "body": "b"},
        files={"images": ("clip.mp4", data, "video/mp4")},
        headers=user["headers"],
    )
    assert resp.status_code == 429
