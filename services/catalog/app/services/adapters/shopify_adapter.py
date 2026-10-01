"""Generic Shopify storefront adapter.

One configurable adapter (see shopify_stores.py::ShopifyStoreConfig) rather
than one duplicated class per store. Implements the SourceAdapter protocol
(app/services/adapters/base.py): fetches raw products from a real Shopify
store's public products.json endpoint, normalizes them into
NormalizedProduct, and returns them. It never retries, never tracks
concurrency, and never returns CrawlResponse/SourceStatus - that remains the
Crawler's job (app/services/crawler.py). A raised exception here is caught
and classified by app/services/retry.py; this module never catches a
whole-response failure and turns it into a silently-empty success.

The network behavior was informed by an earlier standalone
proof-of-concept crawler (not part of this repository) - the same public
endpoints, availability filtering, lowest-price selection, image
extraction, and product-URL construction - reworked onto the shared async
httpx.AsyncClient and the crawler's retry/SourceStatus architecture
instead of synchronous urllib calls.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx
from pydantic import HttpUrl
from pydantic import ValidationError as PydanticValidationError

from app.models.product import ClothingType, NormalizedProduct, SeasonLabel
from app.services.adapters.shopify_stores import ShopifyStoreConfig

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "SmartWardrobe-Catalog/1.0",
}
_PRODUCTS_LIMIT = 30
_CURRENCY_PATTERN = re.compile(r"[A-Za-z]{3}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --- variant / price helpers -------------------------------------------------


def available_variants(product: dict[str, Any]) -> list[dict[str, Any]]:
    """Only variants explicitly marked available; anything else is ignored."""
    variants = product.get("variants")
    if not isinstance(variants, list):
        return []
    return [
        variant
        for variant in variants
        if isinstance(variant, dict) and variant.get("available") is True
    ]


def lowest_price(variants: list[dict[str, Any]]) -> Optional[Decimal]:
    """The lowest valid, non-negative, finite Decimal price among variants.

    Missing prices, values Decimal can't parse, negative prices, and NaN/
    infinite prices are all ignored rather than raised - one bad variant
    price must not fail an otherwise-valid product.
    """
    prices: list[Decimal] = []
    for variant in variants:
        raw_price = variant.get("price")
        if raw_price is None:
            continue
        try:
            price = Decimal(str(raw_price))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if not price.is_finite() or price < 0:
            continue
        prices.append(price)
    return min(prices) if prices else None


# --- image helper -------------------------------------------------------------


def first_image(product: dict[str, Any]) -> Optional[str]:
    """The first usable image URL string, or None.

    Supports Shopify's {"src": "..."} image objects and direct string
    values; anything else (missing images, empty list, unrecognized shape)
    yields None rather than raising - a missing image is always allowed.
    """
    images = product.get("images")
    if not isinstance(images, list) or not images:
        return None
    first = images[0]
    if isinstance(first, dict):
        src = first.get("src")
        return src if isinstance(src, str) and src.strip() else None
    if isinstance(first, str) and first.strip():
        return first
    return None


def safe_image_url(image_src: Optional[str]) -> Optional[str]:
    """Validate an image URL string, dropping it (never the whole product)
    if it turns out not to be a usable absolute URL.
    """
    if not image_src:
        return None
    try:
        HttpUrl(image_src)
    except Exception:
        return None
    return image_src


# --- URL / id helpers ----------------------------------------------------------


def build_product_url(base_url: str, handle: str) -> str:
    """The real store product URL, safely encoding the (often Hebrew) handle."""
    encoded_handle = quote(str(handle), safe="-")
    return f"{base_url}/products/{encoded_handle}"


def select_source_product_id(product: dict[str, Any]) -> Optional[str]:
    """Prefer the Shopify product id; fall back to the handle."""
    product_id = product.get("id")
    if product_id is not None and str(product_id).strip():
        return str(product_id)
    handle = product.get("handle")
    if isinstance(handle, str) and handle.strip():
        return handle.strip()
    return None


# --- currency helper -------------------------------------------------------------


def select_currency(cart_currency: Optional[str], default_currency: str) -> str:
    """A valid three-letter cart.js currency code, else the configured default."""
    if isinstance(cart_currency, str) and _CURRENCY_PATTERN.fullmatch(cart_currency):
        return cart_currency.upper()
    return default_currency


# --- category normalization ----------------------------------------------------
#
# Deterministic keyword matching over the product title (checked first,
# takes priority) and the Shopify product_type (fallback only). No
# stemming/morphology - direct normalized keyword matching, as specified.
# A title with zero recognized keywords falls back to product_type; a title
# with more than one *distinct* matched category is ambiguous and the
# product is skipped rather than guessed into a category.

_CATEGORY_KEYWORDS: dict[ClothingType, tuple[str, ...]] = {
    ClothingType.TOP: (
        "top", "tops", "shirt", "shirts", "t-shirt", "t shirt", "blouse",
        "tank top", "tank tops", "bikini top", "bra top",
        "חולצה", "חולצות", "גופייה", "גופיות", "גופיה",
        "טופ", "חלק עליון", "גוזייה", "גוזיה", "גוזיות",
    ),
    ClothingType.BOTTOM: (
        "pants", "trousers", "jeans", "skirt", "skirts", "shorts",
        "leggings", "tights", "bikini bottom",
        "מכנס", "מכנסיים", "מכנסי", "מכנסון", "מכנסונים",
        "חצאית", "חצאיות", "טייץ", "טייטס", "חלק תחתון", "תחתון ביקיני",
    ),
    ClothingType.DRESS: (
        "dress", "dresses",
        "one-piece swimsuit", "one piece swimsuit", "one-piece swimwear",
        "שמלה", "שמלות", "בגד ים שלם",
    ),
    ClothingType.OUTERWEAR: (
        "jacket", "jackets", "coat", "coats", "blazer", "blazers",
        "cardigan", "cardigans", "outerwear",
        "ז׳קט", "ז'קט", "ג׳קט", "ג'קט", "מעיל", "מעילים",
        "עליונית", "עליוניות",
    ),
    ClothingType.SHOES: (
        "shoe", "shoes", "sneaker", "sneakers", "boot", "boots",
        "sandal", "sandals",
        "נעל", "נעליים", "סניקרס", "מגף", "מגפיים", "סנדל", "סנדלים",
    ),
    ClothingType.ACCESSORY: (
        "accessory", "accessories", "bag", "bags", "hat", "hats",
        "belt", "belts", "scarf", "scarves", "sarong",
        "אביזר", "אביזרים", "תיק", "תיקים", "כובע", "כובעים",
        "חגורה", "חגורות", "צעיף", "צעיפים", "פריאו",
    ),
}


def _normalize_text(text: str) -> str:
    """Lowercase, fold apostrophe/hyphen variants, collapse whitespace.

    Applied identically to both the text being searched and the keywords
    below, so matching stays purely literal/case-insensitive with no
    Hebrew morphology involved.
    """
    normalized = text.strip().lower()
    normalized = (
        normalized.replace("’", "'")  # right single quote
        .replace("‘", "'")  # left single quote
        .replace("׳", "'")  # Hebrew geresh ׳
    )
    normalized = re.sub(r"[-‐‑‒–—―]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _compile_keyword_pattern(keyword: str) -> re.Pattern[str]:
    normalized_keyword = _normalize_text(keyword)
    escaped = re.escape(normalized_keyword).replace(r"\ ", r"\s+")
    return re.compile(rf"\b{escaped}\b", re.UNICODE)


_CATEGORY_PATTERNS: dict[ClothingType, tuple[re.Pattern[str], ...]] = {
    category: tuple(_compile_keyword_pattern(keyword) for keyword in keywords)
    for category, keywords in _CATEGORY_KEYWORDS.items()
}


def _matched_categories(raw_text: Optional[str]) -> set[ClothingType]:
    if not raw_text:
        return set()
    normalized = _normalize_text(raw_text)
    if not normalized:
        return set()
    return {
        category
        for category, patterns in _CATEGORY_PATTERNS.items()
        if any(pattern.search(normalized) for pattern in patterns)
    }


def classify_clothing_type(
    product_type: Optional[str], title: Optional[str]
) -> Optional[ClothingType]:
    """The product's ClothingType, or None if it can't be safely determined.

    Title matches take priority over product_type (real-data example: a
    title containing חצאית must become BOTTOM even when product_type
    incorrectly says "shirts"). product_type is consulted only when the
    title itself yields no recognized category. A title matching more than
    one distinct category is genuinely ambiguous and is not forced into
    either one.
    """
    title_matches = _matched_categories(title)
    if len(title_matches) == 1:
        return next(iter(title_matches))
    if len(title_matches) > 1:
        return None

    type_matches = _matched_categories(product_type)
    if len(type_matches) == 1:
        return next(iter(type_matches))
    return None


class ShopifyAdapter:
    """SourceAdapter for one real Shopify storefront.

    Reuses the shared httpx.AsyncClient passed into fetch() by the Crawler -
    never opens its own client/connection. Raises on a whole-response
    failure (timeout, connection error, HTTP status, malformed JSON,
    invalid top-level structure) so the Crawler's retry/SourceStatus
    machinery can classify and handle it; a single malformed *individual*
    product is skipped internally and never raised.
    """

    def __init__(
        self,
        config: ShopifyStoreConfig,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.name = config.source_name
        self._config = config
        self._now = now

    async def _fetch_currency(self, client: httpx.AsyncClient) -> str:
        """Best-effort cart.js currency lookup - optional support data.

        Any failure (network, HTTP status, malformed JSON, missing/invalid
        currency) falls back to the configured default currency; it must
        never fail the products.json request that follows.
        """
        try:
            response = await client.get(
                f"{self._config.base_url}/cart.js", headers=_HEADERS
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            return self._config.default_currency
        if not isinstance(data, dict):
            return self._config.default_currency
        return select_currency(data.get("currency"), self._config.default_currency)

    async def _fetch_collection_product_ids(
        self, client: httpx.AsyncClient, handle: str
    ) -> set[str]:
        """Product ids (Shopify's raw numeric `id`, as a string) belonging
        to one trusted collection - never raises. A single collection
        being unreachable/malformed only means this store has no season
        evidence from that specific collection this crawl; it must never
        fail the whole store's fetch() the way a products.json failure
        does, since season is enrichment, not the primary crawl result.
        """
        encoded_handle = quote(handle, safe="")
        url = f"{self._config.base_url}/collections/{encoded_handle}/products.json"
        try:
            response = await client.get(url, params={"limit": _PRODUCTS_LIMIT * 8}, headers=_HEADERS)
            response.raise_for_status()
            data = response.json()
        except Exception:
            return set()
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            return set()
        ids: set[str] = set()
        for entry in data["products"]:
            if isinstance(entry, dict) and entry.get("id") is not None:
                ids.add(str(entry["id"]))
        return ids

    async def _fetch_season_by_id(self, client: httpx.AsyncClient) -> dict[str, SeasonLabel]:
        """Best-effort {product_id: season} map built from this store's own
        reviewed, trusted season_collections config (shopify_stores.py) -
        optional enrichment data, never required for a successful crawl.

        A product id found in more than one season's trusted collections is
        genuinely ambiguous (a curation mistake, or two seasons legitimately
        overlapping) and is reported as "unknown" rather than picking one
        arbitrarily - presenting a confident but possibly-wrong season is
        worse than admitting uncertainty. A store with no configured
        season_collections at all (e.g. PIPA_FASHION, ATA - see
        shopify_stores.py) makes zero extra requests and every one of its
        products is "unknown" by construction.
        """
        season_collections = self._config.season_collections
        if not season_collections:
            return {}

        seasons = list(season_collections.keys())
        handle_lists = [season_collections[season] for season in seasons]
        fetches = [
            self._fetch_collection_product_ids(client, handle)
            for handles in handle_lists
            for handle in handles
        ]
        # Flatten (season, handle) pairs in the same order as `fetches` so
        # each result can be attributed back to its season below.
        season_per_fetch = [
            season for season, handles in zip(seasons, handle_lists) for _ in handles
        ]
        results = await asyncio.gather(*fetches, return_exceptions=False)

        ids_by_season: dict[str, set[str]] = {}
        for season, ids in zip(season_per_fetch, results):
            ids_by_season.setdefault(season, set()).update(ids)

        season_by_id: dict[str, SeasonLabel] = {}
        seasons_by_id: dict[str, set[str]] = {}
        for season, ids in ids_by_season.items():
            for product_id in ids:
                seasons_by_id.setdefault(product_id, set()).add(season)
        for product_id, matched_seasons in seasons_by_id.items():
            if len(matched_seasons) == 1:
                season_by_id[product_id] = next(iter(matched_seasons))  # type: ignore[assignment]
            # else: ambiguous (appears in more than one trusted season
            # collection) - left out of the map, defaulting to "unknown"
            # in _parse_product below.
        return season_by_id

    def _parse_product(
        self, raw: Any, currency: str, season_by_id: dict[str, SeasonLabel]
    ) -> Optional[NormalizedProduct]:
        """One product -> NormalizedProduct, or None if a required value is
        missing/invalid. Never raises for a malformed individual product -
        callers rely on that to skip it without losing the rest.
        """
        if not isinstance(raw, dict):
            return None

        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        title = title.strip()

        handle = raw.get("handle")
        if not isinstance(handle, str) or not handle.strip():
            return None
        handle = handle.strip()

        variants = available_variants(raw)
        if not variants:
            return None

        price = lowest_price(variants)
        if price is None:
            return None

        clothing_type = classify_clothing_type(raw.get("product_type"), title)
        if clothing_type is None:
            return None

        source_product_id = select_source_product_id(raw) or handle
        product_url = build_product_url(self._config.base_url, handle)
        image_url = safe_image_url(first_image(raw))

        # Keyed by Shopify's raw numeric `id` specifically (not
        # source_product_id, which may already have fallen back to
        # `handle` above) - that's the same field _fetch_season_by_id
        # reads from each season collection's own product listing, so the
        # two only ever agree when both come from the real numeric id.
        raw_id = raw.get("id")
        season: SeasonLabel = season_by_id.get(str(raw_id), "unknown") if raw_id is not None else "unknown"

        try:
            return NormalizedProduct(
                source=self._config.source_name,
                source_url=product_url,
                source_product_id=source_product_id,
                product_name=title,
                clothing_type=clothing_type,
                price=price,
                currency=currency,
                image_url=image_url,
                availability="in_stock",
                season=season,
                fetched_at=self._now(),
            )
        except PydanticValidationError:
            return None

    async def fetch(
        self,
        clothing_type: ClothingType,
        max_results: int,
        client: httpx.AsyncClient,
    ) -> list[NormalizedProduct]:
        # Currency and season-collection membership are both optional,
        # best-effort enrichment (never raise - see their own docstrings),
        # so they're safe to fetch concurrently with each other. The
        # required products.json fetch below is deliberately kept separate
        # and sequential, since only its own failure should raise for the
        # crawler's retry/SourceStatus machinery to classify.
        currency, season_by_id = await asyncio.gather(
            self._fetch_currency(client), self._fetch_season_by_id(client)
        )

        response = await client.get(
            f"{self._config.base_url}/products.json",
            params={"limit": _PRODUCTS_LIMIT},
            headers=_HEADERS,
        )
        # Lets the existing retry system classify timeouts/429/5xx/ordinary
        # 4xx via classify_exception() in app/services/retry.py.
        response.raise_for_status()

        # response.json() raises a ValueError (json.JSONDecodeError) on
        # malformed JSON, which classify_exception() already maps to
        # PARSE_ERROR and never retries - no retry.py change needed.
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            raise ValueError("invalid shopify products.json response structure")

        normalized: list[NormalizedProduct] = []
        for raw_product in data["products"]:
            try:
                product = self._parse_product(raw_product, currency, season_by_id)
            except Exception:
                # One malformed product must never break the others.
                continue
            if product is None:
                continue
            if product.clothing_type != clothing_type:
                continue
            normalized.append(product)

        return normalized[:max_results]