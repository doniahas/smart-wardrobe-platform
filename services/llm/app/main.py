from typing import Optional

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.services.ollama_client import (
    GenerateFailure,
    GenerateFailureReason,
    check_ollama_status,
    generate_text,
)

app = FastAPI(
    title="SmartWardrobe LLM Service",
    description="Internal local LLM service (skeleton - no model loaded yet)",
    version="0.1.0",
)


@app.get("/health")
async def health_check():
    ollama_reachable, model_loaded = await check_ollama_status()
    return {
        "status": "ok" if ollama_reachable else "degraded",
        "service": "llm",
        "ollama_reachable": ollama_reachable,
        "model_loaded": model_loaded,
    }


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    images: Optional[list[str]] = None
    format: Optional[dict] = None

    @field_validator("prompt")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt cannot be blank")
        return value


class GenerateResponse(BaseModel):
    response: str
    model: str


_GENERATE_FAILURE_STATUS = {
    GenerateFailureReason.UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    GenerateFailureReason.TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
    GenerateFailureReason.INVALID_RESPONSE: status.HTTP_502_BAD_GATEWAY,
}


@app.post("/generate", response_model=GenerateResponse)
async def generate(body: GenerateRequest):
    result = await generate_text(body.prompt, images=body.images, format_schema=body.format)
    if isinstance(result, GenerateFailure):
        raise HTTPException(
            status_code=_GENERATE_FAILURE_STATUS[result.reason], detail=result.detail
        )
    return GenerateResponse(response=result.response, model=result.model)
