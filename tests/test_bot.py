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
        discord.Guild, id=GUILD_ID, voice_client=voice_client, me=fake(discord.Member, id=99)
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
    channel.connect = AsyncMock()
    return channel


def make_member(channel: Any) -> Any:
    return fake(
        discord.Member, id=5, voice=fake(discord.VoiceState, channel=channel) if channel else None
    )


def make_voice_client(channel: Any) -> Any:
    return fake(discord.VoiceClient, channel=channel, move_to=AsyncMock(), disconnect=AsyncMock())


@pytest.fixture
async def bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SobaFM:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    store = Store(tmp_path / "sobafm.db")
    await store.initialize()
    return SobaFM(load_settings(env_file=None), store)


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
    assert await bot.store.remembered_channels() == {GUILD_ID: CHANNEL_ID}


async def test_join_moves_from_another_channel(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild, 11))
    channel = make_channel(guild)

    reply = await bot.join(make_member(channel))

    assert reply == "Moved to <#10>."
    guild.voice_client.move_to.assert_awaited_once_with(channel)
    assert await bot.store.remembered_channels() == {GUILD_ID: CHANNEL_ID}


async def test_join_reports_a_failed_connection(bot: SobaFM) -> None:
    channel = make_channel(make_guild())
    channel.connect.side_effect = TimeoutError

    reply = await bot.join(make_member(channel))

    assert reply == "SobaFM couldn't connect to <#10>. Try again shortly."
    assert await bot.store.remembered_channels() == {}


async def test_leave_disconnects_and_forgets_the_channel(bot: SobaFM) -> None:
    guild = make_guild()
    guild.voice_client = make_voice_client(make_channel(guild))
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    assert await bot.leave(guild) == "Left <#10>."
    guild.voice_client.disconnect.assert_awaited_once()
    assert await bot.store.remembered_channels() == {}


async def test_leave_when_not_connected(bot: SobaFM) -> None:
    assert await bot.leave(make_guild()) == "SobaFM isn't in a voice channel."


async def test_follows_a_move_by_an_administrator(bot: SobaFM) -> None:
    guild = make_guild()
    me = fake(discord.Member, id=guild.me.id, guild=guild)
    before = fake(discord.VoiceState, channel=make_channel(guild, 11))
    after = fake(discord.VoiceState, channel=make_channel(guild))

    await bot.on_voice_state_update(me, before, after)

    assert await bot.store.remembered_channels() == {GUILD_ID: CHANNEL_ID}


async def test_forgets_the_channel_when_disconnected(bot: SobaFM) -> None:
    guild = make_guild()
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    me = fake(discord.Member, id=guild.me.id, guild=guild)
    before = fake(discord.VoiceState, channel=make_channel(guild))
    after = fake(discord.VoiceState, channel=None)

    await bot.on_voice_state_update(me, before, after)

    assert await bot.store.remembered_channels() == {}


async def test_keeps_the_channel_when_shutting_down(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    guild = make_guild()
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    monkeypatch.setattr(bot, "is_closed", MagicMock(return_value=True))
    me = fake(discord.Member, id=guild.me.id, guild=guild)
    before = fake(discord.VoiceState, channel=make_channel(guild))
    after = fake(discord.VoiceState, channel=None)

    await bot.on_voice_state_update(me, before, after)

    assert await bot.store.remembered_channels() == {GUILD_ID: CHANNEL_ID}


async def test_ignores_other_members(bot: SobaFM) -> None:
    guild = make_guild()
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)
    someone = fake(discord.Member, id=5, guild=guild)
    before = fake(discord.VoiceState, channel=make_channel(guild))
    after = fake(discord.VoiceState, channel=None)

    await bot.on_voice_state_update(someone, before, after)

    assert await bot.store.remembered_channels() == {GUILD_ID: CHANNEL_ID}


async def test_rejoins_remembered_channels(bot: SobaFM, monkeypatch: pytest.MonkeyPatch) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    guild.get_channel.return_value = channel
    monkeypatch.setattr(bot, "get_guild", MagicMock(return_value=guild))
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.rejoin_remembered_channels()

    channel.connect.assert_awaited_once_with(self_deaf=True)
    assert await bot.store.remembered_channels() == {GUILD_ID: CHANNEL_ID}


@pytest.mark.parametrize("problem", ["deleted", "no permission", "server left"])
async def test_forgets_channels_it_cannot_rejoin(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    guild = make_guild()
    channel = make_channel(guild, speak=problem != "no permission")
    guild.get_channel.return_value = None if problem == "deleted" else channel
    monkeypatch.setattr(
        bot, "get_guild", MagicMock(return_value=None if problem == "server left" else guild)
    )
    await bot.store.remember_channel(GUILD_ID, CHANNEL_ID)

    await bot.rejoin_remembered_channels()

    channel.connect.assert_not_awaited()
    assert await bot.store.remembered_channels() == {}
