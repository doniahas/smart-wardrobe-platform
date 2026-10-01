"""Configuration for the supported Israeli Shopify stores.

Each store is a plain, immutable configuration object - no behavior, no
network calls. app/services/adapters/shopify_adapter.py::ShopifyAdapter
takes one of these and does the actual fetching/parsing; app/main.py wires
one adapter instance per store into the Crawler at startup.

These four stores are a deliberately curated allowlist, carried over from
an earlier proof-of-concept crawler. Adding, removing, or renaming a store
is a reviewed change: each store's configuration (and any season
collections) must be checked against the real storefront first.
"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class ShopifyStoreConfig:
    """Immutable per-store configuration for ShopifyAdapter.

    `source_name` is the stable name exposed via GET /sources and used to
    select this store in CrawlRequest.sources - it must never change once
    published. `display_name` is human-readable only and never surfaced
    through NormalizedProduct/SourceStatus. `base_url` has no trailing
    slash. `default_currency` is used whenever the store's cart.js currency
    cannot be determined.

    `season_collections` is a manually reviewed allowlist, not a general
    rule: {season_label: (collection_handle, ...)}. Only a store's real
    Shopify collection handles that a human has actually checked and
    confirmed to be a clean, curated seasonal assortment belong here -
    each store's own values below carry the reviewer's dated reasoning
    inline. A collection whose name merely *sounds* seasonal but is
    actually a sale/clearance event (e.g. ATA's "leaving winter" or
    "summer sale" collections, which mix markdown items across categories
    rather than curating a season) is deliberately excluded - an empty
    tuple/mapping for a season or a whole store is the correct, honest
    choice when no collection has been reviewed and approved, not an
    oversight to "fix" by guessing from a plausible-looking name. Adding a
    new store or a new season entry here always means: a human looked at
    that store's real, live collection, confirmed it's a genuine curated
    seasonal assortment (not a sale/clearance/generic collection), and
    copied its exact handle.

    `season_collections` is excluded from the generated `__hash__` (via
    field(hash=False)) but still participates in `__eq__` - a
    Mapping/MappingProxyType isn't hashable, and this field is expected to
    be revised over time (a config update, not a new store) without that
    changing the object's identity for hashing purposes. Two configs that
    differ only in season_collections are still unequal; hash() just
    doesn't look at this one field, so instances stay usable wherever a
    hash is needed (e.g. hash(SHTAIM)).
    """

    source_name: str
    display_name: str
    base_url: str
    default_currency: str = "ILS"
    season_collections: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({}), hash=False
    )


SHTAIM = ShopifyStoreConfig(
    source_name="shopify_shtaim",
    display_name="Shtaim",
    base_url="https://shtaim.store",
    # Reviewed 2026-08-25: clean, explicitly-named, human-curated seasonal
    # collections (Winter Collection / Summer Collection / Autumn
    # collection) - not sale/clearance events. No "spring" collection
    # exists for this store; a spring request falls back to the summer
    # mapping documented in catalog_gateway.py.
    season_collections=MappingProxyType({
        "winter": ("קולקצית-חורף",),
        "summer": ("קולקציית-קיץ",),
        "autumn": ("autumn-collection",),
    }),
)

PIPA_FASHION = ShopifyStoreConfig(
    source_name="shopify_pipa_fashion",
    display_name="PIPA Fashion",
    base_url="https://www.pipafashion.co.il",
    # Reviewed 2026-08-25: the store's only season-named collection
    # ("חורף" / "Winter", handle "collection") uses a generic, non-
    # seasonal-looking handle and currently has zero live products - not
    # confidently a maintained, curated seasonal assortment. No summer/
    # spring/autumn-named collection exists at all. Deliberately left
    # unconfigured (empty) rather than guessed at - every product from
    # this store is honestly "unknown" until a maintained seasonal
    # collection can actually be confirmed.
    season_collections=MappingProxyType({}),
)

KAHIKO_SWIMWEAR = ShopifyStoreConfig(
    source_name="shopify_kahiko_swimwear",
    display_name="Kahiko Swimwear",
    base_url="https://kahikoswimwear.co.il",
    # Reviewed 2026-08-25: "WINTER COLLECTION" and "COZY WINTER" are clean,
    # explicitly winter-named curated collections (off-season loungewear
    # from a swimwear brand). No summer-named collection exists - the
    # store's general swimwear catalog is summer-appropriate by category,
    # but that is exactly the kind of clothing-intuition inference this
    # config must not encode; summer is deliberately left unconfigured
    # here rather than inferred from "it's a swimwear store."
    season_collections=MappingProxyType({
        "winter": ("winter-collection", "cozy-winter"),
    }),
)

ATA = ShopifyStoreConfig(
    source_name="shopify_ata",
    display_name="ATA",
    base_url="https://atawear.co.il",
    # Reviewed 2026-08-25: ATA's only season-named collections are sale/
    # clearance events ("מכירת הקיץ של אתא" / "ATA's Summer Sale",
    # "נפרדים מהחורף" / "leaving winter behind") that mix markdown items
    # across categories rather than curating a season - explicitly the
    # "ambiguous sale/clearance" case this config must not trust. Left
    # unconfigured (empty) until a genuinely curated seasonal collection
    # is approved.
    season_collections=MappingProxyType({}),
)

# Stable registration order - app/main.py registers adapters in exactly this
# order, and GET /sources must list shopify_* sources in this same order.
SHOPIFY_STORES: tuple[ShopifyStoreConfig, ...] = (
    SHTAIM,
    PIPA_FASHION,
    KAHIKO_SWIMWEAR,
    ATA,
)