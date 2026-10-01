from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sobafm.bot import SobaFM
from sobafm.config import load_settings
from sobafm.store import Store

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


def bot_voice_update(guild: Any, before: Any, after: Any) -> tuple[Any, Any, Any]:
    me = fake(discord.Member, id=guild.me.id, guild=guild)
    return me, fake(discord.VoiceState, channel=before), fake(discord.VoiceState, channel=after)


@pytest.fixture
async def bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SobaFM:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    store = Store(tmp_path / "sobafm.db")
    await store.initialize()
    return SobaFM(load_settings(env_file=None), store)


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

    assert isinstance(command, discord.app_commands.Command)
    assert command.guild_only
    assert command.default_permissions == discord.Permissions(manage_guild=True)
    assert bot.tree.allowed_installs.guild
    assert not bot.tree.allowed_installs.user


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
        assert sync.await_args.kwargs["guild"].id == dev_guild_id


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
    channel.connect.assert_awaited_once_with(self_deaf=True)
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


async def test_forgets_the_channel_when_disconnected(bot: SobaFM) -> None:
    guild = make_guild()
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.on_voice_state_update(*bot_voice_update(guild, make_channel(guild), None))

    assert await bot.store.remembered_channel(GUILD_ID) is None


async def test_keeps_the_channel_when_shutting_down(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    monkeypatch.setattr(bot, "is_closed", MagicMock(return_value=True))

    await bot.on_voice_state_update(*bot_voice_update(guild, make_channel(guild), None))

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

    channel.connect.assert_awaited_once_with(self_deaf=True)
    assert await bot.store.remembered_channel(GUILD_ID) == CHANNEL_ID


async def test_rejoin_first_closes_a_connection_from_an_earlier_session(bot: SobaFM) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.get_channel.return_value = channel
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    stale = make_voice_client(channel)

    def disconnect(*, force: bool) -> None:
        channel.connect.assert_not_awaited()

    stale.disconnect.side_effect = disconnect
    bot.voice_connections[GUILD_ID] = stale

    await bot.on_guild_available(guild)

    stale.disconnect.assert_awaited_once_with(force=True)
    channel.connect.assert_awaited_once()
    assert bot.voice_connections[GUILD_ID] is not stale


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
