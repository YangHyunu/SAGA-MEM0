from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class MemoryResult(BaseModel):
    id: str
    memory: str
    user_id: str
    agent_id: Optional[str] = None
    score: Optional[float] = None
    created_at: Optional[str] = None


class MemoryAddRequest(BaseModel):
    model_config = {"str_strip_whitespace": True}

    messages: list[dict]
    user_id: str
    agent_id: Optional[str] = None
    app_id: Optional[str] = None
    metadata: Optional[dict] = None


class MemorySearchRequest(BaseModel):
    model_config = {"str_strip_whitespace": True}

    query: str
    user_id: str
    agent_id: Optional[str] = None
    limit: int = Field(default=10, gt=0, le=100)
    filters: Optional[dict] = None


class MemorySearchResponse(BaseModel):
    results: list[MemoryResult]
