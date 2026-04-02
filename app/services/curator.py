import time

import structlog
from opentelemetry.trace import StatusCode

from app.core.config import settings
from app.core.tracing import tracer
from app.memory.base import MemoryBackend

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

NARRATIVE_MAX_CHARS = 1500

CURATOR_PROMPT = """당신은 롤플레이 서사 요약 전문가입니다.

기존 서사 요약과 최근 대화를 읽고, 갱신된 서사 요약을 작성하세요.

규칙:
1. 시간순으로 주요 사건과 결정을 정리
2. 캐릭터 관계 변화를 반영
3. 현재 상황(위치, 목표, 긴장 관계)을 마지막에 명시
4. {max_chars}자 이내로 작성
5. 일상 대화나 전투 디테일은 생략, 플롯에 영향을 준 것만 포함

기존 서사 요약:
{existing_summary}

최근 대화 ({turn_count}턴):
{recent_conversation}

갱신된 서사 요약:"""


async def curate_narrative_summary(
    memory: MemoryBackend,
    llm_generate_fn,
    user_id: str,
    agent_id: str | None,
    app_id: str | None,
    recent_messages: list[dict],
) -> str | None:
    with tracer.start_as_current_span("curator.narrative") as span:
        try:
            existing = await memory.search(
                query="narrative summary",
                user_id=user_id,
                agent_id=agent_id,
                filters={"metadata": {"type": "narrative_summary"}},
                limit=1,
            )
            results = existing.get("results", [])
            existing_summary = (
                results[0]["memory"]
                if results
                else "(아직 서사가 시작되지 않음)"
            )

            conversation_text = "\n".join(
                f'{m["role"]}: {m["content"][:300]}'
                for m in recent_messages
                if m.get("role") in ("user", "assistant")
            )

            prompt = CURATOR_PROMPT.format(
                existing_summary=existing_summary,
                recent_conversation=conversation_text,
                turn_count=len(recent_messages) // 2,
                max_chars=NARRATIVE_MAX_CHARS,
            )

            new_summary = await llm_generate_fn(
                model=settings.curator_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                temperature=0.3,
            )

            await memory.add(
                messages=[{"role": "system", "content": new_summary}],
                user_id=user_id,
                agent_id=agent_id,
                app_id=app_id,
                metadata={"type": "narrative_summary"},
            )

            span.set_attributes({
                "curator.existing_length": len(existing_summary),
                "curator.new_length": len(new_summary),
                "curator.model": settings.curator_model,
            })
            logger.info(
                "narrative_curated",
                user_id=user_id,
                agent_id=agent_id,
                summary_length=len(new_summary),
            )
            return new_summary

        except Exception:
            span.set_status(StatusCode.ERROR)
            logger.exception(
                "curator_failed",
                user_id=user_id,
                agent_id=agent_id,
            )
            return None
