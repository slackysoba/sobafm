import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import sobafm.bot
from sobafm.bot import PLAY_REPLIES, SobaFM, Voice, listeners
from sobafm.config import load_settings
from sobafm.plan import MusicPlan
from sobafm.station import Outcome, Station
from sobafm.store import Store
from tests.doubles import FakeLyria

GUILD_ID = 1
CHANNEL_ID = 10


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
    channel.connect = AsyncMock(return_value=make_voice_client(channel))
    return channel


def make_member(channel: Any) -> Any:
    return fake(
        discord.Member, id=5, voice=fake(discord.VoiceState, channel=channel) if channel else None
    )


def make_voice_client(channel: Any) -> Any:
    voice = fake(discord.VoiceClient, channel=channel, disconnect=AsyncMock())

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
async def bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lyria: FakeLyria) -> SobaFM:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    store = Store(tmp_path / "sobafm.db")
    await store.initialize()
    return SobaFM(load_settings(env_file=None), store, connect=lyria.connect)


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


@pytest.mark.parametrize("name", ["join", "leave"])
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
    replacement = make_voice_client(make_channel(guild))
    guild.voice_client = replacement
    voice = make_voice(bot, guild, current=current, joined=joined)

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

    assert bot.play_problem(member, "ambient") == (
        "SobaFM isn't in a voice channel. Ask a server manager to use /join."
    )


async def test_play_needs_a_description(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)

    assert bot.play_problem(in_voice(channel, channel), "   ") == (
        "Describe the music you want, for example: rainy lo-fi with soft piano."
    )


async def test_play_needs_the_caller_in_sobafms_channel(bot: SobaFM) -> None:
    guild = make_guild()
    member = in_voice(make_channel(guild, 11), make_channel(guild))

    assert bot.play_problem(member, "ambient") == "Join <#10> to request music."


async def test_play_reports_what_is_playing(bot: SobaFM, monkeypatch: pytest.MonkeyPatch) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    member.mention = "<@5>"
    station = fake_station()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))

    assert bot.play_problem(member, "ambient drones") is None
    assert await bot.play(member, "ambient drones") == (
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

    assert await bot.play(member, "ambient") == PLAY_REPLIES[outcome]


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
    monkeypatch.setattr(bot, "play_problem", MagicMock(return_value=None))
    monkeypatch.setattr(bot, "play", AsyncMock(return_value="Now playing **ambient**."))
    interaction = make_interaction(make_member(None))
    command: Any = bot.tree.get_command("play")

    await command.callback(interaction, request="ambient")

    interaction.response.defer.assert_awaited_once_with()
    interaction.followup.send.assert_awaited_once_with("Now playing **ambient**.")


async def test_play_command_explains_problems_privately(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "play_problem", MagicMock(return_value="Join <#10> first."))
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
    monkeypatch.setattr(sobafm.bot, "START_TIMEOUT_S", 0.01)
    station = fake(Station, close=AsyncMock())
    station.play.return_value = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    guild = make_guild()
    channel = make_channel(guild)

    reply = await bot.play(in_voice(channel, channel), "ambient")

    assert reply == "The music is taking longer than usual to start."
    assert not station.play.return_value.done()  # the station still settles it later


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
    station = bot.station(guild)
    station.play(MusicPlan.from_request("ambient"), "Member")

    await station.reconcile()

    counted.assert_called_with(guild)
    await station.close()
