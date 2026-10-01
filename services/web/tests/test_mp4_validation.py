"""Unit tests for the MP4/ISO-BMFF structural sanity check in
app.services.image_storage (_detect_mp4, _read_box_header, _looks_like_mp4,
_looks_like_image_prefix) - exercised directly, independent of the upload
route layer, mirroring how the pre-existing image-format checks in
test_image_storage.py are unit-tested at this same level.

By design, this validates MP4 *container* structure, not codec
decodability - these tests prove exactly that boundary, not more.
"""
from app.services import image_storage
from tests.mp4_fixtures import (
    _box,
    _ftyp_payload,
    ftyp_only_bytes,
    ftyp_plus_free_only_bytes,
    ftyp_plus_garbage_bytes,
    ftyp_too_small_bytes,
    impossible_box_size_bytes,
    missing_mdat_bytes,
    missing_moov_bytes,
    not_mp4_at_all_bytes,
    truncated_box_bytes,
    valid_mp4_bytes,
    valid_mp4_with_extended_size_bytes,
)


def test_valid_mp4_is_accepted():
    assert image_storage._detect_mp4(valid_mp4_bytes()) is True


def test_valid_mp4_with_extended_box_size_is_accepted():
    assert image_storage._detect_mp4(valid_mp4_with_extended_size_bytes()) is True


def test_empty_bytes_rejected():
    assert image_storage._detect_mp4(b"") is False


def test_too_short_to_contain_a_box_header_rejected():
    assert image_storage._detect_mp4(b"ftyp") is False


def test_random_bytes_rejected():
    assert image_storage._detect_mp4(not_mp4_at_all_bytes()) is False


def test_ftyp_signature_alone_with_no_further_structure_rejected():
    """A bare ftyp box with nothing following it - real MP4s always have
    further top-level structure (moov/mdat/...) after ftyp."""
    assert image_storage._detect_mp4(ftyp_only_bytes()) is False


def test_ftyp_plus_random_garbage_rejected():
    """The specific "trivial ftyp-only spoofing" case the validator exists
    to catch: a real ftyp signature followed by bytes that don't form any
    recognized box."""
    assert image_storage._detect_mp4(ftyp_plus_garbage_bytes()) is False


def test_ftyp_plus_syntactically_valid_non_media_box_only_rejected():
    """The tightened criterion: a real, well-formed ISO-BMFF box (`free`)
    following ftyp is not, by itself, enough - it's neither `moov` nor
    `mdat`, so this must still be rejected as lacking meaningful MP4 media
    structure, not accepted merely because a second syntactically valid
    box name exists."""
    assert image_storage._detect_mp4(ftyp_plus_free_only_bytes()) is False


def test_missing_moov_rejected():
    """A valid ftyp and a valid mdat, but no moov - media data with no
    metadata box is not meaningful MP4 structure."""
    assert image_storage._detect_mp4(missing_moov_bytes()) is False


def test_missing_mdat_rejected():
    """A valid ftyp and a valid moov, but no mdat - metadata with no
    actual media data is not meaningful MP4 structure."""
    assert image_storage._detect_mp4(missing_mdat_bytes()) is False


def test_moov_and_mdat_accepted_regardless_of_order():
    """Order-independence: real encoders write mdat before moov (most
    common, non-faststart) or moov before mdat (faststart) - both must be
    accepted."""
    ftyp = _box(b"ftyp", _ftyp_payload())
    moov = _box(b"moov")
    mdat = _box(b"mdat", b"\x00" * 4)
    assert image_storage._detect_mp4(ftyp + moov + mdat) is True  # moov first
    assert image_storage._detect_mp4(ftyp + mdat + moov) is True  # mdat first


def test_ftyp_box_too_small_for_mandatory_fields_rejected():
    assert image_storage._detect_mp4(ftyp_too_small_bytes()) is False


def test_truncated_box_boundary_rejected():
    """A box that declares a size larger than the bytes actually present."""
    assert image_storage._detect_mp4(truncated_box_bytes()) is False


def test_impossible_box_size_rejected():
    """A box declaring a size smaller than its own header - impossible."""
    assert image_storage._detect_mp4(impossible_box_size_bytes()) is False


def test_does_not_read_past_the_provided_bytes():
    """A pathological box claiming an enormous size must not make the
    validator try to read/seek beyond the buffer it was actually given -
    it must reject cleanly instead of raising."""
    import struct

    huge_claim = struct.pack(">I", 0xFFFFFFFF) + b"moov"
    data = _box(b"ftyp", _ftyp_payload()) + huge_claim
    assert image_storage._detect_mp4(data) is False


# --- _looks_like_mp4 / _looks_like_image_prefix (classification only) ---


def test_looks_like_mp4_true_for_real_ftyp_prefix():
    assert image_storage._looks_like_mp4(valid_mp4_bytes()[:12]) is True


def test_looks_like_mp4_false_for_non_mp4_prefix():
    assert image_storage._looks_like_mp4(b"just plain text prefix!!") is False


def test_looks_like_mp4_false_for_short_prefix():
    assert image_storage._looks_like_mp4(b"short") is False


def test_looks_like_image_prefix_false_for_mp4_prefix():
    """A real MP4 prefix must never also be classified as an image prefix -
    the two classifiers are mutually exclusive on genuine content."""
    assert image_storage._looks_like_image_prefix(valid_mp4_bytes()[:12]) is False
