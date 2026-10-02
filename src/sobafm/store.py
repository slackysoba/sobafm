"""Per-server state in SQLite: one row per guild (ADR-0003)."""

import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

log = logging.getLogger(__name__)

# Parses a stored settings document into its raw fields, so an invalid one can fall back alone.
SETTINGS_DOCUMENT = TypeAdapter(dict[str, object])

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER,
    settings   TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
)
"""


class GuildSettings(BaseModel):
    """A server's settings (SET-1 to SET-3).

    Stored as JSON: missing fields take their defaults and unknown fields are ignored, so adding
    a setting needs no migration.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    duration_minutes: int = Field(default=60, ge=5, le=240)
    volume_percent: int = Field(default=50, ge=1, le=100)
    cooldown_seconds: int = Field(default=30, ge=0, le=600)

    @property
    def volume(self) -> float:
        """The volume as linear gain; 1.0 leaves the level unchanged."""
        return self.volume_percent / 100


class Store:
    """Remembers each server's voice channel and settings. Calls run in a worker thread."""

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

    async def settings(self, guild_id: int) -> GuildSettings:
        rows = await self._execute("SELECT settings FROM guild WHERE guild_id = ?", (guild_id,))
        if not rows:
            return GuildSettings()
        try:
            fields = SETTINGS_DOCUMENT.validate_json(rows[0][0])
        except ValidationError:
            log.warning(
                "Stored settings for server %d are unreadable; using the defaults", guild_id
            )
            return GuildSettings()
        try:
            return GuildSettings.model_validate(fields)
        except ValidationError as error:
            invalid = {str(problem["loc"][0]) for problem in error.errors() if problem["loc"]}
            log.warning(
                "Stored settings for server %d have invalid %s; using the defaults for them",
                guild_id,
                ", ".join(sorted(invalid)),
            )
            return GuildSettings.model_validate(
                {name: value for name, value in fields.items() if name not in invalid}
            )

    async def save_settings(self, guild_id: int, settings: GuildSettings) -> None:
        await self._execute(
            "INSERT INTO guild (guild_id, settings, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE "
            "SET settings = excluded.settings, updated_at = excluded.updated_at",
            (guild_id, settings.model_dump_json(), _now()),
        )

    async def _execute(self, sql: str, parameters: tuple[int | str, ...] = ()) -> list[Any]:
        async with self._lock:  # one statement at a time, so writes commit in call order
            return await asyncio.to_thread(self._run, sql, parameters)

    def _run(self, sql: str, parameters: tuple[int | str, ...]) -> list[Any]:
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute(sql, parameters).fetchall()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
