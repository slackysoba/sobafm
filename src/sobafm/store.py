"""Per-server state in SQLite: one row per guild (ADR-0003)."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER,
    settings   TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
)
"""


class Store:
    """Remembers each server's voice channel. Calls run in a worker thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await self._execute(SCHEMA)

    async def remember_channel(self, guild_id: int, channel_id: int) -> None:
        await self._execute(
            "INSERT INTO guild (guild_id, channel_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE "
            "SET channel_id = excluded.channel_id, updated_at = excluded.updated_at",
            (guild_id, channel_id, _now()),
        )

    async def forget_channel(self, guild_id: int) -> None:
        await self._execute(
            "UPDATE guild SET channel_id = NULL, updated_at = ? WHERE guild_id = ?",
            (_now(), guild_id),
        )

    async def remembered_channel(self, guild_id: int) -> int | None:
        rows = await self._execute("SELECT channel_id FROM guild WHERE guild_id = ?", (guild_id,))
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    async def _execute(self, sql: str, parameters: tuple[int | str, ...] = ()) -> list[Any]:
        async with self._lock:  # one statement at a time, so writes commit in call order
            return await asyncio.to_thread(self._run, sql, parameters)

    def _run(self, sql: str, parameters: tuple[int | str, ...]) -> list[Any]:
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute(sql, parameters).fetchall()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
