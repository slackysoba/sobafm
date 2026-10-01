from pathlib import Path

import pytest

from sobafm.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "data" / "sobafm.db")
    await store.initialize()
    return store


async def test_initialize_creates_the_database(store: Store) -> None:
    assert store.path.exists()
    assert await store.remembered_channels() == {}


async def test_initialize_is_idempotent(store: Store) -> None:
    await store.remember_channel(1, 10)

    await store.initialize()

    assert await store.remembered_channels() == {1: 10}


async def test_remember_channel_replaces_the_previous_one(store: Store) -> None:
    await store.remember_channel(1, 10)
    await store.remember_channel(1, 11)
    await store.remember_channel(2, 20)

    assert await store.remembered_channels() == {1: 11, 2: 20}


async def test_forget_channel(store: Store) -> None:
    await store.remember_channel(1, 10)
    await store.remember_channel(2, 20)

    await store.forget_channel(1)
    await store.forget_channel(3)

    assert await store.remembered_channels() == {2: 20}
