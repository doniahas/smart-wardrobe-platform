"""Phase-1 catalog data models.

These models are defined and validated in isolation. They are not yet
wired to any route (no /crawl or /sources endpoint exists in this phase).

The clothing-type values are mirrored locally from
services/web/app/models.py::ClothingType rather than imported, since
catalog is a separate Docker service and must not import across service
boundaries.
"""

import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator

_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}")


class ClothingType(str, Enum):
    """Mirrors services/web/app/models.py::ClothingType."""

    TOP = "top"
    BOTTOM = "bottom"
    DRESS = "dress"
    OUTERWEAR = "outerwear"
    SHOES = "shoes"
    ACCESSORY = "accessory"


def _require_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError("must be timezone-aware")
    return value


def _reject_blank_if_present(value: Optional[str]) -> Optional[str]:
    if value is not None and not value.strip():
        raise ValueError("must not be empty or whitespace-only if provided")
    return value


SeasonLabel = Literal["winter", "summer", "spring", "autumn", "all", "unknown"]


class NormalizedProduct(BaseModel):
    source: str = Field(min_length=1)
    source_url: HttpUrl
    source_product_id: Optional[str] = None
    product_name: str = Field(min_length=1)
    clothing_type: ClothingType
    color: Optional[str] = None
    material: Optional[str] = None
    price: Optional[Decimal] = Field(default=None, ge=0)
    currency: Optional[str] = None
    image_url: Optional[HttpUrl] = None
    availability: Literal["in_stock", "out_of_stock", "unknown"]
    # Trusted-only: set exclusively from real store collection membership
    # (see ShopifyStoreConfig.season_collections / ShopifyAdapter) - never
    # inferred from title/tag/description keywords. "unknown" is the
    # default and the honest answer whenever a store has no reviewed,
    # trusted season collection for this product - never guessed.
    season: SeasonLabel = "unknown"
    fetched_at: datetime

    @field_validator("source", "product_name")
    @classmethod
    def _strip_and_require_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped

    @field_validator("source_product_id", "color", "material")
    @classmethod
    def _validate_optional_strings(cls, value: Optional[str]) -> Optional[str]:
        return _reject_blank_if_present(value)

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not _CURRENCY_PATTERN.fullmatch(value):
            raise ValueError("currency must be an uppercase three-letter code, e.g. USD")
        return value

    @field_validator("fetched_at")
    @classmethod
    def _validate_fetched_at(cls, value: datetime) -> datetime:
        return _require_timezone_aware(value)


class SourceStatus(BaseModel):
    source: str = Field(min_length=1)
    ok: bool
    last_checked: datetime
    consecutive_failures: int = Field(ge=0)
    detail: Optional[str] = None

    @field_validator("source")
    @classmethod
    def _strip_and_require_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped

    @field_validator("detail")
    @classmethod
    def _validate_detail(cls, value: Optional[str]) -> Optional[str]:
        return _reject_blank_if_present(value)

    @field_validator("last_checked")
    @classmethod
    def _validate_last_checked(cls, value: datetime) -> datetime:
        return _require_timezone_aware(value)


class CrawlRequest(BaseModel):
    clothing_type: ClothingType
    max_results: int = Field(default=20, ge=1, le=100)
    sources: Optional[list[str]] = None

    @field_validator("sources")
    @classmethod
    def _validate_sources(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return value
        for entry in value:
            if not entry.strip():
                raise ValueError("source entries must not be empty or whitespace-only")
        return value


class CrawlResponse(BaseModel):
    products: list[NormalizedProduct]
    source_status: list[SourceStatus]


class SourceInfo(BaseModel):
    source: str = Field(min_length=1)
    last_status: Optional[SourceStatus] = None

    @field_validator("source")
    @classmethod
    def _strip_and_require_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class SourcesResponse(BaseModel):
    sources: list[SourceInfo]