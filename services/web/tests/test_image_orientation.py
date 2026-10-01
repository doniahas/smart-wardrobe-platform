import io

import pytest
from fastapi import HTTPException
from PIL import Image

from app.services import image_storage
from app.services.image_storage import (
    _EXIF_ORIENTATION_TAG,
    _normalize_image_orientation,
    _normalize_orientation_bounded,
)


def _jpeg_with_orientation(orientation, size=(40, 20), color=(200, 50, 50)):
    """A small JPEG fixture carrying a real EXIF Orientation tag - not a
    screenshot, and not derived from any real-image fixture elsewhere in
    the suite, since this is specifically testing EXIF handling."""
    img = Image.new("RGB", size, color)
    exif = img.getexif()
    if orientation is not None:
        exif[_EXIF_ORIENTATION_TAG] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def test_no_exif_is_returned_byte_identical():
    data = _jpeg_with_orientation(orientation=None)
    assert _normalize_image_orientation(data, "JPEG") == data


def test_orientation_1_is_returned_byte_identical():
    data = _jpeg_with_orientation(orientation=1)
    assert _normalize_image_orientation(data, "JPEG") == data


def test_orientation_6_is_transposed_and_tag_is_cleared():
    """Orientation 6/8 both imply a ~90-degree rotation, which swaps
    width/height for a non-square image - a robust, rotation-direction-
    agnostic signal that a real transpose happened. Verifying the exact
    rotation direction is Pillow's own responsibility (already covered by
    Pillow's test suite) - this test is about our wrapper's decision
    logic: detect, transpose, re-encode, strip the now-stale tag."""
    data = _jpeg_with_orientation(orientation=6, size=(40, 20))
    result = _normalize_image_orientation(data, "JPEG")

    assert result != data
    with Image.open(io.BytesIO(result)) as img:
        assert img.size == (20, 40)
        assert img.getexif().get(_EXIF_ORIENTATION_TAG, 1) == 1


def test_orientation_3_rotated_180_same_dimensions_but_bytes_differ():
    """180-degree rotation doesn't change width/height, exercising the
    "content changed, dimensions didn't" case orientation 6 doesn't
    cover."""
    data = _jpeg_with_orientation(orientation=3, size=(40, 20))
    result = _normalize_image_orientation(data, "JPEG")

    assert result != data
    with Image.open(io.BytesIO(result)) as img:
        assert img.size == (40, 20)
        assert img.getexif().get(_EXIF_ORIENTATION_TAG, 1) == 1


def test_png_without_exif_is_untouched():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (0, 0, 0)).save(buf, format="PNG")
    data = buf.getvalue()
    assert _normalize_image_orientation(data, "PNG") == data


def test_normalize_orientation_bounded_returns_normalized_bytes_under_limit(monkeypatch):
    monkeypatch.setattr(image_storage, "MAX_UPLOAD_SIZE_BYTES", 10 * 1024 * 1024)
    data = _jpeg_with_orientation(orientation=6, size=(40, 20))
    result = _normalize_orientation_bounded(data, "JPEG")
    with Image.open(io.BytesIO(result)) as img:
        assert img.size == (20, 40)


def test_normalize_orientation_bounded_rejects_oversized_result(monkeypatch):
    """A re-encode that would exceed the configured max upload size must
    fail cleanly (413) rather than silently persisting/sending an
    oversized image - simulated here by setting the bound impossibly low
    rather than trying to construct a real image that grows past a
    realistic multi-MB limit after re-encoding."""
    monkeypatch.setattr(image_storage, "MAX_UPLOAD_SIZE_BYTES", 10)
    monkeypatch.setattr(image_storage, "MAX_UPLOAD_SIZE_MB", 1)
    data = _jpeg_with_orientation(orientation=6, size=(40, 20))

    with pytest.raises(HTTPException) as exc_info:
        _normalize_orientation_bounded(data, "JPEG")

    assert exc_info.value.status_code == 413
    assert "too large" in exc_info.value.detail
