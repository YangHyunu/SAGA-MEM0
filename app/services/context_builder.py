import asyncio
import time

import structlog
import tiktoken

from app.charx.lorebook import LorebookEngine
from app.charx.schemas import LorebookEntry
from app.core.metrics import context_build_duration_seconds, lorebook_entries_injected_total
from app.core.tracing import tracer
from app.memory.base import MemoryBackend

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

_encoder = tiktoken.encoding_for_model("gpt-4")


def count_tokens(text: str) -> int:
    return len(_encoder.encode(text))


async def build_context(
    messages: list[dict],
    lorebook: LorebookEngine,
    memory: MemoryBackend,
    user_id: str,
    agent_id: str | None,
    token_budget: int,
) -> str:
    """Phase 1~3: 정적/동적 컨텍스트 수집 + Greedy Assembly."""
    with tracer.start_as_current_span("context.build") as span:
        t0 = time.monotonic()

        last_user_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user_content = msg.get("content", "")
                break

        constant_entries = lorebook.get_constant_entries()
        lorebook_entries_injected_total.labels(type="constant").inc(len(constant_entries))

        narrative_task = memory.search(
            query="narrative summary",
            user_id=user_id,
            agent_id=agent_id,
            filters={"metadata": {"type": "narrative_summary"}},
            limit=1,
        )
        triggered_task = _match_triggered_async(lorebook, messages)
        char_memory_task = memory.search(
            query=last_user_content[:500],
            user_id=user_id,
            agent_id=agent_id,
            limit=10,
        )
        shared_memory_task = memory.search(
            query=last_user_content[:500],
            user_id=user_id,
            limit=5,
        )

        results = await asyncio.gather(
            narrative_task,
            triggered_task,
            char_memory_task,
            shared_memory_task,
            return_exceptions=True,
        )

        narrative_result = results[0] if not isinstance(results[0], BaseException) else {}
        triggered_entries = results[1] if not isinstance(results[1], BaseException) else []
        char_memories = results[2] if not isinstance(results[2], BaseException) else {}
        shared_memories = results[3] if not isinstance(results[3], BaseException) else {}

        lorebook_entries_injected_total.labels(type="triggered").inc(len(triggered_entries))

        narrative_summary = None
        narrative_results = narrative_result.get("results", [])
        if narrative_results:
            narrative_summary = narrative_results[0].get("memory")

        context_block = assemble_context(
            narrative_summary=narrative_summary,
            constant_entries=constant_entries,
            triggered_entries=triggered_entries,
            character_memories=char_memories.get("results", []),
            shared_memories=shared_memories.get("results", []),
            token_budget=token_budget,
        )

        elapsed = time.monotonic() - t0
        context_build_duration_seconds.observe(elapsed)

        span.set_attributes({
            "context.token_budget": token_budget,
            "context.tokens_used": count_tokens(context_block),
            "context.constant_count": len(constant_entries),
            "context.triggered_count": len(triggered_entries),
            "context.has_narrative": narrative_summary is not None,
            "context.latency_ms": round(elapsed * 1000, 1),
        })

        logger.info(
            "context_built",
            token_budget=token_budget,
            tokens_used=count_tokens(context_block),
            constant=len(constant_entries),
            triggered=len(triggered_entries),
            has_narrative=narrative_summary is not None,
            duration=round(elapsed, 3),
        )

        return context_block


async def _match_triggered_async(
    lorebook: LorebookEngine,
    messages: list[dict],
) -> list[LorebookEntry]:
    return lorebook.match_triggered(messages)


def assemble_context(
    narrative_summary: str | None,
    constant_entries: list[LorebookEntry],
    triggered_entries: list[LorebookEntry],
    character_memories: list[dict],
    shared_memories: list[dict],
    token_budget: int,
) -> str:
    """Greedy Token Budget Assembly — 우선순위 순으로 토큰 예산 소비."""
    remaining = token_budget
    blocks: list[str] = []

    if narrative_summary:
        text = narrative_summary[:1500]
        t = count_tokens("[서사 요약]\n" + text)
        if t <= remaining:
            blocks.append("[서사 요약]\n" + text)
            remaining -= t

    remaining, block = _greedy_fill(
        entries=[e.content for e in constant_entries],
        header="[세계관 설정]",
        remaining=remaining,
        per_entry_cap=None,
    )
    if block:
        blocks.append(block)

    remaining, block = _greedy_fill(
        entries=[e.content for e in triggered_entries],
        header="[캐릭터 정보]",
        remaining=remaining,
        per_entry_cap=800,
    )
    if block:
        blocks.append(block)

    remaining, block = _greedy_fill(
        entries=[m.get("memory", "") for m in character_memories],
        header="[기억]",
        remaining=remaining,
        per_entry_cap=500,
    )
    if block:
        blocks.append(block)

    remaining, block = _greedy_fill(
        entries=[m.get("memory", "") for m in shared_memories],
        header="[세계 이벤트]",
        remaining=remaining,
        per_entry_cap=500,
    )
    if block:
        blocks.append(block)

    return "\n\n".join(blocks)


def _greedy_fill(
    entries: list[str],
    header: str,
    remaining: int,
    per_entry_cap: int | None,
) -> tuple[int, str | None]:
    header_tokens = count_tokens(header)
    if header_tokens > remaining:
        return remaining, None

    remaining -= header_tokens
    lines: list[str] = []

    for entry in entries:
        text = entry[:per_entry_cap] if per_entry_cap else entry
        t = count_tokens(text)
        if t <= remaining:
            lines.append(text)
            remaining -= t

    if not lines:
        remaining += header_tokens
        return remaining, None

    return remaining, header + "\n" + "\n".join(lines)
