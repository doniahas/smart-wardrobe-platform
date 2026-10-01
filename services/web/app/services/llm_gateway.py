"""Shared LLM-calling logic for both the synchronous /chat route and
the background chat worker.

Extracted from routers/chat.py so the worker doesn't have to duplicate the
httpx call and status-code translation - both call sites need the exact same
failure classification, just reported differently: the route surfaces
status_code/detail as an HTTPException, while the worker persists only the
stable `reason` as a job's error_reason (never the human-readable detail,
which is not meant to be treated as a stable value).
"""
import base64
import logging
from enum import Enum

import httpx
from fastapi import status
from pydantic import BaseModel

from app.core.config import LLM_SERVICE_TIMEOUT_SECONDS, LLM_SERVICE_URL

logger = logging.getLogger(__name__)

# The llm service's own, already well-defined failure statuses (see
# services/llm/app/main.py) are passed through as-is rather than collapsed
# into one generic error - each already maps to a specific, meaningful
# condition (Ollama down, timed out, or returned something unusable).
_PASSTHROUGH_STATUSES = {
    status.HTTP_503_SERVICE_UNAVAILABLE,
    status.HTTP_504_GATEWAY_TIMEOUT,
    status.HTTP_502_BAD_GATEWAY,
}


class ChatGatewayFailureReason(str, Enum):
    """Stable, machine-readable failure classification - safe to persist
    (e.g. as a chat job's error_reason), unlike `detail`, whose wording can
    change independently of the underlying failure mode."""
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_TIMEOUT = "llm_timeout"
    LLM_INVALID_RESPONSE = "llm_invalid_response"


_REASON_BY_STATUS = {
    status.HTTP_503_SERVICE_UNAVAILABLE: ChatGatewayFailureReason.LLM_UNAVAILABLE,
    status.HTTP_504_GATEWAY_TIMEOUT: ChatGatewayFailureReason.LLM_TIMEOUT,
    status.HTTP_502_BAD_GATEWAY: ChatGatewayFailureReason.LLM_INVALID_RESPONSE,
}


class ChatGatewayFailure(BaseModel):
    reason: ChatGatewayFailureReason
    status_code: int
    detail: str


def _safe_json_object(resp: httpx.Response) -> dict | None:
    """Parse the llm service's response body defensively - it's our own
    internal service, but its response is still not trusted blindly."""
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _request_generate(json_body: dict) -> httpx.Response | ChatGatewayFailure:
    """POST to the llm service's /generate endpoint with an arbitrary body.

    Shared by call_llm and call_llm_vision - only the request body differs
    between them (a bare prompt vs. prompt+images+format); the connection-
    error handling is identical. Never logs json_body, so an image's base64
    payload is never written to logs.
    """
    try:
        async with httpx.AsyncClient(timeout=LLM_SERVICE_TIMEOUT_SECONDS) as client:
            return await client.post(f"{LLM_SERVICE_URL}/generate", json=json_body)
    except httpx.RequestError:
        return ChatGatewayFailure(
            reason=ChatGatewayFailureReason.LLM_UNAVAILABLE,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM service is unavailable.",
        )


async def call_llm(prompt: str, format_schema: dict | None = None) -> str | ChatGatewayFailure:
    """Call the llm service's /generate endpoint for one prompt.

    Returns the generated response text, or a ChatGatewayFailure describing
    what went wrong. Never raises for an expected failure mode (connection
    error, timeout, or a malformed/unexpected upstream response).

    `format_schema` (long-term-memory extraction reliability fix) is
    Ollama's structured-output constraint, passed straight through to the
    llm service exactly as call_llm_vision already does - the decoder is
    then grammar-constrained to the given JSON Schema instead of merely
    being asked for JSON in the prompt text. Optional and None by default,
    so the conversational `/chat` reply path (chat_worker.py::
    run_claimed_job) is completely unchanged: free-form prose must never be
    schema-constrained. Only the memory-extraction call passes one - see
    services/memory_extraction.py::MEMORY_EXTRACTION_SCHEMA for why that
    call needs it (qwen2.5:3b is not reliable at JSON/enum compliance from
    prompting alone, the same finding wardrobe_ai.py already records for
    the vision model).
    """
    body: dict = {"prompt": prompt}
    if format_schema is not None:
        body["format"] = format_schema
    resp = await _request_generate(body)
    if isinstance(resp, ChatGatewayFailure):
        return resp

    resp_body = _safe_json_object(resp)

    if resp.status_code in _PASSTHROUGH_STATUSES:
        detail = (resp_body or {}).get("detail", "LLM service could not generate a response.")
        return ChatGatewayFailure(
            reason=_REASON_BY_STATUS[resp.status_code],
            status_code=resp.status_code,
            detail=detail,
        )

    if resp.status_code != 200:
        logger.warning("llm service returned an unexpected status: %d", resp.status_code)
        return ChatGatewayFailure(
            reason=ChatGatewayFailureReason.LLM_INVALID_RESPONSE,
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM service returned an unexpected error.",
        )

    if resp_body is None or not isinstance(resp_body.get("response"), str):
        logger.warning("llm service returned a non-JSON/malformed response body")
        return ChatGatewayFailure(
            reason=ChatGatewayFailureReason.LLM_INVALID_RESPONSE,
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM service returned an unexpected response.",
        )

    return resp_body["response"]


class VisionGenerateResult(BaseModel):
    response: str
    model: str


async def call_llm_vision(
    prompt: str, image_bytes: bytes, format_schema: dict
) -> VisionGenerateResult | ChatGatewayFailure:
    """Call the llm service's /generate endpoint with an image for vision
    analysis (Analyze Item).

    image_bytes is base64-encoded only for the outgoing request - it is
    never logged, and this function is stateless (no disk/DB access), so
    nothing about the image outlives this call. Unlike call_llm, the
    response must also report which model actually produced it (the llm
    service auto-selects its configured vision model whenever `images` is
    present - see services/llm/app/services/ollama_client.py), so this
    can't just reuse call_llm's str-only return type.
    """
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    resp = await _request_generate(
        {"prompt": prompt, "images": [image_b64], "format": format_schema}
    )
    if isinstance(resp, ChatGatewayFailure):
        return resp

    resp_body = _safe_json_object(resp)

    if resp.status_code in _PASSTHROUGH_STATUSES:
        detail = (resp_body or {}).get("detail", "LLM service could not generate a response.")
        return ChatGatewayFailure(
            reason=_REASON_BY_STATUS[resp.status_code],
            status_code=resp.status_code,
            detail=detail,
        )

    if resp.status_code != 200:
        logger.warning("llm service returned an unexpected status: %d", resp.status_code)
        return ChatGatewayFailure(
            reason=ChatGatewayFailureReason.LLM_INVALID_RESPONSE,
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM service returned an unexpected error.",
        )

    if (
        resp_body is None
        or not isinstance(resp_body.get("response"), str)
        or not isinstance(resp_body.get("model"), str)
    ):
        logger.warning("llm service returned a non-JSON/malformed response body")
        return ChatGatewayFailure(
            reason=ChatGatewayFailureReason.LLM_INVALID_RESPONSE,
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM service returned an unexpected response.",
        )

    return VisionGenerateResult(response=resp_body["response"], model=resp_body["model"])
