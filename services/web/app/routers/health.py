from fastapi import APIRouter
import httpx
from app.core.config import RECOMMENDATION_SERVICE_URL
from app.core.database import get_db

router = APIRouter(tags=["health"])

@router.get("/health")
async def health_check():
    return {"status": "ok", "service": "smartwardrobe-web"}

@router.get("/health/full")
async def full_health_check():
    """Check web, MongoDB, and recommendation service together."""
    result = {"web": "ok", "mongo": "unknown", "recommendation": "unknown"}

    try:
        await get_db().command("ping")
        result["mongo"] = "ok"
    except Exception as exc:
        result["mongo"] = f"error: {type(exc).__name__}"

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{RECOMMENDATION_SERVICE_URL}/health")
            result["recommendation"] = "ok" if response.status_code == 200 else f"status {response.status_code}"
    except Exception as exc:
        result["recommendation"] = f"error: {type(exc).__name__}"

    return result
