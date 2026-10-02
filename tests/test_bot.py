import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
from discord.errors import ConnectionClosed
from discord.voice_state import VoiceConnectionState
from pydantic import ValidationError

import sobafm.bot
from sobafm.bot import PLAY_REPLIES, SobaFM, Voice, listeners
from sobafm.config import load_settings
from sobafm.plan import MusicPlan
from sobafm.station import Outcome, Station
from sobafm.store import GuildSettings, Store
from tests.doubles import FakeClock, FakeLyria, settle

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
async def bot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lyria: FakeLyria, clock: FakeClock
) -> SobaFM:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    store = Store(tmp_path / "sobafm.db")
    await store.initialize()
    bot = SobaFM(load_settings(env_file=None), store, connect=lyria.connect, clock=clock)
    CHANNELS.clear()
    monkeypatch.setattr(bot, "get_channel", CHANNELS.get)
    return bot


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


async def test_finish_connecting_waits_for_a_restarted_connector(bot: SobaFM) -> None:
    voice = make_voice(bot, make_guild(), current=True)
    connection = SimpleNamespace(_connector=None)
    cast(Any, voice)._connection = connection
    restart, release = asyncio.Event(), asyncio.Event()

    async def first() -> None:  # ends when discord.py replaces it with a new connector
        await restart.wait()
        connection._connector = asyncio.create_task(release.wait())

    connection._connector = asyncio.create_task(first())
    waiting = asyncio.create_task(voice.finish_connecting())
    await settle()
    restart.set()
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
    """A voice websocket that closes with `code`, then ends discord.py's poll loop."""

    def __init__(self, code: int) -> None:
        self.code: int | None = code

    async def poll_event(self) -> None:
        if (code := self.code) is None:
            raise asyncio.CancelledError
        self.code = None
        raise ConnectionClosed(MagicMock(), shard_id=None, code=code)


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


async def test_play_needs_a_description(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)

    assert await bot.admit(in_voice(channel, channel), "   ") == (
        "Describe the music you want, for example: rainy lo-fi with soft piano."
    )


async def test_play_needs_the_caller_in_sobafms_channel(bot: SobaFM) -> None:
    guild = make_guild()
    member = in_voice(make_channel(guild, 11), make_channel(guild))

    assert await bot.admit(member, "ambient") == "Join <#10> to request music."


async def test_play_reports_what_is_playing(bot: SobaFM, monkeypatch: pytest.MonkeyPatch) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    member.mention = "<@5>"
    station = fake_station()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))

    assert await bot.admit(member, "ambient drones") is None
    assert await bot.play(member, "ambient drones", None) == (
        "Now playing **ambient drones**, requested by <@5>."
    )
    assert station.play.call_args.args[0].prompts[0].text == "ambient drones"


@pytest.mark.parametrize("outcome", [Outcome.BUSY, Outcome.REFUSED, Outcome.FAILED])
async def test_play_explains_failures(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, outcome: Outcome
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    monkeypatch.setattr(bot, "station", MagicMock(return_value=fake_station(outcome)))

    assert await bot.play(member, "ambient", None) == PLAY_REPLIES[outcome]


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
    interaction.followup.send.assert_awaited_once_with("Now playing **ambient**.")


async def test_play_command_explains_problems_privately(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "admit", AsyncMock(return_value="Join <#10> first."))
    interaction = make_interaction(make_member(None))
    command: Any = bot.tree.get_command("play")

    await command.callback(interaction, request="ambient")

    interaction.response.send_message.assert_awaited_once_with("Join <#10> first.", ephemeral=True)
    interaction.response.defer.assert_not_awaited()


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

    await bot.close()

    station.close.assert_awaited_once()
    assert bot.stations == {}


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
    station.play(MusicPlan.from_request("ambient"), "Member")

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
