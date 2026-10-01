import hashlib
import hmac
import io
import time
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import HTTPException, UploadFile, status
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import (
    DM_UPLOAD_DIR,
    EXPLORE_UPLOAD_DIR,
    EXPLORE_UPLOAD_URL_PREFIX,
    JWT_SECRET,
    MAX_DM_IMAGE_SIZE_BYTES,
    MAX_DM_IMAGE_SIZE_MB,
    MAX_DM_VIDEO_SIZE_BYTES,
    MAX_DM_VIDEO_SIZE_MB,
    MAX_EXPLORE_IMAGE_SIZE_BYTES,
    MAX_EXPLORE_IMAGE_SIZE_MB,
    MAX_EXPLORE_VIDEO_SIZE_BYTES,
    MAX_EXPLORE_VIDEO_SIZE_MB,
    MAX_UPLOAD_SIZE_BYTES,
    MAX_UPLOAD_SIZE_MB,
    UPLOAD_DIR,
    UPLOAD_URL_PREFIX,
    WARDROBE_IMAGE_URL_TTL_SECONDS,
)

# Extensions/content-types are derived from the actual decoded image format
# (via Pillow), never from the client-supplied Content-Type header or
# filename, since both are trivially spoofable (e.g. a text file renamed to
# ".jpg"). Shared by both the wardrobe and the Explore (post/comment)
# image uploads below.
_FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_FORMAT_CONTENT_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

_UNSUPPORTED_IMAGE_DETAIL = "Unsupported image type. Allowed types: JPG, PNG, WEBP."
# Used only where a single upload slot may legitimately be either an
# image or an MP4 video (Explore/DM attachments) and content-sniffing found
# neither - kept as its own constant, distinct from _UNSUPPORTED_IMAGE_DETAIL,
# so the wardrobe upload path's image-only error message never mentions
# video (wardrobe items never accept video). Still contains the exact
# "Unsupported image type" phrase existing tests already assert on, since a
# file that matches neither signature is presented the same way it always
# was, just with MP4 now also listed as an accepted type.
_UNSUPPORTED_ATTACHMENT_DETAIL = "Unsupported image type. Allowed types: JPG, PNG, WEBP, MP4."
_UNSUPPORTED_VIDEO_DETAIL = "Unsupported video type. Allowed types: MP4."


# --- MP4/ISO-BMFF structural validation ------------------------------
#
# Purpose and limits: this is a lightweight, dependency-free structural
# sanity check of the MP4 container - it walks the *entire* top-level
# ISO-BMFF box chain entirely within the already-bounded uploaded bytes,
# verifying every box's size is self-consistent, and requires that a valid
# `ftyp` box is followed - anywhere, in either order - by both a `moov` box
# and an `mdat` box: the two top-level boxes that actually carry an MP4's
# metadata and media data respectively. A bare `ftyp` signature, or `ftyp`
# followed only by other well-formed-but-non-media boxes (e.g. `free`/
# `skip`), is not enough - meaningful MP4 media structure (`moov` *and*
# `mdat` both genuinely present) is required, not merely two recognized box
# names. It deliberately does NOT decode any audio/video frame, parse
# *inside* moov/mdat, or use any video-processing library (no ffmpeg, no
# decoder) - it proves the container has the structure of a real MP4, not
# that every codec/frame in the file is actually decodable.


def _read_box_header(data: bytes, offset: int) -> Optional[tuple[bytes, int, int]]:
    """Read one ISO-BMFF box header at `offset` and return
    (box_type, header_len, box_size), or None if the header itself doesn't
    fit in `data`, or the box's declared size is impossible (smaller than
    its own header, or larger than the bytes actually available) - the
    caller treats None as "truncated/malformed box boundary, reject".

    Handles both an ordinary 32-bit size and (when the 32-bit size field is
    exactly 1) the ISO-BMFF 64-bit extended-size form - both are parsed
    entirely from bytes already present in `data`, never by seeking or
    reading past it.
    """
    if offset + 8 > len(data):
        return None
    size32 = int.from_bytes(data[offset : offset + 4], "big")
    box_type = data[offset + 4 : offset + 8]

    if size32 == 1:
        if offset + 16 > len(data):
            return None
        box_size = int.from_bytes(data[offset + 8 : offset + 16], "big")
        header_len = 16
    elif size32 == 0:
        # Spec meaning: "this box extends to the end of the file" - only
        # meaningful for the last box, but harmless to handle generically.
        box_size = len(data) - offset
        header_len = 8
    else:
        box_size = size32
        header_len = 8

    if box_size < header_len or offset + box_size > len(data):
        return None
    return box_type, header_len, box_size


def _looks_like_mp4(prefix: bytes) -> bool:
    """Cheap classification only, from real file structure (never filename
    or Content-Type): a genuine MP4 always has its `ftyp` box type at byte
    offset 4. Used only to pick which size bound applies to the rest of a
    bounded read (see _classify_and_read_bounded) - the authoritative
    accept/reject decision is always _detect_mp4 on the complete bytes.
    """
    return len(prefix) >= 8 and prefix[4:8] == b"ftyp"


def _looks_like_image_prefix(prefix: bytes) -> bool:
    """Cheap classification only, from real magic bytes - same caveat as
    _looks_like_mp4 above: the authoritative decision is always
    _detect_image_format on the complete bytes."""
    if prefix[:3] == b"\xff\xd8\xff":  # JPEG
        return True
    if prefix[:8] == b"\x89PNG\r\n\x1a\n":  # PNG
        return True
    if prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":  # WEBP
        return True
    return False


def _detect_mp4(data: bytes) -> bool:
    """Verify `data` is a structurally well-formed MP4/ISO-BMFF container
    with meaningful media structure: a valid leading `ftyp` box (real
    box-size bookkeeping, large enough to hold its mandatory
    major_brand/minor_version fields), plus both a `moov` box and an
    `mdat` box present somewhere in the top-level chain that follows -
    order-independent, since real encoders place them in either order
    (faststart MP4s put `moov` before `mdat`; most others put `mdat`
    first). `ftyp` alone, or `ftyp` followed only by other well-formed but
    non-media boxes (e.g. `free`/`skip`), is rejected - a syntactically
    valid box being present is not, by itself, enough to accept the file
    as meaningful MP4 media structure.

    Every box in the chain - not just the first - is walked and must be
    individually well-formed: rejects empty/too-short input, and any box
    anywhere whose declared size is impossible or extends past the bytes
    actually available (see _read_box_header). The walk never reads past
    `data`.

    This is container-structure validation for upload security, not proof
    every encoded frame/codec is decodable - see the module note above.
    """
    if len(data) < 8:
        return False

    offset = 0
    box_index = 0
    seen_moov = False
    seen_mdat = False
    while offset < len(data):
        header = _read_box_header(data, offset)
        if header is None:
            return False
        box_type, header_len, box_size = header

        if box_index == 0:
            if box_type != b"ftyp" or box_size - header_len < 8:
                return False
        elif box_type == b"moov":
            seen_moov = True
        elif box_type == b"mdat":
            seen_mdat = True

        box_index += 1
        if box_size == 0:
            break
        offset += box_size

    return seen_moov and seen_mdat


def _detect_image_format(data: bytes) -> Optional[str]:
    """Verify the given bytes decode as a real, supported image and return
    its Pillow format name (e.g. "JPEG"). Returns None for empty, corrupted,
    malformed, or unsupported-format content.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        # verify() invalidates the Image object for further use, so re-open to
        # read the format after confirming the file isn't truncated/corrupted.
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            fmt = img.format
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    return fmt if fmt in _FORMAT_EXTENSIONS else None


# EXIF Orientation tag id (0x0112 / 274) - the raw numeric tag is used
# instead of PIL.ExifTags.Base (Pillow 9.1+ only) so this keeps working on
# any Pillow version that supports getexif()/exif_transpose() at all.
_EXIF_ORIENTATION_TAG = 0x0112


def _normalize_image_orientation(data: bytes, fmt: str) -> bytes:
    """Bake any EXIF Orientation tag into the actual pixel data, so a
    "sideways" phone photo is pixel-correct for anything downstream that
    doesn't itself respect EXIF - most notably the vision model behind
    Analyze Item, which decodes raw pixels with no way to know it should
    mentally rotate the input. Wardrobe images only (see call sites below)
    - Explore/DM images never reach a vision model, so they're untouched.

    A no-op - returns `data` completely unchanged, byte-for-byte - whenever
    there's nothing to correct (no Orientation tag, or already tag 1/
    normal): the common case for most uploads, PNG screenshots included,
    which rarely carry EXIF orientation in practice. This is what keeps
    the change a pure hardening fix rather than a blanket recompression
    pass - no image is ever re-encoded unless it actually needs rotating.

    When a real transform is needed, re-encodes in the same format Pillow
    already detected (`fmt`, from _detect_image_format) at a high, fixed
    JPEG/WEBP quality (95) - PNG re-encoding is always lossless, so no
    quality parameter applies there. No resizing is ever performed.
    """
    with Image.open(io.BytesIO(data)) as img:
        orientation = img.getexif().get(_EXIF_ORIENTATION_TAG, 1)
        if orientation == 1:
            return data
        transposed = ImageOps.exif_transpose(img)
        buf = io.BytesIO()
        save_kwargs = {"quality": 95} if fmt in ("JPEG", "WEBP") else {}
        transposed.save(buf, format=fmt, **save_kwargs)
        return buf.getvalue()


def _normalize_orientation_bounded(data: bytes, fmt: str) -> bytes:
    """_normalize_image_orientation, plus a clean failure if EXIF-triggered
    re-encoding ever pushed the result over the same MAX_UPLOAD_SIZE_BYTES
    bound every wardrobe image upload is already held to (see
    _read_upload_bounded). A quality=95 JPEG/WEBP re-encode is normally
    close to or smaller than the original, but for an upload already near
    the limit it's a real possibility, not a theoretical one - an
    oversized image must never silently reach disk or the vision model.
    """
    normalized = _normalize_image_orientation(data, fmt)
    if len(normalized) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image file is too large (max {MAX_UPLOAD_SIZE_MB}MB).",
        )
    return normalized


async def _read_remaining_bounded(
    file: UploadFile,
    already_read: bytes,
    max_size_bytes: int,
    max_size_mb: int,
    kind_label: str = "Image",
) -> bytes:
    """Stream-read the rest of an upload in 1MB chunks, continuing from
    `already_read` bytes a caller has already consumed from `file` (counted
    toward `max_size_bytes` from the start) - aborting with 413 the instant
    the bound is exceeded rather than buffering the whole file first. This
    is what actually protects against huge-file abuse, independent of
    whatever the caller's own size limit happens to be.

    Generalizes the original single-shot bounded reader (see
    _read_upload_bounded below, now a thin wrapper over this) so a caller
    that peeked a small classification prefix first (see
    _classify_and_read_bounded) can apply the size bound that matches the
    content it detected, without discarding the bytes already read or
    re-reading them from the client.
    """
    chunks = [already_read] if already_read else []
    size = len(already_read)
    too_large = size > max_size_bytes
    if not too_large:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_size_bytes:
                too_large = True
                break
            chunks.append(chunk)
    await file.close()

    if too_large:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{kind_label} file is too large (max {max_size_mb}MB).",
        )
    return b"".join(chunks)


async def _read_upload_bounded(file: UploadFile, max_size_bytes: int, max_size_mb: int) -> bytes:
    """Stream-read an upload in 1MB chunks, aborting with 413 the instant
    max_size_bytes is exceeded rather than buffering the whole file first.
    Unchanged behavior/signature from before video support was added -
    every existing caller (wardrobe uploads) is untouched by it.
    """
    return await _read_remaining_bounded(file, b"", max_size_bytes, max_size_mb, "Image")


async def _classify_and_read_bounded(
    file: UploadFile,
    image_max_bytes: int,
    image_max_mb: int,
    video_max_bytes: int,
    video_max_mb: int,
) -> tuple[str, bytes]:
    """For a resource whose single attachment field may hold either
    an image or an MP4 video (Explore posts/comments, DMs), peek a small,
    fixed prefix of the upload to classify it by real content signature
    (never filename or client-declared Content-Type - see _looks_like_mp4/
    _looks_like_image_prefix), then continue a bounded read using the size
    limit that applies to the detected kind.

    Classifying from a small prefix *before* picking a size bound - rather
    than reading everything up to the larger (video) bound and classifying
    afterward - is what keeps an oversized "image" upload rejected at the
    same tight image bound as before, instead of being allowed to
    buffer up to the larger video bound first. The prefix itself is only 12
    bytes; too short to be a meaningful attack surface on its own.

    Returns (kind, data) where kind is "image" or "video" and data is the
    complete, already-size-checked upload. Raises HTTPException(400) if the
    prefix matches neither accepted signature, or HTTPException(413) if the
    bounded read for the detected kind is exceeded. The *authoritative*
    accept/reject decision for the content itself is always made afterward
    by the caller, via _detect_image_format/_detect_mp4 on the complete
    bytes returned here - this function only ever decides which size bound
    to apply, never whether the content is ultimately valid.
    """
    prefix = await file.read(12)

    if _looks_like_mp4(prefix):
        data = await _read_remaining_bounded(file, prefix, video_max_bytes, video_max_mb, "Video")
        return "video", data

    if _looks_like_image_prefix(prefix):
        data = await _read_remaining_bounded(file, prefix, image_max_bytes, image_max_mb, "Image")
        return "image", data

    await file.close()
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_UNSUPPORTED_ATTACHMENT_DETAIL)


async def _write_image_file(data: bytes, ext: str, upload_dir: Path) -> str:
    """Write already-validated image bytes to disk under upload_dir with a
    random, server-generated filename - never derived from client input, so
    there is no path-traversal surface. Returns the filename, not a URL.
    """
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = upload_dir / filename
    try:
        async with aiofiles.open(dest, "wb") as out:
            await out.write(data)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return filename


def _delete_image_file(image_url: Optional[str], upload_dir: Path, url_prefix: str) -> None:
    """Best-effort disk cleanup, guarded to only ever touch a path under the
    given upload_dir/url_prefix pair.
    """
    if not image_url or not image_url.startswith(f"{url_prefix}/"):
        return
    filename = Path(image_url).name
    path = upload_dir / filename
    if path.is_file():
        path.unlink(missing_ok=True)


async def save_wardrobe_image(file: UploadFile) -> str:
    """Validate, EXIF-orientation-normalize, and persist an uploaded
    wardrobe image, returning its public URL path."""
    data = await _read_upload_bounded(file, MAX_UPLOAD_SIZE_BYTES, MAX_UPLOAD_SIZE_MB)

    fmt = _detect_image_format(data)
    if fmt is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_UNSUPPORTED_IMAGE_DETAIL)
    data = _normalize_orientation_bounded(data, fmt)

    filename = await _write_image_file(data, _FORMAT_EXTENSIONS[fmt], UPLOAD_DIR)
    return f"{UPLOAD_URL_PREFIX}/{filename}"


async def validate_wardrobe_image_bytes(file: UploadFile) -> bytes:
    """Validate an uploaded wardrobe image (size + real decoded format),
    normalize its EXIF orientation, and return its bytes without writing
    anything to disk.

    Used by the AI analyze-image endpoint, which must never persist the
    photo it's asked to analyze - reuses the exact same size/format/
    orientation handling as save_wardrobe_image, just stops short of
    _write_image_file.
    """
    data = await _read_upload_bounded(file, MAX_UPLOAD_SIZE_BYTES, MAX_UPLOAD_SIZE_MB)

    fmt = _detect_image_format(data)
    if fmt is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_UNSUPPORTED_IMAGE_DETAIL)

    return _normalize_orientation_bounded(data, fmt)


def delete_wardrobe_image(image_url: Optional[str]) -> None:
    """Delete a previously uploaded wardrobe image file, if it lives in our local upload dir."""
    _delete_image_file(image_url, UPLOAD_DIR, UPLOAD_URL_PREFIX)


def _wardrobe_image_signature(filename: str, expires: int) -> str:
    payload = f"{filename}:{expires}".encode()
    return hmac.new(JWT_SECRET.encode(), payload, hashlib.sha256).hexdigest()


def sign_wardrobe_image_url(image_url: Optional[str]) -> Optional[str]:
    """Turn a stored wardrobe image_url into a short-lived, signed URL.

    Wardrobe images no longer live behind a public
    static mount (routers/uploads.py validates the signature this appends).
    Only ever called on an image_url already resolved for a request that
    passed the normal user_id-scoped ownership check, so a signed URL is
    never handed out for an image the caller doesn't own.

    A value that isn't one of our own /uploads/wardrobe/ paths - e.g. a
    client-supplied external image_url via the wardrobe form's `image_url`
    field - is returned unchanged: it was never behind our upload
    directory and has nothing for us to sign or protect.
    """
    if not image_url or not image_url.startswith(f"{UPLOAD_URL_PREFIX}/"):
        return image_url
    filename = Path(image_url).name
    expires = int(time.time()) + WARDROBE_IMAGE_URL_TTL_SECONDS
    sig = _wardrobe_image_signature(filename, expires)
    return f"{UPLOAD_URL_PREFIX}/{filename}?exp={expires}&sig={sig}"


def verify_wardrobe_image_signature(filename: str, expires: int, sig: str) -> bool:
    """Constant-time check that `sig` is a still-valid signature this
    server issued for `filename` with expiry `expires`."""
    if time.time() > expires:
        return False
    expected = _wardrobe_image_signature(filename, expires)
    return hmac.compare_digest(expected, sig)


async def save_explore_image(file: UploadFile) -> dict:
    """Validate and persist one uploaded Explore (post/comment) attachment
    - an image or an MP4 video, detected from real content signatures only
    (never filename or client-declared Content-Type; see
    _classify_and_read_bounded/_detect_image_format/_detect_mp4).

    Returns a dict with url/media_type/content_type/size_bytes - the exact
    fields AttachmentPublic needs - since callers here persist all four on
    the post/comment document, not just a bare URL string.

    Public, unauthenticated static delivery only (see EXPLORE_UPLOAD_DIR in
    core/config.py) - not used for direct-message attachments, which need
    their own authenticated, participant-scoped retrieval.

    This function's name predates video support and was
    deliberately kept unchanged (see routers/posts.py, which calls it
    without modification) rather than introducing a second, parallel
    function name - it now generally handles "one Explore attachment",
    image or video, by carefully generalizing the existing media
    primitives.
    """
    kind, data = await _classify_and_read_bounded(
        file,
        MAX_EXPLORE_IMAGE_SIZE_BYTES, MAX_EXPLORE_IMAGE_SIZE_MB,
        MAX_EXPLORE_VIDEO_SIZE_BYTES, MAX_EXPLORE_VIDEO_SIZE_MB,
    )

    if kind == "video":
        if not _detect_mp4(data):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_UNSUPPORTED_VIDEO_DETAIL)
        filename = await _write_image_file(data, ".mp4", EXPLORE_UPLOAD_DIR)
        return {
            "url": f"{EXPLORE_UPLOAD_URL_PREFIX}/{filename}",
            "media_type": "video",
            "content_type": "video/mp4",
            "size_bytes": len(data),
        }

    fmt = _detect_image_format(data)
    if fmt is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_UNSUPPORTED_IMAGE_DETAIL)

    filename = await _write_image_file(data, _FORMAT_EXTENSIONS[fmt], EXPLORE_UPLOAD_DIR)
    return {
        "url": f"{EXPLORE_UPLOAD_URL_PREFIX}/{filename}",
        "media_type": "image",
        "content_type": _FORMAT_CONTENT_TYPES[fmt],
        "size_bytes": len(data),
    }


async def read_explore_image_bytes(image_url: str) -> Optional[bytes]:
    """Read one already-stored Explore attachment's bytes back off disk.

    Originally written for the since-removed photo-based Recreate flow
    (which analyzed an image the post already had rather than one the
    client uploads); it currently has no callers. Read-only - it
    never writes, deletes, or re-validates anything, and the file it reads
    passed save_explore_image's own size/content checks when it was stored.

    `image_url` must be an attachment URL taken from a post document this
    server stored (see routers/posts.py), never a client-supplied string:
    the caller is responsible for having looked the attachment up through
    its post. This function additionally refuses any URL outside the
    Explore prefix and derives the on-disk path from the URL's basename
    only - the same guard _delete_image_file uses - so even a stored value
    that somehow contained path segments can't escape EXPLORE_UPLOAD_DIR.

    Returns None if the file is missing or unreadable, so a caller can
    answer with a controlled error rather than a 500.
    """
    if not image_url or not image_url.startswith(f"{EXPLORE_UPLOAD_URL_PREFIX}/"):
        return None

    path = EXPLORE_UPLOAD_DIR / Path(image_url).name
    if not path.is_file():
        return None

    try:
        async with aiofiles.open(path, "rb") as src:
            return await src.read()
    except OSError:
        return None


def delete_explore_image(image_url: Optional[str]) -> None:
    """Delete a previously uploaded Explore attachment file (image or MP4
    video - this was already generic on the URL/prefix pattern, not on
    content type, so video support needed no change here), if it lives in our
    local upload dir.
    """
    _delete_image_file(image_url, EXPLORE_UPLOAD_DIR, EXPLORE_UPLOAD_URL_PREFIX)


async def save_dm_image(file: UploadFile, conversation_id: str) -> dict:
    """Validate and persist one uploaded direct-message attachment - an
    image or an MP4 video, detected from real content signatures only (see
    save_explore_image above for the identical reasoning; the two are kept
    as separate functions, not merged, since their storage
    directory/URL/size limits are already independently configured and
    this mirrors the pre-existing separation between save_wardrobe_image/
    save_explore_image/save_dm_image).

    Unlike save_explore_image, the returned url embeds conversation_id and
    points at an authenticated retrieval route
    (GET /messages/conversations/{conversation_id}/attachments/{filename}
    in routers/messages.py) rather than a public static path - DM
    attachments are visible only to the conversation's two participants,
    and DM_UPLOAD_DIR is never passed to StaticFiles/app.mount() at all.
    """
    kind, data = await _classify_and_read_bounded(
        file,
        MAX_DM_IMAGE_SIZE_BYTES, MAX_DM_IMAGE_SIZE_MB,
        MAX_DM_VIDEO_SIZE_BYTES, MAX_DM_VIDEO_SIZE_MB,
    )

    if kind == "video":
        if not _detect_mp4(data):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_UNSUPPORTED_VIDEO_DETAIL)
        filename = await _write_image_file(data, ".mp4", DM_UPLOAD_DIR)
        return {
            "url": f"/messages/conversations/{conversation_id}/attachments/{filename}",
            "media_type": "video",
            "content_type": "video/mp4",
            "size_bytes": len(data),
        }

    fmt = _detect_image_format(data)
    if fmt is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_UNSUPPORTED_IMAGE_DETAIL)

    filename = await _write_image_file(data, _FORMAT_EXTENSIONS[fmt], DM_UPLOAD_DIR)
    return {
        "url": f"/messages/conversations/{conversation_id}/attachments/{filename}",
        "media_type": "image",
        "content_type": _FORMAT_CONTENT_TYPES[fmt],
        "size_bytes": len(data),
    }


def delete_dm_image(image_url: Optional[str]) -> None:
    """Delete a previously uploaded DM image file, if it lives in our local
    upload dir.

    Guards on the fixed "/attachments/" path segment rather than a simple
    prefix (unlike delete_wardrobe_image/delete_explore_image), since a DM
    attachment's url embeds a variable conversation_id between the fixed
    route prefix and the filename - there's no single constant prefix to
    match against.
    """
    if not image_url or "/attachments/" not in image_url:
        return
    filename = Path(image_url).name
    path = DM_UPLOAD_DIR / filename
    if path.is_file():
        path.unlink(missing_ok=True)
