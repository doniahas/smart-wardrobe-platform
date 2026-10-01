"""Serves wardrobe images via a signed, time-limited
URL instead of the public StaticFiles mount this used to be. See
services/image_storage.py::sign_wardrobe_image_url for how that signature
is minted - always from an image_url already resolved behind the normal
user_id-scoped wardrobe ownership check, never independently of it.

Deliberately not the app.js Bearer-token pattern used by GET
/wardrobe/{item_id}: an <img src="..."> request can't carry a custom
header, only whatever's in the URL itself.
"""
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from app.core.config import UPLOAD_URL_PREFIX
from app.services import image_storage
from app.services.image_storage import verify_wardrobe_image_signature

router = APIRouter(tags=["uploads"])

# Matches exactly what _write_image_file generates: uuid4().hex + a known
# extension - never derived from client input, so this also doubles as a
# path-traversal guard before the filename ever touches the filesystem.
_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|webp)$")
_CONTENT_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


@router.get(f"{UPLOAD_URL_PREFIX}/{{filename}}")
async def get_wardrobe_image(filename: str, exp: int, sig: str):
    """Serve one wardrobe image file, but only to a request holding a
    valid, unexpired signature for that exact filename.

    Every failure - bad filename shape, bad/expired signature, or a
    genuinely missing file - returns the same 404, matching how every
    other route in this app avoids distinguishing "not yours" from
    "doesn't exist" for an unauthorized caller.
    """
    if not _FILENAME_RE.match(filename) or not verify_wardrobe_image_signature(filename, exp, sig):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")

    file_path = image_storage.UPLOAD_DIR / filename
    if not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")

    return FileResponse(file_path, media_type=_CONTENT_TYPES[Path(filename).suffix])
