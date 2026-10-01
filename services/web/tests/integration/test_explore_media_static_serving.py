"""Real media-storage integration test.

test_explore_media.py thoroughly covers image_storage.save_explore_image()
itself (via its explore_upload_dir fixture, which monkeypatches
image_storage.EXPLORE_UPLOAD_DIR to a throwaway tmp_path) and asserts the
returned attachment `url` *string* starts with "/uploads/explore/" - but
no existing test ever actually sends a GET request to that url. That
leaves the real integration boundary this test targets completely
unproven: app.main mounts `StaticFiles(directory=EXPLORE_UPLOAD_DIR)` at
/uploads/explore once, at import time, using the *original*
core.config.EXPLORE_UPLOAD_DIR - a different Path object than whatever
image_storage.EXPLORE_UPLOAD_DIR gets monkeypatched to per-test. In
production both names resolve to the same directory (there is no bug
here), but it does mean this is the one test in the whole suite that must
deliberately *not* use the explore_upload_dir override: it needs a saved
file and the static mount to agree on where it lives, exactly as they do
outside of tests.

Because of that, this writes into and cleans up after itself in the real
services/web/uploads/explore directory (gitignored - see .gitignore's
`uploads/`) rather than a tmp_path, and is the only test in the suite that
does so.
"""
import io

import pytest
from PIL import Image

from app.core.config import EXPLORE_UPLOAD_DIR


def _image_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_uploaded_explore_image_is_served_back_by_the_real_static_mount(client, make_user):
    user = await make_user("staticservinguser")
    data = _image_bytes()

    create_resp = client.post(
        "/posts",
        data={"title": "t", "body": "b"},
        files={"images": ("photo.jpg", data, "image/jpeg")},
        headers=user["headers"],
    )
    assert create_resp.status_code == 201
    url = create_resp.json()["attachments"][0]["url"]
    filename = url.rsplit("/", 1)[-1]

    try:
        fetch_resp = client.get(url)

        assert fetch_resp.status_code == 200
        assert fetch_resp.headers["content-type"] == "image/jpeg"
        assert fetch_resp.content == data
    finally:
        (EXPLORE_UPLOAD_DIR / filename).unlink(missing_ok=True)
