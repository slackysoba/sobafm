import asyncio
import contextlib
import itertools
import json
import logging
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
from discord.errors import ConnectionClosed
from discord.voice_state import ConnectionFlowState, VoiceConnectionState
from google.genai import errors, types
from pydantic import ValidationError

import sobafm.bot
from sobafm.bot import PLAY_REPLIES, REFUSALS, SobaFM, Voice, escape, listeners, voice_channel_id
from sobafm.config import load_settings
from sobafm.interpreter import Interpreter
from sobafm.interpreter import Outcome as Interpreted
from sobafm.plan import MusicPlan, Prompt
from sobafm.station import Outcome, Program, Station
from sobafm.store import GuildSettings, Store
from tests.doubles import FakeClock, FakeGemini, FakeLyria, answer, settle

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
GUILD_ID = 1
CHANNEL_ID = 10
CHANNELS: dict[int, Any] = {}  # the latest channel made with each ID, which bot.get_channel finds


def fake(spec: type, **attributes: Any) -> Any:
    """A mock constrained to `spec`, so isinstance checks behave like the real type."""
    mock = MagicMock(spec=spec)
    mock.configure_mock(**attributes)
    return mock


def make_guild(voice_client: Any = None) -> Any:
    return fake(
        discord.Guild,
        id=GUILD_ID,
        voice_client=voice_client,
        me=fake(discord.Member, id=99),
        change_voice_state=AsyncMock(),
    )


def make_channel(
    guild: Any,
    channel_id: int = CHANNEL_ID,
    *,
    speak: bool = True,
    kind: type = discord.VoiceChannel,
) -> Any:
    channel = fake(kind, id=channel_id, guild=guild, mention=f"<#{channel_id}>")
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, connect=True, speak=speak
    )

    # Registers the client, as discord.py does, and connects it.
    async def connect(*, self_deaf: bool, cls: type) -> Any:
        guild.voice_client = make_voice_client(channel, cls)
        guild.voice_client.finish_connecting = AsyncMock()
        return guild.voice_client

    channel.connect = AsyncMock(side_effect=connect)
    CHANNELS[channel_id] = channel
    return channel


def make_member(channel: Any) -> Any:
    return fake(
        discord.Member, id=5, voice=fake(discord.VoiceState, channel=channel) if channel else None
    )


def make_voice_client(channel: Any, kind: type = discord.VoiceClient) -> Any:
    voice = fake(kind, channel=channel, disconnect=AsyncMock())

    async def move_to(destination: Any) -> None:
        voice.channel = destination

    voice.move_to = AsyncMock(side_effect=move_to)
    return voice


def make_voice(bot: SobaFM, guild: Any, *, current: bool, joined: bool = True) -> Voice:
    """A tracked voice client, built without discord.py's connection state and socket thread."""
    channel = make_channel(guild)
    channel._get_voice_client_key.return_value = (guild.id, "guild_id")
    voice = Voice.__new__(Voice)
    voice.client, voice.channel, voice.sobafm, voice.joined = bot, channel, bot, joined
    cast(Any, voice)._connection = SimpleNamespace(_connector=None, _runner=None)
    voice.stranded_since = None
    bot.voices.add(voice)
    if current:
        guild.voice_client = voice
    return voice


def tracked_client(bot: SobaFM, guild: Any) -> Any:
    """A voice client SobaFM opened, with its disconnect recorded."""
    voice = fake(Voice, guild=guild, disconnect=AsyncMock())
    bot.voices.add(voice)
    return voice


def bot_voice_update(guild: Any, before: Any, after: Any) -> tuple[Any, Any, Any]:
    me = fake(discord.Member, id=guild.me.id, guild=guild)
    return me, fake(discord.VoiceState, channel=before), fake(discord.VoiceState, channel=after)


@pytest.fixture
def gemini() -> FakeGemini:
    return FakeGemini()


@pytest.fixture
async def bot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lyria: FakeLyria,
    gemini: FakeGemini,
    clock: FakeClock,
) -> SobaFM:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    store = Store(tmp_path / "sobafm.db")
    await store.initialize()
    bot = SobaFM(
        load_settings(env_file=None),
        store,
        connect=lyria.connect,
        interpreter=Interpreter(gemini, "gemini-test"),
        clock=clock,
    )
    CHANNELS.clear()
    monkeypatch.setattr(bot, "get_channel", CHANNELS.get)
    return bot


async def test_gives_gemini_the_options_that_refuse_redirects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    bot = SobaFM(load_settings(env_file=None), Store(tmp_path / "sobafm.db"))

    gemini: Any = bot.interpreter._gemini  # pyright: ignore[reportPrivateUsage]
    client = gemini._api_client  # the SDK's, as the interpreter's client built it

    assert client._http_options.async_client_args["allow_redirects"] is False  # aiohttp's
    assert client._async_httpx_client.follow_redirects is False
    assert client._httpx_client.follow_redirects is False
    await bot.close()


def slow_station(bot: SobaFM, monkeypatch: pytest.MonkeyPatch) -> asyncio.Future[Outcome]:
    """Make the next request outlast the reply, returning the outcome the station will settle."""
    monkeypatch.setattr(sobafm.bot, "START_TIMEOUT_S", 0.01)
    started: asyncio.Future[Outcome] = asyncio.get_running_loop().create_future()
    station = fake(Station, close=AsyncMock())
    station.play.return_value = started
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    return started


async def admitted(bot: SobaFM, member: Any, request: str) -> float:
    """Admit `request`, returning when the cooldown it started began."""
    assert await bot.admit(member, request) is None
    return bot.cooldowns[member.guild.id]


def fake_station(outcome: Outcome = Outcome.PLAYING) -> Any:
    """A station whose next request resolves to `outcome` straight away."""
    station = fake(Station, program=None, close=AsyncMock())
    started = asyncio.get_running_loop().create_future()
    started.set_result(outcome)
    station.play.return_value = started
    return station


def make_interaction(user: Any, *, deferred: bool = True) -> Any:
    interaction = fake(discord.Interaction, user=user, guild=None, command=None)
    interaction.response = fake(
        discord.InteractionResponse, defer=AsyncMock(), send_message=AsyncMock()
    )
    interaction.response.is_done.return_value = deferred
    interaction.followup = fake(discord.Webhook, send=AsyncMock())
    return interaction


def test_uses_only_the_guilds_and_voice_states_intents(bot: SobaFM) -> None:
    assert bot.intents == discord.Intents(guilds=True, voice_states=True)


@pytest.mark.parametrize("name", ["join", "leave", "settings"])
def test_commands_are_for_server_managers_in_servers_only(bot: SobaFM, name: str) -> None:
    command = bot.tree.get_command(name)
    assert command is not None

    payload = command.to_dict(bot.tree)

    assert payload["contexts"] == [0]  # servers only
    assert payload["integration_types"] == [0]  # installed to servers, not users
    assert payload["default_member_permissions"] == discord.Permissions(manage_guild=True).value


@pytest.mark.parametrize("name", ["play", "stop"])
def test_music_commands_are_for_every_member_in_servers_only(bot: SobaFM, name: str) -> None:
    command = bot.tree.get_command(name)
    assert command is not None

    payload = command.to_dict(bot.tree)

    assert payload["contexts"] == [0]
    assert payload["integration_types"] == [0]
    assert payload["default_member_permissions"] is None


@pytest.mark.parametrize("dev_guild_id", [None, 42])
async def test_syncs_commands_globally_or_to_the_development_server(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, dev_guild_id: int | None
) -> None:
    bot.settings.dev_guild_id = dev_guild_id
    sync = AsyncMock()
    monkeypatch.setattr(bot.tree, "sync", sync)

    await bot.setup_hook()

    assert bot.check_voice.is_running()
    bot.check_voice.cancel()
    if dev_guild_id is None:
        sync.assert_awaited_once_with()
    else:
        assert sync.await_args is not None
        guild = sync.await_args.kwargs["guild"]
        assert guild.id == dev_guild_id
        copied = {command.name for command in bot.tree.get_commands(guild=guild)}
        assert copied == {command.name for command in bot.tree.get_commands()}


async def test_join_command_defers_then_replies_privately(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "join", AsyncMock(return_value="Joined <#10>."))
    interaction = make_interaction(make_member(None))
    command: Any = bot.tree.get_command("join")

    await command.callback(interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once_with("Joined <#10>.", ephemeral=True)


@pytest.mark.parametrize("deferred", [True, False])
async def test_a_failed_command_tells_the_caller(bot: SobaFM, deferred: bool) -> None:
    interaction = make_interaction(make_member(None), deferred=deferred)

    await bot.tree.on_error(interaction, discord.app_commands.AppCommandError("boom"))

    reply = "Something went wrong. Try again shortly."
    send = interaction.followup.send if deferred else interaction.response.send_message
    send.assert_awaited_once_with(reply, ephemeral=True)


async def test_join_asks_the_caller_to_join_a_voice_channel(bot: SobaFM) -> None:
    assert await bot.join(make_member(None)) == "Join a voice channel first, then use /join."


async def test_join_refuses_stage_channels(bot: SobaFM) -> None:
    stage = make_channel(make_guild(), kind=discord.StageChannel)

    assert await bot.join(make_member(stage)) == "SobaFM can't play in Stage channels."


async def test_join_names_missing_permissions(bot: SobaFM) -> None:
    channel = make_channel(make_guild(), speak=False)

    reply = await bot.join(make_member(channel))

    assert reply == "SobaFM needs these permissions in <#10>: Speak."
    channel.connect.assert_not_awaited()


async def test_join_connects_and_remembers_the_channel(bot: SobaFM) -> None:
    channel = make_channel(make_guild())

    reply = await bot.join(make_member(channel))

    assert reply == "Joined <#10>."
    channel.connect.assert_awaited_once_with(self_deaf=True, cls=Voice)
    assert await bot.store.remembered_channel(GUILD_ID) == CHANNEL_ID


async def test_join_when_already_in_the_channel(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.voice_client = make_voice_client(channel)

    assert await bot.join(make_member(channel)) == "SobaFM is already in <#10>."
    channel.connect.assert_not_awaited()


async def test_join_moves_from_another_channel_and_stays_deafened(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild, 11))
    channel = make_channel(guild)

    reply = await bot.join(make_member(channel))

    assert reply == "Moved to <#10>."
    guild.voice_client.move_to.assert_awaited_once_with(channel)
    guild.change_voice_state.assert_awaited_once_with(channel=channel, self_deaf=True)
    assert await bot.store.remembered_channel(GUILD_ID) == CHANNEL_ID


async def test_join_clears_the_status_before_moving(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild, 11))
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def moving() -> AsyncGenerator[None]:
        events.append("cleared")
        yield
        events.append("shown again")

    bot.stations[GUILD_ID] = fake(Station, moving=moving)
    move_to = guild.voice_client.move_to.side_effect

    async def moved(destination: Any) -> None:
        events.append("moved")
        await move_to(destination)

    guild.voice_client.move_to.side_effect = moved

    assert await bot.join(make_member(make_channel(guild))) == "Moved to <#10>."
    assert events == ["cleared", "moved", "shown again"]


async def test_join_reports_a_failed_move(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild, 11))
    guild.voice_client.move_to = AsyncMock()  # discord.py logs the timeout and returns
    await bot.store.remember_channel(GUILD_ID, 11)

    reply = await bot.join(make_member(make_channel(guild)))

    assert reply == "SobaFM couldn't connect to <#10>. Try again shortly."
    assert await bot.store.remembered_channel(GUILD_ID) == 11


async def test_join_reports_a_move_that_ends_the_connection(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild, 11))
    # discord.py took the move for a disconnect
    guild.voice_client.is_connected.return_value = False
    await bot.store.remember_channel(GUILD_ID, 11)

    reply = await bot.join(make_member(make_channel(guild)))

    assert reply == "SobaFM couldn't connect to <#10>. Try again shortly."
    guild.change_voice_state.assert_not_awaited()
    assert await bot.store.remembered_channel(GUILD_ID) == 11


async def test_join_reports_a_connection_that_never_completes(bot: SobaFM) -> None:
    channel = make_channel(make_guild())
    connect = channel.connect.side_effect

    async def give_up(*, self_deaf: bool, cls: type) -> Any:  # rejected handshakes, no error
        voice = await connect(self_deaf=self_deaf, cls=cls)
        voice.is_connected.return_value = False
        return voice

    channel.connect.side_effect = give_up

    reply = await bot.join(make_member(channel))

    assert reply == "SobaFM couldn't connect to <#10>. Try again shortly."
    channel.guild.voice_client.disconnect.assert_awaited_once_with(force=True)
    assert await bot.store.remembered_channel(GUILD_ID) is None


async def test_join_waits_for_discord_py_to_restart_the_connection(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    moved_to = make_channel(guild, 11)
    connect = channel.connect.side_effect

    # An administrator moves SobaFM during the handshake, which restarts discord.py's connector.
    async def restart(*, self_deaf: bool, cls: type) -> Any:
        voice = await connect(self_deaf=self_deaf, cls=cls)
        voice.is_connected.return_value = False

        async def finish() -> None:  # the restarted connector completes in the new channel
            voice.channel = moved_to
            voice.is_connected.return_value = True

        voice.finish_connecting.side_effect = finish
        raise asyncio.CancelledError

    channel.connect.side_effect = restart

    reply = await bot.join(make_member(channel))

    assert reply == "Joined <#11>."
    assert await bot.store.remembered_channel(GUILD_ID) == 11
    guild.voice_client.disconnect.assert_not_awaited()


async def test_join_reports_a_restarted_connection_that_gives_up(bot: SobaFM) -> None:
    channel = make_channel(make_guild())
    connect = channel.connect.side_effect
    clients: list[Any] = []

    async def restart(*, self_deaf: bool, cls: type) -> Any:
        voice = await connect(self_deaf=self_deaf, cls=cls)
        voice.is_connected.return_value = False
        clients.append(voice)

        async def give_up() -> None:  # discord.py leaves and unregisters the client
            channel.guild.voice_client = None

        voice.finish_connecting.side_effect = give_up
        raise asyncio.CancelledError

    channel.connect.side_effect = restart

    reply = await bot.join(make_member(channel))

    assert reply == "SobaFM couldn't connect to <#10>. Try again shortly."
    clients[0].disconnect.assert_not_awaited()  # discord.py has left already


async def test_finish_connecting_waits_for_a_restarted_connector(bot: SobaFM) -> None:
    voice = make_voice(bot, make_guild(), current=True)
    connection = SimpleNamespace(_connector=None)
    cast(Any, voice)._connection = connection
    release = asyncio.Event()
    connection._connector = asyncio.create_task(asyncio.Event().wait())
    waiting = asyncio.create_task(voice.finish_connecting())
    await settle()

    connection._connector.cancel()  # discord.py cancels its connector, then starts a new one
    connection._connector = asyncio.create_task(release.wait())
    await settle()
    assert not waiting.done()  # the new connector still runs

    release.set()
    await settle()
    assert waiting.done()


async def test_join_passes_on_its_own_cancellation(bot: SobaFM) -> None:
    channel = make_channel(make_guild())
    connecting = asyncio.Event()

    async def stall(*, self_deaf: bool, cls: type) -> Any:
        connecting.set()
        await asyncio.Event().wait()

    channel.connect.side_effect = stall
    joining = asyncio.create_task(bot.join(make_member(channel)))
    await connecting.wait()

    joining.cancel()

    with pytest.raises(asyncio.CancelledError):
        await joining


async def test_join_reports_a_channel_that_is_gone(bot: SobaFM) -> None:
    channel = make_channel(make_guild())
    del CHANNELS[CHANNEL_ID]  # deleted, or not yet in a new gateway session

    reply = await bot.join(make_member(channel))

    assert reply == "SobaFM couldn't connect to <#10>. Try again shortly."
    channel.connect.assert_not_awaited()


async def test_join_reports_a_move_that_leaves_another_client(bot: SobaFM) -> None:
    guild = make_guild()
    voice = make_voice_client(make_channel(guild, 11))
    guild.voice_client = voice
    channel = make_channel(guild)

    async def replaced(destination: Any) -> None:  # the move ended this client
        voice.channel = destination
        guild.voice_client = make_voice_client(destination)

    voice.move_to.side_effect = replaced

    reply = await bot.join(make_member(channel))

    assert reply == "SobaFM couldn't connect to <#10>. Try again shortly."


async def test_join_connects_through_the_current_channel(bot: SobaFM) -> None:
    guild = make_guild()
    earlier = make_channel(guild)
    current = make_channel(guild)  # the same channel, from the gateway session now running

    await bot.join(make_member(earlier))

    current.connect.assert_awaited_once_with(self_deaf=True, cls=Voice)
    earlier.connect.assert_not_awaited()


async def test_join_reports_a_failed_connection(bot: SobaFM) -> None:
    channel = make_channel(make_guild())
    channel.connect.side_effect = TimeoutError

    reply = await bot.join(make_member(channel))

    assert reply == "SobaFM couldn't connect to <#10>. Try again shortly."
    assert await bot.store.remembered_channel(GUILD_ID) is None


async def test_leave_disconnects_and_forgets_the_channel(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    assert await bot.leave(guild) == "Left <#10>."
    guild.voice_client.disconnect.assert_awaited_once_with(force=True)
    assert await bot.store.remembered_channel(GUILD_ID) is None


async def test_leave_when_not_connected(bot: SobaFM) -> None:
    assert await bot.leave(make_guild()) == "SobaFM isn't in a voice channel."


async def test_follows_a_move_by_an_administrator(bot: SobaFM) -> None:
    guild = make_guild()

    await bot.on_voice_state_update(
        *bot_voice_update(guild, make_channel(guild, 11), make_channel(guild))
    )

    assert await bot.store.remembered_channel(GUILD_ID) == CHANNEL_ID


class ClosingSocket:
    """A voice websocket that closes with each code in turn, then ends discord.py's poll loop."""

    def __init__(self, *codes: int) -> None:
        self.codes = list(codes)

    async def poll_event(self) -> None:
        if not self.codes:
            raise asyncio.CancelledError
        raise ConnectionClosed(MagicMock(), shard_id=None, code=self.codes.pop(0))


def voice_state(channel_id: int | None) -> Any:
    """SobaFM's VOICE_STATE_UPDATE payload."""
    return {"channel_id": channel_id, "session_id": "session", "guild_id": GUILD_ID}


async def test_a_move_after_discord_pys_reconnect_stays_a_move(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = make_voice(bot, make_guild(), current=True)
    connection: Any = VoiceConnectionState(voice)  # discord.py's own, with its reader paused
    cast(Any, voice)._connection = connection
    moved, disconnected = AsyncMock(return_value=True), AsyncMock()
    try:
        # discord.py's general reconnect leaves the channel and rejoins it
        connection._expecting_disconnect = True
        await voice.on_voice_state_update(voice_state(None))
        await voice.on_voice_state_update(voice_state(CHANNEL_ID))
        # an administrator's move then closes the voice websocket with 4014
        monkeypatch.setattr(connection, "_potential_reconnect", moved)
        monkeypatch.setattr(connection, "disconnect", disconnected)
        connection.ws = ClosingSocket(4014)
        await connection._poll_voice_ws(reconnect=True)
    finally:
        connection._socket_reader.stop()

    moved.assert_awaited_once()
    disconnected.assert_not_awaited()


class InstantBackoff:
    """discord.py's reconnect backoff, without the wait."""

    def delay(self) -> float:
        return 0.0


def registry_guild(bot: SobaFM) -> Any:
    """A guild whose voice client is the one in discord.py's own registry, as in the real Guild."""
    guild = make_guild()
    registry = cast(Any, bot)._connection
    type(guild).voice_client = property(lambda _: registry._get_voice_client(GUILD_ID))
    return guild


@pytest.mark.parametrize("worker", ["_connector", "_runner"])
async def test_a_client_discord_py_is_connecting_is_not_stranded(bot: SobaFM, worker: str) -> None:
    voice = make_voice(bot, make_guild(), current=True)
    connection: Any = VoiceConnectionState(voice)
    cast(Any, voice)._connection = connection
    try:
        task = asyncio.create_task(asyncio.Event().wait())
        setattr(connection, worker, task)
        assert not voice.stranded()

        task.cancel()
        await asyncio.wait({task})
    finally:
        connection._socket_reader.stop()

    assert voice.stranded()


@pytest.mark.parametrize(
    "failure", ["reset connection", "lost voice state update", "failed resume"]
)
async def test_finds_a_client_discord_pys_runner_left_without_cleaning_up(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(bot, "dispatch", MagicMock())
    monkeypatch.setattr("discord.voice_state.ExponentialBackoff", InstantBackoff)
    guild = make_guild()
    voice = make_voice(bot, guild, current=True)
    connection: Any = VoiceConnectionState(voice)  # discord.py's own, with its reader paused
    cast(Any, voice)._connection = connection
    waiting = asyncio.Event()  # set once the reconnect waits for Discord's reply
    timed_out = asyncio.Event()  # ends that wait as a timeout, when the test is ready
    if failure == "reset connection":
        # an unhandled close starts a reconnect, whose voice state update finds the gateway
        # connection reset
        connection.ws = ClosingSocket(4006)
        guild.change_voice_state.side_effect = [None, aiohttp.ClientConnectionResetError()]
    elif failure == "lost voice state update":
        # the reconnect gets no reply, so it times out, and the runner then polls the websocket
        # it closed itself
        connection.ws = ClosingSocket(4006, 1000)

        async def no_reply(*_: object, **__: object) -> None:  # the wait for Discord's reply
            waiting.set()
            await timed_out.wait()
            raise TimeoutError

        monkeypatch.setattr(connection, "_wait_for_state", no_reply)
    else:
        # a resume, which keeps the connected state, finds the gateway connection reset
        connection.state = ConnectionFlowState.connected
        connection.ws = ClosingSocket(4015)
        guild.change_voice_state.side_effect = aiohttp.ClientConnectionResetError()
    try:
        connection._runner = asyncio.create_task(connection._poll_voice_ws(reconnect=True))
        await settle()
        if failure == "lost voice state update":
            assert waiting.is_set()  # the reconnect sent its rejoin request, and awaits the reply
            assert not voice.stranded()
            timed_out.set()
            await connection._runner
        else:
            with pytest.raises(aiohttp.ClientConnectionResetError):
                await connection._runner
    finally:
        connection._socket_reader.stop()

    assert voice.stranded()
    assert voice in bot.voices  # never cleaned up
    cast(MagicMock, bot.dispatch).assert_not_called()
    error = voice.failure()  # why the runner stopped, for the log
    if failure == "lost voice state update":
        assert error is None
    else:
        assert isinstance(error, aiohttp.ClientConnectionResetError)


async def test_closes_a_stranded_client_once_discord_confirms_it_left(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "dispatch", MagicMock())
    guild = registry_guild(bot)
    voice = make_voice(bot, guild, current=False)
    cast(Any, bot)._connection._add_voice_client(GUILD_ID, voice)
    connection: Any = VoiceConnectionState(voice)  # stranded: no connector or runner
    cast(Any, voice)._connection = connection
    cast(Any, voice)._player = None
    try:
        replacing = asyncio.create_task(bot.replace_voice(voice))
        await settle()
        guild.change_voice_state.assert_awaited_once_with(channel=None)  # discord.py leaves...
        assert not replacing.done()  # ...and waits for Discord to confirm

        await voice.on_voice_state_update(voice_state(None))
        async with asyncio.timeout(1):
            assert await replacing
    finally:
        connection._socket_reader.stop()

    assert guild.voice_client is None
    assert voice not in bot.voices
    cast(MagicMock, bot.dispatch).assert_not_called()  # replaced, so SobaFM did not leave


def stranded_voice(bot: SobaFM, monkeypatch: pytest.MonkeyPatch, *answers: bool) -> Voice:
    """The server's voice client, which `stranded()` reports as `answers`, then the last."""
    guild = make_guild()
    voice = make_voice(bot, guild, current=True)
    guild.get_channel.return_value = voice.channel
    states = iter(answers)
    monkeypatch.setattr(voice, "stranded", lambda: next(states, answers[-1]))
    monkeypatch.setattr(voice, "failure", lambda: None)

    async def disconnect(*, force: bool) -> None:  # as discord.py does, leaving the channel
        cast(Any, voice.channel).connect.assert_not_awaited()
        voice.cleanup()
        guild.voice_client = None

    monkeypatch.setattr(voice, "disconnect", AsyncMock(side_effect=disconnect))
    monkeypatch.setattr(bot, "dispatch", MagicMock())
    return voice


async def recovery_after_checks(bot: SobaFM, clock: FakeClock) -> asyncio.Task[None]:
    """Check voice as a client stays stranded long enough, and return its recovery."""
    for clock.now in (0.0, 60.0):
        await bot.check_voice()
    return bot.recoveries[GUILD_ID]


async def finish_recovery(recovery: asyncio.Task[None]) -> None:
    async with asyncio.timeout(1):
        await recovery


async def test_replaces_a_stranded_voice_client(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    voice = stranded_voice(bot, monkeypatch, True)
    error = ConnectionResetError("reset by peer")
    monkeypatch.setattr(voice, "failure", lambda: error)
    guild, channel = voice.guild, cast(Any, voice.channel)
    station = fake_station()
    bot.stations[guild.id] = station
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.check_voice()
    clock.now = 59.9
    await bot.check_voice()
    assert GUILD_ID not in bot.recoveries  # not yet stranded for long enough
    clock.now = 60.0
    await bot.check_voice()
    with caplog.at_level(logging.WARNING, logger="sobafm.bot"):
        await finish_recovery(bot.recoveries[GUILD_ID])

    [stopped] = [record for record in caplog.records if "stopped reconnecting" in record.message]
    assert stopped.exc_info is not None
    assert stopped.exc_info[1] is error  # why discord.py stopped
    station.close.assert_awaited_once_with(Outcome.DISCONNECTED)
    cast(AsyncMock, voice.disconnect).assert_awaited_once_with(force=True)
    channel.connect.assert_awaited_once_with(self_deaf=True, cls=Voice)
    assert await bot.store.remembered_channel(GUILD_ID) == CHANNEL_ID
    assert voice not in bot.voices
    assert GUILD_ID not in bot.recoveries


async def test_rejoins_once_the_outage_ends(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sobafm.bot, "REJOIN_RETRY_S", 0.01)
    voice = stranded_voice(bot, monkeypatch, True)
    channel = cast(Any, voice.channel)
    connect = channel.connect.side_effect
    failures = iter(
        [
            aiohttp.ClientConnectionResetError(),  # the gateway is down
            TimeoutError(),  # Discord doesn't answer
            aiohttp.ServerDisconnectedError(),  # the voice server drops the handshake
            ValueError("bad IP discovery"),  # anything else
        ]
    )
    attempts: list[float] = []

    async def reconnect(**kwargs: Any) -> Any:
        attempts.append(time.monotonic())
        if (failure := next(failures, None)) is not None:
            raise failure
        return await connect(**kwargs)

    channel.connect.side_effect = reconnect
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    with caplog.at_level(logging.WARNING, logger="sobafm.bot"):
        await finish_recovery(await recovery_after_checks(bot, clock))

    assert len(attempts) == 5
    assert all(later - earlier >= 0.01 for earlier, later in itertools.pairwise(attempts))
    rejoins = [record for record in caplog.records if record.message.startswith("Could not rejoin")]
    # one line each for what an outage causes, and a traceback for anything else
    assert [bool(record.exc_info) for record in rejoins] == [False, False, True, True]
    assert voice.guild.voice_client is not None
    cast(MagicMock, bot.dispatch).assert_not_called()  # replaced, so SobaFM did not leave
    assert await bot.store.remembered_channel(GUILD_ID) == CHANNEL_ID


async def test_stops_rejoining_once_the_channel_is_forgotten(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    monkeypatch.setattr(sobafm.bot, "REJOIN_RETRY_S", 0)
    voice = stranded_voice(bot, monkeypatch, True)
    channel = cast(Any, voice.channel)
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    async def leave(**_: Any) -> Any:  # a /leave forgets the channel during the outage
        await bot.store.forget_channel(GUILD_ID)
        raise TimeoutError

    channel.connect.side_effect = leave

    await finish_recovery(await recovery_after_checks(bot, clock))

    channel.connect.assert_awaited_once()


async def test_keeps_a_client_that_recovers_before_its_replacement(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    voice = stranded_voice(bot, monkeypatch, True, True, False)  # until the replacement starts

    await finish_recovery(await recovery_after_checks(bot, clock))

    cast(AsyncMock, voice.disconnect).assert_not_awaited()


async def test_replacing_skips_a_client_closed_meanwhile(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = stranded_voice(bot, monkeypatch, True)
    station = fake_station()
    bot.stations[GUILD_ID] = station
    cast(Any, voice.guild).voice_client = None  # /leave closed it while this waited

    assert not await bot.replace_voice(voice)

    cast(AsyncMock, voice.disconnect).assert_not_awaited()
    station.close.assert_not_awaited()


async def test_keeps_a_voice_client_discord_py_reconnects(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    # discord.py restarts its connector, then gives up later
    stranded_voice(bot, monkeypatch, True, False, True)

    for clock.now in (0.0, 30.0, 40.0, 99.9):
        await bot.check_voice()

    assert GUILD_ID not in bot.recoveries


@pytest.mark.parametrize("problem", ["still joining", "from an earlier session"])
async def test_leaves_other_voice_clients_to_their_owners(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, problem: str
) -> None:
    voice = stranded_voice(bot, monkeypatch, True)
    recover = AsyncMock()
    monkeypatch.setattr(bot, "recover_voice", recover)
    if problem == "still joining":
        voice.joined = False  # connect_to() decides what happens to it
    else:  # rejoin() and connect_to() close it
        cast(Any, voice.guild).voice_client = make_voice_client(voice.channel)

    for clock.now in (0.0, 60.0, 120.0):
        await bot.check_voice()

    recover.assert_not_called()


async def test_starts_one_recovery_per_server(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    stranded_voice(bot, monkeypatch, True)
    closed = asyncio.Event()

    async def close(outcome: Outcome) -> None:
        await closed.wait()

    station = fake_station()
    station.close.side_effect = close
    bot.stations[GUILD_ID] = station
    recovery = await recovery_after_checks(bot, clock)
    await settle()  # the recovery is closing the station

    clock.now = 70.0
    await bot.check_voice()

    assert bot.recoveries == {GUILD_ID: recovery}
    closed.set()
    await finish_recovery(recovery)


async def test_retries_a_recovery_that_failed(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    voice = stranded_voice(bot, monkeypatch, True)
    station = fake_station()
    station.close.side_effect = RuntimeError("a bug")
    bot.stations[GUILD_ID] = station
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    with caplog.at_level(logging.ERROR, logger="sobafm.bot"):
        await finish_recovery(await recovery_after_checks(bot, clock))  # closing the station fails
    clock.now = 70.0
    await bot.check_voice()
    await finish_recovery(bot.recoveries[GUILD_ID])

    assert "Could not recover the voice connection" in caplog.text
    cast(AsyncMock, voice.disconnect).assert_awaited_once_with(force=True)


async def test_waits_before_rejoining_again(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    voice = stranded_voice(bot, monkeypatch, True)
    channel = cast(Any, voice.channel)
    attempted = asyncio.Event()

    async def fail(**_: Any) -> Any:
        attempted.set()
        raise TimeoutError

    channel.connect.side_effect = fail
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    recovery = await recovery_after_checks(bot, clock)
    async with asyncio.timeout(1):
        await attempted.wait()

    await asyncio.sleep(0.2)

    channel.connect.assert_awaited_once()  # the next attempt is 30 seconds away
    recovery.cancel()
    await asyncio.wait({recovery})


async def test_closing_stops_a_recovery_while_an_old_client_closes(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    voice = stranded_voice(bot, monkeypatch, True)
    channel = cast(Any, voice.channel)
    old = tracked_client(bot, voice.guild)  # from an earlier gateway session
    old.joined = True
    closing = asyncio.Event()

    async def leave(*, force: bool) -> None:  # discord.py waits to leave, swallowing a cancel
        closing.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()

    old.disconnect.side_effect = leave
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    recovery = await recovery_after_checks(bot, clock)
    async with asyncio.timeout(1):
        await closing.wait()

        await bot.close()

    assert recovery.cancelled()
    channel.connect.assert_not_awaited()


async def test_closing_stops_a_recovery_between_attempts(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    voice = stranded_voice(bot, monkeypatch, True)
    channel = cast(Any, voice.channel)
    waiting = asyncio.Event()

    async def lossy(**_: Any) -> Any:  # the handshake times out, and discord.py waits to leave
        waiting.set()
        with contextlib.suppress(asyncio.CancelledError):  # discord.py swallows a cancel here
            await asyncio.Event().wait()
        await asyncio.sleep(0.1)  # and finishes leaving
        raise TimeoutError

    channel.connect.side_effect = lossy
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    recovery = await recovery_after_checks(bot, clock)
    async with asyncio.timeout(1):
        await waiting.wait()

        await bot.close()

    assert recovery.cancelled()  # finished before close() returned, with no second attempt
    channel.connect.assert_awaited_once()


async def test_closing_stops_a_recovery_while_discord_py_leaves(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    monkeypatch.setattr(bot, "dispatch", MagicMock())
    guild = registry_guild(bot)
    voice = make_voice(bot, guild, current=False)
    cast(Any, bot)._connection._add_voice_client(GUILD_ID, voice)
    guild.get_channel.return_value = voice.channel
    connection: Any = VoiceConnectionState(voice)  # stranded: no connector or runner
    cast(Any, voice)._connection = connection
    cast(Any, voice)._player = None
    connection.timeout = 5  # Discord doesn't confirm the departure in time
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    try:
        recovery = await recovery_after_checks(bot, clock)
        await settle()  # discord.py leaves and waits for Discord to confirm
        guild.change_voice_state.assert_awaited_once_with(channel=None)

        async with asyncio.timeout(1):
            await bot.close()
    finally:
        connection._socket_reader.stop()

    assert recovery.cancelled()  # though discord.py swallows the cancel while it waits
    cast(Any, voice.channel).connect.assert_not_awaited()


async def test_ignores_its_own_reconnect(bot: SobaFM, monkeypatch: pytest.MonkeyPatch) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    # discord.py reconnects by leaving and rejoining, keeping the same client
    await bot.on_voice_state_update(*bot_voice_update(guild, channel, None))
    await bot.on_voice_state_update(*bot_voice_update(guild, None, channel))

    assert await bot.store.remembered_channel(GUILD_ID) == CHANNEL_ID


@pytest.mark.parametrize(
    ("current", "joined", "left"), [(True, True, True), (True, False, False), (False, True, False)]
)
def test_a_finished_client_reports_whether_sobafm_left(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, *, current: bool, joined: bool, left: bool
) -> None:
    dispatch = MagicMock()
    monkeypatch.setattr(bot, "dispatch", dispatch)
    guild = make_guild()
    registry = cast(Any, bot)._connection  # discord.py's own registry of voice clients
    type(guild).voice_client = property(lambda _: registry._get_voice_client(GUILD_ID))
    replacement = make_voice_client(make_channel(guild))
    registry._add_voice_client(GUILD_ID, replacement)
    voice = make_voice(bot, guild, current=False, joined=joined)
    if current:
        registry._add_voice_client(GUILD_ID, voice)

    voice.cleanup()

    assert voice not in bot.voices
    assert dispatch.called is left
    # a client from an earlier session leaves its replacement registered
    assert (guild.voice_client is replacement) is not current


def test_keeps_the_channel_when_shutting_down(bot: SobaFM, monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch = MagicMock()
    monkeypatch.setattr(bot, "dispatch", dispatch)
    monkeypatch.setattr(bot, "is_closed", MagicMock(return_value=True))

    make_voice(bot, make_guild(), current=True).cleanup()

    dispatch.assert_not_called()


async def test_forgets_the_channel_after_leaving_voice(bot: SobaFM) -> None:
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.on_voice_lost(make_guild())

    assert await bot.store.remembered_channel(GUILD_ID) is None


async def test_keeps_the_channel_when_already_rejoined(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.on_voice_lost(guild)

    assert await bot.store.remembered_channel(GUILD_ID) == CHANNEL_ID


async def test_ignores_other_members(bot: SobaFM) -> None:
    guild = make_guild()
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    someone = fake(discord.Member, id=5, guild=guild)
    before = fake(discord.VoiceState, channel=make_channel(guild))
    after = fake(discord.VoiceState, channel=None)

    await bot.on_voice_state_update(someone, before, after)

    assert await bot.store.remembered_channel(GUILD_ID) == CHANNEL_ID


async def test_rejoins_when_the_server_becomes_available(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.get_channel.return_value = channel
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.on_guild_available(guild)

    channel.connect.assert_awaited_once_with(self_deaf=True, cls=Voice)
    assert await bot.store.remembered_channel(GUILD_ID) == CHANNEL_ID
    assert GUILD_ID not in bot.stations  # no status was left to clear


async def show_a_title_then_close(bot: SobaFM, guild: Any, *, connected: bool) -> AsyncMock:
    """Play a program until its title shows, then close the station, connected or not."""
    shown = AsyncMock(return_value=True)
    bot.show_voice_status = shown  # pyright: ignore[reportAttributeAccessIssue]
    station = bot.station(guild, 0.5)
    station.play(LOFI, "<@5>", duration_seconds=600)
    assert station.program is not None
    station.program.settle(Outcome.PLAYING)
    await station.reconcile()
    await settle()
    guild.voice_client.is_connected.return_value = connected
    await bot.close_station(guild, Outcome.DISCONNECTED if not connected else Outcome.STOPPED)
    await settle()
    return shown


async def test_keeps_a_title_a_closed_station_could_not_clear(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))

    await show_a_title_then_close(bot, guild, connected=False)  # SobaFM lost its connection

    assert bot.statuses[GUILD_ID].shown == {CHANNEL_ID: "Rainy lo-fi"}


async def test_keeps_no_title_a_closed_station_cleared(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))

    shown = await show_a_title_then_close(bot, guild, connected=True)

    assert shown.await_args_list[-1].args == (CHANNEL_ID, None)
    assert bot.statuses[GUILD_ID].shown == {}


async def test_clears_a_title_left_behind_once_it_rejoins(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.voice_client = make_voice_client(channel)
    shown = await show_a_title_then_close(bot, guild, connected=False)
    guild.voice_client = None
    guild.get_channel.return_value = channel  # still occupied, so the title remains
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.on_guild_available(guild)  # SobaFM rejoins its channel
    await settle()

    assert shown.await_args_list[-1].args == (CHANNEL_ID, None)
    assert bot.statuses[GUILD_ID].shown == {}
    assert GUILD_ID not in bot.stations  # with no program, no station is needed


async def test_join_clears_a_title_left_behind(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.voice_client = make_voice_client(channel)
    shown = await show_a_title_then_close(bot, guild, connected=False)
    guild.voice_client = None

    assert await bot.join(make_member(channel)) == "Joined <#10>."
    await settle()

    assert shown.await_args_list[-1].args == (CHANNEL_ID, None)


async def test_forgets_a_title_in_a_channel_that_empties(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.voice_client = make_voice_client(channel)
    shown = await show_a_title_then_close(bot, guild, connected=False)
    guild.voice_client = None
    guild.get_channel.return_value = channel
    channel.voice_states = {}  # the last member leaves, and Discord clears the status
    member = make_member(None)
    member.guild = guild

    await bot.on_voice_state_update(
        member, fake(discord.VoiceState, channel=channel), fake(discord.VoiceState, channel=None)
    )
    calls = len(shown.await_args_list)
    channel.voice_states = {5: object()}  # a member sets their own status there, then /join
    await bot.join(make_member(channel))
    await settle()

    assert bot.statuses[GUILD_ID].shown == {}
    assert len(shown.await_args_list) == calls  # a status SobaFM didn't set stays


async def test_forgets_titles_in_channels_that_emptied_while_away(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.voice_client = make_voice_client(channel)
    shown = await show_a_title_then_close(bot, guild, connected=False)
    guild.voice_client = None
    guild.get_channel.return_value = channel
    channel.voice_states = {}  # it emptied while the gateway was away
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    calls = len(shown.await_args_list)

    await bot.on_guild_available(guild)
    await settle()

    assert bot.statuses[GUILD_ID].shown == {}
    assert len(shown.await_args_list) == calls


async def test_rejoining_leaves_a_station_to_show_its_own_title(bot: SobaFM) -> None:
    shown = AsyncMock(return_value=True)
    bot.show_voice_status = shown  # pyright: ignore[reportAttributeAccessIssue]
    guild = make_guild()
    channel = make_channel(guild)
    guild.get_channel.return_value = channel
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    guild.voice_client = make_voice_client(channel)
    station = bot.station(guild, 0.5)
    station.play(LOFI, "<@5>", duration_seconds=600)
    assert station.program is not None
    station.program.settle(Outcome.PLAYING)
    await station.reconcile()
    await settle()
    guild.voice_client = None  # a new gateway session keeps the station, which plays on

    await bot.on_guild_available(guild)
    await settle()
    titles = [call.args for call in shown.await_args_list]
    await station.close()

    assert titles == [(CHANNEL_ID, "Rainy lo-fi")]  # still showing what is heard


async def test_a_servers_stations_share_its_statuses(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    first = bot.station(guild, 0.5)
    await bot.close_station(guild)

    assert bot.station(guild, 0.5).statuses is first.statuses
    await bot.close_station(guild)


async def test_the_voice_check_clears_a_left_title_once_back_without_a_station(
    bot: SobaFM,
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.voice_client = make_voice_client(channel)
    shown = await show_a_title_then_close(bot, guild, connected=False)
    guild.voice_client = make_voice_client(channel)  # moved back, not through connect_to()

    await bot.check_voice()
    await settle()

    assert shown.await_args_list[-1].args == (CHANNEL_ID, None)
    assert bot.statuses[GUILD_ID].shown == {}


async def test_the_voice_check_leaves_a_playing_stations_title(bot: SobaFM) -> None:
    shown = AsyncMock(return_value=True)
    bot.show_voice_status = shown  # pyright: ignore[reportAttributeAccessIssue]
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    station = bot.station(guild, 0.5)
    station.play(LOFI, "<@5>", duration_seconds=600)
    assert station.program is not None
    station.program.settle(Outcome.PLAYING)
    await station.reconcile()
    await settle()

    await bot.check_voice()
    await settle()
    titles = [call.args for call in shown.await_args_list]
    await bot.close_station(guild)

    assert titles == [(CHANNEL_ID, "Rainy lo-fi")]


async def test_a_failing_status_sync_leaves_the_voice_check_running(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    statuses = bot.statuses_for(make_guild())
    monkeypatch.setattr(statuses, "show", MagicMock(side_effect=RuntimeError("a bug")))

    with caplog.at_level(logging.ERROR, logger="sobafm.bot"):
        await bot.check_voice()  # an error would stop discord.py's loop, and with it recoveries

    assert "Could not sync voice channel statuses" in caplog.text


async def test_the_voice_check_retries_a_refused_clear(bot: SobaFM, clock: FakeClock) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.voice_client = make_voice_client(channel)
    shown = await show_a_title_then_close(bot, guild, connected=False)
    guild.voice_client = make_voice_client(channel)
    shown.side_effect = [discord.HTTPException(MagicMock(status=500), "unavailable"), True]

    await bot.check_voice()  # refused
    await settle()
    await bot.check_voice()  # too soon to try again
    await settle()
    assert bot.statuses[GUILD_ID].shown == {CHANNEL_ID: "Rainy lo-fi"}
    clock.now += 10
    await bot.check_voice()
    await settle()

    assert bot.statuses[GUILD_ID].shown == {}


async def test_connecting_clears_a_title_once_a_closed_stations_request_lands(
    bot: SobaFM,
) -> None:
    answered = asyncio.Event()
    sent: list[tuple[int, str | None]] = []

    async def late(channel_id: int, status: str | None) -> bool:
        sent.append((channel_id, status))
        await answered.wait()
        return True

    bot.show_voice_status = late  # pyright: ignore[reportAttributeAccessIssue]
    guild = make_guild()
    channel = make_channel(guild)
    guild.voice_client = make_voice_client(channel)
    station = bot.station(guild, 0.5)
    station.play(LOFI, "<@5>", duration_seconds=600)
    assert station.program is not None
    station.program.settle(Outcome.PLAYING)
    await station.reconcile()  # the title's request is held, as by a rate limit
    guild.voice_client.is_connected.return_value = False
    await bot.close_station(guild, Outcome.DISCONNECTED)
    guild.voice_client = None
    guild.get_channel.return_value = channel
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    await bot.on_guild_available(guild)  # SobaFM rejoins while the title is still in flight

    answered.set()
    await settle()

    assert sent == [(CHANNEL_ID, "Rainy lo-fi"), (CHANNEL_ID, None)]
    assert bot.statuses[GUILD_ID].shown == {}


@pytest.mark.parametrize("departure", ["disconnects", "moves away", "SobaFM leaves"])
async def test_forgets_a_title_whatever_departure_empties_its_channel(
    bot: SobaFM, departure: str
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.voice_client = make_voice_client(channel)
    await show_a_title_then_close(bot, guild, connected=False)
    guild.get_channel.return_value = channel
    channel.voice_states = {}
    if departure == "SobaFM leaves":
        member, before, after = bot_voice_update(guild, channel, None)
    else:
        member = make_member(None)
        member.guild = guild
        before = fake(discord.VoiceState, channel=channel)
        elsewhere = make_channel(guild, 11) if departure == "moves away" else None
        after = fake(discord.VoiceState, channel=elsewhere)

    await bot.on_voice_state_update(member, before, after)

    assert bot.statuses[GUILD_ID].shown == {}


async def test_forgets_a_title_in_a_deleted_channel(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    await show_a_title_then_close(bot, guild, connected=False)
    guild.voice_client = None
    guild.get_channel.return_value = None  # the channel is gone

    await bot.on_guild_available(guild)

    assert bot.statuses[GUILD_ID].shown == {}


async def test_forgets_titles_in_a_server_it_is_removed_from(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    await show_a_title_then_close(bot, guild, connected=False)

    await bot.on_guild_remove(guild)

    assert GUILD_ID not in bot.statuses


async def test_rejoin_first_closes_a_client_from_an_earlier_session(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.get_channel.return_value = channel
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    old = tracked_client(bot, guild)
    guild.voice_client = old
    # discord.py reconnects to voice, which SobaFM sees as leaving and rejoining
    await bot.on_voice_state_update(*bot_voice_update(guild, channel, None))
    await bot.on_voice_state_update(*bot_voice_update(guild, None, channel))
    guild.voice_client = None  # a new gateway session: discord.py forgets the client

    def disconnect(*, force: bool) -> None:
        channel.connect.assert_not_awaited()

    old.disconnect.side_effect = disconnect

    await bot.on_guild_available(guild)

    old.disconnect.assert_awaited_once_with(force=True)
    channel.connect.assert_awaited_once()


async def test_leave_closes_a_client_from_an_earlier_session(bot: SobaFM) -> None:
    guild = make_guild()
    old = tracked_client(bot, guild)

    assert await bot.leave(guild) == "SobaFM isn't in a voice channel."
    old.disconnect.assert_awaited_once_with(force=True)


async def test_rejoin_skips_a_connected_server(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.voice_client = make_voice_client(channel)
    guild.get_channel.return_value = channel
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.on_guild_available(guild)

    channel.connect.assert_not_awaited()


async def test_rejoin_keeps_the_channel_when_connecting_fails(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    channel.connect.side_effect = TimeoutError
    guild.get_channel.return_value = channel
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.on_guild_available(guild)

    assert await bot.store.remembered_channel(GUILD_ID) == CHANNEL_ID


@pytest.mark.parametrize("problem", ["deleted", "no permission"])
async def test_forgets_channels_it_cannot_rejoin(bot: SobaFM, problem: str) -> None:
    guild = make_guild()
    channel = make_channel(guild, speak=problem != "no permission")
    guild.get_channel.return_value = None if problem == "deleted" else channel
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.on_guild_available(guild)

    channel.connect.assert_not_awaited()
    assert await bot.store.remembered_channel(GUILD_ID) is None


async def test_forgets_the_channel_when_removed_from_the_server(bot: SobaFM) -> None:
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.on_guild_remove(make_guild())

    assert await bot.store.remembered_channel(GUILD_ID) is None


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze Discord's clock, so relative timestamps are known."""
    monkeypatch.setattr(discord.utils, "utcnow", lambda: NOW)
    return NOW


def ends(seconds: float) -> str:
    """How a reply shows an end time `seconds` from NOW."""
    return f"<t:{int((NOW + timedelta(seconds=seconds)).timestamp())}:R>"


def in_voice(member_channel: Any, bot_channel: Any) -> Any:
    """A member in `member_channel` while SobaFM is in `bot_channel`."""
    guild = bot_channel.guild
    guild.voice_client = make_voice_client(bot_channel)
    member = make_member(member_channel)
    member.guild = guild
    member.guild_permissions = discord.Permissions.none()
    return member


async def test_play_needs_sobafm_in_a_voice_channel(bot: SobaFM) -> None:
    member = make_member(make_channel(make_guild()))
    member.guild = make_guild()

    assert await bot.admit(member, "ambient") == (
        "SobaFM isn't in a voice channel. Ask a server manager to use /join."
    )


@pytest.mark.parametrize("request_text", ["   ", "\u202e \u2066"], ids=["blank", "bidi controls"])
async def test_play_needs_a_description(bot: SobaFM, request_text: str) -> None:
    guild = make_guild()
    channel = make_channel(guild)

    assert await bot.admit(in_voice(channel, channel), request_text) == (
        "Describe the music you want, for example: rainy lo-fi with soft piano."
    )


async def test_play_needs_the_caller_in_sobafms_channel(bot: SobaFM) -> None:
    guild = make_guild()
    member = in_voice(make_channel(guild, 11), make_channel(guild))

    assert await bot.admit(member, "ambient") == "Join <#10> to request music."


async def test_play_reports_what_is_playing(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, now: datetime
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    member.mention = "<@5>"
    station = fake_station()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))

    assert await bot.admit(member, "ambient drones") is None
    assert await bot.play(member, "ambient drones", None) == (
        "Now playing **ambient drones**, requested by <@5>.\n"
        "Style: ambient drones\n"
        f"Ends {ends(3600)}."
    )
    assert station.play.call_args.args[0].prompts[0].text == "ambient drones"


LOFI = MusicPlan(title="Rainy lo-fi", prompts=[Prompt(text="lo-fi hip hop")], bpm=80)


async def test_play_refines_the_current_plan(bot: SobaFM, gemini: FakeGemini) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    member.mention = "<@5>"
    station = fake_station()
    station.program = fake(Program, plan=LOFI)
    bot.stations[guild.id] = station
    refined = {"title": "Faster lo-fi", "prompts": [{"text": "lo-fi hip hop"}], "bpm": 100}
    gemini.response = answer(json.dumps({"kind": "refine", "plan": refined}))

    reply = await bot.play(member, "faster", None)

    assert json.loads(gemini.calls[-1]["contents"])["current_plan"] == LOFI.model_dump(mode="json")
    assert station.play.call_args.args[0].bpm == 100
    assert reply.startswith(
        "Now playing **Faster lo-fi**, requested by <@5>.\nStyle: lo-fi hip hop, 100 BPM\n"
    )


@pytest.mark.parametrize(
    ("response", "refusal"),
    [
        (answer(json.dumps({"kind": "not_music"})), REFUSALS[Interpreted.NOT_MUSIC]),
        (answer("", finish=types.FinishReason.SAFETY), REFUSALS[Interpreted.BLOCKED]),
    ],
    ids=["not music", "blocked"],
)
async def test_play_refuses_without_touching_the_music(
    bot: SobaFM, gemini: FakeGemini, response: types.GenerateContentResponse, refusal: str
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    station = fake_station()
    station.program = fake(Program, plan=LOFI)
    bot.stations[guild.id] = station
    gemini.response = response
    cooldown = await admitted(bot, member, "what's the weather?")

    reply = await bot.play(member, "what's the weather?", cooldown)

    assert reply == refusal
    assert station.method_calls == []  # the current music keeps playing
    assert await bot.admit(member, "jazz") is None  # nothing changed, so no cooldown


async def test_play_reports_a_rejected_key_once_lyria_rejects_it_too(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    station = fake_station(Outcome.REJECTED)
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    info = {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}
    body = {"code": 400, "status": "INVALID_ARGUMENT", "details": [info]}
    gemini.error = errors.ClientError(400, {"error": body})

    reply = await bot.play(in_voice(channel, channel), "rainy lo-fi", None)

    # Gemini's rejection falls back to the request text (AI-4), which Lyria RealTime rejects.
    assert station.play.call_args.args[0] == MusicPlan.from_request("rainy lo-fi")
    assert reply == PLAY_REPLIES[Outcome.REJECTED]


def test_each_cause_has_its_own_reply() -> None:
    assert "API key" in PLAY_REPLIES[Outcome.REJECTED]
    assert "quota" in PLAY_REPLIES[Outcome.EXHAUSTED]
    assert "unavailable" in PLAY_REPLIES[Outcome.UNAVAILABLE]
    causes = [Outcome.REJECTED, Outcome.EXHAUSTED, Outcome.UNAVAILABLE, Outcome.FAILED]
    assert len({PLAY_REPLIES[outcome] for outcome in causes}) == len(causes)


async def test_a_refused_request_never_reaches_lyria(
    bot: SobaFM, gemini: FakeGemini, lyria: FakeLyria
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    gemini.response = answer(json.dumps({"kind": "not_music"}))

    await bot.play(in_voice(channel, channel), "what's the weather?", None)

    assert guild.id not in bot.stations
    assert lyria.sessions == []
    assert bot.stop(guild) == "Nothing is playing."  # nothing is left being interpreted


async def test_a_newer_request_supersedes_one_being_interpreted(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    station = fake_station()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    held = gemini.held = asyncio.Event()
    older = asyncio.create_task(bot.play(member, "jazz", None))
    await settle()  # Gemini holds the older request...
    assert len(gemini.calls) == 1
    gemini.held = None

    await bot.play(member, "lo-fi", None)  # ...and answers the newer one first

    assert bot.stop(guild) == "Nothing is playing."  # the replaced request is no longer tracked
    held.set()
    assert await older == PLAY_REPLIES[Outcome.REPLACED]
    assert [call.args[0].title for call in station.play.call_args_list] == ["lo-fi"]


async def test_an_older_request_answered_first_plays_until_a_newer_one(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    station = fake_station()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    first = gemini.held = asyncio.Event()
    older = asyncio.create_task(bot.play(member, "jazz", None))
    await settle()  # the older request is with Gemini when the newer one arrives
    assert len(gemini.calls) == 1
    second = gemini.held = asyncio.Event()
    newer = asyncio.create_task(bot.play(member, "lo-fi", None))
    await settle()
    assert len(gemini.calls) == 2

    first.set()  # Gemini answers the older request first
    assert (await older).startswith("Now playing **jazz**")
    second.set()
    assert (await newer).startswith("Now playing **lo-fi**")
    assert [call.args[0].title for call in station.play.call_args_list] == ["jazz", "lo-fi"]


async def test_a_refused_request_leaves_an_older_one_to_play(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    station = fake_station()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    held = gemini.held = asyncio.Event()
    older = asyncio.create_task(bot.play(member, "jazz", None))
    await settle()
    assert len(gemini.calls) == 1
    gemini.held, gemini.response = None, answer(json.dumps({"kind": "not_music"}))

    assert await bot.play(member, "what's the weather?", None) == REFUSALS[Interpreted.NOT_MUSIC]
    held.set()
    assert (await older).startswith("Now playing **jazz**")
    assert [call.args[0].title for call in station.play.call_args_list] == ["jazz"]


async def test_a_request_that_ends_first_leaves_newer_ones_to_stop(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    station = fake_station()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    first = gemini.held = asyncio.Event()
    gemini.response = answer(json.dumps({"kind": "not_music"}))
    older = asyncio.create_task(bot.play(member, "what's the weather?", None))
    await settle()
    assert len(gemini.calls) == 1
    second = gemini.held = asyncio.Event()
    gemini.response = None
    newer = asyncio.create_task(bot.play(member, "jazz", None))
    await settle()
    assert len(gemini.calls) == 2
    first.set()
    assert await older == REFUSALS[Interpreted.NOT_MUSIC]  # while the newer one is with Gemini

    assert bot.stop(guild) == "Stopped the request before it played."
    second.set()
    assert await newer == PLAY_REPLIES[Outcome.STOPPED]
    station.play.assert_not_called()


async def test_stop_supersedes_a_request_being_interpreted(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    station = fake_station()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    held = gemini.held = asyncio.Event()
    request = asyncio.create_task(bot.play(in_voice(channel, channel), "jazz", None))
    await settle()
    assert len(gemini.calls) == 1

    assert bot.stop(guild) == "Stopped the request before it played."
    held.set()
    assert await request == PLAY_REPLIES[Outcome.STOPPED]
    station.play.assert_not_called()


async def test_a_stopped_request_stays_stopped_when_a_newer_one_plays(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    monkeypatch.setattr(bot, "station", MagicMock(return_value=fake_station()))
    held = gemini.held = asyncio.Event()
    older = asyncio.create_task(bot.play(member, "jazz", None))
    await settle()
    assert len(gemini.calls) == 1
    assert bot.stop(guild) == "Stopped the request before it played."
    gemini.held = None

    assert (await bot.play(member, "lo-fi", None)).startswith("Now playing **lo-fi**")
    assert bot.stop(guild) == "Nothing is playing."  # the stopped request is no longer tracked
    held.set()
    assert await older == PLAY_REPLIES[Outcome.STOPPED]


async def test_a_request_the_station_rejects_leaves_older_ones_to_play(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    station = fake_station()
    station.play.side_effect = [RuntimeError("a bug"), station.play.return_value]
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    held = gemini.held = asyncio.Event()
    older = asyncio.create_task(bot.play(member, "jazz", None))
    await settle()
    assert len(gemini.calls) == 1
    gemini.held = None

    with pytest.raises(RuntimeError):
        await bot.play(member, "lo-fi", None)

    held.set()
    assert (await older).startswith("Now playing **jazz**")


async def test_a_request_from_before_leaving_creates_no_station(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    guild.voice_client = None  # SobaFM left before the request was tracked

    assert await bot.play(member, "jazz", None) == PLAY_REPLIES[Outcome.DISCONNECTED]
    assert guild.id not in bot.stations


async def test_closing_ends_requests_being_interpreted(bot: SobaFM, gemini: FakeGemini) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    held = gemini.held = asyncio.Event()
    request = asyncio.create_task(bot.play(in_voice(channel, channel), "jazz", None))
    await settle()
    assert len(gemini.calls) == 1

    await bot.close()

    held.set()
    assert await request == PLAY_REPLIES[Outcome.STOPPED]
    assert guild.id not in bot.stations


async def test_stop_supersedes_a_request_reading_its_settings(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    station = fake_station()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    read = asyncio.Event()
    settings = bot.store.settings

    async def slow_settings(guild_id: int) -> GuildSettings:
        await read.wait()
        return await settings(guild_id)

    monkeypatch.setattr(bot.store, "settings", slow_settings)
    request = asyncio.create_task(bot.play(in_voice(channel, channel), "jazz", None))
    await settle()  # interpreted, and waiting for the server's settings

    assert bot.stop(guild) == "Stopped the request before it played."
    read.set()
    assert await request == PLAY_REPLIES[Outcome.STOPPED]
    station.play.assert_not_called()


async def test_leaving_supersedes_a_request_being_interpreted(
    bot: SobaFM, gemini: FakeGemini
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    held = gemini.held = asyncio.Event()
    request = asyncio.create_task(bot.play(in_voice(channel, channel), "jazz", None))
    await settle()
    assert len(gemini.calls) == 1

    await bot.close_station(guild, Outcome.DISCONNECTED)

    held.set()
    assert await request == PLAY_REPLIES[Outcome.DISCONNECTED]
    assert guild.id not in bot.stations  # no station for a server SobaFM has left


async def test_a_slow_start_announces_the_program_once_it_plays(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch, now: datetime
) -> None:
    started = slow_station(bot, monkeypatch)
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    member.mention = "<@5>"
    gemini.error = errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})
    announced: list[str] = []

    async def announce(answer: str) -> None:
        announced.append(answer)

    # Played as typed, so the request reaches the answer, which must be escaped.
    reply = await bot.play(member, "rainy lo-fi\u202e discord.gg/x", None, announce)
    assert reply.startswith("The music is taking longer than usual to start.")
    await settle()
    assert announced == []

    started.set_result(Outcome.PLAYING)
    await settle()

    assert announced == [
        "Now playing **rainy lo-fi discord\\.gg/x**, requested by <@5>.\n"
        "Style: rainy lo-fi discord\\.gg/x\n"
        f"Ends {ends(3600)}.\n"
        "Gemini is unavailable right now, so the request was used as typed."
    ]


@pytest.mark.parametrize("outcome", [Outcome.UNAVAILABLE, Outcome.REPLACED, Outcome.STOPPED])
async def test_a_slow_start_announces_why_it_didnt_play(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, outcome: Outcome
) -> None:
    started = slow_station(bot, monkeypatch)
    guild = make_guild()
    channel = make_channel(guild)
    announced: list[str] = []

    async def announce(answer: str) -> None:
        announced.append(answer)

    await bot.play(in_voice(channel, channel), "ambient", None, announce)
    started.set_result(outcome)
    await settle()

    assert announced == [PLAY_REPLIES[outcome]]


async def test_a_failed_announcement_is_logged(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    started = slow_station(bot, monkeypatch)
    guild = make_guild()
    channel = make_channel(guild)

    async def announce(answer: str) -> None:  # the interaction's token has expired
        raise discord.NotFound(MagicMock(status=404), "Unknown Webhook")

    await bot.play(in_voice(channel, channel), "ambient", None, announce)
    with caplog.at_level(logging.WARNING, logger="sobafm.bot"):
        started.set_result(Outcome.PLAYING)
        await settle()

    assert "Could not update the answer to a slow start" in caplog.text
    assert not bot.announcements


async def test_closing_cancels_pending_announcements(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = slow_station(bot, monkeypatch)
    guild = make_guild()
    channel = make_channel(guild)
    announced: list[str] = []

    async def announce(answer: str) -> None:
        announced.append(answer)

    await bot.play(in_voice(channel, channel), "ambient", None, announce)
    await settle()  # the announcement waits for the start to settle
    async with asyncio.timeout(1):
        await bot.close()

    assert not started.cancelled()  # the program's own future is left alone
    started.set_result(Outcome.STOPPED)
    await settle()
    assert announced == []


async def test_closing_drops_announcements_before_closing_stations(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = slow_station(bot, monkeypatch)
    guild = make_guild()
    channel = make_channel(guild)
    station = cast(Any, bot.station).return_value

    async def close(*_: Any) -> None:  # closing the station settles the start
        if not started.done():
            started.set_result(Outcome.STOPPED)

    station.close.side_effect = close
    station.program = None
    bot.stations[guild.id] = station
    announced: list[str] = []

    async def announce(answer: str) -> None:
        announced.append(answer)

    await bot.play(in_voice(channel, channel), "ambient", None, announce)
    await settle()
    await bot.close()
    await settle()

    assert announced == []


async def test_a_fast_start_announces_nothing(bot: SobaFM, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot, "station", MagicMock(return_value=fake_station()))
    guild = make_guild()
    channel = make_channel(guild)
    announced: list[str] = []

    async def announce(answer: str) -> None:
        announced.append(answer)

    reply = await bot.play(in_voice(channel, channel), "ambient", None, announce)
    await settle()

    assert reply.startswith("Now playing")
    assert announced == []
    assert not bot.announcements


async def test_a_slow_start_announces_the_end_time_from_the_hand_off(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter([NOW])  # Discord's clock at the hand-off; later readings run 30 s on
    monkeypatch.setattr(discord.utils, "utcnow", lambda: next(times, NOW + timedelta(seconds=30)))
    started = slow_station(bot, monkeypatch)
    guild = make_guild()
    channel = make_channel(guild)
    announced: list[str] = []

    async def announce(answer: str) -> None:
        announced.append(answer)

    await bot.play(in_voice(channel, channel), "ambient", None, announce)
    started.set_result(Outcome.PLAYING)
    await settle()

    assert announced[0].endswith(f"Ends {ends(3600)}.")


def test_disables_mentions_by_default(bot: SobaFM) -> None:
    # Answers and their edits quote model-written text, which could mention @everyone (FB-4).
    assert bot.allowed_mentions is not None
    assert bot.allowed_mentions.to_dict() == {"parse": []}


async def test_a_slow_start_says_when_the_request_was_used_as_typed(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow_station(bot, monkeypatch)
    guild = make_guild()
    channel = make_channel(guild)
    gemini.error = errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})

    reply = await bot.play(in_voice(channel, channel), "rainy lo-fi", None)

    assert reply == (
        "The music is taking longer than usual to start."
        "\nGemini is unavailable right now, so the request was used as typed."
    )


async def test_play_reports_the_end_time_from_when_the_station_took_the_request(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter([NOW])  # Discord's clock at the hand-off; later readings run 30 s on
    monkeypatch.setattr(discord.utils, "utcnow", lambda: next(times, NOW + timedelta(seconds=30)))
    guild = make_guild()
    channel = make_channel(guild)
    monkeypatch.setattr(bot, "station", MagicMock(return_value=fake_station()))

    reply = await bot.play(in_voice(channel, channel), "ambient", None)

    assert reply.endswith(f"Ends {ends(3600)}.")


@pytest.mark.parametrize(
    ("error", "note"),
    [
        (
            errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}}),
            "Gemini is unavailable right now, so the request was used as typed.",
        ),
        (
            errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}),
            "Gemini's quota is used up for now, so the request was used as typed.",
        ),
        (RuntimeError("a bug"), "Gemini couldn't interpret the request, so it was used as typed."),
    ],
    ids=["outage", "quota", "other"],
)
async def test_play_says_why_the_request_was_used_as_typed(
    bot: SobaFM,
    gemini: FakeGemini,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    error: Exception,
    note: str,
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    member.mention = "<@5>"
    station = fake_station()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    gemini.error = error

    reply = await bot.play(member, "rainy lo-fi", None)

    assert reply == (
        "Now playing **rainy lo-fi**, requested by <@5>.\n"
        "Style: rainy lo-fi\n"
        f"Ends {ends(3600)}.\n" + note
    )
    assert station.play.call_args.args[0] == MusicPlan.from_request("rainy lo-fi")


async def test_play_says_when_a_failed_request_was_added_to_the_current_music(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    member.mention = "<@5>"
    station = fake_station()
    current = MusicPlan.from_request("rainy lo-fi piano")
    station.program = MagicMock(plan=current)
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    monkeypatch.setattr(bot, "stations", {guild.id: station})
    gemini.error = errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})

    reply = await bot.play(member, "darker", None)

    assert reply.endswith(
        "\nGemini is unavailable right now, so the request was added to the current music as typed."
    )
    assert station.play.call_args.args[0] == current.with_request("darker")


async def test_play_escapes_the_title(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    member.mention = "<@5>"
    monkeypatch.setattr(bot, "station", MagicMock(return_value=fake_station()))
    prompt = "*lo-fi*\nEnds <t:0:R>. https://evil.example"  # a line, a timestamp, and a link
    plan = {"title": "**Loud**\n_lo-fi_\u202e", "prompts": [{"text": prompt}]}
    gemini.response = answer(json.dumps({"kind": "new", "plan": plan}))

    reply = await bot.play(member, "lo-fi", None)

    title, style, _ = reply.splitlines()
    assert title == "Now playing **\\*\\*Loud\\*\\* \\_lo-fi\\_**, requested by <@5>."
    assert style == "Style: \\*lo-fi\\* Ends \\<t\\:0\\:R>\\. https\\://evil\\.example"


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome.BUSY,
        Outcome.REFUSED,
        Outcome.REJECTED,
        Outcome.EXHAUSTED,
        Outcome.UNAVAILABLE,
        Outcome.FAILED,
    ],
)
async def test_play_explains_failures(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, outcome: Outcome
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    monkeypatch.setattr(bot, "station", MagicMock(return_value=fake_station(outcome)))

    assert await bot.play(member, "ambient", None) == PLAY_REPLIES[outcome]


def playing_station(plan: MusicPlan, *, time_left: float, started: bool) -> Any:
    """A station with `plan` as its program, which plays once `started`."""
    program = Program(plan, "<@5>", ends_at=0.0)
    if started:
        program.settle(Outcome.PLAYING)
    return fake(Station, program=program, time_left=time_left)


async def test_now_says_when_nothing_is_playing(bot: SobaFM) -> None:
    guild = make_guild()
    assert bot.now(guild) == "Nothing is playing."

    bot.stations[GUILD_ID] = fake(Station, program=None, time_left=None)
    assert bot.now(guild) == "Nothing is playing."


@pytest.mark.parametrize(("started", "phase"), [(False, "Starting"), (True, "Now playing")])
async def test_now_describes_the_program(
    bot: SobaFM, now: datetime, *, started: bool, phase: str
) -> None:
    bot.stations[GUILD_ID] = playing_station(LOFI, time_left=600, started=started)

    assert bot.now(make_guild()) == (
        f"{phase} **Rainy lo-fi**, requested by <@5>.\n"
        "Style: lo-fi hip hop, 80 BPM\n"
        f"Ends {ends(600)}."
    )


async def test_now_escapes_the_program(bot: SobaFM, now: datetime) -> None:
    plan = MusicPlan(
        title="Lo-fi\u202e https://evil.example", prompts=[Prompt(text="discord.gg/x")]
    )
    bot.stations[GUILD_ID] = playing_station(plan, time_left=600, started=True)

    assert bot.now(make_guild()) == (
        "Now playing **Lo-fi https\\://evil\\.example**, requested by <@5>.\n"
        "Style: discord\\.gg/x\n"
        f"Ends {ends(600)}."
    )


def status_channel(guild: Any, *, allowed: bool) -> Any:
    """SobaFM's voice channel, where it may or may not set the status."""
    channel = make_channel(guild)
    channel.permissions_for.return_value = discord.Permissions(set_voice_channel_status=allowed)
    channel.edit = AsyncMock()
    return channel


@pytest.mark.parametrize("status", ["Rainy lo-fi", None])
async def test_sets_the_voice_channel_status(bot: SobaFM, status: str | None) -> None:
    channel = status_channel(make_guild(), allowed=True)

    assert await bot.show_voice_status(CHANNEL_ID, status)
    channel.edit.assert_awaited_once_with(status=status)


async def test_skips_the_status_without_permission(bot: SobaFM) -> None:
    channel = status_channel(make_guild(), allowed=False)

    assert not await bot.show_voice_status(CHANNEL_ID, "Rainy lo-fi")
    channel.edit.assert_not_awaited()


async def test_skips_the_status_of_a_stage_channel(bot: SobaFM) -> None:
    channel = make_channel(make_guild(), kind=discord.StageChannel)
    channel.permissions_for.return_value = discord.Permissions(set_voice_channel_status=True)
    channel.edit = AsyncMock()

    assert not await bot.show_voice_status(CHANNEL_ID, "Rainy lo-fi")
    channel.edit.assert_not_awaited()


async def test_skips_the_status_of_a_channel_that_is_gone(bot: SobaFM) -> None:
    assert not await bot.show_voice_status(CHANNEL_ID, "Rainy lo-fi")


async def test_leaves_a_refused_status_to_the_station(bot: SobaFM) -> None:
    channel = status_channel(make_guild(), allowed=True)
    channel.edit.side_effect = discord.HTTPException(MagicMock(status=403), "Missing Access")

    with pytest.raises(discord.HTTPException):
        await bot.show_voice_status(CHANNEL_ID, "Rainy lo-fi")


async def test_shows_the_status_only_once_connected(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    guild.voice_client.is_connected.return_value = False  # still in the handshake

    assert voice_channel_id(guild) is None

    guild.voice_client.is_connected.return_value = True
    assert voice_channel_id(guild) == CHANNEL_ID


@pytest.mark.parametrize(
    ("text", "escaped"),
    [
        (
            "[a](b) **lofi** [Free Nitro](https://evil.example)",
            "\\[a\\](b) \\*\\*lofi\\*\\* \\[Free Nitro\\](https\\://evil\\.example)",
        ),
        ("<t:0:R> <@5> <#10> <:x:1>", "\\<t\\:0\\:R> \\<@5> \\<#10> \\<\\:x\\:1>"),
        ("a\\b ~~s~~ ||p|| `c` __u__", "a\\\\b \\~\\~s\\~\\~ \\|\\|p\\|\\| \\`c\\` \\_\\_u\\_\\_"),
        (
            "lo-fi https://evil.example steam://run/1 discord.gg/x",
            "lo-fi https\\://evil\\.example steam\\://run/1 discord\\.gg/x",
        ),
        (
            "discord\u3002gg/x discord\uff0egg/x discord\uff61gg/x",
            "discord\\\u3002gg/x discord\\\uff0egg/x discord\\\uff61gg/x",
        ),
        ("Rainy lo-fi #2: 50% off > 3", "Rainy lo-fi #2\\: 50% off > 3"),
        ("Rainy lo-fi", "Rainy lo-fi"),
    ],
    ids=[
        "masked links",
        "mentions and timestamps",
        "inline markup",
        "links",
        "lookalike dots",
        "punctuation",
        "plain",
    ],
)
def test_escapes_each_markup_character(text: str, escaped: str) -> None:
    assert escape(text) == escaped
    # Discord shows a backslash before punctuation as the punctuation alone.
    assert re.sub(r"\\([^0-9A-Za-z\s])", r"\1", escaped) == text


async def test_a_station_shows_its_status_in_sobafms_channel(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, "show_voice_status", shown)
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    station = bot.station(guild, 0.5)
    station.play(LOFI, "<@5>", duration_seconds=600)
    assert station.program is not None
    station.program.settle(Outcome.PLAYING)  # as the station does once the program is heard

    await station.reconcile()
    await settle()
    await station.close()

    assert shown.await_args_list == [((CHANNEL_ID, "Rainy lo-fi"),), ((CHANNEL_ID, None),)]


async def test_stop_needs_the_channel_or_manage_server(bot: SobaFM) -> None:
    guild = make_guild()
    member = in_voice(make_channel(guild, 11), make_channel(guild))
    assert bot.stop_problem(member) == "Join <#10> to stop the music."

    member.guild_permissions = discord.Permissions(manage_guild=True)
    assert bot.stop_problem(member) is None


async def test_stop_ends_the_program(bot: SobaFM) -> None:
    guild = make_guild()
    assert bot.stop(guild) == "Nothing is playing."

    station = fake_station()
    station.program = object()
    bot.stations[guild.id] = station

    assert bot.stop(guild) == "Stopped the music."
    station.stop.assert_called_once()


async def test_leave_closes_the_station(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    station = fake_station()
    bot.stations[guild.id] = station

    async def close(outcome: Outcome = Outcome.STOPPED) -> None:  # while SobaFM is still there
        guild.voice_client.disconnect.assert_not_awaited()

    station.close.side_effect = close

    await bot.leave(guild)

    station.close.assert_awaited_once()
    assert guild.id not in bot.stations


async def test_leaving_voice_closes_the_station(bot: SobaFM) -> None:
    guild = make_guild()
    station = fake_station()
    bot.stations[guild.id] = station

    await bot.on_voice_lost(guild)

    station.close.assert_awaited_once_with(Outcome.DISCONNECTED)
    assert guild.id not in bot.stations


async def test_a_voice_reconnect_keeps_the_station(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    bot.stations[guild.id] = fake_station()

    await bot.on_voice_state_update(*bot_voice_update(guild, channel, None))
    await bot.on_voice_state_update(*bot_voice_update(guild, None, channel))

    assert guild.id in bot.stations


async def test_play_command_answers_publicly_once_the_music_starts(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "admit", AsyncMock(return_value=None))
    monkeypatch.setattr(bot, "play", AsyncMock(return_value="Now playing **ambient**."))
    interaction = make_interaction(make_member(None))
    command: Any = bot.tree.get_command("play")

    await command.callback(interaction, request="ambient")

    interaction.response.defer.assert_awaited_once_with()
    interaction.followup.send.assert_awaited_once_with(
        "Now playing **ambient**.", suppress_embeds=True, wait=True
    )


async def test_play_command_edits_a_slow_starts_answer(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    announced: list[Callable[[str], Awaitable[object]]] = []

    async def play(*args: Any) -> str:
        announced.append(args[3])
        return "The music is taking longer than usual to start."

    monkeypatch.setattr(bot, "admit", AsyncMock(return_value=None))
    monkeypatch.setattr(bot, "play", play)
    interaction = make_interaction(make_member(None))
    message = fake(discord.WebhookMessage, edit=AsyncMock())
    interaction.followup.send.return_value = message
    command: Any = bot.tree.get_command("play")

    await command.callback(interaction, request="ambient")
    async with asyncio.timeout(1):
        await announced[0]("Now playing **ambient**.")

    message.edit.assert_awaited_once_with(content="Now playing **ambient**.")


def slow_play_command(bot: SobaFM, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    """`/play` with a slow start: the start's future, the interaction, and the message it sends."""
    started = slow_station(bot, monkeypatch)
    monkeypatch.setattr(bot, "admit", AsyncMock(return_value=None))
    guild = make_guild()
    channel = make_channel(guild)
    interaction = make_interaction(in_voice(channel, channel))
    message = fake(discord.WebhookMessage, edit=AsyncMock())
    interaction.followup.send.return_value = message
    return started, interaction, message


async def test_play_command_edits_its_answer_only_once_sent(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, interaction, message = slow_play_command(bot, monkeypatch)

    async def send(*_: Any, **__: Any) -> Any:  # the start settles while the answer is sent
        started.set_result(Outcome.PLAYING)
        await settle()
        message.edit.assert_not_awaited()
        return message

    interaction.followup.send.side_effect = send
    command: Any = bot.tree.get_command("play")

    await command.callback(interaction, request="ambient")
    await settle()

    message.edit.assert_awaited_once()


async def test_a_shutdown_during_the_answer_leaves_play_answered(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, interaction, message = slow_play_command(bot, monkeypatch)

    async def send(*_: Any, **__: Any) -> Any:  # the start settles, then SobaFM shuts down
        started.set_result(Outcome.PLAYING)
        await settle()
        await bot.close()
        return message

    interaction.followup.send.side_effect = send
    command: Any = bot.tree.get_command("play")

    async with asyncio.timeout(1):
        await command.callback(interaction, request="ambient")  # no error after answering

    message.edit.assert_not_awaited()


async def test_a_failed_answer_leaves_nothing_to_announce(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    started, interaction, message = slow_play_command(bot, monkeypatch)
    interaction.followup.send.side_effect = discord.NotFound(MagicMock(status=404), "expired")
    command: Any = bot.tree.get_command("play")

    with pytest.raises(discord.NotFound):
        await command.callback(interaction, request="ambient")
    caplog.clear()  # what /play logged, which a lower --log-level captures
    with caplog.at_level(logging.WARNING):
        started.set_result(Outcome.PLAYING)
        await settle()

    assert not bot.announcements  # the announcement ended quietly
    assert not caplog.records
    message.edit.assert_not_awaited()


async def test_play_command_explains_problems_privately(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "admit", AsyncMock(return_value="Join <#10> first."))
    interaction = make_interaction(make_member(None))
    command: Any = bot.tree.get_command("play")

    await command.callback(interaction, request="ambient")

    interaction.response.send_message.assert_awaited_once_with("Join <#10> first.", ephemeral=True)
    interaction.response.defer.assert_not_awaited()


async def test_now_command_replies_privately(bot: SobaFM, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot, "now", MagicMock(return_value="Nothing is playing."))
    interaction = make_interaction(make_member(None))
    interaction.guild = make_guild()
    command: Any = bot.tree.get_command("now")

    await command.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "Nothing is playing.", ephemeral=True, suppress_embeds=True
    )


async def test_stop_command_replies_privately(bot: SobaFM, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot, "stop_problem", MagicMock(return_value=None))
    monkeypatch.setattr(bot, "stop", MagicMock(return_value="Stopped the music."))
    interaction = make_interaction(make_member(None))
    command: Any = bot.tree.get_command("stop")

    await command.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with("Stopped the music.", ephemeral=True)


async def test_play_says_when_the_music_is_slow_to_start(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = slow_station(bot, monkeypatch)
    guild = make_guild()
    channel = make_channel(guild)

    reply = await bot.play(in_voice(channel, channel), "ambient", None)

    assert reply == "The music is taking longer than usual to start."
    assert not started.done()  # the station still settles it later


async def test_closing_the_client_closes_every_station(bot: SobaFM) -> None:
    station = fake_station()
    bot.stations[GUILD_ID] = station

    bot.check_voice.start()

    await bot.close()

    station.close.assert_awaited_once()
    assert bot.stations == {}
    assert not bot.check_voice.is_running()


@pytest.mark.parametrize(
    ("kind", "counted"),
    [
        ("member", True),
        ("bot", False),
        ("self-deafened member", False),
        ("server-deafened member", False),
        ("member missing from the cache", True),
        ("deafened member missing from the cache", False),
    ],
)
def test_counts_listeners_who_are_neither_bots_nor_deafened(kind: str, *, counted: bool) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.voice_client = make_voice_client(channel)
    cached = {
        "member": fake(discord.Member, bot=False),
        "bot": fake(discord.Member, bot=True),  # including SobaFM itself
        "self-deafened member": fake(discord.Member, bot=False),
        "server-deafened member": fake(discord.Member, bot=False),
    }
    guild.get_member.return_value = cached.get(kind)
    channel.voice_states = {
        1: fake(
            discord.VoiceState,
            self_deaf=kind == "self-deafened member",
            deaf=kind in ("server-deafened member", "deafened member missing from the cache"),
        )
    }

    assert listeners(guild) == int(counted)


def test_counts_no_listeners_without_a_voice_connection() -> None:
    assert listeners(make_guild()) == 0


def test_looks_members_up_in_the_current_guild() -> None:
    current = make_guild()
    channel = make_channel(current)
    current.get_member.return_value = fake(discord.Member, bot=True)
    stale = make_guild(voice_client=make_voice_client(channel))  # from an earlier session
    stale.get_member.return_value = None
    channel.voice_states = {1: fake(discord.VoiceState, self_deaf=False, deaf=False)}

    assert listeners(stale) == 0


async def test_stations_count_the_listeners_in_their_server(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    counted = MagicMock(return_value=1)
    monkeypatch.setattr(sobafm.bot, "listeners", counted)
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    station = bot.station(guild, 0.5)
    station.play(MusicPlan.from_request("ambient"), "Member", duration_seconds=3600)

    await station.reconcile()

    counted.assert_called_with(guild)
    await station.close()


def test_settings_command_options_are_bounded(bot: SobaFM) -> None:
    command = bot.tree.get_command("settings")
    assert command is not None

    payload = command.to_dict(bot.tree)

    bounds = {
        o["name"]: (o["min_value"], o["max_value"], o["required"]) for o in payload["options"]
    }
    assert bounds == {
        "duration": (5, 240, False),
        "volume": (1, 100, False),
        "cooldown": (0, 600, False),
    }


async def test_settings_show_the_defaults(bot: SobaFM) -> None:
    reply = await bot.configure(make_guild(), duration_minutes=None, volume_percent=None)

    assert reply == (
        "SobaFM's settings for this server:\n"
        "- Play duration: 60 minutes\n"
        "- Volume: 50%\n"
        "- Change cooldown: 30 seconds"
    )


async def test_settings_save_changes_and_apply_the_volume_at_once(bot: SobaFM) -> None:
    guild = make_guild()
    station = fake_station()
    station.mixer = MagicMock()
    bot.stations[guild.id] = station

    reply = await bot.configure(guild, volume_percent=80, cooldown_seconds=0)

    assert reply.startswith("Saved. ")
    assert "- Volume: 80%" in reply
    assert await bot.store.settings(guild.id) == GuildSettings(
        volume_percent=80, cooldown_seconds=0
    )
    station.mixer.set_volume.assert_called_once_with(0.8)


async def test_a_new_station_plays_at_its_volume(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = MagicMock(return_value=fake_station())
    monkeypatch.setattr(sobafm.bot, "Station", created)

    bot.station(make_guild(), 0.8)

    assert created.call_args.kwargs["volume"] == 0.8


async def test_play_gives_each_program_the_servers_duration(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    station = fake_station()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    member = in_voice(channel, channel)
    await bot.play(member, "ambient", None)

    await bot.configure(guild, duration_minutes=5)  # applies from the next request
    await bot.play(member, "jazz", None)

    durations = [call.kwargs["duration_seconds"] for call in station.play.call_args_list]
    assert durations == [3600, 300]


async def test_settings_leave_the_program_playing_its_end_time(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    station = bot.station(guild, 0.5)
    station.play(MusicPlan.from_request("ambient"), "Member", duration_seconds=3600)
    assert station.program is not None
    ends_at = station.program.ends_at

    await bot.configure(guild, duration_minutes=5)

    assert station.program.ends_at == ends_at
    await station.close()


async def test_play_creates_the_station_at_the_stored_volume(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    await bot.store.save_settings(guild.id, GuildSettings(volume_percent=80))
    station = MagicMock(return_value=fake_station())
    monkeypatch.setattr(bot, "station", station)

    await bot.play(in_voice(channel, channel), "ambient", None)

    station.assert_called_once_with(guild, 0.8)


async def test_refuses_a_change_inside_the_cooldown(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    assert await bot.admit(member, "ambient") is None

    reply = await bot.admit(member, "jazz")

    assert reply == "The music changed recently. Try again in 30 seconds."


async def test_a_refused_request_leaves_the_cooldown_alone(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    outside = in_voice(make_channel(guild, 11), channel)
    assert await bot.admit(outside, "ambient") is not None
    member = in_voice(channel, channel)
    assert await bot.admit(member, " ") is not None

    assert await bot.admit(member, "jazz") is None


async def test_a_zero_cooldown_admits_every_change(bot: SobaFM) -> None:
    guild = make_guild()
    await bot.store.save_settings(guild.id, GuildSettings(cooldown_seconds=0))
    channel = make_channel(guild)
    member = in_voice(channel, channel)

    assert [await bot.admit(member, "jazz") for _ in range(3)] == [None, None, None]


async def test_settings_command_replies_privately(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure = AsyncMock(return_value="SobaFM's settings for this server: ...")
    monkeypatch.setattr(bot, "configure", configure)
    interaction = make_interaction(make_member(None))
    interaction.guild = make_guild()
    command: Any = bot.tree.get_command("settings")

    await command.callback(interaction, volume=80)

    configure.assert_awaited_once_with(
        interaction.guild, duration_minutes=None, volume_percent=80, cooldown_seconds=None
    )
    interaction.response.send_message.assert_awaited_once_with(
        "SobaFM's settings for this server: ...", ephemeral=True
    )


async def test_admits_one_of_several_concurrent_requests(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)

    replies = await asyncio.gather(*(bot.admit(member, "jazz") for _ in range(5)))

    assert replies.count(None) == 1


async def test_the_cooldown_counts_down_to_the_second(bot: SobaFM, clock: FakeClock) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    assert await bot.admit(member, "ambient") is None

    clock.now = 29.5
    assert await bot.admit(member, "jazz") == "The music changed recently. Try again in 1 second."
    clock.now = 30.0
    assert await bot.admit(member, "jazz") is None


@pytest.mark.parametrize("outcome", list(Outcome))
async def test_only_a_request_that_plays_keeps_the_cooldown(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, outcome: Outcome
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    monkeypatch.setattr(bot, "station", MagicMock(return_value=fake_station(outcome)))
    cooldown = await admitted(bot, member, "ambient")

    await bot.play(member, "ambient", cooldown)

    assert (await bot.admit(member, "jazz") is None) is (outcome is not Outcome.PLAYING)


@pytest.mark.parametrize("outcome", [Outcome.PLAYING, Outcome.FAILED])
async def test_a_slow_start_keeps_the_cooldown_until_it_settles(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, outcome: Outcome
) -> None:
    started = slow_station(bot, monkeypatch)
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    cooldown = await admitted(bot, member, "ambient")
    await bot.play(member, "ambient", cooldown)
    assert await bot.admit(member, "jazz") is not None  # it may still start

    started.set_result(outcome)
    await asyncio.sleep(0)

    assert (await bot.admit(member, "jazz") is None) is (outcome is not Outcome.PLAYING)


async def test_a_slow_start_never_frees_a_later_cooldown(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    started = slow_station(bot, monkeypatch)
    guild = make_guild()
    await bot.store.save_settings(guild.id, GuildSettings(cooldown_seconds=1))
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    first = await admitted(bot, member, "ambient")
    await bot.play(member, "ambient", first)
    clock.now = 1.5
    await admitted(bot, member, "jazz")

    started.set_result(Outcome.REPLACED)
    await asyncio.sleep(0)

    assert await bot.admit(member, "lo-fi") is not None


async def test_the_play_command_frees_the_cooldown_when_nothing_plays(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "station", MagicMock(return_value=fake_station(Outcome.FAILED)))
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    command: Any = bot.tree.get_command("play")

    await command.callback(make_interaction(member), request="ambient")

    assert await bot.admit(member, "jazz") is None


async def test_the_play_command_keeps_a_cooldown_started_during_its_reply(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    monkeypatch.setattr(bot, "station", MagicMock(return_value=fake_station(Outcome.REPLACED)))
    guild = make_guild()
    await bot.store.save_settings(guild.id, GuildSettings(cooldown_seconds=1))
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    interaction = make_interaction(member)

    async def admit_another() -> None:  # another request is admitted while this one defers
        clock.now = 1.5
        assert await bot.admit(member, "jazz") is None

    interaction.response.defer.side_effect = admit_another
    command: Any = bot.tree.get_command("play")

    await command.callback(interaction, request="ambient")

    assert await bot.admit(member, "lo-fi") is not None


async def test_a_request_that_raises_frees_the_cooldown(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "station", MagicMock(side_effect=RuntimeError("a bug")))
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    cooldown = await admitted(bot, member, "ambient")

    with pytest.raises(RuntimeError):
        await bot.play(member, "ambient", cooldown)

    assert await bot.admit(member, "jazz") is None


@pytest.mark.parametrize(
    "failure",
    [discord.NotFound(MagicMock(status=404), "expired"), aiohttp.ServerDisconnectedError()],
    ids=["expired", "disconnected"],
)
async def test_an_unanswered_interaction_frees_the_cooldown(
    bot: SobaFM, failure: Exception
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    interaction = make_interaction(member)
    interaction.response.defer.side_effect = failure
    command: Any = bot.tree.get_command("play")

    with pytest.raises(type(failure)):
        await command.callback(interaction, request="ambient")

    assert await bot.admit(member, "jazz") is None


async def test_settings_keep_what_a_change_leaves_out(bot: SobaFM) -> None:
    guild = make_guild()
    await bot.store.save_settings(guild.id, GuildSettings(duration_minutes=90))

    await bot.configure(guild, volume_percent=70)

    assert await bot.store.settings(guild.id) == GuildSettings(
        duration_minutes=90, volume_percent=70
    )


async def test_settings_reject_a_value_out_of_range(bot: SobaFM) -> None:
    guild = make_guild()

    with pytest.raises(ValidationError):
        await bot.configure(guild, volume_percent=101)

    assert await bot.store.settings(guild.id) == GuildSettings()


async def test_concurrent_settings_changes_both_apply(bot: SobaFM) -> None:
    guild = make_guild()

    await asyncio.gather(
        bot.configure(guild, volume_percent=80), bot.configure(guild, duration_minutes=90)
    )

    assert await bot.store.settings(guild.id) == GuildSettings(
        duration_minutes=90, volume_percent=80
    )


async def test_settings_name_a_single_second(bot: SobaFM) -> None:
    reply = await bot.configure(make_guild(), cooldown_seconds=1)

    assert reply.endswith("- Change cooldown: 1 second")
