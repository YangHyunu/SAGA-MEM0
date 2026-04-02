import time

import structlog
from opentelemetry.trace import StatusCode

from app.core.config import settings
from app.core.metrics import memory_events_total, memory_operation_duration_seconds
from app.core.tracing import tracer
from app.memory.base import MemoryBackend
from app.services.curator import curate_narrative_summary
from app.services.database import Database

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


async def post_turn_process(
    memory: MemoryBackend,
    db: Database,
    llm_generate_fn,
    messages: list[dict],
    assistant_response: str,
    session_id: str,
    user_id: str,
    agent_id: str | None,
    app_id: str | None,
) -> None:
    """응답 반환 후 비동기 실행. 유저 대기 없음."""
    with tracer.start_as_current_span("post_turn") as span:
        try:
            user_msg = next(
                (m for m in reversed(messages) if m.get("role") == "user"),
                None,
            )
            if not user_msg:
                return

            t0 = time.monotonic()
            result = await memory.add(
                messages=[
                    user_msg,
                    {"role": "assistant", "content": assistant_response},
                ],
                user_id=user_id,
                agent_id=agent_id,
                app_id=app_id,
            )
            elapsed = time.monotonic() - t0

            events = result.get("results", [])
            for event in events:
                event_type = event.get("event", "NONE")
                memory_events_total.labels(event_type=event_type).inc()

            span.set_attributes({
                "post_turn.mem0_events": len(events),
                "post_turn.latency_ms": round(elapsed * 1000, 1),
            })

            turn_number = await db.get_turn_count(session_id) + 1
            await db.log_turn(
                session_id=session_id,
                turn_number=turn_number,
                user_content=user_msg.get("content", ""),
                assistant_content=assistant_response,
            )

            if turn_number % settings.curator_interval == 0:
                curator_messages = messages[-(settings.curator_interval * 2):]
                await curate_narrative_summary(
                    memory=memory,
                    llm_generate_fn=llm_generate_fn,
                    user_id=user_id,
                    agent_id=agent_id,
                    app_id=app_id,
                    recent_messages=curator_messages,
                )
                span.set_attribute("post_turn.curator_triggered", True)

            logger.info(
                "post_turn_completed",
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                turn_number=turn_number,
                mem0_events=len(events),
                curator_triggered=(turn_number % settings.curator_interval == 0),
            )

        except Exception:
            span.set_status(StatusCode.ERROR)
            logger.exception(
                "post_turn_failed",
                user_id=user_id,
                session_id=session_id,
            )
