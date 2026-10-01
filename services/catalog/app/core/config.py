"""Configuration for the catalog service.

All values are read once at import time (i.e. at process/app startup) and
validated immediately - an invalid environment variable fails loudly here,
rather than surfacing as a confusing error deep inside a request handler
later.
"""

import os

SERVICE_NAME = "smartwardrobe-catalog"
CATALOG_PORT = int(os.getenv("CATALOG_PORT", "8003"))


def _read_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _read_float(name: str, default: float, *, minimum: float, exclusive: bool = False) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if exclusive:
        if value <= minimum:
            raise ValueError(f"{name} must be > {minimum}, got {value}")
    elif value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


# Maximum number of adapter fetches allowed to be in flight at once.
CATALOG_MAX_CONCURRENCY = _read_int("CATALOG_MAX_CONCURRENCY", 5, minimum=1)

# Total attempts per adapter call, INCLUDING the first (non-retry) call.
# CATALOG_MAX_ATTEMPTS=3 means: 1 initial call + up to 2 retries.
CATALOG_MAX_ATTEMPTS = _read_int("CATALOG_MAX_ATTEMPTS", 3, minimum=1)

# Base delay (seconds) for exponential backoff between retry attempts.
# 0 is allowed (retries fire immediately, still bounded by max attempts).
CATALOG_RETRY_BASE_DELAY_SECONDS = _read_float(
    "CATALOG_RETRY_BASE_DELAY_SECONDS", 0.5, minimum=0.0
)

# Per-request timeout (seconds) for the shared AsyncClient. Must be > 0.
CATALOG_REQUEST_TIMEOUT_SECONDS = _read_float(
    "CATALOG_REQUEST_TIMEOUT_SECONDS", 10.0, minimum=0.0, exclusive=True
)


def _read_optional_source_list(name: str) -> "list[str] | None":
    """Comma-separated adapter-name allowlist, or None if unset/blank -
    None is the default, and the only value ever used in
    docker-compose.yml/.env.example/production. Only syntactic parsing
    happens here (split/strip/drop-empty); this module deliberately never
    imports the adapters themselves (matching the rest of this file's
    scope), so validating that each name is an actually-registered source
    happens in app/main.py's lifespan(), the only place the real adapter
    registry exists - see that module's _select_registered_adapters.
    """
    raw = os.getenv(name)
    if raw is None:
        return None
    entries = [item.strip() for item in raw.split(",") if item.strip()]
    return entries or None


# Restricts which SourceAdapters app/main.py registers into the
# Crawler at startup. Unset (the default, and the only value ever used in
# docker-compose.yml/.env.example) registers every source exactly as
# before this variable existed - zero behavior change for any real
# deployment. Intended to be set only in a disposable/isolated test
# environment (e.g. CATALOG_ENABLED_SOURCES=mock_demo) so the real,
# unmodified crawler can never dispatch to a live external store during an
# E2E test (see tests/system/test_e2e_catalog_flow.py). This is a
# startup-time source allowlist, not a
# new public API parameter - GET /recommendations/catalog-suggestions
# still takes no `sources` argument, exactly as before.
CATALOG_ENABLED_SOURCES = _read_optional_source_list("CATALOG_ENABLED_SOURCES")