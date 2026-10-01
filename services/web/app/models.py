import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from typing import Literal, Optional, List
from enum import Enum
from datetime import datetime

from app.core.pagination import Page


# --- Enums ---

class ClothingType(str, Enum):
    TOP = "top"
    BOTTOM = "bottom"
    DRESS = "dress"
    OUTERWEAR = "outerwear"
    SHOES = "shoes"
    ACCESSORY = "accessory"

class Season(str, Enum):
    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"
    ALL = "all"

class CatalogSeason(str, Enum):
    """A catalog product's trusted season classification (services/catalog's
    NormalizedProduct.season - see ShopifyStoreConfig.season_collections).

    Distinct from Season above: a *wardrobe* item's season is always a
    real, user-declared value (Season is a required field on
    WardrobeItemCreate), but an external catalog product's season is only
    ever known when the crawler found real, reviewed collection-membership
    evidence for it - UNKNOWN is the honest, expected default for most
    products, never an error case to work around.
    """
    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"
    ALL = "all"
    UNKNOWN = "unknown"

class Occasion(str, Enum):
    CASUAL = "casual"
    WORK = "work"
    FORMAL = "formal"
    SPORT = "sport"
    UNIVERSITY = "university"
    TRAVEL = "travel"

class Style(str, Enum):
    CASUAL = "casual"
    FORMAL = "formal"
    SPORT = "sport"
    STREETWEAR = "streetwear"
    ELEGANT = "elegant"
    MINIMAL = "minimal"

class FitPreference(str, Enum):
    FITTED = "fitted"
    RELAXED = "relaxed"
    OVERSIZED = "oversized"
    REGULAR = "regular"

class CommunicationStyle(str, Enum):
    CONCISE = "concise"
    DETAILED = "detailed"
    FRIENDLY = "friendly"
    FORMAL = "formal"


# --- User models ---

class UserRegister(BaseModel):
    """נתונים שהמשתמש שולח בהרשמה."""
    username: str = Field(min_length=3, max_length=30)
    email: EmailStr
    password: str = Field(min_length=6)

class UserLogin(BaseModel):
    """נתונים שהמשתמש שולח בהתחברות."""
    username: str
    password: str

class UserPublic(BaseModel):
    """מה שמחזירים למשתמש — בלי סיסמה!"""
    username: str
    email: str


# --- Token model ---

class Token(BaseModel):
    """ה-JWT token שמוחזר אחרי התחברות."""
    access_token: str
    token_type: str = "bearer"


# --- Wardrobe item models ---

# color stays the semantic category (free string, matched against the
# static WARDROBE_COLOR_PALETTE in app.js/wardrobe_ai.py for
# recommendation/filtering) - color_hex is a purely optional, display-only
# exact shade from the wardrobe Color picker's own curated swatch grid.
# It is never typed by a user (the picker only ever hands back one of its
# predefined swatch hexes, or the AI suggestion pipeline's own
# category-default lookup - see wardrobe_ai.py), so an unrecognized shape
# is rejected rather than silently dropped or guessed.
_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _normalize_color_hex(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if not _HEX_COLOR_RE.match(stripped):
        raise ValueError("color_hex must be a #RRGGBB hex value")
    return "#" + stripped.lstrip("#").lower()


def _coerce_scalar_to_list(value):
    """A wardrobe item created before style/occasion became multi-value
    lists stored a bare scalar string for each. Reading it back through
    WardrobeItemPublic (inherited from here) must not fail just because
    the field is now a list - a scalar becomes its own one-element list;
    an already-list value (every item created after this change) passes
    through unchanged. Only used for reading - the create/update Form
    parameters in routers/wardrobe.py always send real lists from the
    frontend's multi-select, so this coercion is never needed on write."""
    if value is None or isinstance(value, list):
        return value
    return [value]


def _as_list(value) -> list:
    """Normalizes a style/occasion value that may be a list (a new item),
    a bare scalar (a legacy item, read straight from a raw Mongo document
    rather than through WardrobeItemPublic's own coercion), or None/absent
    into a plain list - used by recommendation-scoring code that reads
    wardrobe documents directly."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _sanitize_legacy_enum_list(value, enum_cls) -> list[str]:
    """Defensively recovers a memory list field that may still contain a
    pre-fix corrupted entry: update_memory_from_feedback used to append an
    entire style/occasion tag list as a single nested element instead of
    unpacking it (see that function's own history/tests). Recursively
    flattens any nested list, keeps only entries that are valid
    `enum_cls` string values, silently drops everything else (wrong type,
    None, unrecognized legacy vocabulary) rather than raising, and
    deduplicates while preserving first-seen order - so already-corrupted
    stored data degrades to "ignore the bad entry" instead of a 500/422,
    and new data is never partially trusted."""
    if not isinstance(value, list):
        return []

    flat: list = []

    def _walk(v):
        if isinstance(v, list):
            for item in v:
                _walk(item)
        else:
            flat.append(v)

    for entry in value:
        _walk(entry)

    seen: set[str] = set()
    result: list[str] = []
    for v in flat:
        if not isinstance(v, str):
            continue
        try:
            member = enum_cls(v)
        except ValueError:
            continue
        if member.value not in seen:
            seen.add(member.value)
            result.append(member.value)
    return result


def _first_or_scalar(value):
    """Reduces a possibly list-valued style/occasion - as read straight
    from a raw Mongo wardrobe document, never a validated model instance -
    to the single value SharedOutfitComponentSnapshot's still-scalar
    style/occasion field expects.

    Scoped to the two remaining snapshot-construction call sites
    (services/recommendation_feedback.py::build_recommendation_feedback_
    components and routers/posts.py::_build_shared_outfit_snapshot):
    SharedOutfitComponentSnapshot deliberately still stores one style and
    one occasion per component, so this just picks the first of a source
    item's now possibly-several values rather than preserving all of them.
    Not a temporary shim - both call sites are permanent parts of
    recommendation feedback and outfit sharing.
    """
    if isinstance(value, list):
        return value[0] if value else None
    return value


class WardrobeItemCreate(BaseModel):
    """נתונים ליצירת פריט לבוש חדש."""
    name: str = Field(min_length=1, max_length=100)
    clothing_type: ClothingType
    color: str
    color_hex: Optional[str] = None
    season: Season
    style: List[Style]
    occasion: List[Occasion]
    image_url: Optional[str] = None
    tags: List[str] = []

    @field_validator("color_hex")
    @classmethod
    def _validate_color_hex(cls, value):
        return _normalize_color_hex(value)

    @field_validator("style", "occasion", mode="before")
    @classmethod
    def _coerce_legacy_scalar(cls, value):
        return _coerce_scalar_to_list(value)

class WardrobeItemPublic(WardrobeItemCreate):
    """פריט לבוש כפי שמוחזר מה-API.

    This model represents *verified wardrobe facts* - every instance
    must be built solely from a single wardrobe document's own fields
    (see services/grounding.py). It must never be constructed by merging
    in data from the memory/preferences collection or from a
    client's free-text claims. extra="forbid" makes any attempt to attach
    an unlisted attribute (e.g. an invented "material" field) fail loudly
    instead of silently succeeding.
    """
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str

class WardrobeItemUpdate(BaseModel):
    """שדות אופציונליים לעדכון פריט קיים."""
    name: Optional[str] = None
    clothing_type: Optional[ClothingType] = None
    color: Optional[str] = None
    color_hex: Optional[str] = None
    season: Optional[Season] = None
    style: Optional[List[Style]] = None
    occasion: Optional[List[Occasion]] = None
    image_url: Optional[str] = None
    tags: Optional[List[str]] = None

    @field_validator("color_hex")
    @classmethod
    def _validate_color_hex(cls, value):
        return _normalize_color_hex(value)


# --- AI-assisted wardrobe item analysis (Analyze Item) ---
#
# The response of POST /wardrobe/analyze-image. Every field is a suggestion
# to prefill the existing wardrobe form, never a fact - nothing here is ever
# written to the database directly; only a user-confirmed POST /wardrobe
# does that. clothing_type/season/style/occasion are validated against the
# same enums as WardrobeItemCreate (see services/wardrobe_ai.py); a value
# the model returned that doesn't match becomes null rather than being
# guessed or coerced into some valid-but-wrong enum member.
#
# The former multi-garment "Analyze Full Outfit" endpoint and its
# OutfitImageAnalysis response model were removed - DetectedGarment now
# exists solely as WardrobeAiSuggestion's base class.

class DetectedGarment(BaseModel):
    """The suggested fields for one garment, as reported by the vision
    model from a single clothing-item photo.

    Every field is Optional: a value the model didn't return, returned
    blank, or returned outside the project's enums becomes null rather
    than being guessed (see services/wardrobe_ai.py). None of this is a
    verified fact - it is a suggestion for the user to confirm, and
    nothing here is ever persisted without that confirmation.
    """
    name: Optional[str] = None
    clothing_type: Optional[ClothingType] = None
    color: Optional[str] = None
    color_hex: Optional[str] = None
    season: Optional[Season] = None
    style: Optional[List[Style]] = None
    occasion: Optional[List[Occasion]] = None

    @field_validator("color_hex")
    @classmethod
    def _validate_color_hex(cls, value):
        return _normalize_color_hex(value)


class WardrobeAiSuggestion(DetectedGarment):
    """AI-suggested wardrobe fields for a single photo - DetectedGarment's
    fields plus which model produced them. The response shape of
    POST /wardrobe/analyze-image.
    """
    model: str


# --- Recommendation models ---

class RecommendationRequest(BaseModel):
    """בקשת המלצת outfit.

    occasion/season/style, when given, are hard constraints: a candidate
    that doesn't match one of them is never selected, regardless of how
    well it scores on personalization/learned preferences. See
    services/recommendation/app/services/rule_based.py::
    passes_requested_filters (and its web-side mirror,
    services/personalization.py::candidate_passes_requested_filters) for
    where that's enforced.
    """
    occasion: Optional[Occasion] = None
    season: Optional[Season] = None
    style: Optional[Style] = None

class OutfitRecommendation(BaseModel):
    """outfit מומלץ — רשימת פריטים."""
    items: List[WardrobeItemPublic]
    explanation: str
    missing_types: List[str] = Field(default_factory=list)


# --- Catalog (crawler) product suggestions ---
#
# Deliberately a separate response type from OutfitRecommendation, returned
# by its own endpoint (GET /recommendations/catalog-suggestions) rather than
# a new field folded into OutfitRecommendation. This keeps the existing,
# already-100%-tested POST /recommendations contract byte-for-byte
# unchanged (see test_recommendations_route.py::
# test_recommendation_response_never_leaks_internal_verification_or_fallback_details,
# which asserts an exact response key set) while still letting the client
# fetch real catalog products for a clothing_type the wardrobe is missing.
#
# Every field here is copied straight from the catalog service's own
# NormalizedProduct (services/catalog/app/models/product.py) via
# services/catalog_gateway.py, which re-validates and drops anything
# malformed before it ever reaches this model - see that module for the
# "never invent, verify or omit" handling. extra="forbid" ensures a
# malformed/unexpected field from the catalog service can never silently
# attach itself to what is presented to the client as a real product.

class CatalogProductPublic(BaseModel):
    """One external clothing product, as shown to the client."""
    model_config = ConfigDict(extra="forbid")

    source: str
    source_url: str
    product_name: str
    clothing_type: ClothingType
    color: Optional[str] = None
    material: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    image_url: Optional[str] = None
    availability: Literal["in_stock", "out_of_stock", "unknown"]
    # Trusted collection-membership classification (see catalog's
    # ShopifyStoreConfig.season_collections) - defaults to UNKNOWN exactly
    # like the catalog service's own NormalizedProduct.season default, for
    # any product the catalog service returned without one (e.g. an older
    # cached response, or a source that doesn't populate it).
    season: CatalogSeason = CatalogSeason.UNKNOWN


class CatalogSuggestionsResponse(BaseModel):
    """Response of GET /recommendations/catalog-suggestions.

    catalog_available distinguishes "the catalog service was reached but had
    no matching products" (True, products=[]) from "the catalog service
    could not be reached/timed out/returned something unusable" (False,
    products=[]) - both degrade to an empty, non-error list, but the client
    can use this to show a different, honest message for each case rather
    than implying "this store has nothing in stock" when the real cause was
    an outage.

    season_requested/season_verified let the client distinguish a genuine
    season-verified match from a clothing-type-only fallback: season_
    verified is True only when `season` was actually requested AND every
    product in `products` has trusted collection evidence confirming it
    (directly, or via the documented spring->summer/autumn->winter mapping
    - see catalog_gateway.py::season_matches). It is False both when no
    season was requested at all (nothing to verify) and when one was
    requested but no verified match existed, in which case `products` falls
    back to the unfiltered clothing-type-only result - never a mix of
    verified and unverified items presented as if all were confirmed.
    """
    clothing_type: ClothingType
    products: List[CatalogProductPublic] = Field(default_factory=list)
    catalog_available: bool
    season_requested: Optional[Season] = None
    season_verified: bool = False


# --- Chat models ---

class ChatRequest(BaseModel):
    """Chat request sent to the local LLM."""
    message: str = Field(min_length=1)

    @field_validator("message")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message cannot be blank")
        return stripped


class ChatResponse(BaseModel):
    """The LLM's response."""
    response: str


class ChatJobStatus(str, Enum):
    """Lifecycle of a queued chat job: PENDING until a worker claims it,
    PROCESSING while it runs, then COMPLETED or FAILED (set by the
    background worker - see services/chat_worker.py)."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ChatJobPublic(BaseModel):
    """A queued chat job as returned by POST/GET /chat/jobs."""
    job_id: str
    status: ChatJobStatus
    result: Optional[str] = None
    error_reason: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


# --- Saved outfit models ---

class SavedOutfitCreate(BaseModel):
    """נתונים לשמירת outfit מתוך פריטים קיימים ב-wardrobe."""
    item_ids: List[str] = Field(min_length=1)
    name: Optional[str] = None
    occasion: Optional[Occasion] = None
    season: Optional[Season] = None
    style: Optional[Style] = None

class SavedOutfitPublic(BaseModel):
    """outfit שמור כפי שמוחזר מה-API."""
    id: str
    user_id: str
    item_ids: List[str]
    items: List[WardrobeItemPublic]
    name: Optional[str] = None
    occasion: Optional[Occasion] = None
    season: Optional[Season] = None
    style: Optional[Style] = None
    created_at: datetime

class SavedOutfitPatch(BaseModel):
    """PATCH /outfits/{id} - שדות אופציונליים לעדכון חלקי.

    A field absent from the request body is left unchanged (see
    exclude_unset at the call site). Unlike other *Patch models in this
    codebase, an explicit null is never treated as "clear this field" -
    every field here is rejected with a 422 if sent as null, exactly like
    an omitted field is a no-op. item_ids, if provided, must be non-empty
    and passes through outfits.py's shared validation exactly like create.
    """
    item_ids: Optional[List[str]] = Field(default=None, min_length=1)
    name: Optional[str] = None
    occasion: Optional[Occasion] = None
    season: Optional[Season] = None
    style: Optional[Style] = None


# --- Outfit feedback models ---

class FeedbackCreate(BaseModel):
    """נתונים ליצירת feedback על outfit שמור."""
    outfit_id: str
    rating: int = Field(ge=1, le=5)
    liked: Optional[bool] = None
    comment: Optional[str] = Field(default=None, max_length=500)

class FeedbackUpdate(BaseModel):
    """שדות אופציונליים לעדכון feedback קיים."""
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    liked: Optional[bool] = None
    comment: Optional[str] = Field(default=None, max_length=500)

class FeedbackPublic(BaseModel):
    """feedback כפי שמוחזר מה-API."""
    id: str
    user_id: str
    outfit_id: str
    rating: int
    liked: Optional[bool] = None
    comment: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# --- Long-term user memory models ---
#
# These fields represent user preferences, opinions, or communication
# style - never verified facts about a wardrobe item or product. They may
# only ever influence recommendation *scoring* (see
# services/personalization.py::apply_memory_preferences). None of these
# fields (including free-text `notes`) may be merged into, or used to
# construct, a WardrobeItemPublic/GroundedWardrobeItem - those are built
# solely from a wardrobe document's own fields.

_MEMORY_MAX_LIST_SIZE = 20
_MEMORY_MAX_COLOR_LEN = 30


def _normalize_color_list(value: Optional[List[str]]) -> Optional[List[str]]:
    if value is None:
        return None
    if len(value) > _MEMORY_MAX_LIST_SIZE:
        raise ValueError(f"List cannot exceed {_MEMORY_MAX_LIST_SIZE} items")
    normalized: List[str] = []
    seen: set[str] = set()
    for raw in value:
        color = raw.strip().lower()
        if not color:
            raise ValueError("Color values cannot be empty")
        if len(color) > _MEMORY_MAX_COLOR_LEN:
            raise ValueError(f"Color value exceeds max length of {_MEMORY_MAX_COLOR_LEN}")
        if color not in seen:
            seen.add(color)
            normalized.append(color)
    return normalized


def _dedupe_preserve_order(value):
    if value is None:
        return None
    if len(value) > _MEMORY_MAX_LIST_SIZE:
        raise ValueError(f"List cannot exceed {_MEMORY_MAX_LIST_SIZE} items")
    seen = set()
    result = []
    for item in value:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _check_color_overlap(liked, disliked) -> None:
    if liked and disliked:
        overlap = set(liked) & set(disliked)
        if overlap:
            raise ValueError(f"Colors cannot be both liked and disliked: {sorted(overlap)}")


# --- Generic durable preferences (long-term-memory follow-up completion) ---
#
# A small, bounded fallback for an important durable preference that
# genuinely doesn't fit any of the structured fields above (e.g. "I prefer
# more covered, modest clothing", "I prefer clothes that are easy to put
# on with one hand") - NOT a dedicated field for any one specific concept,
# and deliberately NOT unrestricted free text like `notes`. Each entry is
# flat (extra="forbid", no nesting), short, length-capped, and
# control-character/whitespace-normalized at write time - safe to later
# re-sanitize and place as DATA in an LLM prompt (see
# services/chat_context.py). `topic` is normalized (lowercased, whitespace-
# collapsed) specifically so a later durable statement about the SAME
# topic can be matched and replace the old value instead of accumulating
# contradictory entries forever (see
# services/personalization.py::merge_chat_memory_candidate).

MEMORY_MAX_GENERIC_PREFERENCES = 10
MEMORY_MAX_TOPIC_LEN = 60
MEMORY_MAX_PREFERENCE_LEN = 200

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1F\x7F]+")
_MEMORY_WHITESPACE_RE = re.compile(r"\s+")


def _sanitize_preference_text(raw, max_len: int, *, lowercase: bool = False) -> str:
    """Strip raw control characters (including newlines/tabs, which would
    otherwise let a stored preference forge fake line breaks), collapse
    any remaining whitespace to single spaces, trim, and cap length.
    Raises ValueError on a non-string, empty-after-sanitizing, or
    over-length input - never silently truncates without complaint, so a
    caller can't accidentally persist a clipped, misleading value."""
    if not isinstance(raw, str):
        raise ValueError("must be a string")
    value = _CONTROL_CHAR_RE.sub(" ", raw)
    value = _MEMORY_WHITESPACE_RE.sub(" ", value).strip()
    if lowercase:
        value = value.lower()
    if not value:
        raise ValueError("cannot be blank")
    if len(value) > max_len:
        raise ValueError(f"exceeds max length of {max_len}")
    return value


class GenericPreference(BaseModel):
    """One short, bounded, topic-keyed durable preference outside the
    structured fields above. `topic` is a short category (e.g. "clothing
    coverage", "fabric", "ease of dressing"), normalized to lowercase so
    repeated statements about the same topic can be matched; `preference`
    is a short, one-sentence description of what the user wants."""
    model_config = ConfigDict(extra="forbid")

    topic: str
    preference: str

    @field_validator("topic")
    @classmethod
    def _validate_topic(cls, value):
        return _sanitize_preference_text(value, MEMORY_MAX_TOPIC_LEN, lowercase=True)

    @field_validator("preference")
    @classmethod
    def _validate_preference(cls, value):
        return _sanitize_preference_text(value, MEMORY_MAX_PREFERENCE_LEN)


def _validate_generic_preferences(value):
    if value is None:
        return None
    if len(value) > MEMORY_MAX_GENERIC_PREFERENCES:
        raise ValueError(f"List cannot exceed {MEMORY_MAX_GENERIC_PREFERENCES} items")
    # Within one write, a later entry for the same (already-normalized)
    # topic wins - mirrors how a single message can't state two different
    # values for the same field either.
    by_topic: dict[str, GenericPreference] = {}
    for item in value:
        by_topic[item.topic] = item
    return list(by_topic.values())


class UserMemoryUpdate(BaseModel):
    """גוף הבקשה ל-PUT /memory - יוצר/מחליף את כל פרופיל הזיכרון."""
    preferred_styles: List[Style] = []
    liked_colors: List[str] = []
    disliked_colors: List[str] = []
    disliked_item_types: List[ClothingType] = []
    preferred_occasions: List[Occasion] = []
    preferred_fit: Optional[FitPreference] = None
    communication_style: Optional[CommunicationStyle] = None
    additional_preferences: List[GenericPreference] = []
    notes: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("liked_colors", "disliked_colors")
    @classmethod
    def _validate_colors(cls, value):
        return _normalize_color_list(value)

    @field_validator("preferred_styles", "disliked_item_types", "preferred_occasions")
    @classmethod
    def _validate_dedupe(cls, value):
        return _dedupe_preserve_order(value)

    @field_validator("additional_preferences")
    @classmethod
    def _validate_additional_preferences(cls, value):
        return _validate_generic_preferences(value)

    def model_post_init(self, __context) -> None:
        _check_color_overlap(self.liked_colors, self.disliked_colors)


class UserMemoryPatch(BaseModel):
    """גוף הבקשה ל-PATCH /memory - עדכון חלקי, רק שדות שנשלחו יתעדכנו."""
    preferred_styles: Optional[List[Style]] = None
    liked_colors: Optional[List[str]] = None
    disliked_colors: Optional[List[str]] = None
    disliked_item_types: Optional[List[ClothingType]] = None
    preferred_occasions: Optional[List[Occasion]] = None
    preferred_fit: Optional[FitPreference] = None
    communication_style: Optional[CommunicationStyle] = None
    additional_preferences: Optional[List[GenericPreference]] = None
    notes: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("liked_colors", "disliked_colors")
    @classmethod
    def _validate_colors(cls, value):
        return _normalize_color_list(value)

    @field_validator("preferred_styles", "disliked_item_types", "preferred_occasions")
    @classmethod
    def _validate_dedupe(cls, value):
        return _dedupe_preserve_order(value)

    @field_validator("additional_preferences")
    @classmethod
    def _validate_additional_preferences(cls, value):
        return _validate_generic_preferences(value)

    def model_post_init(self, __context) -> None:
        _check_color_overlap(self.liked_colors, self.disliked_colors)


class UserMemoryPublic(BaseModel):
    """פרופיל הזיכרון של המשתמש כפי שמוחזר מה-API."""
    preferred_styles: List[Style] = []
    liked_colors: List[str] = []
    disliked_colors: List[str] = []
    disliked_item_types: List[ClothingType] = []
    preferred_occasions: List[Occasion] = []
    preferred_fit: Optional[FitPreference] = None
    communication_style: Optional[CommunicationStyle] = None
    additional_preferences: List[GenericPreference] = []
    notes: Optional[str] = None
    updated_at: Optional[datetime] = None


# --- Chat-driven memory learning models ---
#
# These models describe the *shape* of what the LLM is allowed to propose as
# durable, learnable preferences extracted from a chat message. They hold
# extracted values only - no merge/write semantics (e.g. "add X", "should
# this overwrite Y") and no persistence, user-identity, or timestamp fields.
# Reuses the exact same normalization/dedup/overlap validators as
# UserMemoryUpdate/UserMemoryPatch so a chat-learned value is validated
# identically to one a user sets manually via PUT/PATCH /memory.
# extra="forbid" ensures the LLM's raw output can never smuggle in fields
# this model doesn't expect (e.g. user_id, notes, wardrobe facts).

class ChatMemoryCandidate(BaseModel):
    """Durable user preferences extracted from a single chat message."""
    model_config = ConfigDict(extra="forbid")

    preferred_styles: List[Style] = []
    liked_colors: List[str] = []
    disliked_colors: List[str] = []
    disliked_item_types: List[ClothingType] = []
    preferred_occasions: List[Occasion] = []
    preferred_fit: Optional[FitPreference] = None
    communication_style: Optional[CommunicationStyle] = None
    additional_preferences: List[GenericPreference] = []

    @field_validator("liked_colors", "disliked_colors")
    @classmethod
    def _validate_colors(cls, value):
        return _normalize_color_list(value)

    @field_validator("preferred_styles", "disliked_item_types", "preferred_occasions")
    @classmethod
    def _validate_dedupe(cls, value):
        return _dedupe_preserve_order(value)

    @field_validator("additional_preferences")
    @classmethod
    def _validate_additional_preferences(cls, value):
        return _validate_generic_preferences(value)

    def model_post_init(self, __context) -> None:
        _check_color_overlap(self.liked_colors, self.disliked_colors)


class ChatMemoryExtractionResult(BaseModel):
    """Raw structured output expected from the memory-extraction LLM call.

    Business rules for what happens when should_save is False, and how a
    candidate's fields get merged into a user's persisted memory, belong to
    later backend code - this model only validates shape.
    """
    model_config = ConfigDict(extra="forbid")

    should_save: bool
    candidate: ChatMemoryCandidate
    confidence: float = Field(ge=0.0, le=1.0)


# --- Forum: attachments ---
#
# Resource-agnostic on purpose - the same model is embedded in PostPublic,
# CommentPublic, and DirectMessagePublic rather than each defining its own
# shape. `media_type` accepts "video" (MP4 only - see
# services/image_storage.py's MP4 structural validation) alongside the
# original "image". This was a one-line, additive widening - no
# migration was needed, since every document already stored
# says "image" literally and remains valid under the wider Literal.

class AttachmentPublic(BaseModel):
    """A single image or MP4 video attached to a post, comment, or message."""
    url: str
    media_type: Literal["image", "video"]
    content_type: str
    size_bytes: int


# --- Forum: posts ---
#
# Posts are immutable after creation - there is no update endpoint, only
# author-owned delete (see routers/posts.py). `is_anonymous` hides the
# author from every public response, including the author's own
# GET /posts/mine view, but the real `author_id` is always stored on the
# document and always used for the internal ownership check on delete -
# only the *response* ever omits it, never the database or the query used
# to authorize an action.
#
# likes_count/dislikes_count/comments_count are never stored on the post
# document - all three are derived at read time (likes/dislikes from the
# reactions collection, comments_count from the comments collection) and
# only ever appear as computed response fields. See services/reactions.py
# and services/comments.py for why: this MongoDB deployment runs as a
# standalone instance with no replica set, so a denormalized counter next
# to its source-of-truth collection would create a permanent
# crash-consistency gap with no transaction available to keep them in sync.
#
# There is no PostCreate model: POST /posts is multipart (title/body/
# is_anonymous as Form fields, images as File uploads, see routers/posts.py)
# so the request body isn't a single JSON-bindable Pydantic model.

class PostPublic(BaseModel):
    """A post as returned by the API.

    likes_count/dislikes_count/my_reaction are always derived from the
    reactions collection at request time (see services/reactions.py) - never
    stored on the post document itself. `my_reaction` is the requesting
    user's own reaction to this post (1, -1, or None), needed so the
    frontend can render the Like/Dislike button as already-active on load.
    """
    id: str
    author_id: Optional[str] = None
    author_username: Optional[str] = None
    is_anonymous: bool
    title: str
    body: str
    attachments: List[AttachmentPublic] = Field(default_factory=list)
    shared_outfit: Optional["SharedOutfitSnapshot"] = None
    likes_count: int = 0
    dislikes_count: int = 0
    comments_count: int = 0
    my_reaction: Optional[int] = None
    created_at: datetime


class MyPostsPage(Page[PostPublic]):
    """GET /posts/mine - a page of the caller's own posts, plus aggregate
    likes/dislikes received across every post they've ever authored (not
    just the current page)."""
    total_likes_received: int
    total_dislikes_received: int


# --- Forum: reactions ---
#
# One document per (user_id, target_type, target_id) - never a value=0 row.
# No reaction means no document; clicking the currently-active reaction
# again deletes the document outright (see services/reactions.py). Counts
# are always derived from these documents at read time, never denormalized
# onto the target (post/comment) document: this MongoDB deployment runs as
# a standalone instance with no replica set, so there is no multi-document
# transaction available to keep a separate counter and this collection in
# sync - a denormalized counter would create a permanent, unrepairable
# crash-consistency gap instead of the reaction documents remaining the
# single source of truth.

class ReactionRequest(BaseModel):
    """Request body for POST /posts/{id}/reactions (and, later, comment reactions)."""
    value: Literal[1, -1]


class ReactionPublic(BaseModel):
    """The caller's resulting reaction state and the target's current totals."""
    my_reaction: Optional[int] = None
    likes_count: int
    dislikes_count: int


# --- Forum: comments ---
#
# Flat only - no replies/nested threads. Immutable after creation, like
# posts - only author-owned delete exists, no update endpoint. Comments are
# never anonymous, unlike posts, so author_id/author_username are always
# populated here (nothing to hide). likes_count/dislikes_count/my_reaction
# follow the exact same derived-from-reactions convention as PostPublic,
# via the same services/reactions.py with target_type="comment".
#
# There is no CommentCreate model, for the same reason there's no
# PostCreate: POST /posts/{id}/comments is multipart (body as a Form field,
# images as File uploads, see routers/posts.py).

class CommentPublic(BaseModel):
    """A comment as returned by the API."""
    id: str
    post_id: str
    author_id: str
    author_username: str
    body: str
    attachments: List[AttachmentPublic] = Field(default_factory=list)
    likes_count: int = 0
    dislikes_count: int = 0
    my_reaction: Optional[int] = None
    created_at: datetime


# --- Direct messages ---
#
# One-to-one only. Messages are immutable - no edit/delete endpoints exist.
# A conversation is never created as its own user-facing action; it's
# get-or-created the first time two users exchange a message (see
# services/conversations.py). There is no DirectMessageCreate model:
# POST /messages/{other_user_id} is multipart (body as a Form field, images
# as File uploads, see routers/messages.py) for the same reason posts and
# comments have no *Create model - the request body isn't a single
# JSON-bindable Pydantic model.
#
# Attachment URLs point at an authenticated, participant-scoped retrieval
# route (GET /messages/conversations/{id}/attachments/{filename}), never a
# public static path - unlike Explore (post/comment) attachments, DM
# attachments are visible only to the conversation's two participants.

class DirectMessagePublic(BaseModel):
    """A message as returned by the API."""
    id: str
    conversation_id: str
    sender_id: str
    body: str
    attachments: List[AttachmentPublic] = Field(default_factory=list)
    created_at: datetime


class ConversationPublic(BaseModel):
    """One row of the caller's inbox (GET /messages/conversations).

    Deliberately minimal - no last-message preview, unread count, or read
    receipts; those are out of scope for this feature.
    """
    conversation_id: str
    other_user_id: str
    other_username: str
    last_message_at: datetime


# --- User discovery for DM initiation ---
#
# Exists solely so a client can start a *new* conversation with someone
# they have no other way to find (no shared post, no existing thread) -
# not a profile/social-graph feature. Deliberately the narrowest possible
# shape: only the two fields actually needed to pick a DM recipient and
# call the existing POST /messages/{other_user_id}. No email, no password
# hash, no memory/preferences, no wardrobe data, and no relation
# whatsoever to anonymous-post identity (see routers/users.py).
class UserSummaryPublic(BaseModel):
    """One row of the user-discovery list (GET /users)."""
    id: str
    username: str


# --- Notifications ---
#
# Recipient-scoped and private - every endpoint filters by recipient_id, so
# a notification is only ever visible to the one person it's for. For an
# anonymous post, recipient_id is the real author_id (never null) - the
# same "retain internally, hide publicly" rule as everywhere else - but
# since nothing here is ever exposed to anyone but that recipient, there is
# no leak. actor_id/actor_username are always the real acting user - only
# post authorship can be anonymous, reactions and messages never are.
#
# target_type is derivable from type (e.g. "like_post" always targets a
# post) but stored anyway so the frontend doesn't need a
# type -> target_type lookup table. Triggers are deliberately narrow:
# new direct messages, and likes/dislikes on posts and comments - new
# comments are deliberately not a trigger here.

class NotificationPublic(BaseModel):
    """A notification as returned by the API."""
    id: str
    type: Literal["message", "like_post", "dislike_post", "like_comment", "dislike_comment"]
    actor_id: str
    actor_username: str
    target_type: Literal["post", "comment", "message"]
    target_id: str
    read: bool
    created_at: datetime


class UnreadCountPublic(BaseModel):
    count: int


class MarkAllReadResult(BaseModel):
    updated_count: int


# --- Forum: Recreate with My Wardrobe ---
#
# A post's author may optionally attach one of their own saved outfits when
# creating a post. What gets stored on the post is never a reference to the
# outfit or its wardrobe items (those can be edited/deleted later, see
# routers/wardrobe.py's cascade-delete logic) - it's a one-time, immutable,
# sanitized copy: SharedOutfitSnapshot. Deleting the source outfit or any of
# its items afterward has no effect on the post or on Recreate, because
# nothing here is ever dereferenced back to the original documents.
#
# The snapshot deliberately excludes every identifier and free-text field
# (wardrobe item id, outfit id, item name, image_url, tags) - only the five
# categorical attributes needed for matching are kept, which is also exactly
# what's safe to expose to any other authenticated user via GET
# /posts/{id}/recreate.

class SharedOutfitComponentSnapshot(BaseModel):
    """One wardrobe item's categorical attributes, copied at post-creation
    time. No id, name, image, or tags - see module note above."""
    model_config = ConfigDict(extra="forbid")

    clothing_type: ClothingType
    color: str
    season: Season
    style: Style
    occasion: Occasion


class SharedOutfitSnapshot(BaseModel):
    """An immutable, sanitized copy of a saved outfit, embedded directly on
    the post that shared it."""
    model_config = ConfigDict(extra="forbid")

    outfit_name: Optional[str] = None
    outfit_occasion: Optional[Occasion] = None
    outfit_season: Optional[Season] = None
    outfit_style: Optional[Style] = None
    components: List[SharedOutfitComponentSnapshot] = Field(min_length=1)


class RecreateMatchResult(BaseModel):
    """One snapshot component matched against the requesting user's own
    wardrobe. matched_item is None when the caller's wardrobe has no item
    of the required clothing_type at all - never a fabricated placeholder."""
    model_config = ConfigDict(extra="forbid")

    component: SharedOutfitComponentSnapshot
    matched_item: Optional[WardrobeItemPublic] = None
    match_score: Optional[float] = None
    match_explanation: str


class RecreateResponse(BaseModel):
    post_id: str
    matches: List[RecreateMatchResult]


# The former AI-derived "Recreate from a post photo" path (
# PhotoRecreateRequest/PhotoRecreateResponse) was removed - recreation is
# only ever offered for a post that has structured shared_outfit data
# (RecreateResponse above), never derived from an arbitrary photo.


# --- Recommendation-level like/dislike feedback ---
#
# Feedback on a *fresh, unsaved* recommendation - independent of the
# outfit-feedback flow above, which requires the outfit to already exist
# in db.outfits. Stored as a frozen categorical snapshot, mirroring
# SharedOutfitSnapshot's own reasoning above: db.recommendation_feedback
# never references a live wardrobe item id, so later analysis (see
# services/recommendation_feedback_learning.py) never depends on items
# that may since have been edited or deleted.

class RecommendationFeedbackReasonScope(str, Enum):
    COMBINATION = "combination"
    COLOR = "color"
    STYLE = "style"
    CLOTHING_TYPE = "clothing_type"
    OCCASION_SEASON_MISMATCH = "occasion_season_mismatch"


_ITEM_SCOPED_REASONS = {
    RecommendationFeedbackReasonScope.COLOR,
    RecommendationFeedbackReasonScope.STYLE,
    RecommendationFeedbackReasonScope.CLOTHING_TYPE,
}


class RecommendationFeedbackCreate(BaseModel):
    """נתונים ליצירת feedback על המלצה שטרם נשמרה.

    reason_item_id must be present, and a member of item_ids, exactly
    when reason_scope names a single-item attribute (color/style/
    clothing_type) - combination and occasion_season_mismatch describe
    the recommendation as a whole and must not carry one.
    """
    model_config = ConfigDict(extra="forbid")

    item_ids: List[str] = Field(min_length=1)
    liked: bool
    reason_scope: Optional[RecommendationFeedbackReasonScope] = None
    reason_item_id: Optional[str] = None
    occasion: Optional[Occasion] = None
    season: Optional[Season] = None
    style: Optional[Style] = None
    comment: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _validate_reason_item_id(self):
        needs_item = self.reason_scope in _ITEM_SCOPED_REASONS
        if needs_item and self.reason_item_id is None:
            raise ValueError(f"reason_item_id is required when reason_scope is {self.reason_scope.value!r}")
        if not needs_item and self.reason_item_id is not None:
            raise ValueError("reason_item_id must be omitted unless reason_scope is color, style, or clothing_type")
        if self.reason_item_id is not None and self.reason_item_id not in self.item_ids:
            raise ValueError("reason_item_id must be one of item_ids")
        return self


class RecommendationFeedbackPublic(BaseModel):
    """recommendation feedback כפי שמוחזר מה-API."""
    model_config = ConfigDict(extra="forbid")

    id: str
    liked: bool
    components: List[SharedOutfitComponentSnapshot]
    reason_scope: Optional[RecommendationFeedbackReasonScope] = None
    reason_component_index: Optional[int] = None
    occasion: Optional[Occasion] = None
    season: Optional[Season] = None
    style: Optional[Style] = None
    comment: Optional[str] = None
    created_at: datetime
