import json
import time
import uuid
from typing import AsyncIterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import llm_inference_duration_seconds, llm_tokens_total
from app.core.tracing import tracer
from app.schemas.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    Usage,
)

_GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_RETRY_STATUS_CODES = {429, 500, 502, 503}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRY_STATUS_CODES
    return False


def _build_google_payload(request: ChatCompletionRequest) -> dict:
    system_parts: list[str] = []
    contents: list[dict] = []

    for msg in request.messages:
        if msg.role == "system":
            system_parts.append(msg.content)
        elif msg.role == "user":
            contents.append({"role": "user", "parts": [{"text": msg.content}]})
        elif msg.role == "assistant":
            contents.append({"role": "model", "parts": [{"text": msg.content}]})

    payload: dict = {"contents": contents}

    if system_parts:
        payload["system_instruction"] = {
            "parts": [{"text": "\n\n".join(system_parts)}]
        }

    generation_config: dict = {"temperature": request.temperature}
    if request.max_tokens is not None:
        generation_config["maxOutputTokens"] = request.max_tokens
    payload["generationConfig"] = generation_config

    return payload


def _parse_google_response(data: dict, request: ChatCompletionRequest) -> ChatCompletionResponse:
    candidates = data.get("candidates", [])
    choices: list[Choice] = []

    for i, candidate in enumerate(candidates):
        content = candidate.get("content", {})
        parts = content.get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        role = content.get("role", "model")
        # Google uses "model" role, map back to "assistant"
        mapped_role = "assistant" if role == "model" else role
        choices.append(
            Choice(
                index=i,
                message=ChatMessage(role=mapped_role, content=text),
                finish_reason=candidate.get("finishReason", "stop").lower(),
            )
        )

    usage_meta = data.get("usageMetadata", {})
    prompt_tokens: int = usage_meta.get("promptTokenCount", 0)
    completion_tokens: int = usage_meta.get("candidatesTokenCount", 0)
    total_tokens: int = usage_meta.get("totalTokenCount", prompt_tokens + completion_tokens)

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex}",
        object="chat.completion",
        created=int(time.time()),
        model=request.model,
        choices=choices,
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
    )


class GoogleProvider:
    def __init__(self) -> None:
        self._api_key = settings.google_api_key

    def _endpoint(self, model: str, action: str) -> str:
        return f"{_GOOGLE_BASE_URL}/{model}:{action}"

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def chat(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        payload = _build_google_payload(request)
        url = self._endpoint(request.model, "generateContent")
        params = {"key": self._api_key}

        start_time = time.perf_counter()
        with tracer.start_as_current_span("llm.google.chat") as span:
            span.set_attribute("llm.model", request.model)
            span.set_attribute("llm.provider", "google")

            async with httpx.AsyncClient(timeout=120.0) as client:
                try:
                    response = await client.post(url, params=params, json=payload)
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.exception(
                        "google_request_failed",
                        status_code=exc.response.status_code,
                        model=request.model,
                    )
                    raise

            latency = time.perf_counter() - start_time
            data = response.json()

            result = _parse_google_response(data, request)
            prompt_tokens = result.usage.prompt_tokens
            completion_tokens = result.usage.completion_tokens

            span.set_attribute("llm.input_tokens", prompt_tokens)
            span.set_attribute("llm.output_tokens", completion_tokens)
            span.set_attribute("llm.latency_seconds", latency)

            llm_inference_duration_seconds.labels(
                model=request.model, provider="google"
            ).observe(latency)
            llm_tokens_total.labels(model=request.model, type="input").inc(prompt_tokens)
            llm_tokens_total.labels(model=request.model, type="output").inc(completion_tokens)

            logger.info(
                "google_chat_completed",
                model=request.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency=latency,
            )

            return result

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]:
        payload = _build_google_payload(request)
        url = self._endpoint(request.model, "streamGenerateContent")
        params = {"key": self._api_key, "alt": "sse"}

        start_time = time.perf_counter()
        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        with tracer.start_as_current_span("llm.google.stream") as span:
            span.set_attribute("llm.model", request.model)
            span.set_attribute("llm.provider", "google")

            async with httpx.AsyncClient(timeout=300.0) as client:
                try:
                    async with client.stream(
                        "POST", url, params=params, json=payload
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            if line.startswith("data: "):
                                raw = line[len("data: "):]
                                try:
                                    google_chunk = json.loads(raw)
                                except json.JSONDecodeError:
                                    continue

                                candidates = google_chunk.get("candidates", [])
                                for i, candidate in enumerate(candidates):
                                    parts = candidate.get("content", {}).get("parts", [])
                                    text = "".join(p.get("text", "") for p in parts)
                                    finish_reason = candidate.get("finishReason")
                                    normalized_finish = (
                                        finish_reason.lower()
                                        if finish_reason and finish_reason != "STOP"
                                        else (None if not finish_reason else "stop")
                                    )
                                    oai_chunk = {
                                        "id": chunk_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": request.model,
                                        "choices": [
                                            {
                                                "index": i,
                                                "delta": {"content": text},
                                                "finish_reason": normalized_finish,
                                            }
                                        ],
                                    }
                                    yield f"data: {json.dumps(oai_chunk)}\n\n"
                except httpx.HTTPStatusError as exc:
                    logger.exception(
                        "google_stream_failed",
                        status_code=exc.response.status_code,
                        model=request.model,
                    )
                    raise

            yield "data: [DONE]\n\n"

            latency = time.perf_counter() - start_time
            span.set_attribute("llm.latency_seconds", latency)
            llm_inference_duration_seconds.labels(
                model=request.model, provider="google"
            ).observe(latency)

            logger.info(
                "google_stream_completed",
                model=request.model,
                latency=latency,
            )
