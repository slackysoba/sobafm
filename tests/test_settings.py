"""The settings store against real SQLite files, and the pure change cooldown; no sleeps."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from sobafm import settings as settings_module
from sobafm.cooldown import Cooldown
from sobafm.settings import (
    DEFAULTS,
    MAX_QUEUED_WRITES,
    ServerSettings,
    SettingsError,
    SettingsStore,
)

GUILD = 1234
OTHER = 5678


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "sobafm.db"


@contextmanager
def lock_held(path: Path) -> Generator[sqlite3.Connection]:
    """Hold the database's write lock from another connection, as an operator's shell could."""
    blocker = sqlite3.connect(path, autocommit=True)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        yield blocker
    finally:
        blocker.close()


def rows(path: Path) -> list[tuple[int, int, int, int]]:
    """What is actually on disk, read through a separate connection."""
    reader = sqlite3.connect(path, autocommit=True)
    try:
        return reader.execute(
            "SELECT guild_id, generated_minutes, volume_percent, cooldown_seconds "
            "FROM server_settings ORDER BY guild_id"
        ).fetchall()
    finally:
        reader.close()


class HeldWrites:
    """Holds the store's next writes on its worker, so a schedule can pause them."""

    def __init__(self, store: SettingsStore) -> None:
        self.started = threading.Event()
        self.gate = threading.Event()
        real = store._write  # pyright: ignore[reportPrivateUsage]

        def write(
            guild_id: int,
            generated_minutes: int | None,
            volume_percent: int | None,
            cooldown_seconds: int | None,
        ) -> ServerSettings:
            self.started.set()
            assert self.gate.wait(10)
            return real(guild_id, generated_minutes, volume_percent, cooldown_seconds)

        store._write = write  # pyright: ignore[reportPrivateUsage]


# Values and ranges.


def test_the_defaults_are_the_required_ones() -> None:
    assert (DEFAULTS.generated_minutes, DEFAULTS.volume_percent, DEFAULTS.cooldown_seconds) == (
        60,
        50,
        30,
    )


@pytest.mark.parametrize(
    ("name", "valid", "invalid"),
    [
        ("generated_minutes", (5, 240), (4, 241, 0, -5)),
        ("volume_percent", (1, 100), (0, 101, -1)),
        ("cooldown_seconds", (0, 600), (-1, 601)),
    ],
)
def test_each_range_is_enforced_at_its_edges(
    name: str, valid: tuple[int, ...], invalid: tuple[int, ...]
) -> None:
    for value in valid:
        assert getattr(ServerSettings(**{name: value}), name) == value
        assert getattr(DEFAULTS.changed(**{name: value}), name) == value
    for value in invalid:
        with pytest.raises(ValueError, match=name):
            ServerSettings(**{name: value})
        with pytest.raises(ValueError, match=name):
            DEFAULTS.changed(**{name: value})


@pytest.mark.parametrize("value", [True, 50.0, "50"])
def test_only_whole_numbers_are_accepted(value: object) -> None:
    with pytest.raises(ValueError, match="volume_percent"):
        ServerSettings(volume_percent=value)  # pyright: ignore[reportArgumentType]


# Persistence.


def test_settings_survive_a_reopen(path: Path) -> None:
    async def write() -> None:
        store = SettingsStore.open(path)
        assert await store.update(GUILD, volume_percent=20) == ServerSettings(60, 20, 30)
        # A later change keeps the values it doesn't name.
        assert await store.update(GUILD, generated_minutes=5, cooldown_seconds=0) == ServerSettings(
            5, 20, 0
        )
        assert await store.close() is True

    asyncio.run(write())
    assert rows(path) == [(GUILD, 5, 20, 0)]

    async def read() -> None:
        store = SettingsStore.open(path)
        assert store.get(GUILD) == ServerSettings(5, 20, 0)
        assert store.get(OTHER) == DEFAULTS  # settings are per server
        assert await store.close() is True

    asyncio.run(read())


def test_an_invalid_value_changes_nothing(path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        with pytest.raises(ValueError, match="volume_percent"):
            await store.update(GUILD, volume_percent=101)
        assert store.get(GUILD) == DEFAULTS
        assert await store.close() is True

    asyncio.run(scenario())
    assert rows(path) == []


def test_a_failed_write_leaves_the_settings_unchanged(path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        await store.update(GUILD, volume_percent=20)
        vandal = sqlite3.connect(path, autocommit=True)
        vandal.execute("DROP TABLE server_settings")
        vandal.close()
        with pytest.raises(SettingsError) as error:
            await store.update(GUILD, volume_percent=80)
        assert str(error.value) == "SQLITE_ERROR" and str(path) not in str(error.value)
        assert store.get(GUILD).volume_percent == 20
        assert await store.close() is True

    asyncio.run(scenario())


# What happens at startup when the database can't be used.


def garbage(path: Path) -> None:
    path.write_bytes(b"not a database, and long enough to be read as one" * 100)


def newer_schema(path: Path) -> None:
    connection = sqlite3.connect(path, autocommit=True)
    connection.execute(f"PRAGMA user_version = {settings_module.SCHEMA_VERSION + 1}")
    connection.close()


def tampered(*row: object) -> Callable[[Path], None]:
    """Prepare a table with no constraints holding ``row``, as another program could write."""

    def prepare(path: Path) -> None:
        connection = sqlite3.connect(path, autocommit=True)
        connection.execute(
            "CREATE TABLE server_settings (guild_id, generated_minutes, volume_percent, "
            "cooldown_seconds)"
        )
        connection.execute("INSERT INTO server_settings VALUES (?, ?, ?, ?)", row)
        connection.execute("PRAGMA user_version = 1")
        connection.close()

    return prepare


@pytest.mark.parametrize(
    ("prepare", "kind"),
    [
        (garbage, "SQLITE_NOTADB"),
        (newer_schema, "unknown_schema_version"),
        (tampered(GUILD, 60, 101, 30), "invalid_row"),
        (tampered(GUILD, 60, "loud", 30), "invalid_row"),
        (tampered(-1, 60, 50, 30), "invalid_row"),
        (tampered("x", 60, 50, 30), "invalid_row"),
    ],
)
def test_a_file_that_cant_be_used_is_refused_at_startup(
    path: Path, prepare: Callable[[Path], None], kind: str
) -> None:
    prepare(path)
    before = path.read_bytes()
    with pytest.raises(SettingsError) as error:
        SettingsStore.open(path)
    assert str(error.value) == kind
    assert path.read_bytes() == before  # a refused file is left as it was


def test_a_missing_directory_is_refused_without_naming_it(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as error:
        SettingsStore.open(tmp_path / "absent" / "sobafm.db")
    assert str(error.value) == "SQLITE_CANTOPEN"
    assert str(tmp_path) not in str(error.value)


def test_a_directory_in_place_of_the_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="SQLITE_CANTOPEN"):
        SettingsStore.open(tmp_path)


def test_every_startup_commits_a_real_write(path: Path) -> None:
    """The table is already there the second time, and SQLite still counts a commit.

    This is what notices a directory where the file can be read but its journal can't
    be created. Bytes 24 to 27 of the header are SQLite's file change counter.
    """

    def commits() -> int:
        return int.from_bytes(path.read_bytes()[24:28])

    asyncio.run(SettingsStore.open(path).close())
    first = commits()
    asyncio.run(SettingsStore.open(path).close())
    assert commits() == first + 1


def test_a_database_that_refuses_writes_is_refused_even_when_it_already_exists(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(SettingsStore.open(path).close())
    connect = sqlite3.connect

    def read_only(database: Path, *, timeout: float, autocommit: bool) -> sqlite3.Connection:
        connection = connect(database, timeout=timeout, autocommit=autocommit)
        connection.execute("PRAGMA query_only = ON")
        return connection

    monkeypatch.setattr(settings_module.sqlite3, "connect", read_only)
    with pytest.raises(SettingsError, match="SQLITE_READONLY"):
        SettingsStore.open(path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_read_only_directory_is_refused_at_startup(tmp_path: Path) -> None:
    """The same check at the filesystem: SQLite can't create its journal beside the file."""
    path = tmp_path / "sobafm.db"
    asyncio.run(SettingsStore.open(path).close())
    tmp_path.chmod(0o555)
    try:
        with pytest.raises(SettingsError):
            SettingsStore.open(path)
    finally:
        tmp_path.chmod(0o755)


def test_too_many_stored_servers_are_refused_at_startup(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fill() -> None:
        store = SettingsStore.open(path)
        await store.update(1, volume_percent=20)
        await store.update(2, volume_percent=20)
        await store.close()

    asyncio.run(fill())
    monkeypatch.setattr(settings_module, "MAX_SERVERS", 1)
    with pytest.raises(SettingsError, match="too_many_servers"):
        SettingsStore.open(path)


def test_a_further_server_keeps_the_defaults_at_the_limit(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_module, "MAX_SERVERS", 1)

    async def scenario() -> None:
        store = SettingsStore.open(path)
        await store.update(GUILD, volume_percent=20)
        with pytest.raises(SettingsError, match="too_many_servers"):
            await store.update(OTHER, volume_percent=20)
        assert store.get(OTHER) == DEFAULTS
        await store.update(GUILD, volume_percent=30)  # an existing server can still change
        assert await store.close() is True

    asyncio.run(scenario())
    assert rows(path) == [(GUILD, 60, 30, 30)]


# The remembered voice channel.

CHANNEL = 9_001
OTHER_CHANNEL = 9_002
# The schema version 1 wrote, exactly as #126 shipped it.
VERSION_1_SCHEMA = """
    CREATE TABLE IF NOT EXISTS server_settings (
        guild_id INTEGER PRIMARY KEY CHECK (guild_id > 0),
        generated_minutes INTEGER NOT NULL
            CHECK (generated_minutes BETWEEN 5 AND 240),
        volume_percent INTEGER NOT NULL
            CHECK (volume_percent BETWEEN 1 AND 100),
        cooldown_seconds INTEGER NOT NULL
            CHECK (cooldown_seconds BETWEEN 0 AND 600)
    ) STRICT
"""


def channel_rows(path: Path) -> list[tuple[int, int]]:
    reader = sqlite3.connect(path, autocommit=True)
    try:
        return reader.execute(
            "SELECT guild_id, channel_id FROM voice_channels ORDER BY guild_id"
        ).fetchall()
    finally:
        reader.close()


def schema_version(path: Path) -> int:
    reader = sqlite3.connect(path, autocommit=True)
    try:
        return reader.execute("PRAGMA user_version").fetchone()[0]
    finally:
        reader.close()


def table_names(path: Path) -> set[str]:
    reader = sqlite3.connect(path, autocommit=True)
    try:
        rows = reader.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {name for (name,) in rows}
    finally:
        reader.close()


def version_1_database(path: Path) -> None:
    connection = sqlite3.connect(path, autocommit=True)
    connection.execute(VERSION_1_SCHEMA)
    connection.execute("INSERT INTO server_settings VALUES (?, 90, 20, 5)", (GUILD,))
    connection.execute("PRAGMA user_version = 1")
    connection.close()


def test_a_channel_survives_a_reopen_and_forgetting_it_does_too(path: Path) -> None:
    async def remember() -> None:
        store = SettingsStore.open(path)
        assert store.channel(GUILD) is None and store.channels() == {}
        await store.remember_channel(GUILD, CHANNEL)
        await store.remember_channel(OTHER, OTHER_CHANNEL)
        await store.remember_channel(GUILD, OTHER_CHANNEL)  # the latest channel replaces it
        assert store.channel(GUILD) == OTHER_CHANNEL
        assert await store.close() is True

    async def forget() -> None:
        store = SettingsStore.open(path)
        assert store.channels() == {GUILD: OTHER_CHANNEL, OTHER: OTHER_CHANNEL}
        await store.forget_channel(GUILD)
        await store.forget_channel(GUILD)  # forgetting nothing changes nothing
        assert store.channel(GUILD) is None
        assert await store.close() is True

    asyncio.run(remember())
    assert channel_rows(path) == sorted([(GUILD, OTHER_CHANNEL), (OTHER, OTHER_CHANNEL)])
    asyncio.run(forget())
    assert channel_rows(path) == [(OTHER, OTHER_CHANNEL)]

    async def reopened() -> None:
        store = SettingsStore.open(path)
        assert store.channels() == {OTHER: OTHER_CHANNEL}
        assert await store.close() is True

    asyncio.run(reopened())


def test_a_channel_and_settings_are_independent(path: Path) -> None:
    """Remembering a channel makes no settings row, so it can't use up that limit."""

    async def scenario() -> None:
        store = SettingsStore.open(path)
        await store.remember_channel(GUILD, CHANNEL)
        assert store.get(GUILD) == DEFAULTS
        await store.update(GUILD, volume_percent=20)
        await store.forget_channel(GUILD)
        assert store.get(GUILD).volume_percent == 20
        assert await store.close() is True

    asyncio.run(scenario())
    assert rows(path) == [(GUILD, 60, 20, 30)] and channel_rows(path) == []


def test_a_version_1_database_upgrades_in_place_and_keeps_its_settings(path: Path) -> None:
    version_1_database(path)
    assert schema_version(path) == 1 and table_names(path) == {"server_settings"}

    async def upgraded() -> None:
        store = SettingsStore.open(path)
        assert store.get(GUILD) == ServerSettings(90, 20, 5)
        assert store.channels() == {}
        await store.remember_channel(GUILD, CHANNEL)
        assert await store.close() is True

    asyncio.run(upgraded())
    assert schema_version(path) == settings_module.SCHEMA_VERSION == 2
    assert rows(path) == [(GUILD, 90, 20, 5)] and channel_rows(path) == [(GUILD, CHANNEL)]

    async def again() -> None:  # an upgraded file opens like any other
        store = SettingsStore.open(path)
        assert store.channel(GUILD) == CHANNEL and store.get(GUILD).cooldown_seconds == 5
        assert await store.close() is True

    asyncio.run(again())


def test_a_refused_upgrade_leaves_the_version_1_file_as_it_was(path: Path) -> None:
    tampered(GUILD, 60, 101, 30)(path)
    before = path.read_bytes()
    with pytest.raises(SettingsError, match="invalid_row"):
        SettingsStore.open(path)
    assert path.read_bytes() == before
    assert schema_version(path) == 1 and "voice_channels" not in table_names(path)


def tampered_channel(guild_id: object, channel_id: object) -> Callable[[Path], None]:
    def prepare(path: Path) -> None:
        asyncio.run(SettingsStore.open(path).close())
        connection = sqlite3.connect(path, autocommit=True)
        connection.execute("DROP TABLE voice_channels")
        connection.execute("CREATE TABLE voice_channels (guild_id, channel_id)")
        connection.execute("INSERT INTO voice_channels VALUES (?, ?)", (guild_id, channel_id))
        connection.close()

    return prepare


@pytest.mark.parametrize(
    "prepare",
    [
        tampered_channel(GUILD, 0),
        tampered_channel(GUILD, -5),
        tampered_channel(GUILD, "x"),
        tampered_channel(GUILD, None),
        tampered_channel(-1, CHANNEL),
        tampered_channel("x", CHANNEL),
    ],
)
def test_an_invalid_channel_row_is_refused_at_startup(
    path: Path, prepare: Callable[[Path], None]
) -> None:
    prepare(path)
    before = path.read_bytes()
    with pytest.raises(SettingsError, match="invalid_row"):
        SettingsStore.open(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", [0, -1, True, "1"])
def test_an_invalid_channel_id_changes_nothing(path: Path, value: object) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        with pytest.raises(ValueError, match="channel_id"):
            await store.remember_channel(GUILD, value)  # pyright: ignore[reportArgumentType]
        assert store.channel(GUILD) is None
        assert await store.close() is True

    asyncio.run(scenario())
    assert channel_rows(path) == []


def test_a_failed_channel_write_leaves_what_was_remembered(path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        await store.remember_channel(GUILD, CHANNEL)
        vandal = sqlite3.connect(path, autocommit=True)
        vandal.execute("DROP TABLE voice_channels")
        vandal.close()
        with pytest.raises(SettingsError, match="SQLITE_ERROR"):
            await store.remember_channel(GUILD, OTHER_CHANNEL)
        with pytest.raises(SettingsError, match="SQLITE_ERROR"):
            await store.forget_channel(GUILD)
        assert store.channel(GUILD) == CHANNEL
        assert await store.close() is True

    asyncio.run(scenario())


def test_channel_writes_wait_off_the_event_loop(path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        with lock_held(path) as blocker:
            write = asyncio.create_task(store.remember_channel(GUILD, CHANNEL))
            for _ in range(20):
                await asyncio.sleep(0)
            assert not write.done() and store.channel(GUILD) is None
            blocker.execute("COMMIT")
            await write
        assert store.channel(GUILD) == CHANNEL
        assert await store.close() is True

    asyncio.run(scenario())


def test_the_server_limit_applies_to_channels(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "MAX_SERVERS", 1)

    async def scenario() -> None:
        store = SettingsStore.open(path)
        await store.remember_channel(GUILD, CHANNEL)
        with pytest.raises(SettingsError, match="too_many_servers"):
            await store.remember_channel(OTHER, CHANNEL)
        await store.remember_channel(GUILD, OTHER_CHANNEL)  # a known server can still change
        assert store.channels() == {GUILD: OTHER_CHANNEL}
        assert await store.close() is True

    asyncio.run(scenario())
    monkeypatch.setattr(settings_module, "MAX_SERVERS", 0)
    with pytest.raises(SettingsError, match="too_many_servers"):
        SettingsStore.open(path)


def test_a_write_after_close_is_refused_for_channels_too(path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        assert await store.close() is True
        with pytest.raises(SettingsError, match="closed"):
            await store.remember_channel(GUILD, CHANNEL)
        with pytest.raises(SettingsError, match="closed"):
            await store.forget_channel(GUILD)

    asyncio.run(scenario())


# The event loop and the worker thread.


def test_a_write_waits_off_the_event_loop_and_reads_never_wait(path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        with lock_held(path) as blocker:
            write = asyncio.create_task(store.update(GUILD, volume_percent=20))
            for _ in range(20):
                await asyncio.sleep(0)  # the loop keeps running while the write waits
            assert not write.done()
            assert store.get(GUILD) == DEFAULTS  # unchanged until it has committed
            blocker.execute("COMMIT")
            assert (await write).volume_percent == 20
        assert store.get(GUILD).volume_percent == 20
        assert await store.close() is True

    asyncio.run(scenario())


def test_only_the_worker_thread_can_use_the_connection(path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        connection = store._connection  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(sqlite3.ProgrammingError, match="thread"):
            connection.execute("SELECT 1")
        assert await store.close() is True

    asyncio.run(scenario())


def test_writes_apply_in_order_and_the_queue_is_bounded(path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        held = HeldWrites(store)
        writes = [
            asyncio.create_task(store.update(GUILD, volume_percent=volume))
            for volume in range(1, MAX_QUEUED_WRITES + 1)
        ]
        await asyncio.to_thread(held.started.wait, 10)
        with pytest.raises(SettingsError, match="busy"):
            await store.update(GUILD, volume_percent=99)
        held.gate.set()
        await asyncio.gather(*writes)
        assert store.get(GUILD).volume_percent == MAX_QUEUED_WRITES  # the last one queued
        assert (await store.update(GUILD, cooldown_seconds=0)).cooldown_seconds == 0
        assert await store.close() is True

    asyncio.run(scenario())
    assert rows(path) == [(GUILD, 60, MAX_QUEUED_WRITES, 0)]


def test_a_cancelled_caller_does_not_abandon_a_write_already_running(path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        held = HeldWrites(store)
        write = asyncio.create_task(store.update(GUILD, volume_percent=20))
        await asyncio.to_thread(held.started.wait, 10)
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write
        assert store.get(GUILD) == DEFAULTS
        held.gate.set()
        assert await store.close() is True  # joins the write
        assert store.get(GUILD).volume_percent == 20

    asyncio.run(scenario())
    assert rows(path) == [(GUILD, 60, 20, 30)]


def test_a_cancelled_caller_gives_back_a_write_not_yet_running(path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        held = HeldWrites(store)
        running = asyncio.create_task(store.update(GUILD, volume_percent=20))
        await asyncio.to_thread(held.started.wait, 10)
        queued = asyncio.create_task(store.update(GUILD, volume_percent=80))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        held.gate.set()
        await running
        assert await store.close() is True
        assert store.get(GUILD).volume_percent == 20  # the copy and the row agree

    asyncio.run(scenario())
    assert rows(path) == [(GUILD, 60, 20, 30)]


def test_close_joins_a_queued_write_and_refuses_later_ones(path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore.open(path)
        held = HeldWrites(store)
        write = asyncio.create_task(store.update(GUILD, volume_percent=20))
        await asyncio.to_thread(held.started.wait, 10)
        closing = asyncio.create_task(store.close())
        await asyncio.sleep(0)
        with pytest.raises(SettingsError, match="closed"):
            await store.update(GUILD, volume_percent=80)
        assert not closing.done()
        held.gate.set()
        assert await closing is True
        assert (await write).volume_percent == 20
        assert store.get(GUILD).volume_percent == 20  # reads still work after close
        assert await store.close() is True  # closing again changes nothing

    asyncio.run(scenario())
    assert rows(path) == [(GUILD, 60, 20, 30)]


def test_close_reports_a_write_that_outlasts_its_bound(
    path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings_module, "CLOSE_SECONDS", 0.01)

    async def scenario() -> None:
        store = SettingsStore.open(path)
        held = HeldWrites(store)
        write = asyncio.create_task(store.update(GUILD, volume_percent=20))
        await asyncio.to_thread(held.started.wait, 10)
        assert await store.close() is False
        held.gate.set()
        await write  # the worker still finishes it
        executor = store._executor  # pyright: ignore[reportPrivateUsage]
        await asyncio.to_thread(executor.shutdown)

    with caplog.at_level(logging.ERROR, logger="sobafm"):
        asyncio.run(scenario())
    assert "settings database did not close in time" in caplog.text
    assert rows(path) == [(GUILD, 60, 20, 30)]


# The change cooldown.


def test_a_claim_blocks_until_its_time_passes_and_a_refusal_never_extends_it() -> None:
    cooldown = Cooldown()
    assert cooldown.claim(100.0, 30) is not None
    for now in (100.0, 110.0, 129.9):
        assert cooldown.claim(now, 30) is None
    assert cooldown.remaining(110.0, 30) == 20.0
    assert cooldown.remaining(130.0, 30) == 0.0
    assert cooldown.claim(130.0, 30) is not None  # measured from the claim, not the refusals


def test_a_zero_cooldown_never_blocks() -> None:
    cooldown = Cooldown()
    assert cooldown.claim(100.0, 0) is not None
    assert cooldown.claim(100.0, 0) is not None


def test_a_request_releases_its_own_claim() -> None:
    cooldown = Cooldown()
    claim = cooldown.claim(100.0, 30)
    cooldown.release(claim)
    assert cooldown.remaining(101.0, 30) == 0.0
    assert cooldown.claim(101.0, 30) is not None


def test_a_stale_release_cannot_free_a_newer_claim() -> None:
    cooldown = Cooldown()
    older = cooldown.claim(100.0, 30)
    assert older is not None
    newer = cooldown.claim(130.0, 30)  # the older one's time has passed
    assert newer is not None
    cooldown.release(older)
    cooldown.release(None)
    assert cooldown.remaining(131.0, 30) == 29.0
    assert cooldown.claim(131.0, 30) is None


def test_a_changed_length_applies_to_a_running_cooldown() -> None:
    cooldown = Cooldown()
    assert cooldown.claim(100.0, 600) is not None
    assert cooldown.claim(101.0, 600) is None
    assert cooldown.claim(101.0, 1) is not None
    assert cooldown.claim(102.0, 1) is not None  # exactly one second later
    assert cooldown.claim(102.5, 600) is None  # and raising it extends the wait
