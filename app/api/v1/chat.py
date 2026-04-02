import asyncio
import json
import time
import uuid

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.charx.lorebook import LorebookEngine
from app.core.config import settings
from app.core.limiter import limiter
from app.core.metrics import llm_inference_duration_seconds, llm_tokens_total
from app.core.tracing import tracer
from app.memory.base import MemoryBackend
from app.schemas.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    Usage,
)
from app.services.context_builder import build_context
from app.services.llm import route_to_provider
from app.services.message_compressor import MessageCompressor
from app.services.post_turn import post_turn_process
from app.services.system_stabilizer import SystemStabilizer
from app.services.window_recovery import build_recovery_block, detect_window_shift

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

router = APIRouter()


def _get_memory(request: Request) -> MemoryBackend:
    return request.app.state.memory


def _get_lorebook(request: Request) -> LorebookEngine | None:
    return getattr(request.app.state, "lorebook", None)


def _get_db(request: Request):
    return request.app.state.db


@router.post("/chat/completions", response_model=None)
@limiter.limit(settings.rate_limit_default)
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    memory: MemoryBackend = Depends(_get_memory),
    lorebook: LorebookEngine | None = Depends(_get_lorebook),
    db=Depends(_get_db),
):
    with tracer.start_as_current_span("chat_completion") as span:
        session_id = request.headers.get("x-session-id", str(uuid.uuid4()))
        user_id = request.headers.get("x-user-id", "default")
        agent_id = request.headers.get("x-agent-id", None)
        app_id = request.headers.get("x-app-id", None)

        span.set_attributes({
            "chat.model": body.model,
            "chat.stream": body.stream,
            "chat.message_count": len(body.messages),
        })

        messages = [m.model_dump() for m in body.messages]

        # Phase: SystemStabilizer — canonical system prompt 보호
        stabilizer = SystemStabilizer(db)
        system_msgs = [m for m in messages if m["role"] == "system"]
        lorebook_delta = ""
        if system_msgs:
            canonical, lorebook_delta = await stabilizer.stabilize(
                session_id=session_id,
                system_message=system_msgs[0]["content"],
            )
            messages[0]["content"] = canonical

        # Phase: WindowRecovery — RisuAI 윈도우 이동 감지
        recovery_block = ""
        if db:
            lost_turns = await detect_window_shift(messages, session_id, db)
            if lost_turns > 0 and memory:
                recovery_block = await build_recovery_block(
                    memory, user_id, agent_id, lost_turns,
                )

        # Phase: MessageCompressor — 토큰 초과 시 압축
        if memory:
            compressor = MessageCompressor()
            messages = await compressor.compress(messages, memory)

        # Phase: Context Build — 로어북 + 메모리 검색 + 조립
        context_block = ""
        if lorebook and memory:
            context_block = await build_context(
                messages=messages,
                lorebook=lorebook,
                memory=memory,
                user_id=user_id,
                agent_id=agent_id,
                token_budget=4000,
            )

        # Phase: 최종 메시지 조립 — dynamic context를 마지막 user 메시지에 prepend
        dynamic_parts = [p for p in [recovery_block, lorebook_delta, context_block] if p]
        if dynamic_parts and messages:
            dynamic_block = "\n\n".join(dynamic_parts)
            last_idx = None
            for i in range(len(messages) - 1, -1, -1):
                if messages[i]["role"] == "user":
                    last_idx = i
                    break
            if last_idx is not None:
                messages[last_idx]["content"] = (
                    dynamic_block + "\n\n" + messages[last_idx]["content"]
                )

        updated_request = body.model_copy(
            update={"messages": [ChatMessage(**m) for m in messages]}
        )

        provider, provider_name = route_to_provider(body.model)

        if body.stream:
            return StreamingResponse(
                _stream_response(
                    provider=provider,
                    request=updated_request,
                    provider_name=provider_name,
                    memory=memory,
                    db=db,
                    messages=messages,
                    session_id=session_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    app_id=app_id,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        t0 = time.monotonic()
        response = await provider.chat(updated_request)
        elapsed = time.monotonic() - t0

        llm_inference_duration_seconds.labels(
            model=body.model,
            provider=provider_name,
        ).observe(elapsed)

        if response.usage:
            llm_tokens_total.labels(model=body.model, type="input").inc(
                response.usage.prompt_tokens
            )
            llm_tokens_total.labels(model=body.model, type="output").inc(
                response.usage.completion_tokens
            )

        assistant_content = ""
        if response.choices:
            assistant_content = response.choices[0].message.content

        asyncio.create_task(
            post_turn_process(
                memory=memory,
                db=db,
                llm_generate_fn=_llm_generate,
                messages=messages,
                assistant_response=assistant_content,
                session_id=session_id,
                user_id=user_id,
                agent_id=agent_id,
                app_id=app_id,
            )
        )

        logger.info(
            "chat_completion_done",
            model=body.model,
            provider=provider_name,
            duration=round(elapsed, 3),
            stream=False,
        )

        return response


async def _stream_response(
    provider,
    request: ChatCompletionRequest,
    provider_name: str,
    memory: MemoryBackend,
    db,
    messages: list[dict],
    session_id: str,
    user_id: str,
    agent_id: str | None,
    app_id: str | None,
):
    collected_content = []
    t0 = time.monotonic()

    async for chunk in provider.stream(request):
        yield chunk
        try:
            if chunk.startswith("data: ") and not chunk.strip().endswith("[DONE]"):
                data = json.loads(chunk[6:])
                delta = data.get("choices", [{}])[0].get("delta", {})
                if "content" in delta:
                    collected_content.append(delta["content"])
        except (json.JSONDecodeError, IndexError, KeyError):
            pass

    elapsed = time.monotonic() - t0
    llm_inference_duration_seconds.labels(
        model=request.model,
        provider=provider_name,
    ).observe(elapsed)

    assistant_content = "".join(collected_content)

    asyncio.create_task(
        post_turn_process(
            memory=memory,
            db=db,
            llm_generate_fn=_llm_generate,
            messages=messages,
            assistant_response=assistant_content,
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            app_id=app_id,
        )
    )


async def _llm_generate(
    model: str,
    messages: list[dict],
    max_tokens: int = 1000,
    temperature: float = 0.3,
) -> str:
    provider, _ = route_to_provider(model)
    req = ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(**m) for m in messages],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    response = await provider.chat(req)
    if response.choices:
        return response.choices[0].message.content
    return ""
