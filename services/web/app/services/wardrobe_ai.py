"""AI-assisted wardrobe image analysis (Analyze Item).

analyze_wardrobe_image suggests wardrobe metadata from a photo of a single
clothing item. Never touches disk or the database, and never decides a
value on the user's behalf - see routers/wardrobe.py, where the result is
returned for the user to review/edit before anything is saved via the
unchanged POST /wardrobe.

The former multi-garment "Analyze Full Outfit" path and its
photo-based Recreate bridge were removed - recreation now only
works from structured saved/shared outfit data (services/recreate.py),
never from an arbitrary photo. Nothing here was a shim for that removal;
what remains is exactly what Analyze Item itself needs.
"""
import json
import logging
import re

from app.models import (
    ClothingType,
    Occasion,
    Season,
    Style,
    WardrobeAiSuggestion,
)
from app.services.llm_gateway import ChatGatewayFailure, call_llm_vision

logger = logging.getLogger(__name__)
# Without this, the diagnostic logged at the bottom of analyze_wardrobe_image
# never actually emits: this module's logger otherwise inherits the root
# logger's default level (WARNING - nothing in this project configures
# logging globally), and a bare getLogger() with no handler anywhere in its
# chain falls back to Python's "handler of last resort", which itself only
# ever passes WARNING and above. Self-contained to this one logger - not a
# global logging.basicConfig() change - so no other module's log verbosity
# is affected.
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)

# A garment may reasonably fit a few style/occasion values at once, but not
# an unbounded number - keeps suggestions genuinely selective ("not every
# value, just in case") rather than an unbounded guess list.
_MAX_SUGGESTED_TAGS = 3

# Ollama's structured-output constraint (see services/llm's OLLAMA_VISION_MODEL
# support) - the moondream spike showed sub-4B vision models are unreliable
# at JSON compliance from prompting alone, so the schema is enforced by
# Ollama's decoder, not just requested in the prompt text.
_GARMENT_PROPERTIES = {
    "name": {"type": ["string", "null"]},
    "clothing_type": {
        "type": ["string", "null"],
        "enum": [e.value for e in ClothingType] + [None],
    },
    "color": {"type": ["string", "null"]},
    "season": {
        "type": ["string", "null"],
        "enum": [e.value for e in Season] + [None],
    },
    "style": {
        "type": ["array", "null"],
        "items": {"type": "string", "enum": [e.value for e in Style]},
        "maxItems": _MAX_SUGGESTED_TAGS,
    },
    "occasion": {
        "type": ["array", "null"],
        "items": {"type": "string", "enum": [e.value for e in Occasion]},
        "maxItems": _MAX_SUGGESTED_TAGS,
    },
}
_GARMENT_REQUIRED = ["name", "clothing_type", "color", "season", "style", "occasion"]

# One garment, one flat object.
_SCHEMA = {
    "type": "object",
    "properties": _GARMENT_PROPERTIES,
    "required": _GARMENT_REQUIRED,
}

_NAME_INSTRUCTION = (
    "For name, give a short garment description naming what the item "
    "actually is (for example \"Gray hoodie\", \"White sneakers\", "
    "\"Denim skirt\") - never a bare style, occasion, or season "
    "word by itself (for example never just \"Casual\", \"Formal\", or "
    "\"Spring\" as the whole name). If you aren't sure what to call it, "
    "use null rather than guessing."
)

_COLOR_INSTRUCTION = (
    "For color, name the actual visible color of the garment in plain "
    "words (for example \"olive green\", \"navy blue\", \"burgundy\") - "
    "never a hex code, and never a generic gray/black value unless the "
    "garment itself is actually gray or black."
)

_STYLE_OCCASION_INSTRUCTION = (
    "For style and occasion, a garment can genuinely fit more than one - "
    "list every value that clearly applies as an array (for example "
    "[\"casual\", \"streetwear\"]), not just the single best-fitting one. "
    "Only include a value you're actually confident about; use an empty "
    "array rather than padding it with a value that doesn't really apply."
)

_PROMPT = (
    "You are looking at a single clothing item photo. Identify: a short "
    "name/description, clothing_type, color, season, style, and occasion. "
    f"{_NAME_INSTRUCTION} {_COLOR_INSTRUCTION} {_STYLE_OCCASION_INSTRUCTION} "
    "If you are unsure about a field, use null rather than guessing. "
    "Respond only with JSON matching the given schema."
)

# clothing_type consistency check (Design C): a second, lightweight vision
# call asking for nothing but clothing_type, run only when the production
# call above already gave a non-null one - see _verify_clothing_type. This
# never asks about name/color/season/style/occasion, and its answer is
# never used to replace or "correct" the production value - only to
# confirm it (kept as-is) or withhold it (nulled) when the two disagree.
# No garment-keyword taxonomy of any kind is involved.
_TYPE_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "clothing_type": {
            "type": ["string", "null"],
            "enum": [e.value for e in ClothingType] + [None],
        },
    },
    "required": ["clothing_type"],
}

_TYPE_VERIFY_PROMPT = (
    "You are looking at a single clothing item photo. Identify only its "
    "clothing_type. If you are unsure, use null rather than guessing. "
    "Respond only with JSON matching the given schema."
)


def _none_if_blank(value):
    """Treat "" (and whitespace-only strings) the same as null - the
    moondream spike showed the model returns "" for "no answer" on some
    fields even though the schema allows null."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    return stripped if stripped else None


def _valid_enum_or_none(enum_cls, value):
    """A value that doesn't match one of enum_cls's members becomes null -
    never guessed or coerced into some other valid member."""
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


def _valid_enum_list_or_none(enum_cls, value):
    """List-valued counterpart to _valid_enum_or_none, for style/occasion
    (see models.py - a garment may suggest more than one of each). Drops
    anything that isn't a real enum member rather than rejecting the whole
    list or guessing a replacement, deduplicates while preserving order,
    and caps at _MAX_SUGGESTED_TAGS as a second line of defense on top of
    the schema's own maxItems. An empty or all-invalid result becomes
    None, not [] - the same "null rather than guess" convention every
    other suggested field already follows."""
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        try:
            member = enum_cls(item)
        except (ValueError, TypeError):
            continue
        if member not in result:
            result.append(member)
        if len(result) >= _MAX_SUGGESTED_TAGS:
            break
    return result or None


# Every Style/Occasion/Season value, lowercased - these are tag/descriptor
# vocabularies, never garment nouns, so a `name` that is *exactly* one of
# them (not merely containing one) is a field-bleed artifact rather than a
# real description (see _null_if_name_is_a_tag_value). ClothingType is
# deliberately excluded: unlike a style/occasion/season word, "top" or
# "dress" alone is still a plausible if terse genuine garment name.
_NON_NAME_TAG_VALUES = frozenset(
    member.value for enum_cls in (Style, Occasion, Season) for member in enum_cls
)


def _null_if_name_is_a_tag_value(name: str | None) -> str | None:
    """Catches the model reusing one of its own Style/Occasion/Season
    values as the whole `name` (e.g. "Casual") instead of actually
    describing the garment - a small, generic self-consistency check
    against the model's own returned vocabulary, not a garment-domain
    lookup table. Only an exact (case-insensitive, whitespace-trimmed)
    match triggers this; a real description that merely contains one of
    these words (e.g. "Casual Friday blazer") is left untouched - see
    _NAME_INSTRUCTION for the matching prompt-side guidance."""
    if name is None:
        return None
    if name.strip().lower() in _NON_NAME_TAG_VALUES:
        return None
    return name


_HEX_COLOR_RE = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Chromatic garment-color words the model might still mention in `name`
# even when `color` itself came back as a hex code - used only to recover
# a semantic color when the hex looks like a generic/default value.
# Achromatic words (gray/black/white) are deliberately excluded: they
# don't conflict with a grayscale or near-black hex, so there's nothing to
# reconcile.
_COLOR_KEYWORDS = [
    "olive", "green", "navy", "blue", "red", "maroon", "burgundy", "pink",
    "purple", "violet", "indigo", "yellow", "mustard", "orange", "brown",
    "tan", "beige", "cream", "ivory", "teal", "turquoise", "gold", "khaki",
    "coral", "lavender", "mint", "magenta", "crimson", "peach", "rust",
]
_COLOR_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(_COLOR_KEYWORDS) + r")\b", re.IGNORECASE
)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _is_suspicious_grayscale(rgb: tuple[int, int, int]) -> bool:
    """A hex value with the shape of a "we didn't really look" default -
    near-zero saturation (gray) or very low brightness (near-black) - as
    opposed to a legitimately gray/black/dark garment, which this alone
    can't tell apart. Reconciliation against `name` (see
    _reconcile_color) is what tells the two cases apart."""
    r, g, b = rgb
    is_grayscale = (max(r, g, b) - min(r, g, b)) <= 12
    is_near_black = max(r, g, b) <= 40
    return is_grayscale or is_near_black


def _named_color_from_rgb(rgb: tuple[int, int, int]) -> str:
    """Best-effort hue bucketing into a plain color word - not real color
    science, just enough to turn a hex code into something a user reads as
    a color name rather than a swatch of digits. Only called on hex-shaped
    `color` values (see _reconcile_color); a color the model already
    phrased as a word never passes through here."""
    if _is_suspicious_grayscale(rgb):
        if max(rgb) <= 40:
            return "black"
        if min(rgb) >= 235:
            return "white"
        return "gray"

    r, g, b = (c / 255 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    l = (mx + mn) / 2
    if mx == r:
        h = 60 * (((g - b) / d) % 6)
    elif mx == g:
        h = 60 * (((b - r) / d) + 2)
    else:
        h = 60 * (((r - g) / d) + 4)
    h %= 360

    if h < 15 or h >= 345:
        return "maroon" if l < 0.35 else "red"
    if h < 45:
        return "brown" if l < 0.35 else "orange"
    if h < 90:
        return "olive" if l < 0.45 else "yellow"
    if h < 170:
        return "green"
    if h < 200:
        return "teal"
    if h < 250:
        return "navy" if l < 0.35 else "blue"
    if h < 290:
        return "purple"
    return "pink"


def _reconcile_color(name: str | None, color: str | None, context: str) -> str | None:
    """Catch the case that motivated this function: the model names a
    visible chromatic color (e.g. "Green" in `name`) but `color` itself
    comes back as a hex code (e.g. "#222222") instead of a color word.

    `color` is a free-text, user-facing field (shown as text and used
    directly as a swatch's CSS background - see app.js) - it must never
    surface a raw hex code to the user, so any hex-shaped value is always
    replaced with a plain color name, never passed through as-is:
      - if the hex looks like a "we didn't really look" default
        (grayscale/near-black - see _is_suspicious_grayscale) and `name`
        already mentions a chromatic color, that word is used instead,
        since it means the hex contradicts the model's own description;
      - otherwise the hex is converted to its nearest plain color name via
        _named_color_from_rgb (covers both a legitimate, non-suspicious
        hex, and a suspicious one that `name` doesn't contradict - e.g.
        "gray hoodie" plus a grayscale hex both resolve to "gray").
    A plain color word/phrase the model already gave (e.g. "navy", "olive
    green") is left untouched - only hex-shaped values go through this.
    """
    if color is None:
        return None
    match = _HEX_COLOR_RE.match(color.strip())
    if not match:
        return color

    try:
        rgb = _hex_to_rgb(match.group(0))
    except ValueError:
        return color

    if _is_suspicious_grayscale(rgb):
        keyword_match = _COLOR_KEYWORD_RE.search(name or "")
        if keyword_match:
            keyword = keyword_match.group(1).lower()
            logger.warning(
                "%s: color %r is inconsistent with name %r - using %r instead",
                context, color, name, keyword,
            )
            return keyword

    derived = _named_color_from_rgb(rgb)
    logger.debug("%s: replacing hex color %r with named color %r", context, color, derived)
    return derived


# --- Semantic color palette (wardrobe Color field UX pass) ---
#
# The wardrobe form's Color field is a fixed palette, never free text or a
# raw hex code (see static/app.js's WARDROBE_COLOR_PALETTE - this project
# has no shared build step between the Python backend and the static JS
# frontend, so each side keeps its own copy of these same 14 values, kept
# in sync by convention the same way WARDROBE_FIELD_OPTIONS in app.js
# already mirrors the clothing_type/season/style/occasion enums here).
#
# This is only used to steer an AI suggestion onto one of these values (or
# None) before it reaches the review UI - it never validates or rejects a
# value written directly through POST /wardrobe, which stays a free string
# so older saved items (and any value a user types via a legacy client)
# keep working unchanged.
WARDROBE_COLOR_PALETTE = (
    "black", "white", "gray", "navy", "blue", "green", "olive", "beige",
    "brown", "red", "pink", "purple", "yellow", "orange",
)

# Each category's representative shade - the first/default swatch in that
# category's curated grid on the frontend (static/app.js's
# WARDROBE_COLOR_SHADES). Used only to give an AI color suggestion a
# starting color_hex to display before the user refines it via the picker
# (see _garment_fields) - never treated as an estimate of the actual
# garment's shade. Kept in sync with the frontend's own copy by
# convention, the same way WARDROBE_COLOR_PALETTE itself already is.
_COLOR_PALETTE_HEX = {
    "black": "#111111",
    "white": "#ffffff",
    "gray": "#888888",
    "navy": "#1f2d50",
    "blue": "#2b6cb0",
    "green": "#2f7d32",
    "olive": "#6b8e23",
    "beige": "#d9c9a3",
    "brown": "#6b4423",
    "red": "#c0392b",
    "pink": "#e07a9e",
    "purple": "#7d4bb0",
    "yellow": "#d4b106",
    "orange": "#d2691e",
}
assert set(_COLOR_PALETTE_HEX) == set(WARDROBE_COLOR_PALETTE)

# Synonym -> canonical palette value, checked in this order. Order matters
# whenever a phrase mentions two color words at once (e.g. "olive green",
# "navy blue"): the narrower/more specific bucket is listed first so it
# wins over the broader hue it's a shade of, rather than the match being
# decided by word position in the phrase.
_COLOR_PALETTE_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
    ("navy", ("navy",)),
    ("olive", ("olive", "khaki")),
    ("beige", ("beige", "tan", "sand", "ivory", "cream", "camel", "nude")),
    ("red", ("red", "burgundy", "maroon", "crimson", "wine", "rust", "scarlet")),
    ("pink", ("pink", "rose", "salmon", "coral", "peach", "fuchsia", "magenta")),
    ("purple", ("purple", "violet", "lavender", "plum", "indigo", "lilac")),
    ("yellow", ("yellow", "mustard", "gold", "lemon")),
    ("gray", ("gray", "grey", "silver", "charcoal")),
    ("blue", ("blue", "denim", "sky", "teal", "turquoise", "cyan")),
    ("green", ("green", "mint", "sage", "forest", "emerald")),
    ("brown", ("brown", "chocolate", "coffee", "chestnut", "taupe")),
    ("black", ("black",)),
    ("white", ("white",)),
    ("orange", ("orange", "amber", "apricot")),
]
assert {canonical for canonical, _ in _COLOR_PALETTE_SYNONYMS} == set(WARDROBE_COLOR_PALETTE)


def _map_to_palette(color: str | None, context: str) -> str | None:
    """Collapse a color word/phrase (already de-hexed by _reconcile_color,
    if it started as a hex value) onto one of the 14 palette values, or
    None if nothing recognizable matches - the same "null rather than
    guess" convention _valid_enum_or_none uses for the other suggested
    fields.

    This is a generic synonym match against the text, not a lookup keyed
    to any particular garment - "olive green" maps to "olive" the same way
    regardless of what the rest of the suggestion says, so no single test
    photo gets special-cased here or anywhere else in this pipeline.
    """
    if color is None:
        return None
    lowered = color.strip().lower()
    if not lowered:
        return None
    for canonical, synonyms in _COLOR_PALETTE_SYNONYMS:
        if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in synonyms):
            if canonical != lowered:
                logger.debug(
                    "%s: mapping color %r to palette value %r", context, color, canonical,
                )
            return canonical
    logger.info("%s: color %r did not match the palette - leaving unset", context, color)
    return None


def _parse_object(raw: str, context: str) -> dict:
    """Parse the model's text as a JSON object, degrading to {} rather than
    raising. Malformed output is not a failure mode here - the LLM call
    itself succeeded, the model just said nothing usable (see each caller's
    docstring)."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None

    if not isinstance(parsed, dict):
        logger.warning("%s: model response was not a JSON object", context)
        return {}
    return parsed


def _garment_fields(parsed: dict, context: str) -> dict:
    """Normalize one raw garment object into DetectedGarment's fields.

    Blank strings become null, and a value outside the project's enums
    becomes null rather than being coerced into a valid-but-wrong member.
    `color` additionally goes through _reconcile_color (catches a hex
    value that contradicts a chromatic color the model already named) and
    then _map_to_palette (collapses whatever color word/phrase comes out
    of that onto one of the 14 palette values, or None) - see both
    functions' docstrings. `color_hex` is never asked of the model itself
    (this local vision model has already shown it isn't reliable at
    estimating a real shade - see _reconcile_color's docstring for the
    bug that motivated it); it is only that mapped category's own
    representative shade from _COLOR_PALETTE_HEX, a starting point for the
    user to refine via the Color picker, not a shade estimate. `style`/
    `occasion` go through _valid_enum_list_or_none instead: the model may
    suggest more than one of each (see _STYLE_OCCASION_INSTRUCTION), so
    each is validated/deduped/capped as a list rather than a single enum.
    `name` additionally goes through _null_if_name_is_a_tag_value first,
    so a bled tag value never reaches color reconciliation either.
    """
    name = _null_if_name_is_a_tag_value(_none_if_blank(parsed.get("name")))
    color = _reconcile_color(name, _none_if_blank(parsed.get("color")), context)
    color = _map_to_palette(color, context)
    return {
        "name": name,
        "clothing_type": _valid_enum_or_none(ClothingType, _none_if_blank(parsed.get("clothing_type"))),
        "color": color,
        "color_hex": _COLOR_PALETTE_HEX.get(color),
        "season": _valid_enum_or_none(Season, _none_if_blank(parsed.get("season"))),
        "style": _valid_enum_list_or_none(Style, parsed.get("style")),
        "occasion": _valid_enum_list_or_none(Occasion, parsed.get("occasion")),
    }


async def _verify_clothing_type(image_bytes: bytes) -> ClothingType | None:
    """Ask a second, lightweight time whether the same photo's
    clothing_type agrees with what the production call already said -
    Design C's consistency check. Always returns a plain ClothingType or
    None, never raises and never a failure object: an unreachable/timed-out
    llm service, a malformed response, or the model itself saying null are
    all indistinguishable to the caller and all mean the same thing here -
    "not confirmed" - so analyze_wardrobe_image can treat them with one
    uniform comparison instead of three separate cases.
    """
    result = await call_llm_vision(_TYPE_VERIFY_PROMPT, image_bytes, _TYPE_VERIFY_SCHEMA)
    if isinstance(result, ChatGatewayFailure):
        logger.info(
            "wardrobe image analysis: clothing_type verification call failed (%s) - treating as unconfirmed",
            result.reason,
        )
        return None

    parsed = _parse_object(result.response, "wardrobe image analysis (clothing_type verification)")
    return _valid_enum_or_none(ClothingType, _none_if_blank(parsed.get("clothing_type")))


async def analyze_wardrobe_image(image_bytes: bytes) -> WardrobeAiSuggestion | ChatGatewayFailure:
    """Ask the local vision model to suggest wardrobe fields for one photo.

    Never raises for an expected failure mode - a ChatGatewayFailure means
    the llm service itself was unavailable/timed out/errored (the caller
    maps this to an HTTP error). Malformed or empty model output is not
    treated as a failure - it comes back as a WardrobeAiSuggestion with
    every field null, since the LLM call itself succeeded, it just had
    nothing useful to say.

    Logs twice, at two different levels for two different purposes: the
    raw model text (the exact JSON string) at DEBUG - development-only,
    off by default - and, at INFO (on by default - see the logger setup
    above), the model's own parsed field values, the clothing_type
    verification call's outcome, and the final normalized values side by
    side, so a run can be inspected to tell genuine model output apart
    from something _garment_fields (or the verification gate below)
    changed. Neither log line ever includes the request image itself -
    only the structured text/list fields these dicts can contain.

    clothing_type consistency check (Design C): when the production call
    above gives a non-null clothing_type, a second, lightweight vision
    call (_verify_clothing_type) asks the same photo for nothing but
    clothing_type. If it agrees, the production value is kept unchanged.
    If it disagrees, or can't be confirmed at all (the second call fails,
    times out, or the model itself says null there), clothing_type is
    nulled - never replaced with the second call's own value. Every other
    field is completely unaffected by this check.
    """
    result = await call_llm_vision(_PROMPT, image_bytes, _SCHEMA)
    if isinstance(result, ChatGatewayFailure):
        return result

    logger.debug("wardrobe image analysis: raw model response: %s", result.response)
    parsed = _parse_object(result.response, "wardrobe image analysis")
    fields = _garment_fields(parsed, "wardrobe image analysis")

    production_type = fields["clothing_type"]
    verification_type = None
    if production_type is not None:
        verification_type = await _verify_clothing_type(image_bytes)
        if verification_type != production_type:
            fields["clothing_type"] = None

    logger.info(
        "wardrobe image analysis: model parsed name=%r clothing_type=%r color=%r "
        "season=%r style=%r occasion=%r | clothing_type verification=%r | "
        "normalized name=%r clothing_type=%r color=%r season=%r style=%r "
        "occasion=%r model=%r",
        parsed.get("name"), parsed.get("clothing_type"), parsed.get("color"),
        parsed.get("season"), parsed.get("style"), parsed.get("occasion"),
        verification_type,
        fields["name"], fields["clothing_type"], fields["color"], fields["season"],
        fields["style"], fields["occasion"], result.model,
    )
    return WardrobeAiSuggestion(**fields, model=result.model)
