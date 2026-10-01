"""Stress testing: repeated-upload volume coverage
that the existing single-file tests in test_image_storage.py don't provide -
several near-the-size-limit legitimate uploads in a row, and a burst of
oversized uploads, both checking the resulting STATE (saved files, wardrobe
item documents) afterward rather than only the HTTP status codes.

Reuses test_image_storage.py's exact fixtures/helpers (`upload_dir`,
`_wardrobe_form`, `_image_bytes`, monkeypatching `image_storage.
MAX_UPLOAD_SIZE_BYTES`) rather than inventing a parallel style, and stays
on the wardrobe-image upload surface (the same one those tests already
cover) - this file only adds the volume/repetition dimension, not a new
upload surface.
"""
import pytest

from app.services import image_storage
from tests.test_image_storage import _image_bytes, _wardrobe_form, upload_dir  # noqa: F401


@pytest.mark.asyncio
async def test_repeated_near_limit_valid_uploads_all_saved_correctly(
    client, make_user, upload_dir, monkeypatch
):
    """Several DIFFERENT, near-the-configured-size-limit (but still legal)
    images uploaded one after another for the same user - proves repeated
    legitimate load is handled without any file being dropped, overwritten,
    or mixed up with another upload's content. Checks the resulting state
    (saved files on disk, wardrobe item documents), not just 201s."""
    user = await make_user("upload_repeat_user")

    images = [_image_bytes("PNG", size=(200 + i, 200 + i)) for i in range(6)]
    largest = max(len(data) for data in images)
    # Cap set just above the largest generated image - each upload sits
    # close to (not comfortably under) the configured limit.
    monkeypatch.setattr(image_storage, "MAX_UPLOAD_SIZE_BYTES", largest + 200)

    image_urls = []
    for i, data in enumerate(images):
        resp = client.post(
            "/wardrobe",
            data=_wardrobe_form(),
            files={"image": (f"photo{i}.png", data, "image/png")},
            headers=user["headers"],
        )
        assert resp.status_code == 201
        image_urls.append(resp.json()["image_url"])

    # State after the repeated uploads, not just status codes: exactly one
    # wardrobe item per upload, and every URL is distinct (no overwrite).
    listing = client.get("/wardrobe", headers=user["headers"])
    assert listing.status_code == 200
    assert len(listing.json()) == len(images)
    assert len(set(image_urls)) == len(images)

    saved_files = list(upload_dir.iterdir())
    assert len(saved_files) == len(images)

    # Every saved file's actual bytes match the exact image that was
    # supposed to produce it - not a neighbor's content leaking in.
    for image_url, expected_bytes in zip(image_urls, images):
        path_part = image_url.split("?", 1)[0]
        filename = path_part.rsplit("/", 1)[-1]
        saved = upload_dir / filename
        assert saved.is_file()
        assert saved.read_bytes() == expected_bytes


@pytest.mark.asyncio
async def test_burst_of_oversized_uploads_all_rejected_and_state_stays_clean(
    client, make_user, upload_dir, monkeypatch
):
    """A rapid burst of oversized upload attempts - every single one must be
    independently rejected with 413, leave no file on disk and no wardrobe
    item document behind, and the service must remain fully usable for a
    legitimate upload immediately afterward (not left in a degraded state
    by the burst)."""
    monkeypatch.setattr(image_storage, "MAX_UPLOAD_SIZE_BYTES", 10)
    user = await make_user("upload_burst_user")
    oversized = _image_bytes("PNG")
    assert len(oversized) > 10

    burst_size = 15
    for i in range(burst_size):
        resp = client.post(
            "/wardrobe",
            data=_wardrobe_form(),
            files={"image": (f"bad{i}.png", oversized, "image/png")},
            headers=user["headers"],
        )
        assert resp.status_code == 413

    # State after the burst, not just status codes: nothing was written to
    # disk, and no wardrobe item document exists for any of the 15
    # independently-rejected attempts.
    assert list(upload_dir.iterdir()) == []
    listing = client.get("/wardrobe", headers=user["headers"])
    assert listing.status_code == 200
    assert listing.json() == []

    # Service remains usable afterward: a legitimate, within-cap upload
    # still works normally right after the burst, and produces exactly one
    # correctly-saved item - the burst left no lingering broken state.
    monkeypatch.setattr(image_storage, "MAX_UPLOAD_SIZE_BYTES", 10_000_000)
    good_resp = client.post(
        "/wardrobe",
        data=_wardrobe_form(),
        files={"image": ("good.png", oversized, "image/png")},
        headers=user["headers"],
    )
    assert good_resp.status_code == 201

    listing2 = client.get("/wardrobe", headers=user["headers"])
    assert listing2.status_code == 200
    assert len(listing2.json()) == 1
    saved_files = list(upload_dir.iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes() == oversized
