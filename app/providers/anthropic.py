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

_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_RETRY_STATUS_CODES = {429, 500, 502, 503}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRY_STATUS_CODES
    return False


def _build_anthropic_payload(request: ChatCompletionRequest) -> tuple[str, list[dict]]:
    """Return (system_text, messages_list) with cache_control breakpoints applied."""
    system_parts: list[str] = []
    non_system: list[ChatMessage] = []

    for msg in request.messages:
        if msg.role == "system":
            system_parts.append(msg.content)
        else:
            non_system.append(msg)

    system_text = "\n\n".join(system_parts)

    # Build messages with cache_control breakpoints.
    # BP1: system (handled separately as string with cache_control on last system block)
    # BP2: second-to-last assistant message
    # BP3: last assistant message
    messages: list[dict] = []
    assistant_indices: list[int] = []

    for i, msg in enumerate(non_system):
        messages.append({"role": msg.role, "content": msg.content})
        if msg.role == "assistant":
            assistant_indices.append(i)

    # Apply BP2 and BP3 cache_control to assistant messages
    if len(assistant_indices) >= 2:
        bp2_idx = assistant_indices[-2]
        messages[bp2_idx]["content"] = [
            {
                "type": "text",
                "text": non_system[bp2_idx].content,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    if len(assistant_indices) >= 1:
        bp3_idx = assistant_indices[-1]
        messages[bp3_idx]["content"] = [
            {
                "type": "text",
                "text": non_system[bp3_idx].content,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    return system_text, messages


def _build_request_body(request: ChatCompletionRequest, stream: bool) -> dict:
    system_text, messages = _build_anthropic_payload(request)

    body: dict = {
        "model": request.model,
        "messages": messages,
        "temperature": request.temperature,
        "stream": stream,
    }

    if system_text:
        # BP1: system as a list with cache_control
        body["system"] = [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    max_tok = request.max_tokens if request.max_tokens is not None else 4096
    body["max_tokens"] = max_tok

    return body


class AnthropicProvider:
    def __init__(self) -> None:
        self._api_key = settings.anthropic_api_key
        self._headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def chat(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        body = _build_request_body(request, stream=False)

        start_time = time.perf_counter()
        with tracer.start_as_current_span("llm.anthropic.chat") as span:
            span.set_attribute("llm.model", request.model)
            span.set_attribute("llm.provider", "anthropic")

            async with httpx.AsyncClient(timeout=120.0) as client:
                try:
                    response = await client.post(
                        _ANTHROPIC_BASE_URL,
                        headers=self._headers,
                        json=body,
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.exception(
                        "anthropic_request_failed",
                        status_code=exc.response.status_code,
                        model=request.model,
                    )
                    raise

            latency = time.perf_counter() - start_time
            data = response.json()

            usage_data = data.get("usage", {})
            prompt_tokens: int = usage_data.get("input_tokens", 0)
            completion_tokens: int = usage_data.get("output_tokens", 0)
            total_tokens: int = prompt_tokens + completion_tokens

            span.set_attribute("llm.input_tokens", prompt_tokens)
            span.set_attribute("llm.output_tokens", completion_tokens)
            span.set_attribute("llm.latency_seconds", latency)

            llm_inference_duration_seconds.labels(
                model=request.model, provider="anthropic"
            ).observe(latency)
            llm_tokens_total.labels(model=request.model, type="input").inc(prompt_tokens)
            llm_tokens_total.labels(model=request.model, type="output").inc(completion_tokens)

            logger.info(
                "anthropic_chat_completed",
                model=request.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency=latency,
            )

            # Anthropic content is a list of blocks; join text blocks
            content_blocks = data.get("content", [])
            text = "".join(
                block.get("text", "") for block in content_blocks if block.get("type") == "text"
            )
            finish_reason = data.get("stop_reason", "end_turn")

            return ChatCompletionResponse(
                id=data.get("id", f"msg-{uuid.uuid4().hex}"),
                object="chat.completion",
                created=int(time.time()),
                model=data.get("model", request.model),
                choices=[
                    Choice(
                        index=0,
                        message=ChatMessage(role="assistant", content=text),
                        finish_reason=finish_reason,
                    )
                ],
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
        body = _build_request_body(request, stream=True)

        start_time = time.perf_counter()
        chunk_id = f"msg-{uuid.uuid4().hex}"
        created = int(time.time())

        with tracer.start_as_current_span("llm.anthropic.stream") as span:
            span.set_attribute("llm.model", request.model)
            span.set_attribute("llm.provider", "anthropic")

            async with httpx.AsyncClient(timeout=300.0) as client:
                try:
                    async with client.stream(
                        "POST",
                        _ANTHROPIC_BASE_URL,
                        headers=self._headers,
                        json=body,
                    ) as response:
                        response.raise_for_status()

                        event_type: str = ""
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            if line.startswith("event: "):
                                event_type = line[len("event: "):].strip()
                                continue
                            if not line.startswith("data: "):
                                continue

                            raw = line[len("data: "):]
                            try:
                                event_data = json.loads(raw)
                            except json.JSONDecodeError:
                                continue

                            if event_type == "content_block_delta":
                                delta = event_data.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    text = delta.get("text", "")
                                    oai_chunk = {
                                        "id": chunk_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": request.model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": text},
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                    yield f"data: {json.dumps(oai_chunk)}\n\n"

                            elif event_type == "message_delta":
                                stop_reason = event_data.get("delta", {}).get("stop_reason", "end_turn")
                                oai_chunk = {
                                    "id": chunk_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": request.model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {},
                                            "finish_reason": stop_reason,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(oai_chunk)}\n\n"

                except httpx.HTTPStatusError as exc:
                    logger.exception(
                        "anthropic_stream_failed",
                        status_code=exc.response.status_code,
                        model=request.model,
                    )
                    raise

            yield "data: [DONE]\n\n"

            latency = time.perf_counter() - start_time
            span.set_attribute("llm.latency_seconds", latency)
            llm_inference_duration_seconds.labels(
                model=request.model, provider="anthropic"
            ).observe(latency)

            logger.info(
                "anthropic_stream_completed",
                model=request.model,
                latency=latency,
            )
