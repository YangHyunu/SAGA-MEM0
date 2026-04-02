import structlog
import tiktoken

from app.memory.base import MemoryBackend

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

COMPRESS_THRESHOLD_RATIO = 0.50
COMPRESS_TARGET_RATIO = 0.85
MIN_REMAINING_TURNS = 5
MIN_CHUNK_TURNS = 3
MAX_CHUNK_TURNS = 8
CHUNK_PREFIX = "[Yang-Ban: 이전 대화 요약"

_encoder: tiktoken.Encoding | None = None


def count_tokens(text: str) -> int:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.encoding_for_model("gpt-4")
    return len(_encoder.encode(text))


def _msg_token_count(msg: dict) -> int:
    content = msg.get("content") or ""
    return count_tokens(content)


def _is_immutable(msg: dict) -> bool:
    content = msg.get("content") or ""
    return content.startswith(CHUNK_PREFIX)


class MessageCompressor:
    def __init__(self, context_limit: int = 128000) -> None:
        self.context_limit = context_limit

    async def compress(
        self,
        messages: list[dict],
        memory: MemoryBackend,
    ) -> list[dict]:
        """토큰 초과 시 오래된 턴을 summary chunk로 교체.

        동작:
        1. total_tokens 계산
        2. threshold = context_limit * COMPRESS_THRESHOLD_RATIO
        3. total > threshold이면 압축 시작
        4. target = threshold * COMPRESS_TARGET_RATIO
        5. system 메시지 보존, 최소 MIN_REMAINING_TURNS 턴 보존
        6. 오래된 턴을 MIN_CHUNK_TURNS~MAX_CHUNK_TURNS 단위로 그룹화
        7. 각 chunk → memory.search()로 해당 기간 기억 조회 → 요약 텍스트
        8. 원본 메시지를 [요약 user + assistant 쌍]으로 교체
        9. 이미 CHUNK_PREFIX로 시작하는 메시지는 immutable — 건드리지 않음
        """
        total_tokens = sum(_msg_token_count(m) for m in messages)
        threshold = int(self.context_limit * COMPRESS_THRESHOLD_RATIO)

        if total_tokens <= threshold:
            return messages

        target_tokens = int(threshold * COMPRESS_TARGET_RATIO)

        logger.info(
            "compression_started",
            total_tokens=total_tokens,
            threshold=threshold,
            target_tokens=target_tokens,
        )

        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # 대화 턴 쌍(user+assistant) 구성, 홀수 메시지는 마지막에 남김
        turns: list[tuple[dict, dict]] = []
        i = 0
        while i + 1 < len(non_system):
            u = non_system[i]
            a = non_system[i + 1]
            if u.get("role") == "user" and a.get("role") == "assistant":
                turns.append((u, a))
                i += 2
            else:
                i += 1

        tail_msgs = non_system[len(turns) * 2 :]

        # immutable 턴(이미 압축된 chunk) 과 압축 가능한 턴 분리
        compressible: list[tuple[int, tuple[dict, dict]]] = []
        for idx, turn in enumerate(turns):
            u, a = turn
            if _is_immutable(u) or _is_immutable(a):
                continue
            compressible.append((idx, turn))

        # 최소 MIN_REMAINING_TURNS 턴은 보존 — 뒤쪽 턴부터 보존
        preserve_count = MIN_REMAINING_TURNS
        candidate_indices = [
            (idx, turn)
            for idx, turn in compressible
            if idx < len(turns) - preserve_count
        ]

        if not candidate_indices:
            logger.info("compression_skipped_no_candidates")
            return messages

        # 청크 단위로 그룹화
        chunks: list[list[tuple[int, tuple[dict, dict]]]] = []
        chunk: list[tuple[int, tuple[dict, dict]]] = []
        for item in candidate_indices:
            chunk.append(item)
            if len(chunk) >= MAX_CHUNK_TURNS:
                chunks.append(chunk)
                chunk = []
        if len(chunk) >= MIN_CHUNK_TURNS:
            chunks.append(chunk)
        elif chunk:
            # MIN_CHUNK_TURNS 미만이면 앞 청크에 합치거나 버림
            if chunks:
                chunks[-1].extend(chunk)
            # else: 압축 대상 없음으로 처리

        if not chunks:
            logger.info("compression_skipped_chunk_too_small")
            return messages

        # 각 청크를 memory.search로 요약
        replacement_map: dict[int, list[dict]] = {}
        for ch in chunks:
            first_idx = ch[0][0]
            # 해당 청크 내 텍스트를 쿼리로 사용
            sample_content = (ch[0][1][0].get("content") or "")[:200]
            query = f"대화 요약: {sample_content}" if sample_content else "최근 대화 요약"

            try:
                result = await memory.search(query, "system", limit=3)
                mem_texts: list[str] = []
                if isinstance(result, dict):
                    for item in result.get("results", []):
                        t = item.get("memory") or item.get("text") or ""
                        if t:
                            mem_texts.append(t)
            except Exception:
                logger.exception("compression_memory_search_failed", chunk_start=first_idx)
                mem_texts = []

            turn_count = len(ch)
            summary_lines = [f"{CHUNK_PREFIX} ({turn_count}턴)]"]
            for t in mem_texts:
                summary_lines.append(f"- {t}")
            if not mem_texts:
                summary_lines.append("(요약 정보 없음)")

            summary_text = "\n".join(summary_lines)

            summary_pair = [
                {"role": "user", "content": summary_text},
                {"role": "assistant", "content": "(이전 대화 내용이 요약되었습니다.)"},
            ]
            # 청크의 첫 번째 index에 요약 쌍 삽입
            replacement_map[first_idx] = summary_pair
            # 나머지 indices는 제거 표시 (None)
            for i_item, (idx, _) in enumerate(ch):
                if i_item > 0:
                    replacement_map[idx] = []

        # 새 turns 목록 구성
        new_non_system: list[dict] = []
        for idx, turn in enumerate(turns):
            if idx in replacement_map:
                new_non_system.extend(replacement_map[idx])
            else:
                new_non_system.extend(list(turn))
        new_non_system.extend(tail_msgs)

        result_messages = system_msgs + new_non_system
        new_total = sum(_msg_token_count(m) for m in result_messages)

        logger.info(
            "compression_complete",
            original_tokens=total_tokens,
            compressed_tokens=new_total,
            chunks_replaced=len(chunks),
        )

        return result_messages
