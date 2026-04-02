import time

import structlog
from mem0 import AsyncMemory
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import Settings, settings
from app.core.logging import logger
from app.core.metrics import memory_events_total, memory_operation_duration_seconds
from app.core.tracing import tracer


class InstrumentedMemory:
    def __init__(self, cfg: Settings) -> None:
        vector_store_config: dict = {
            "collection_name": cfg.mem0_collection_name,
        }
        if cfg.qdrant_url:
            vector_store_config["url"] = cfg.qdrant_url
        else:
            vector_store_config["path"] = cfg.qdrant_path

        config: dict = {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": "gpt-4o-mini",
                    "temperature": 0.1,
                    "max_tokens": 2000,
                    "api_key": cfg.openai_api_key,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": cfg.mem0_embedder_model,
                    "api_key": cfg.openai_api_key,
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": vector_store_config,
            },
            "version": "v1.1",
        }
        self._memory: AsyncMemory = AsyncMemory.from_config(config)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def add(
        self,
        messages: list[dict],
        user_id: str,
        *,
        agent_id: str | None = None,
        app_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        with tracer.start_as_current_span("memory.add") as span:
            span.set_attributes(
                {
                    "memory.operation": "add",
                    "memory.user_id": user_id,
                    "memory.agent_id": agent_id or "",
                    "memory.message_count": len(messages),
                }
            )
            start = time.perf_counter()
            try:
                result: dict = await self._memory.add(
                    messages,
                    user_id=user_id,
                    agent_id=agent_id,
                    app_id=app_id,
                    metadata=metadata or {},
                )
                latency_ms = (time.perf_counter() - start) * 1000
                span.set_attributes(
                    {
                        "memory.latency_ms": latency_ms,
                        "memory.events": len(result.get("results", [])),
                    }
                )
                memory_operation_duration_seconds.labels(backend="mem0", op="add").observe(
                    latency_ms / 1000
                )
                for event in result.get("results", []):
                    event_type = event.get("event", "unknown")
                    memory_events_total.labels(event_type=event_type).inc()
                logger.info(
                    "memory_add_completed",
                    user_id=user_id,
                    agent_id=agent_id,
                    latency_ms=round(latency_ms, 2),
                    events=len(result.get("results", [])),
                )
                return result
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
                logger.exception("memory_add_failed", user_id=user_id, agent_id=agent_id)
                raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def search(
        self,
        query: str,
        user_id: str,
        *,
        agent_id: str | None = None,
        limit: int = 5,
        filters: dict | None = None,
    ) -> dict:
        with tracer.start_as_current_span("memory.search") as span:
            span.set_attributes(
                {
                    "memory.operation": "search",
                    "memory.user_id": user_id,
                    "memory.agent_id": agent_id or "",
                    "memory.limit": limit,
                }
            )
            start = time.perf_counter()
            try:
                result: dict = await self._memory.search(
                    query,
                    user_id=user_id,
                    agent_id=agent_id,
                    limit=limit,
                    filters=filters or {},
                )
                latency_ms = (time.perf_counter() - start) * 1000
                hits = len(result.get("results", []))
                span.set_attributes(
                    {
                        "memory.latency_ms": latency_ms,
                        "memory.hits": hits,
                    }
                )
                memory_operation_duration_seconds.labels(backend="mem0", op="search").observe(
                    latency_ms / 1000
                )
                logger.info(
                    "memory_search_completed",
                    user_id=user_id,
                    agent_id=agent_id,
                    query_len=len(query),
                    hits=hits,
                    latency_ms=round(latency_ms, 2),
                )
                return result
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
                logger.exception("memory_search_failed", user_id=user_id, agent_id=agent_id)
                raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def get(
        self,
        user_id: str,
        *,
        agent_id: str | None = None,
    ) -> dict:
        with tracer.start_as_current_span("memory.get") as span:
            span.set_attributes(
                {
                    "memory.operation": "get",
                    "memory.user_id": user_id,
                    "memory.agent_id": agent_id or "",
                }
            )
            start = time.perf_counter()
            try:
                result: dict = await self._memory.get_all(
                    user_id=user_id,
                    agent_id=agent_id,
                )
                latency_ms = (time.perf_counter() - start) * 1000
                hits = len(result.get("results", []))
                span.set_attributes(
                    {
                        "memory.latency_ms": latency_ms,
                        "memory.hits": hits,
                    }
                )
                memory_operation_duration_seconds.labels(backend="mem0", op="get").observe(
                    latency_ms / 1000
                )
                logger.info(
                    "memory_get_completed",
                    user_id=user_id,
                    agent_id=agent_id,
                    hits=hits,
                    latency_ms=round(latency_ms, 2),
                )
                return result
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
                logger.exception("memory_get_failed", user_id=user_id, agent_id=agent_id)
                raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def delete(self, memory_id: str) -> bool:
        with tracer.start_as_current_span("memory.delete") as span:
            span.set_attributes(
                {
                    "memory.operation": "delete",
                    "memory.memory_id": memory_id,
                }
            )
            start = time.perf_counter()
            try:
                await self._memory.delete(memory_id=memory_id)
                latency_ms = (time.perf_counter() - start) * 1000
                span.set_attributes({"memory.latency_ms": latency_ms})
                memory_operation_duration_seconds.labels(backend="mem0", op="delete").observe(
                    latency_ms / 1000
                )
                logger.info(
                    "memory_delete_completed",
                    memory_id=memory_id,
                    latency_ms=round(latency_ms, 2),
                )
                return True
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
                logger.exception("memory_delete_failed", memory_id=memory_id)
                raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def update(self, memory_id: str, data: str) -> dict:
        with tracer.start_as_current_span("memory.update") as span:
            span.set_attributes(
                {
                    "memory.operation": "update",
                    "memory.memory_id": memory_id,
                }
            )
            start = time.perf_counter()
            try:
                result: dict = await self._memory.update(memory_id=memory_id, data=data)
                latency_ms = (time.perf_counter() - start) * 1000
                span.set_attributes({"memory.latency_ms": latency_ms})
                memory_operation_duration_seconds.labels(backend="mem0", op="update").observe(
                    latency_ms / 1000
                )
                logger.info(
                    "memory_update_completed",
                    memory_id=memory_id,
                    latency_ms=round(latency_ms, 2),
                )
                return result
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
                logger.exception("memory_update_failed", memory_id=memory_id)
                raise
