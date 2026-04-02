import hashlib

import structlog

from app.services.database import Database

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def _hash_content(content: str) -> str:
    return hashlib.md5(content[:2000].encode()).hexdigest()[:16]


def _jaccard_similarity(a: str, b: str) -> float:
    set_a = set(a.split())
    set_b = set(b.split())
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def _extract_delta(canonical: str, current: str) -> str:
    canonical_paragraphs = set(canonical.split("\n\n"))
    current_paragraphs = current.split("\n\n")
    delta_parts = [p for p in current_paragraphs if p.strip() and p not in canonical_paragraphs]
    return "\n\n".join(delta_parts)


class SystemStabilizer:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def stabilize(
        self,
        session_id: str,
        system_message: str,
    ) -> tuple[str, str]:
        """시스템 메시지를 안정화하여 canonical + delta를 반환.

        Returns:
            (canonical_system, delta) — canonical은 캐시 대상, delta는 dynamic context로 이동
        """
        canonical = await self._db.get_kv(session_id, "canonical_system")
        canonical_hash = await self._db.get_kv(session_id, "canonical_system_hash")

        current_hash = _hash_content(system_message)

        if canonical is None:
            await self._db.set_kv(session_id, "canonical_system", system_message)
            await self._db.set_kv(session_id, "canonical_system_hash", current_hash)
            logger.info("system_stabilizer_canonical_set", session_id=session_id)
            return system_message, ""

        if current_hash == canonical_hash:
            return canonical, ""

        similarity = _jaccard_similarity(canonical, system_message)

        if similarity < 0.30:
            await self._db.set_kv(session_id, "canonical_system", system_message)
            await self._db.set_kv(session_id, "canonical_system_hash", current_hash)
            logger.info(
                "system_stabilizer_canonical_replaced",
                session_id=session_id,
                similarity=round(similarity, 3),
            )
            return system_message, ""

        delta = _extract_delta(canonical, system_message)
        logger.info(
            "system_stabilizer_delta_extracted",
            session_id=session_id,
            similarity=round(similarity, 3),
            delta_length=len(delta),
        )
        return canonical, delta
