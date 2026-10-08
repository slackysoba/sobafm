"""Owned initial voice attempts, using discord.py's real connector and fake transports."""

import asyncio
import socket
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import discord
import pytest
from discord.errors import ConnectionClosed
from discord.voice_state import ConnectionFlowState, VoiceConnectionState
from pydantic import SecretStr

from sobafm.bot import SobaFM, Voice
from sobafm.config import Settings
from sobafm.interpreter import Interpreter
from sobafm.store import Store
from tests.doubles import FakeGemini, FakeLyria, settle

USER = {"id": "99", "username": "synthetic", "discriminator": "0", "avatar": None, "bot": True}


def add_guild(bot: SobaFM) -> discord.Guild:
    state = cast(Any, bot)._connection
    state.user = discord.ClientUser(state=state, data=cast(Any, USER))
    guild = discord.Guild(
        state=state,
        data=cast(
            Any,
            {
                "id": "1",
                "name": "synthetic",
                "owner_id": "55",
                "unavailable": False,
                "roles": [
                    {
                        "id": "1",
                        "name": "everyone",
                        "permissions": str(discord.Permissions.all().value),
                        "position": 0,
                    }
                ],
                "members": [{"user": USER, "roles": [], "joined_at": None, "flags": 0}],
                "channels": [
                    {
                        "id": str(channel_id),
                        "type": 2,
                        "name": "synthetic",
                        "position": 0,
                        "permission_overwrites": [],
                        "bitrate": 64000,
                        "user_limit": 0,
                    }
                    for channel_id in (10, 11, 12)
                ],
            },
        ),
    )
    state._add_guild(guild)
    return guild


class Transport:
    """Only websocket I/O is fake; the library runs its actual handshake and close retry."""

    seq_ack = -1
    close_code = 1000
    secret_key: bytes | None = None

    def __init__(self, world: World, connection: Any) -> None:
        self.world = world
        self.connection = connection
        self.entered = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.closed = False
        self.ready = world.complete_next
        self.signal = asyncio.Event()
        world.complete_next = False
        if self.ready:
            self.signal.set()

    async def close(self, code: int = 1000) -> None:
        self.close_entered.set()
        if self.world.hold_close is not None:
            await self.world.hold_close.wait()
        self.close_code = code
        self.closed = True
        self.signal.set()

    async def poll_event(self) -> None:
        self.entered.set()
        if self.secret_key is None:
            await self.signal.wait()
            if not self.closed:
                self.connection.ip = "127.0.0.1"
                self.secret_key = bytes(32)
                return
        else:
            while not self.closed:
                self.signal.clear()
                await self.signal.wait()
        raise ConnectionClosed(cast(Any, self), shard_id=0)


@dataclass
class World:
    bot: SobaFM
    guild: discord.Guild
    voices: list[Voice] = field(default_factory=list[Voice])
    changes: list[int | None] = field(default_factory=list[int | None])
    sockets: list[Transport] = field(default_factory=list[Transport])
    opened: asyncio.Queue[Transport] = field(default_factory=asyncio.Queue[Transport])
    complete_next: bool = False
    hold_close: asyncio.Event | None = None


@pytest.fixture
def gemini() -> FakeGemini:
    return FakeGemini()


@pytest.fixture
async def world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lyria: FakeLyria, gemini: FakeGemini
) -> AsyncGenerator[World]:
    settings = Settings.model_construct(
        discord_token=SecretStr("synthetic"), gemini_api_key=SecretStr("synthetic")
    )
    store = Store(tmp_path / "state.db")
    await store.initialize()
    bot = SobaFM(settings, store, connect=lyria.connect, interpreter=Interpreter(gemini, "fake"))
    await bot._async_setup_hook()  # pyright: ignore[reportPrivateUsage]
    world = World(bot, add_guild(bot))

    async def gateway(guild: discord.Guild, *, channel: Any, **_: object) -> None:
        world.changes.append(channel.id if channel else None)
        voice = guild.voice_client
        if isinstance(voice, Voice) and voice not in world.voices:
            world.voices.append(voice)
        voice = next((v for v in reversed(world.voices) if v.guild is guild), None)
        if voice is not None:
            connection = cast(Any, voice)._connection
            if channel is None:
                asyncio.get_running_loop().call_soon(connection._disconnected.set)
            else:
                connection.state = ConnectionFlowState.got_both_voice_updates

    async def websocket(connection: Any, resume: bool) -> Any:
        connection.state = ConnectionFlowState.websocket_connected
        transport = Transport(world, connection)
        world.sockets.append(transport)
        world.opened.put_nowait(transport)
        return transport

    def no_network(*_: object, **__: object) -> None:
        raise AssertionError("Unexpected network I/O")

    monkeypatch.setattr(discord.Guild, "change_voice_state", gateway)
    monkeypatch.setattr(VoiceConnectionState, "_connect_websocket", websocket)
    monkeypatch.setattr(VoiceConnectionState, "_create_socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_connection", no_network)
    monkeypatch.setattr(bot, "dispatch", MagicMock())
    try:
        yield world
    finally:
        # Release only synthetic resources, including when a regression fails before cleanup.
        if world.hold_close is not None:
            world.hold_close.set()
        for voice in world.voices:
            connection = cast(Any, voice)._connection
            workers = [t for t in voice._connection_tasks() if t is not None]  # pyright: ignore[reportPrivateUsage]
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            connection._socket_reader.stop()
        await bot.close()


async def initial_attempt(world: World, *, restarted: bool) -> tuple[asyncio.Task[None], Voice]:
    bot = world.bot
    await bot.store.remember_channel(1, 10)
    await bot.on_guild_available(world.guild)
    owner = bot.recoveries[1]
    transport = await world.opened.get()
    await transport.entered.wait()
    voice = world.voices[0]
    if restarted:
        first = cast(Any, voice)._connection._connector
        await voice.on_voice_state_update(cast(Any, {"channel_id": "11", "session_id": "fake"}))
        transport = await world.opened.get()
        await transport.entered.wait()
        await settle()
        assert first.cancelled()
    return owner, voice


@pytest.mark.parametrize("restarted", [False, True])
@pytest.mark.parametrize("gateway", ["same", "reset", "replacement", "same guild replacement"])
async def test_cancelled_attempt_disposes_real_workers_and_preserves_replacement(
    world: World, restarted: bool, gateway: str, lyria: FakeLyria, gemini: FakeGemini
) -> None:
    bot = world.bot
    async with asyncio.timeout(4):
        owner, voice = await initial_attempt(world, restarted=restarted)
        connection = cast(Any, voice)._connection
        connector = connection._connector
        current = world.guild
        if gateway in {"reset", "replacement"}:
            cast(Any, bot)._connection.clear(views=False)
            current = add_guild(bot)
            await bot.on_guild_available(current)
            assert bot.recoveries[1] is owner
        if "replacement" in gateway:
            if gateway == "same guild replacement":
                cast(Any, bot)._connection._remove_voice_client(1)
            world.complete_next = True
            channel = bot.get_channel(12)
            assert isinstance(channel, discord.VoiceChannel)
            replacement = await channel.connect(self_deaf=True, cls=Voice)
            before = list(world.changes)
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
            assert current.voice_client is replacement
            assert replacement.is_connected()
            assert world.changes == before  # no old-client guild-wide departure
        else:
            await bot.close()
            assert bot.voice_clients == []
        assert owner.cancelled()
        assert connector.done()
        assert not voice.is_connected()
        assert voice not in bot.voices
        assert connection._socket_reader._end.is_set()
        assert all(t is None or t.done() for t in voice._connection_tasks())  # pyright: ignore[reportPrivateUsage]
        assert bot.connecting == {}
        assert await bot.store.remembered_channel(1) == 10
        before = list(world.changes)
        for transport in world.sockets:
            if transport.connection is connection:
                transport.signal.set()
        # The reproduced leak retried ConnectionClosed after the library's real one-second delay.
        await asyncio.sleep(1.1)
        assert world.changes == before
        assert bot.stations == {}
        assert gemini.calls == []
        assert lyria.sessions == []


async def test_repeated_cancellation_waits_for_real_connector_cleanup(world: World) -> None:
    async with asyncio.timeout(4):
        owner, voice = await initial_attempt(world, restarted=True)
        connection = cast(Any, voice)._connection
        transport = world.sockets[-1]
        world.hold_close = asyncio.Event()
        owner.cancel()
        await transport.close_entered.wait()
        owner.cancel()
        await settle()
        assert not owner.done()
        assert world.bot.connecting[owner] is voice
        before = connection._connector
        await voice.on_voice_state_update(cast(Any, {"channel_id": "12", "session_id": "fake"}))
        await voice.on_voice_server_update(
            {"guild_id": "1", "token": "synthetic", "endpoint": "synthetic.invalid"}
        )
        assert connection._connector is before  # ending clients accept no new handshake events
        world.hold_close.set()
        await asyncio.gather(owner, return_exceptions=True)
        assert owner.cancelled()
        assert connection._connector.done()
        assert connection._socket_reader._end.is_set()
        assert voice not in world.bot.voices
        assert world.bot.connecting == {}
        assert await world.bot.store.remembered_channel(1) == 10
