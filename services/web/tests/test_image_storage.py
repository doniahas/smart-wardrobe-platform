import io
import re

import pytest
from PIL import Image

from app.services import image_storage
from tests.mp4_fixtures import valid_mp4_bytes


def _wardrobe_form() -> dict:
    return {
        "name": "Item",
        "clothing_type": "top",
        "color": "red",
        "season": "all",
        "style": "casual",
        "occasion": "casual",
    }


def _image_bytes(fmt: str, size=(20, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    """Redirect saved uploads to a throwaway directory instead of the real one."""
    monkeypatch.setattr(image_storage, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fmt,ext,content_type",
    [
        ("JPEG", ".jpg", "image/jpeg"),
        ("PNG", ".png", "image/png"),
        ("WEBP", ".webp", "image/webp"),
    ],
)
async def test_valid_supported_image_is_accepted_and_saved(
    client, make_user, upload_dir, fmt, ext, content_type
):
    user = await make_user(f"imguser_{fmt.lower()}")
    data = _image_bytes(fmt)

    resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": (f"photo{ext}", data, content_type)},
        headers=user["headers"],
    )

    assert resp.status_code == 201
    image_url = resp.json()["image_url"]
    assert image_url.startswith("/uploads/wardrobe/")
    # image_url is signed (?exp=...&sig=...),
    # so the bare filename has to be read from the path portion only.
    path_part = image_url.split("?", 1)[0]
    filename = path_part.rsplit("/", 1)[-1]
    assert filename.endswith(ext)
    assert re.fullmatch(rf"[0-9a-f]{{32}}\{ext}", filename)

    saved = upload_dir / filename
    assert saved.is_file()
    assert saved.read_bytes() == data


def _jpeg_with_orientation(orientation, size=(40, 20), color=(200, 50, 30)) -> bytes:
    img = Image.new("RGB", size, color)
    exif = img.getexif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_persisted_wardrobe_image_is_orientation_normalized(client, make_user, upload_dir):
    """A sideways EXIF-tagged upload must be persisted pixel-correct, the
    same as what Analyze Item sees for the same upload (see
    test_wardrobe_analyze_image.py's matching orientation test) - not left
    as raw, possibly-sideways pixels on disk just because this path
    doesn't go through the AI route."""
    user = await make_user("imguser_exif_persisted")
    raw = _jpeg_with_orientation(orientation=6, size=(40, 20))

    resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("photo.jpg", raw, "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 201
    image_url = resp.json()["image_url"]
    filename = image_url.split("?", 1)[0].rsplit("/", 1)[-1]
    saved = upload_dir / filename

    saved_bytes = saved.read_bytes()
    assert saved_bytes != raw
    with Image.open(saved) as img:
        assert img.size == (20, 40)  # width/height swapped - normalized, not the raw 40x20
        assert img.getexif().get(0x0112, 1) == 1


@pytest.mark.asyncio
async def test_text_file_renamed_to_jpg_is_rejected(client, make_user, upload_dir):
    user = await make_user("imguser_faketext")

    resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("totally_a_photo.jpg", b"just plain text, not an image", "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]
    assert list(upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_corrupted_image_bytes_are_rejected(client, make_user, upload_dir):
    user = await make_user("imguser_corrupted")
    truncated = _image_bytes("PNG", size=(50, 50))[: 100]

    resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("broken.png", truncated, "image/png")},
        headers=user["headers"],
    )

    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]
    assert list(upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_empty_file_is_rejected(client, make_user, upload_dir):
    user = await make_user("imguser_empty")

    resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("empty.png", b"", "image/png")},
        headers=user["headers"],
    )

    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]
    assert list(upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_unsupported_real_image_format_is_rejected(client, make_user, upload_dir):
    """A genuine, non-corrupted image in a format we don't support (BMP) must
    still be rejected, since only JPEG/PNG/WEBP are allowed."""
    user = await make_user("imguser_bmp")
    data = _image_bytes("BMP")

    resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("photo.bmp", data, "image/bmp")},
        headers=user["headers"],
    )

    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]
    assert list(upload_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_oversized_upload_still_rejected_with_413(client, make_user, upload_dir, monkeypatch):
    """The pre-existing size limit must keep working even though content is
    now also validated - and validation must not bypass it."""
    monkeypatch.setattr(image_storage, "MAX_UPLOAD_SIZE_BYTES", 10)
    user = await make_user("imguser_oversized")
    data = _image_bytes("PNG")
    assert len(data) > 10

    resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("photo.png", data, "image/png")},
        headers=user["headers"],
    )

    assert resp.status_code == 413


# --- Signed wardrobe image delivery ---
# /uploads/wardrobe is no longer a public static mount (see
# app/routers/uploads.py) - these prove a signed URL from a real API
# response is actually servable, and that it can't be tampered with.


@pytest.mark.asyncio
async def test_signed_wardrobe_image_url_is_fetchable(client, make_user, upload_dir):
    user = await make_user("imguser_fetch")
    data = _image_bytes("PNG")
    create_resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("photo.png", data, "image/png")},
        headers=user["headers"],
    )
    image_url = create_resp.json()["image_url"]

    resp = client.get(image_url)

    assert resp.status_code == 200
    assert resp.content == data


@pytest.mark.asyncio
async def test_wardrobe_image_without_signature_is_rejected(client, make_user, upload_dir):
    user = await make_user("imguser_nosig")
    data = _image_bytes("PNG")
    create_resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("photo.png", data, "image/png")},
        headers=user["headers"],
    )
    filename = create_resp.json()["image_url"].split("?", 1)[0].rsplit("/", 1)[-1]

    resp = client.get(f"/uploads/wardrobe/{filename}")

    assert resp.status_code == 422  # exp/sig are required query params


@pytest.mark.asyncio
async def test_wardrobe_image_with_tampered_signature_is_rejected(client, make_user, upload_dir):
    user = await make_user("imguser_tamperedsig")
    data = _image_bytes("PNG")
    create_resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("photo.png", data, "image/png")},
        headers=user["headers"],
    )
    image_url = create_resp.json()["image_url"]
    tampered_url = image_url[:-1] + ("0" if image_url[-1] != "0" else "1")

    resp = client.get(tampered_url)

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_expired_wardrobe_image_signature_is_rejected(client, make_user, upload_dir, monkeypatch):
    from app.core.config import WARDROBE_IMAGE_URL_TTL_SECONDS

    user = await make_user("imguser_expiredsig")
    data = _image_bytes("PNG")
    create_resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("photo.png", data, "image/png")},
        headers=user["headers"],
    )
    # Signed with the real TTL relative to "now" - still valid at this point.
    image_url = create_resp.json()["image_url"]

    # Advance the clock past the signature's expiry only for the fetch below,
    # rather than trying to mint an already-expired signature (which would
    # need TTL subtracted twice - once at sign time, once at verify time).
    real_time = image_storage.time.time
    monkeypatch.setattr(
        image_storage.time, "time", lambda: real_time() + WARDROBE_IMAGE_URL_TTL_SECONDS + 10
    )

    resp = client.get(image_url)

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_nonexistent_wardrobe_image_signed_correctly_still_404s(client, upload_dir):
    """A well-signed URL for a filename that was never actually written to
    disk must still 404, not 500."""
    fake_filename = "0" * 32 + ".png"
    signed_url = image_storage.sign_wardrobe_image_url(f"/uploads/wardrobe/{fake_filename}")

    resp = client.get(signed_url)

    assert resp.status_code == 404
    assert list(upload_dir.iterdir()) == []


# --- Boundary regression: wardrobe items never accept video ---------------
#
# Video support is scoped to Explore posts/comments and DMs only -
# save_wardrobe_image/validate_wardrobe_image_bytes were deliberately left
# untouched. This proves that boundary holds under a genuinely valid MP4,
# not just under obviously-bad input.


@pytest.mark.asyncio
async def test_wardrobe_upload_rejects_a_genuinely_valid_mp4_video(client, make_user, upload_dir):
    user = await make_user("imguser_realvideo")

    resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("clip.mp4", valid_mp4_bytes(), "video/mp4")},
        headers=user["headers"],
    )

    assert resp.status_code == 400
    assert "Unsupported image type" in resp.json()["detail"]
    assert list(upload_dir.iterdir()) == []


# --- One photo, several wardrobe items ---
#
# Several wardrobe items may be created from the same original photo,
# each uploaded through POST /wardrobe separately rather than sharing a
# single stored path across the new documents. These tests pin why that
# matters: routers/wardrobe.py
# deletes an item's image file on DELETE (and on an image replace), so two
# documents pointing at one file would let either one unlink it out from
# under the other - this is the existing contract any multi-item client
# strategy depends on.


def _add_item_with_photo(client, user, name: str, photo: bytes) -> tuple[str, str]:
    """Add one wardrobe item with the given photo; return (id, filename)."""
    form = _wardrobe_form()
    form["name"] = name
    resp = client.post(
        "/wardrobe",
        data=form,
        files={"image": ("outfit.jpg", photo, "image/jpeg")},
        headers=user["headers"],
    )
    assert resp.status_code == 201
    body = resp.json()
    filename = body["image_url"].split("?", 1)[0].rsplit("/", 1)[-1]
    return body["id"], filename


@pytest.mark.asyncio
async def test_same_outfit_photo_added_as_several_items_stores_a_file_per_item(
    client, make_user, upload_dir
):
    """Two garments confirmed from one outfit photo become two independent
    wardrobe documents, each owning its own stored copy - never one shared
    path."""
    user = await make_user("multiadd_distinct")
    photo = _image_bytes("JPEG")

    _, first_file = _add_item_with_photo(client, user, "Detected top", photo)
    _, second_file = _add_item_with_photo(client, user, "Detected bottom", photo)

    assert first_file != second_file
    assert sorted(p.name for p in upload_dir.iterdir()) == sorted([first_file, second_file])
    # Same source photo, so identical bytes - but two separate files.
    assert (upload_dir / first_file).read_bytes() == (upload_dir / second_file).read_bytes()


@pytest.mark.asyncio
async def test_deleting_one_multi_added_item_leaves_the_others_image_intact(
    client, make_user, upload_dir
):
    """The delete-semantics reason the frontend re-uploads per item: with
    separate files, removing one garment can't break the other's image."""
    user = await make_user("multiadd_delete")
    photo = _image_bytes("JPEG")

    first_id, first_file = _add_item_with_photo(client, user, "Detected top", photo)
    second_id, second_file = _add_item_with_photo(client, user, "Detected bottom", photo)

    resp = client.delete(f"/wardrobe/{first_id}", headers=user["headers"])
    assert resp.status_code == 204

    remaining = [p.name for p in upload_dir.iterdir()]
    assert first_file not in remaining
    assert second_file in remaining

    # The surviving item still resolves to a real, readable image.
    resp = client.get(f"/wardrobe/{second_id}", headers=user["headers"])
    assert resp.status_code == 200
    assert resp.json()["image_url"].split("?", 1)[0].endswith(second_file)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field", ["name", "clothing_type", "color", "season", "style", "occasion"]
)
async def test_wardrobe_rejects_an_item_missing_a_required_field(
    client, make_user, upload_dir, missing_field
):
    """Server-side backing for the review UI's "fill this in before this
    item can be added" rule: an AI field left null is never defaulted, and
    the endpoint refuses the item rather than inventing a value."""
    user = await make_user(f"multiadd_missing_{missing_field}")
    form = _wardrobe_form()
    form.pop(missing_field)

    resp = client.post(
        "/wardrobe",
        data=form,
        files={"image": ("outfit.jpg", _image_bytes("JPEG"), "image/jpeg")},
        headers=user["headers"],
    )

    assert resp.status_code == 422
    assert list(upload_dir.iterdir()) == []