"""Adversarial path-traversal / unsafe-filename tests against the two
routes that ever read a client-influenced filename off disk:
GET /uploads/wardrobe/{filename} and
GET /messages/conversations/{conversation_id}/attachments/{filename}.

Both routes are defended in application code today (see
app/routers/uploads.py and app/routers/messages.py::get_dm_attachment) -
these tests exist to prove that defense holds under actual adversarial
input, not just by reading the code. Fixtures (`upload_dir`,
`dm_upload_dir`) intentionally mirror the local-fixture pattern already
established in test_image_storage.py/test_dm_media.py rather than
introducing a new shared one, consistent with how every other
media-related test file in this suite already does it.
"""
import io

import pytest
from PIL import Image

from app.core.config import UPLOAD_URL_PREFIX
from app.services import image_storage

# Attack strings use percent-encoded traversal sequences (%2e%2e%2f / %2f),
# never literal ".."/"/" - a raw ".." segment in a URL path is normalized
# away by the HTTP client itself before the request is even sent (per
# standard URL dot-segment resolution), which would only prove something
# about the test client, not about this application's own filename
# handling. Percent-encoded sequences survive client-side normalization
# and are decoded server-side, so they actually reach the route handler
# as literal ".."/"/" characters inside the `filename` value - a genuine
# exercise of this app's own validation, not a client-side artifact.
_TRAVERSAL_FILENAMES = [
    "..%2f..%2f..%2f..%2fetc%2fpasswd",
    "..%2f..%2fconftest.py",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd.jpg",
    "....%2f%2f....%2f%2fetc%2fpasswd",
]


def _image_bytes(fmt: str = "PNG", size=(20, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format=fmt)
    return buf.getvalue()


# --- GET /uploads/wardrobe/{filename} ---


@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    """Redirect saved uploads to a throwaway directory instead of the real one."""
    monkeypatch.setattr(image_storage, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest.mark.parametrize("malicious_filename", _TRAVERSAL_FILENAMES)
def test_wardrobe_image_traversal_filename_rejected_even_with_valid_signature(
    client, upload_dir, malicious_filename
):
    """The filename allowlist regex (_FILENAME_RE in routers/uploads.py) is
    checked before signature verification, so a crafted filename must be
    rejected even when paired with a validly-computed signature for that
    exact string - proving the regex gate, not just "no one bothered to
    forge a signature", is what actually stops this.
    """
    signed_url = image_storage.sign_wardrobe_image_url(
        f"{UPLOAD_URL_PREFIX}/{malicious_filename}"
    )

    resp = client.get(signed_url)

    assert resp.status_code == 404
    assert "root:" not in resp.text
    assert "conftest" not in resp.text.lower()


@pytest.mark.parametrize("malicious_filename", _TRAVERSAL_FILENAMES)
def test_wardrobe_image_traversal_filename_rejected_with_garbage_signature(
    client, upload_dir, malicious_filename
):
    """The more realistic attacker scenario: no valid signature at all,
    just a crafted filename and arbitrary exp/sig values.
    """
    resp = client.get(
        f"{UPLOAD_URL_PREFIX}/{malicious_filename}",
        params={"exp": 9999999999, "sig": "not-a-real-signature"},
    )

    assert resp.status_code == 404


def test_wardrobe_image_traversal_does_not_actually_escape_upload_dir(
    client, upload_dir, tmp_path_factory
):
    """Concretely proves there is a real file just outside UPLOAD_DIR that
    a traversal *would* reach if the defense were broken, and that it is
    never returned.
    """
    secret_dir = tmp_path_factory.mktemp("outside_upload_dir")
    secret_file = secret_dir / "secret.txt"
    secret_file.write_text("top-secret-content-must-never-leak")

    # upload_dir (== image_storage.UPLOAD_DIR) and secret_dir are sibling
    # tmp_path-derived directories, so a working traversal from one to the
    # other is exactly two "../" hops away - encoded, per the module
    # docstring above.
    malicious_filename = f"..%2f{secret_dir.name}%2fsecret.txt"
    signed_url = image_storage.sign_wardrobe_image_url(
        f"{UPLOAD_URL_PREFIX}/{malicious_filename}"
    )

    resp = client.get(signed_url)

    assert resp.status_code == 404
    assert "top-secret-content-must-never-leak" not in resp.text


# --- GET /messages/conversations/{conversation_id}/attachments/{filename} ---


@pytest.fixture()
def dm_upload_dir(tmp_path, monkeypatch):
    """Redirect saved DM uploads to a throwaway directory instead of the real one."""
    monkeypatch.setattr(image_storage, "DM_UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("malicious_filename", _TRAVERSAL_FILENAMES)
async def test_dm_attachment_traversal_filename_rejected_for_real_participant(
    client, make_user, dm_upload_dir, malicious_filename
):
    """Even as a genuine, authenticated participant of a real conversation
    with a real attachment, requesting a crafted traversal filename must
    404 - the route looks the filename up against stored, trusted
    attachment metadata (never builds a filesystem path from the raw
    request parameter directly), so anything not matching a real stored
    attachment's URL simply isn't found, regardless of what characters it
    contains.
    """
    sender = await make_user(f"dmtraversal_sender_{abs(hash(malicious_filename))}")
    recipient = await make_user(f"dmtraversal_recipient_{abs(hash(malicious_filename))}")

    send_resp = client.post(
        f"/messages/{recipient['user_id']}",
        data={"body": "hi"},
        files={"images": ("photo.png", _image_bytes(), "image/png")},
        headers=sender["headers"],
    )
    conversation_id = send_resp.json()["conversation_id"]

    resp = client.get(
        f"/messages/conversations/{conversation_id}/attachments/{malicious_filename}",
        headers=sender["headers"],
    )

    assert resp.status_code == 404
