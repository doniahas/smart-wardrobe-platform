"""Small, deterministic MP4/ISO-BMFF byte builders shared by the
video test files (test_mp4_validation.py, test_explore_media.py,
test_dm_media.py, security/test_video_attachment_security.py).

Deliberately not named test_*.py so pytest never tries to collect this
module itself. Every fixture here is built in code, not stored as a
committed binary file, deliberately avoiding large binary fixtures -
each is a handful of real (or deliberately
broken) ISO-BMFF box headers, nothing else.
"""
import struct


def _box(box_type: bytes, payload: bytes = b"") -> bytes:
    """One ordinary (32-bit size) ISO-BMFF box: 4-byte size + 4-byte type
    + payload."""
    size = 8 + len(payload)
    return struct.pack(">I", size) + box_type + payload


def _ftyp_payload(major_brand: bytes = b"isom", compatible: bytes = b"isom") -> bytes:
    # major_brand(4) + minor_version(4) + at least one compatible_brands(4)
    # entry - the minimum a real encoder always writes.
    return major_brand + b"\x00\x00\x00\x00" + compatible


def valid_mp4_bytes() -> bytes:
    """A minimal, genuinely well-formed MP4 container: a real ftyp box
    (with actual major_brand/minor_version/compatible_brands payload) plus
    an empty moov box and a small mdat box. Not a playable video (mdat's
    "media data" is just placeholder bytes - _detect_mp4 never inspects
    codec/frame data, see its docstring), but a structurally valid MP4
    container, which is exactly what the validator is meant to check.
    """
    ftyp = _box(b"ftyp", _ftyp_payload())
    moov = _box(b"moov")
    mdat = _box(b"mdat", b"\x00" * 16)
    return ftyp + moov + mdat


def valid_mp4_with_extended_size_bytes() -> bytes:
    """Same as valid_mp4_bytes, but moov uses the ISO-BMFF 64-bit
    extended-size form (32-bit size field == 1, followed by a real 8-byte
    size) - proves that code path is parsed correctly too, not just the
    ordinary 32-bit-size case every other fixture here uses."""
    ftyp = _box(b"ftyp", _ftyp_payload())
    header_len = 16
    moov_ext = struct.pack(">I", 1) + b"moov" + struct.pack(">Q", header_len)
    mdat = _box(b"mdat", b"\x00" * 4)
    return ftyp + moov_ext + mdat


def ftyp_plus_garbage_bytes() -> bytes:
    """A trivial spoof: a real ftyp signature/box followed by unrelated
    bytes that don't form any recognized box at all - the exact "ftyp +
    random junk" case the validator must reject, not just accept because
    the file starts with a valid-looking ftyp box."""
    ftyp = _box(b"ftyp", _ftyp_payload())
    return ftyp + b"this is not a real mp4 box at all, just plain junk bytes after it"


def ftyp_only_bytes() -> bytes:
    """A single, otherwise well-formed ftyp box and nothing else - no
    second top-level box follows at all. Rejected because real MP4s always
    have further structure (moov/mdat/...) after ftyp."""
    return _box(b"ftyp", _ftyp_payload())


def ftyp_plus_free_only_bytes() -> bytes:
    """A real, syntactically well-formed ISO-BMFF box (`free`) following a
    valid ftyp - genuinely valid box structure, but neither `moov` nor
    `mdat`. Must still be rejected: a syntactically valid but non-media
    box being present is not, by itself, enough to turn an arbitrary file
    into an accepted video - meaningful media structure (moov *and* mdat)
    is required."""
    ftyp = _box(b"ftyp", _ftyp_payload())
    free = _box(b"free", b"\x00" * 4)
    return ftyp + free


def missing_moov_bytes() -> bytes:
    """A valid ftyp and a valid mdat, but no moov box anywhere - real MP4
    media data with no metadata box is not meaningful MP4 structure."""
    ftyp = _box(b"ftyp", _ftyp_payload())
    mdat = _box(b"mdat", b"\x00" * 8)
    return ftyp + mdat


def missing_mdat_bytes() -> bytes:
    """A valid ftyp and a valid moov, but no mdat box anywhere - metadata
    with no actual media data is not meaningful MP4 structure."""
    ftyp = _box(b"ftyp", _ftyp_payload())
    moov = _box(b"moov")
    return ftyp + moov


def ftyp_too_small_bytes() -> bytes:
    """An ftyp box whose payload is too small to hold the mandatory
    major_brand+minor_version fields (needs >= 8 bytes) - structurally
    implausible even though the box header itself is well-formed."""
    return _box(b"ftyp", b"iso")


def truncated_box_bytes() -> bytes:
    """A second box (after a valid ftyp) that declares a size larger than
    the bytes actually present - a truncated/corrupted upload."""
    ftyp = _box(b"ftyp", _ftyp_payload())
    truncated_moov = struct.pack(">I", 100) + b"moov" + b"\x00" * 8
    return ftyp + truncated_moov


def impossible_box_size_bytes() -> bytes:
    """A second box declaring a size smaller than its own 8-byte header -
    structurally impossible, must be rejected outright."""
    ftyp = _box(b"ftyp", _ftyp_payload())
    impossible = struct.pack(">I", 4) + b"moov"
    return ftyp + impossible


def not_mp4_at_all_bytes() -> bytes:
    """Arbitrary bytes that don't even start with an ftyp box type."""
    return b"just some random bytes, not an mp4 container at all, definitely not a video"
