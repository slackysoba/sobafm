import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from sobafm.store import GuildSettings, Store


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "data" / "sobafm.db")
    await store.initialize()
    return store


async def test_initialize_creates_the_database(store: Store) -> None:
    assert store.path.exists()
    assert await store.remembered_channel(1) is None


async def test_initialize_is_idempotent(store: Store) -> None:
    await store.remember_channel(1, 10)

    await store.initialize()

    assert await store.remembered_channel(1) == 10


async def test_remember_channel_replaces_the_previous_one(store: Store) -> None:
    await store.remember_channel(1, 10)
    await store.remember_channel(1, 11)
    await store.remember_channel(2, 20)

    assert (await store.remembered_channel(1), await store.remembered_channel(2)) == (11, 20)


async def test_forget_channel(store: Store) -> None:
    await store.remember_channel(1, 10)
    await store.remember_channel(2, 20)

    await store.forget_channel(1)
    await store.forget_channel(3)

    assert (await store.remembered_channel(1), await store.remembered_channel(2)) == (None, 20)


async def test_writes_commit_in_call_order(store: Store) -> None:
    await asyncio.gather(*(store.remember_channel(1, channel) for channel in range(10, 30)))

    assert await store.remembered_channel(1) == 29


async def test_settings_default_for_a_new_server(store: Store) -> None:
    assert await store.settings(1) == GuildSettings()


async def test_settings_survive_alongside_the_channel(store: Store) -> None:
    await store.save_settings(1, GuildSettings(duration_minutes=90))
    await store.remember_channel(1, 10)
    await store.forget_channel(1)

    assert await store.settings(1) == GuildSettings(duration_minutes=90)
    await store.remember_channel(2, 20)
    await store.save_settings(2, GuildSettings(volume_percent=70))
    assert await store.remembered_channel(2) == 20


def store_raw_settings(store: Store, text: str) -> None:
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("INSERT INTO guild (guild_id, settings, updated_at) VALUES (1, ?, '')", (text,))


async def test_stored_settings_ignore_unknown_fields(store: Store) -> None:
    store_raw_settings(store, '{"volume_percent": 30, "theme": "dark"}')

    assert await store.settings(1) == GuildSettings(volume_percent=30)


async def test_invalid_stored_settings_fall_back_to_the_defaults(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    store_raw_settings(store, '{"volume_percent": 500}')

    assert await store.settings(1) == GuildSettings()
    assert "invalid" in caplog.text


async def test_an_invalid_stored_setting_keeps_the_valid_ones(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    store_raw_settings(store, '{"volume_percent": 500, "duration_minutes": 90}')

    assert await store.settings(1) == GuildSettings(duration_minutes=90)
    assert "invalid volume_percent" in caplog.text
