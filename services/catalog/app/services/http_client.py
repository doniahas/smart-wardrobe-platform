"""Shared httpx.AsyncClient creation for the catalog service.

One client is created for the whole FastAPI application lifespan (see
app/main.py) and reused by every adapter call, instead of creating a new
AsyncClient per request or per adapter call. No cookies, no auth, no
real-source-specific headers, no database access - just a plain, pooled,
timeout-bounded async HTTP client.
"""

import httpx

from app.core.config import CATALOG_REQUEST_TIMEOUT_SECONDS


def create_http_client() -> httpx.AsyncClient:
    """Build the one shared AsyncClient used for the app's lifespan.

    Finite connection limits keep this from opening unbounded outbound
    connections; no cookies/auth are configured since the catalog service
    has no source credentials in this phase.
    """
    timeout = httpx.Timeout(CATALOG_REQUEST_TIMEOUT_SECONDS)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    return httpx.AsyncClient(timeout=timeout, limits=limits)