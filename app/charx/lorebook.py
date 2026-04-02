import re

import structlog

from app.charx.schemas import LorebookEntry

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


class LorebookEngine:
    def __init__(self, entries: list[LorebookEntry]) -> None:
        self._constant: list[LorebookEntry] = []
        self._triggered: list[LorebookEntry] = []
        self._keyword_index: dict[str, list[LorebookEntry]] = {}

        self.build_index(entries)

    def build_index(self, entries: list[LorebookEntry]) -> None:
        self._constant = []
        self._triggered = []
        self._keyword_index = {}

        for entry in entries:
            if not entry.enabled:
                continue

            if entry.constant or not entry.keys:
                self._constant.append(entry)
            else:
                self._triggered.append(entry)
                for key in entry.keys:
                    normalized = key.strip().lower()
                    if normalized:
                        self._keyword_index.setdefault(normalized, []).append(entry)

        self._constant.sort(key=lambda e: e.insertion_order)
        self._triggered.sort(key=lambda e: e.insertion_order)

        logger.info(
            "lorebook_index_built",
            constant_count=len(self._constant),
            triggered_count=len(self._triggered),
            keyword_count=len(self._keyword_index),
        )

    def get_constant_entries(self) -> list[LorebookEntry]:
        return list(self._constant)

    def match_triggered(
        self,
        messages: list[dict],
        last_n: int = 3,
    ) -> list[LorebookEntry]:
        recent = messages[-last_n:] if len(messages) > last_n else messages
        text = " ".join(
            msg.get("content", "") for msg in recent if isinstance(msg.get("content"), str)
        ).lower()

        if not text.strip():
            return []

        matched: dict[int, LorebookEntry] = {}

        for keyword, entries in self._keyword_index.items():
            pattern = re.compile(re.escape(keyword), re.IGNORECASE)
            if pattern.search(text):
                for entry in entries:
                    entry_id = id(entry)
                    if entry_id not in matched:
                        matched[entry_id] = entry

        result = sorted(matched.values(), key=lambda e: e.insertion_order)

        constant_ids = {id(e) for e in self._constant}
        result = [e for e in result if id(e) not in constant_ids]

        logger.info(
            "lorebook_keyword_matched",
            keywords_scanned=len(self._keyword_index),
            entries_matched=len(result),
        )

        return result
