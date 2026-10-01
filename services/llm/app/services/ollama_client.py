"""Client responsible for talking to Ollama: checking reachability
and requesting text generation.

Neither function raises for an expected failure mode (connection error,
timeout, malformed/empty upstream response) - each returns a value the
caller inspects instead, so /health and /generate can always return a
controlled response rather than a 500.
"""
from enum import Enum

import httpx
from pydantic import BaseModel

from app.core.config import (
    OLLAMA_GENERATE_TIMEOUT_SECONDS,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT_SECONDS,
    OLLAMA_URL,
    OLLAMA_VISION_MODEL,
)


async def check_ollama_status() -> tuple[bool, bool]:
    """Return (ollama_reachable, model_loaded).

    Never raises - any connection failure, timeout, non-200 response, or
    malformed body is treated as "not reachable" rather than propagated,
    so the /health endpoint this feeds can always return a controlled
    response instead of a 500 when Ollama is unavailable.
    """
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags")
    except httpx.RequestError:
        return False, False

    if response.status_code != 200:
        return False, False

    try:
        data = response.json()
    except ValueError:
        return True, False

    models = data.get("models", []) if isinstance(data, dict) else []
    model_loaded = any(
        isinstance(model, dict) and model.get("name") == OLLAMA_MODEL
        for model in models
    )
    return True, model_loaded


class GenerateFailureReason(str, Enum):
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"


class GenerateResult(BaseModel):
    response: str
    model: str


class GenerateFailure(BaseModel):
    reason: GenerateFailureReason
    detail: str


async def generate_text(
    prompt: str,
    images: list[str] | None = None,
    format_schema: dict | None = None,
) -> GenerateResult | GenerateFailure:
    """Request non-streaming text (or vision) generation from Ollama for one prompt.

    Uses OLLAMA_GENERATE_TIMEOUT_SECONDS, not the short health-check
    timeout - generation can legitimately take tens of seconds on CPU.
    "stream": False is required so Ollama returns one JSON object instead
    of newline-delimited partial-token chunks.

    The caller never picks a model directly: "images provided" is the only
    signal that switches the request to OLLAMA_VISION_MODEL, so this stays
    a deterministic, server-controlled decision rather than client input.
    format_schema, when given, is passed through as Ollama's structured-
    output constraint (see the moondream spike - sub-4B vision models are
    unreliable at JSON compliance from prompting alone).
    """
    model = OLLAMA_VISION_MODEL if images else OLLAMA_MODEL
    body: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    if images:
        body["images"] = images
    if format_schema is not None:
        body["format"] = format_schema

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_GENERATE_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{OLLAMA_URL}/api/generate", json=body)
    except httpx.TimeoutException:
        return GenerateFailure(
            reason=GenerateFailureReason.TIMEOUT, detail="Ollama did not respond in time."
        )
    except httpx.RequestError:
        return GenerateFailure(
            reason=GenerateFailureReason.UNAVAILABLE, detail="Ollama service is unavailable."
        )

    if response.status_code != 200:
        return GenerateFailure(
            reason=GenerateFailureReason.INVALID_RESPONSE,
            detail="Ollama returned an unexpected response.",
        )

    try:
        data = response.json()
    except ValueError:
        return GenerateFailure(
            reason=GenerateFailureReason.INVALID_RESPONSE,
            detail="Ollama returned an unexpected response.",
        )

    text = data.get("response") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        return GenerateFailure(
            reason=GenerateFailureReason.INVALID_RESPONSE,
            detail="Ollama returned an unexpected response.",
        )

    return GenerateResult(response=text, model=model)
