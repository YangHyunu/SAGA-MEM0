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

_OPENAI_BASE_URL = "https://api.openai.com/v1/chat/completions"
_RETRY_STATUS_CODES = {429, 500, 502, 503}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRY_STATUS_CODES
    return False


def _merge_system_messages(request: ChatCompletionRequest) -> list[dict]:
    system_parts: list[str] = []
    other_messages: list[dict] = []

    for msg in request.messages:
        if msg.role == "system":
            system_parts.append(msg.content)
        else:
            other_messages.append({"role": msg.role, "content": msg.content})

    merged: list[dict] = []
    if system_parts:
        merged.append({"role": "system", "content": "\n\n".join(system_parts)})
    merged.extend(other_messages)
    return merged


def _build_request_body(request: ChatCompletionRequest) -> dict:
    body: dict = {
        "model": request.model,
        "messages": _merge_system_messages(request),
        "temperature": request.temperature,
        "stream": request.stream,
    }
    if request.max_tokens is not None:
        body["max_completion_tokens"] = request.max_tokens
    if request.user is not None:
        body["user"] = request.user
    return body


class OpenAIProvider:
    def __init__(self) -> None:
        self._api_key = settings.openai_api_key
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def chat(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        body = _build_request_body(request)
        body["stream"] = False

        start_time = time.perf_counter()
        with tracer.start_as_current_span("llm.openai.chat") as span:
            span.set_attribute("llm.model", request.model)
            span.set_attribute("llm.provider", "openai")

            async with httpx.AsyncClient(timeout=120.0) as client:
                try:
                    response = await client.post(
                        _OPENAI_BASE_URL,
                        headers=self._headers,
                        json=body,
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.exception(
                        "openai_request_failed",
                        status_code=exc.response.status_code,
                        model=request.model,
                    )
                    raise

            latency = time.perf_counter() - start_time
            data = response.json()

            prompt_tokens: int = data.get("usage", {}).get("prompt_tokens", 0)
            completion_tokens: int = data.get("usage", {}).get("completion_tokens", 0)
            total_tokens: int = data.get("usage", {}).get("total_tokens", 0)

            span.set_attribute("llm.input_tokens", prompt_tokens)
            span.set_attribute("llm.output_tokens", completion_tokens)
            span.set_attribute("llm.latency_seconds", latency)

            llm_inference_duration_seconds.labels(
                model=request.model, provider="openai"
            ).observe(latency)
            llm_tokens_total.labels(model=request.model, type="input").inc(prompt_tokens)
            llm_tokens_total.labels(model=request.model, type="output").inc(completion_tokens)

            logger.info(
                "openai_chat_completed",
                model=request.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency=latency,
            )

            choices = [
                Choice(
                    index=c["index"],
                    message=ChatMessage(
                        role=c["message"]["role"],
                        content=c["message"]["content"],
                    ),
                    finish_reason=c.get("finish_reason"),
                )
                for c in data.get("choices", [])
            ]

            return ChatCompletionResponse(
                id=data.get("id", f"chatcmpl-{uuid.uuid4().hex}"),
                object="chat.completion",
                created=data.get("created", int(time.time())),
                model=data.get("model", request.model),
                choices=choices,
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                ),
            )

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]:
        body = _build_request_body(request)
        body["stream"] = True

        start_time = time.perf_counter()
        with tracer.start_as_current_span("llm.openai.stream") as span:
            span.set_attribute("llm.model", request.model)
            span.set_attribute("llm.provider", "openai")

            async with httpx.AsyncClient(timeout=300.0) as client:
                try:
                    async with client.stream(
                        "POST",
                        _OPENAI_BASE_URL,
                        headers=self._headers,
                        json=body,
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            if line.startswith("data: "):
                                payload = line[len("data: "):]
                                if payload.strip() == "[DONE]":
                                    yield "data: [DONE]\n\n"
                                    break
                                yield f"data: {payload}\n\n"
                except httpx.HTTPStatusError as exc:
                    logger.exception(
                        "openai_stream_failed",
                        status_code=exc.response.status_code,
                        model=request.model,
                    )
                    raise

            latency = time.perf_counter() - start_time
            span.set_attribute("llm.latency_seconds", latency)
            llm_inference_duration_seconds.labels(
                model=request.model, provider="openai"
            ).observe(latency)

            logger.info(
                "openai_stream_completed",
                model=request.model,
                latency=latency,
            )
