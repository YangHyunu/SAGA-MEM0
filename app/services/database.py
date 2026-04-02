from pathlib import Path

import aiosqlite
import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

DB_PATH = Path("./yangban.db")

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS session_kv (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, key)
);

CREATE TABLE IF NOT EXISTS turn_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    user_content TEXT,
    assistant_content TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, db_path: str | Path = DB_PATH) -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.executescript(_CREATE_TABLES)
        await self._conn.commit()
        logger.info("database_connected", path=self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def get_kv(self, session_id: str, key: str) -> str | None:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        cursor = await self._conn.execute(
            "SELECT value FROM session_kv WHERE session_id = ? AND key = ?",
            (session_id, key),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_kv(self, session_id: str, key: str, value: str) -> None:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        await self._conn.execute(
            "INSERT OR REPLACE INTO session_kv (session_id, key, value) VALUES (?, ?, ?)",
            (session_id, key, value),
        )
        await self._conn.commit()

    async def get_turn_count(self, session_id: str) -> int:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM turn_log WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def log_turn(
        self,
        session_id: str,
        turn_number: int,
        user_content: str,
        assistant_content: str,
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        await self._conn.execute(
            "INSERT INTO turn_log (session_id, turn_number, user_content, assistant_content) VALUES (?, ?, ?, ?)",
            (session_id, turn_number, user_content, assistant_content),
        )
        await self._conn.commit()
