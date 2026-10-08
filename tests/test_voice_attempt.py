"""Owned initial voice attempts, using discord.py's real connector and fake transports."""

import asyncio
import socket
import time
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
from sobafm.plan import MusicPlan, Prompt
from sobafm.station import Outcome, Program, Station
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


@pytest.mark.parametrize("event_kind", ["move", "server"])
async def test_callback_already_closing_a_transport_is_quiesced_before_worker_drain(
    world: World, monkeypatch: pytest.MonkeyPatch, event_kind: str
) -> None:
    def synthetic_socket(_: object) -> None:
        return

    monkeypatch.setattr(VoiceConnectionState, "_create_socket", synthetic_socket)
    async with asyncio.timeout(4):
        owner, voice = await initial_attempt(world, restarted=True)
        connection = cast(Any, voice)._connection
        transport = world.sockets[-1]
        world.hold_close = asyncio.Event()
        if event_kind == "move":
            callback = asyncio.create_task(
                voice.on_voice_state_update(cast(Any, {"channel_id": "10", "session_id": "fake"}))
            )
        else:
            callback = asyncio.create_task(
                voice.on_voice_server_update(
                    {"guild_id": "1", "token": "synthetic", "endpoint": "synthetic.invalid"}
                )
            )
        await transport.close_entered.wait()
        assert callback in voice._callbacks  # pyright: ignore[reportPrivateUsage]
        closing = asyncio.create_task(world.bot.close())
        await settle()
        assert voice._ending  # pyright: ignore[reportPrivateUsage]
        world.hold_close.set()
        await asyncio.gather(callback, return_exceptions=True)
        await closing
        assert owner.cancelled()
        assert callback.done()
        assert voice._callbacks == set()  # pyright: ignore[reportPrivateUsage]
        assert all(task is None or task.done() for task in voice._connection_tasks())  # pyright: ignore[reportPrivateUsage]
        assert connection._socket_reader._end.is_set()
        assert voice not in world.bot.voices
        before = list(world.changes)
        await asyncio.sleep(1.1)
        assert world.changes == before
        assert await world.bot.store.remembered_channel(1) == 10


def reserve_backoff_program(station: Station, bot: SobaFM) -> Program:
    program = Program(
        MusicPlan(title="synthetic", prompts=[Prompt(text="synthetic")]),
        "synthetic",
        time.monotonic() + 60,
        retry_at=time.monotonic() + 60,
    )
    station.program = program
    assert bot.pool.reserve(station)
    return program


async def test_fast_new_gateway_rejoin_retires_program_previous_plan_and_old_requests(
    world: World, lyria: FakeLyria, gemini: FakeGemini
) -> None:
    bot = world.bot
    world.complete_next = True
    channel = world.guild.get_channel(10)
    assert isinstance(channel, discord.VoiceChannel)
    old = await channel.connect(self_deaf=True, cls=Voice)
    old.joined = True
    await bot.store.remember_channel(1, 10)
    station = bot.station(world.guild, 0.5)
    program = reserve_backoff_program(station, bot)
    previous = Program(program.plan, "synthetic", time.monotonic() + 60)
    previous.started.set_result(Outcome.PLAYING)
    cast(Any, station)._previous = previous
    request: asyncio.Future[Outcome] = asyncio.get_running_loop().create_future()
    bot.interpreting[1].append(request)
    bot.request_owners[request] = (world.guild, old)
    await settle()  # ordinary station loop is asleep until its next .25-second tick
    cast(Any, bot)._connection.clear(views=False)
    current = add_guild(bot)
    world.complete_next = True
    await bot.on_guild_available(current)
    async with asyncio.timeout(2):
        await bot.recoveries[1]
    assert station.program is None
    assert cast(Any, station)._previous is None
    assert program.started.result() is Outcome.DISCONNECTED
    assert request.result() is Outcome.DISCONNECTED
    assert station not in cast(Any, bot.pool)._holders
    assert station not in bot.station_owners
    assert request not in bot.request_owners
    assert bot.stations == {}
    assert isinstance(current.voice_client, Voice)
    assert current.voice_client.is_connected()
    assert old not in bot.voices
    assert lyria.sessions == []
    assert gemini.calls == []
    assert await bot.store.remembered_channel(1) == 10


async def test_stale_client_cleanup_preserves_current_replacement_station_and_request(
    world: World,
) -> None:
    bot = world.bot
    world.complete_next = True
    channel = world.guild.get_channel(10)
    assert isinstance(channel, discord.VoiceChannel)
    old = await channel.connect(self_deaf=True, cls=Voice)
    old.joined = True
    cast(Any, bot)._connection.clear(views=False)
    current = add_guild(bot)
    replacement_channel = current.get_channel(11)
    assert isinstance(replacement_channel, discord.VoiceChannel)
    world.complete_next = True
    replacement = await replacement_channel.connect(self_deaf=True, cls=Voice)
    replacement.joined = True
    station = bot.station(current, 0.5)
    program = reserve_backoff_program(station, bot)
    request: asyncio.Future[Outcome] = asyncio.get_running_loop().create_future()
    bot.interpreting[1].append(request)
    bot.request_owners[request] = (current, replacement)
    before = list(world.changes)
    await bot.on_guild_available(current)
    async with asyncio.timeout(2):
        await bot.recoveries[1]
    assert old not in bot.voices
    assert current.voice_client is replacement
    assert replacement.is_connected()
    assert bot.stations[1] is station
    assert station.program is program
    assert not request.done()
    assert station in cast(Any, bot.pool)._holders
    assert world.changes == before
    bot.interpreting.pop(1)
    bot.request_owners.pop(request)
    await bot.close_station(current)


async def test_current_library_reconnect_preserves_station_and_pending_request(
    world: World,
) -> None:
    bot = world.bot
    world.complete_next = True
    channel = world.guild.get_channel(10)
    assert isinstance(channel, discord.VoiceChannel)
    voice = await channel.connect(self_deaf=True, cls=Voice)
    station = bot.station(world.guild, 0.5)
    program = reserve_backoff_program(station, bot)
    request: asyncio.Future[Outcome] = asyncio.get_running_loop().create_future()
    bot.interpreting[1].append(request)
    bot.request_owners[request] = (world.guild, voice)
    connection = cast(Any, voice)._connection
    connection.state = ConnectionFlowState.got_both_voice_updates
    await bot.retire_stale_state(world.guild)
    assert station.program is program
    assert bot.stations[1] is station
    assert not request.done()
    assert station in cast(Any, bot.pool)._holders
    bot.interpreting.pop(1)
    bot.request_owners.pop(request)
    await bot.close_station(world.guild)


async def test_old_station_close_cannot_stop_a_replacement_client_registered_mid_close(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = world.bot
    world.complete_next = True
    channel = world.guild.get_channel(10)
    assert isinstance(channel, discord.VoiceChannel)
    old = await channel.connect(self_deaf=True, cls=Voice)
    station = bot.station(world.guild, 0.5)
    reserve_backoff_program(station, bot)
    cast(Any, bot)._connection.clear(views=False)
    current = add_guild(bot)
    stopped = MagicMock()
    entered, release = asyncio.Event(), asyncio.Event()
    close = station.close

    async def held_close(outcome: Outcome) -> None:
        entered.set()
        await release.wait()
        await close(outcome)

    monkeypatch.setattr(station, "close", held_close)
    retiring = asyncio.create_task(bot.retire_stale_state(current))
    async with asyncio.timeout(2):
        await entered.wait()
        world.complete_next = True
        destination = current.get_channel(11)
        assert isinstance(destination, discord.VoiceChannel)
        replacement = await destination.connect(self_deaf=True, cls=Voice)
        monkeypatch.setattr(replacement, "stop", stopped)
        release.set()
        await retiring
    stopped.assert_not_called()
    assert replacement.is_connected()
    assert station.program is None
    assert old in bot.voices  # this check retires state; client cleanup remains its owner's job
