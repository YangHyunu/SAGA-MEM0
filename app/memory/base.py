from typing import Protocol


class MemoryBackend(Protocol):
    async def add(
        self,
        messages: list[dict],
        user_id: str,
        *,
        agent_id: str | None = None,
        app_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict: ...

    async def search(
        self,
        query: str,
        user_id: str,
        *,
        agent_id: str | None = None,
        limit: int = 5,
        filters: dict | None = None,
    ) -> dict: ...

    async def get(
        self,
        user_id: str,
        *,
        agent_id: str | None = None,
    ) -> dict: ...

    async def delete(self, memory_id: str) -> bool: ...

    async def update(self, memory_id: str, data: str) -> dict: ...
