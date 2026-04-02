import structlog
from fastapi import APIRouter, Depends, Request

from app.core.config import settings
from app.core.limiter import limiter
from app.memory.base import MemoryBackend
from app.schemas.memory import (
    MemoryAddRequest,
    MemorySearchRequest,
    MemorySearchResponse,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

router = APIRouter()


def _get_memory(request: Request) -> MemoryBackend:
    return request.app.state.memory


@router.post("/memory")
@limiter.limit(settings.rate_limit_default)
async def add_memory(
    request: Request,
    body: MemoryAddRequest,
    memory: MemoryBackend = Depends(_get_memory),
) -> dict:
    result = await memory.add(
        messages=body.messages,
        user_id=body.user_id,
        agent_id=body.agent_id,
        app_id=body.app_id,
        metadata=body.metadata,
    )
    logger.info(
        "memory_added",
        user_id=body.user_id,
        agent_id=body.agent_id,
    )
    return result


@router.post("/memory/search")
@limiter.limit(settings.rate_limit_default)
async def search_memory(
    request: Request,
    body: MemorySearchRequest,
    memory: MemoryBackend = Depends(_get_memory),
) -> dict:
    result = await memory.search(
        query=body.query,
        user_id=body.user_id,
        agent_id=body.agent_id,
        limit=body.limit,
        filters=body.filters,
    )
    logger.info(
        "memory_searched",
        user_id=body.user_id,
        hits=len(result.get("results", [])),
    )
    return result


@router.delete("/memory/{memory_id}")
@limiter.limit(settings.rate_limit_default)
async def delete_memory(
    request: Request,
    memory_id: str,
    memory: MemoryBackend = Depends(_get_memory),
) -> dict:
    success = await memory.delete(memory_id=memory_id)
    logger.info("memory_deleted", memory_id=memory_id, success=success)
    return {"deleted": success, "memory_id": memory_id}
