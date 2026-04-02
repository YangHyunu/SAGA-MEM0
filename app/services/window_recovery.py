import hashlib

import structlog

from app.memory.base import MemoryBackend
from app.services.database import Database

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


async def detect_window_shift(
    messages: list[dict],
    session_id: str,
    db: Database,
) -> int:
    """첫 non-system 메시지의 MD5 해시를 비교하여 윈도우 이동 감지.

    Returns:
        잃어버린 추정 턴 수 (0이면 이동 없음)
    """
    first_msg: dict | None = None
    for msg in messages:
        if msg.get("role") != "system":
            first_msg = msg
            break

    if first_msg is None:
        return 0

    content = first_msg.get("content") or ""
    current_hash = hashlib.md5(content[:500].encode()).hexdigest()[:12]

    stored_hash = await db.get_kv(session_id, "window_first_msg_hash")

    if stored_hash is None:
        await db.set_kv(session_id, "window_first_msg_hash", current_hash)
        logger.info(
            "window_hash_initialized",
            session_id=session_id,
            hash=current_hash,
        )
        return 0

    if stored_hash == current_hash:
        return 0

    total_turns = await db.get_turn_count(session_id)
    visible_turns = sum(
        1 for m in messages if m.get("role") in ("user", "assistant")
    ) // 2
    lost_turns = max(0, total_turns - visible_turns)

    logger.info(
        "window_shift_detected",
        session_id=session_id,
        stored_hash=stored_hash,
        current_hash=current_hash,
        total_turns=total_turns,
        visible_turns=visible_turns,
        lost_turns=lost_turns,
    )

    await db.set_kv(session_id, "window_first_msg_hash", current_hash)

    return lost_turns


async def build_recovery_block(
    memory: MemoryBackend,
    user_id: str,
    agent_id: str | None,
    lost_turns: int,
) -> str:
    """잃어버린 턴의 기억을 mem0에서 복구하여 컨텍스트 블록 생성."""
    if lost_turns == 0:
        return ""

    limit = min(lost_turns, 15)

    try:
        result = await memory.search(
            "최근 대화 요약",
            user_id,
            agent_id=agent_id,
            limit=limit,
        )
    except Exception:
        logger.exception(
            "recovery_block_search_failed",
            user_id=user_id,
            agent_id=agent_id,
            lost_turns=lost_turns,
        )
        return ""

    memories: list[str] = []
    if isinstance(result, dict):
        for item in result.get("results", []):
            mem_text = item.get("memory") or item.get("text") or ""
            if mem_text:
                memories.append(mem_text)

    if not memories:
        return ""

    lines = [f"[이전 대화 기억 ({lost_turns}턴 분)]"]
    for mem in memories:
        lines.append(f"- {mem}")

    block = "\n".join(lines)

    logger.info(
        "recovery_block_built",
        user_id=user_id,
        agent_id=agent_id,
        lost_turns=lost_turns,
        memory_count=len(memories),
    )

    return block
