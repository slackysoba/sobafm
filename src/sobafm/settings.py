"""Persisted per-server settings: a small SQLite store on one dedicated thread.

Every database call runs on the store's single worker thread, which also creates the
connection, so SQLite itself refuses any use from the event loop. The voice callback
never touches this module: it reads a gain value the adapter sets.

Reads don't wait. `get` returns a validated in-memory copy loaded at startup, which
only the worker changes and only after a write has committed. `update` validates, then
queues one write and awaits it. A cancelled caller doesn't abandon a write that has
started: it still commits and updates the copy, so the two never disagree.

Startup fails closed: `open` proves the database can be read and written, and refuses
a file it can't use rather than letting a server silently forget its settings. Errors
name a safe kind, never a path or a value.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MIN_GENERATED_MINUTES = 5
DEFAULT_GENERATED_MINUTES = 60
MAX_GENERATED_MINUTES = 240
MIN_VOLUME_PERCENT = 1
DEFAULT_VOLUME_PERCENT = 50
MAX_VOLUME_PERCENT = 100
MIN_COOLDOWN_SECONDS = 0
DEFAULT_COOLDOWN_SECONDS = 30
MAX_COOLDOWN_SECONDS = 600

# Only servers that changed a setting have a row. This bounds the memory the copy can
# take under the service limit; a further server keeps the defaults and is told so.
MAX_SERVERS = 1_000
MAX_QUEUED_WRITES = 8
# How long SQLite waits for a lock held by another connection, such as an operator's
# sqlite3 shell, before reporting it busy.
LOCK_WAIT_SECONDS = 5.0
# Bound on joining queued writes at shutdown, within Compose's ten-second stop grace.
CLOSE_SECONDS = 5.0
SCHEMA_VERSION = 1

_CREATE_TABLE = f"""
    CREATE TABLE IF NOT EXISTS server_settings (
        guild_id INTEGER PRIMARY KEY CHECK (guild_id > 0),
        generated_minutes INTEGER NOT NULL
            CHECK (generated_minutes BETWEEN {MIN_GENERATED_MINUTES} AND {MAX_GENERATED_MINUTES}),
        volume_percent INTEGER NOT NULL
            CHECK (volume_percent BETWEEN {MIN_VOLUME_PERCENT} AND {MAX_VOLUME_PERCENT}),
        cooldown_seconds INTEGER NOT NULL
            CHECK (cooldown_seconds BETWEEN {MIN_COOLDOWN_SECONDS} AND {MAX_COOLDOWN_SECONDS})
    ) STRICT
"""
_SELECT = """
    SELECT guild_id, generated_minutes, volume_percent, cooldown_seconds
    FROM server_settings LIMIT ?
"""
_UPSERT = """
    INSERT INTO server_settings (guild_id, generated_minutes, volume_percent, cooldown_seconds)
    VALUES (?, ?, ?, ?)
    ON CONFLICT (guild_id) DO UPDATE SET
        generated_minutes = excluded.generated_minutes,
        volume_percent = excluded.volume_percent,
        cooldown_seconds = excluded.cooldown_seconds
"""


class SettingsError(Exception):
    """The store can't do what was asked; the message is a safe kind only."""


def _kind(error: sqlite3.Error) -> str:
    """SQLite's error name, such as SQLITE_READONLY: unlike its message, it has no path."""
    return getattr(error, "sqlite_errorname", None) or type(error).__name__


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """One server's validated settings; generated duration is unused until `/freestyle`."""

    generated_minutes: int = DEFAULT_GENERATED_MINUTES
    volume_percent: int = DEFAULT_VOLUME_PERCENT
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS

    def __post_init__(self) -> None:
        for name, value, low, high in (
            (
                "generated_minutes",
                self.generated_minutes,
                MIN_GENERATED_MINUTES,
                MAX_GENERATED_MINUTES,
            ),
            ("volume_percent", self.volume_percent, MIN_VOLUME_PERCENT, MAX_VOLUME_PERCENT),
            (
                "cooldown_seconds",
                self.cooldown_seconds,
                MIN_COOLDOWN_SECONDS,
                MAX_COOLDOWN_SECONDS,
            ),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be a whole number from {low} to {high}")

    def changed(
        self,
        *,
        generated_minutes: int | None = None,
        volume_percent: int | None = None,
        cooldown_seconds: int | None = None,
    ) -> ServerSettings:
        """These settings with the supplied values changed; raises ValueError when invalid."""
        return ServerSettings(
            self.generated_minutes if generated_minutes is None else generated_minutes,
            self.volume_percent if volume_percent is None else volume_percent,
            self.cooldown_seconds if cooldown_seconds is None else cooldown_seconds,
        )


DEFAULTS = ServerSettings()


def _open_database(path: Path) -> tuple[sqlite3.Connection, dict[int, ServerSettings]]:
    """Open and load the database on the calling thread, or raise `SettingsError`."""
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=LOCK_WAIT_SECONDS, autocommit=True)
        # The root filesystem is read-only, so SQLite's temporary files can't live there.
        connection.execute("PRAGMA temp_store = MEMORY")
        version: int = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise SettingsError("unknown_schema_version")
        # Setting the version always writes, so committing it proves that the file and its
        # journal are writable, even when the table already exists. A refusal before the
        # commit rolls back, which leaves a file SobaFM can't use exactly as it was.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_CREATE_TABLE)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        rows = connection.execute(_SELECT, (MAX_SERVERS + 1,)).fetchall()
        if len(rows) > MAX_SERVERS:
            raise SettingsError("too_many_servers")
        settings: dict[int, ServerSettings] = {}
        for guild_id, generated_minutes, volume_percent, cooldown_seconds in rows:
            if type(guild_id) is not int or guild_id <= 0:
                raise SettingsError("invalid_row")
            settings[guild_id] = ServerSettings(generated_minutes, volume_percent, cooldown_seconds)
        connection.execute("COMMIT")
    except (SettingsError, sqlite3.Error, ValueError) as exc:
        if connection is not None:
            connection.close()
        if isinstance(exc, SettingsError):
            raise
        kind = _kind(exc) if isinstance(exc, sqlite3.Error) else "invalid_row"
        raise SettingsError(kind) from exc
    return connection, settings


class SettingsStore:
    """Owns the database connection, its worker thread and the validated copy.

    Create one with `open`; `close` joins queued writes and closes the connection.
    """

    def __init__(
        self,
        executor: ThreadPoolExecutor,
        connection: sqlite3.Connection,
        settings: dict[int, ServerSettings],
    ) -> None:
        self._executor = executor
        self._connection = connection
        self._settings = settings
        self._queue_slots = threading.BoundedSemaphore(MAX_QUEUED_WRITES)
        self._closed = False

    @classmethod
    def open(cls, path: Path) -> SettingsStore:
        """Open, verify and load the database; blocks, so call it before the event loop."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sobafm-settings")
        try:
            connection, settings = executor.submit(_open_database, path).result()
        except BaseException:
            executor.shutdown()
            raise
        log.info("settings database opened: servers=%d", len(settings))
        return cls(executor, connection, settings)

    def get(self, guild_id: int) -> ServerSettings:
        """This server's settings, or the defaults; never waits and touches no database."""
        return self._settings.get(guild_id, DEFAULTS)

    async def update(
        self,
        guild_id: int,
        *,
        generated_minutes: int | None = None,
        volume_percent: int | None = None,
        cooldown_seconds: int | None = None,
    ) -> ServerSettings:
        """Durably change the supplied values and return the result.

        Raises ValueError for an invalid value, before anything is queued, and
        `SettingsError` when the write can't happen, which leaves everything unchanged.
        """
        self.get(guild_id).changed(
            generated_minutes=generated_minutes,
            volume_percent=volume_percent,
            cooldown_seconds=cooldown_seconds,
        )
        if self._closed:
            raise SettingsError("closed")
        if not self._queue_slots.acquire(blocking=False):
            raise SettingsError("busy")
        write = self._executor.submit(
            self._write, guild_id, generated_minutes, volume_percent, cooldown_seconds
        )
        write.add_done_callback(lambda _: self._queue_slots.release())
        return await asyncio.wrap_future(write)

    def _write(
        self,
        guild_id: int,
        generated_minutes: int | None,
        volume_percent: int | None,
        cooldown_seconds: int | None,
    ) -> ServerSettings:
        """Commit one change; runs on the worker, so writes apply in the order they queued."""
        if guild_id not in self._settings and len(self._settings) >= MAX_SERVERS:
            raise SettingsError("too_many_servers")
        updated = self.get(guild_id).changed(
            generated_minutes=generated_minutes,
            volume_percent=volume_percent,
            cooldown_seconds=cooldown_seconds,
        )
        try:
            # In autocommit mode this one statement is its own durable transaction.
            self._connection.execute(
                _UPSERT,
                (
                    guild_id,
                    updated.generated_minutes,
                    updated.volume_percent,
                    updated.cooldown_seconds,
                ),
            )
        except sqlite3.Error as exc:
            raise SettingsError(_kind(exc)) from exc
        self._settings[guild_id] = updated
        return updated

    async def close(self) -> bool:
        """Refuse new writes, join queued ones and close; report whether it all finished."""
        if self._closed:
            return True
        self._closed = True
        closing = asyncio.wrap_future(self._executor.submit(self._connection.close))
        done, _ = await asyncio.wait({closing}, timeout=CLOSE_SECONDS)
        self._executor.shutdown(wait=False)
        if not done:
            log.error("settings database did not close in time")
            return False
        if (error := closing.exception()) is not None:
            log.error("settings database close failed: %s", type(error).__name__)
            return False
        return True
