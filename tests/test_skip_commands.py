import asyncio
from typing import Any, cast

import discord
import pytest

from sobafm.bot import SKIP_REPLIES, SobaFM
from sobafm.program import Outcome
from tests.doubles import FakeClock, FakeGemini, settle
from tests.test_bot import bot as bot
from tests.test_bot import gemini as gemini
from tests.test_bot import in_voice, make_channel, make_guild, make_interaction
from tests.test_catalog_commands import enable_catalog
from tests.test_catalog_station import Playback
from tests.test_skip import pcm, prepared


async def attached(bot: SobaFM, *, ready: bool = True) -> tuple[Playback, Any, Any]:
    enable_catalog(bot)
    r = await prepared(ready=ready)
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    player = cast(Any, r.player)
    player.channel, player.guild = channel, guild
    guild.voice_client = player
    bot.stations[guild.id] = r.station
    bot.station_owners[r.station] = (guild, player)
    return r, guild, member


async def natural_advance(r: Playback) -> None:
    first, second = r.selection.made[:2]
    first.finish()
    second.finish()
    await r.tick()
    while first.frames:
        assert r.station.mixer.read() == pcm(1000)
    assert r.station.mixer.read() == pcm(2000)
    await r.tick()
    assert r.station.heard_source is second


async def test_registered_skip_is_private_shared_cooldown_and_has_no_model_call(
    bot: SobaFM, gemini: FakeGemini
) -> None:
    r, guild, member = await attached(bot)
    command: Any = bot.tree.get_command("skip")
    assert command.guild_only
    assert command.default_permissions is None
    assert not command.parameters
    call = make_interaction(member)
    acknowledged = asyncio.Event()

    async def acknowledge(**kwargs: Any) -> None:
        acknowledged.set()

    call.response.defer.side_effect = acknowledge
    task = asyncio.create_task(command.callback(call))
    await acknowledged.wait()
    await settle()
    assert not task.done()
    call.response.defer.assert_awaited_once_with(ephemeral=True)
    assert r.station.mixer.read() == pcm(2000)
    await r.tick()
    await task
    assert call.followup.send.call_args.args == (SKIP_REPLIES[Outcome.PLAYING],)
    kwargs = call.followup.send.call_args.kwargs
    assert kwargs["ephemeral"]
    assert kwargs["suppress_embeds"]
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert guild.id in bot.cooldowns
    assert "recently" in (await bot.admit(member, "freestyle") or "")
    assert not gemini.calls
    assert not bot.announcements
    snapshot = bot.credit_snapshot(guild)
    assert snapshot is not None
    assert snapshot.record is r.selection.made[1].credits
    await r.station.close()


async def test_no_ready_is_an_admitted_failure_that_keeps_audio_and_original_preparation(
    bot: SobaFM, gemini: FakeGemini
) -> None:
    r, guild, member = await attached(bot, ready=False)
    queries = list(r.selection.catalog.queries)
    call = make_interaction(member)
    command: Any = bot.tree.get_command("skip")
    await command.callback(call)
    assert call.followup.send.call_args.args == (SKIP_REPLIES[Outcome.NO_MATCH],)
    assert guild.id not in bot.cooldowns
    assert r.station.mixer.read() == pcm(1000)
    r.selection.made[1].send_audio(5, sample=2000)
    await r.tick()
    assert r.station.mixer.read() == pcm(1000)
    assert r.selection.catalog.queries == queries
    assert not gemini.calls
    await r.station.close()


@pytest.mark.parametrize("hold", ["defer", "voice lock", "retire"])
@pytest.mark.parametrize("change", ["natural", "program", "voice"])
async def test_pre_ack_target_cannot_skip_a_later_track_after_any_command_wait(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, hold: str, change: str
) -> None:
    r, guild, member = await attached(bot)
    command: Any = bot.tree.get_command("skip")
    call = make_interaction(member)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await release.wait()

    async def acknowledged(**kwargs: Any) -> None:
        entered.set()

    if hold == "defer":
        call.response.defer.side_effect = held
    elif hold == "retire":
        monkeypatch.setattr(bot, "retire_stale_state", held)
    else:
        await bot.voice_locks[guild.id].acquire()
        call.response.defer.side_effect = acknowledged
    task = asyncio.create_task(command.callback(call))
    await entered.wait()
    if hold == "voice lock":
        await settle()
        call.response.defer.assert_awaited_once()
    if change == "natural":
        await natural_advance(r)
    elif change == "program":
        assert r.station.program is not None
        r.station.play(r.station.program.plan, "Another member")
    else:
        r.station.invalidate_playback()
    release.set()
    if hold == "voice lock":
        bot.voice_locks[guild.id].release()
    await task
    assert call.followup.send.call_args.args == (SKIP_REPLIES[Outcome.REPLACED],)
    assert guild.id not in bot.cooldowns
    if change == "natural":
        assert r.station.heard_source is r.selection.made[1]
        assert r.station.mixer.read() == pcm(2000)
        assert len(r.selection.made) <= 3
    else:
        assert not r.selection.made[0].retired
    await r.station.close()


@pytest.mark.parametrize("end", ["timeout", "cancel", "defer cancel"])
async def test_skip_wait_has_terminal_withdrawal_without_late_intent_or_announcement(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    r, guild, member = await attached(bot)
    call = make_interaction(member)
    command: Any = bot.tree.get_command("skip")
    armed = asyncio.Event()
    original = r.station.skip

    def arm(target: Any) -> asyncio.Future[Outcome]:
        result = original(target)
        armed.set()
        return result

    monkeypatch.setattr(r.station, "skip", arm)
    if end == "timeout":
        monkeypatch.setattr("sobafm.bot.START_TIMEOUT_S", 0.001)
    elif end == "defer cancel":
        call.response.defer.side_effect = asyncio.CancelledError
    task = asyncio.create_task(command.callback(call))
    if end != "defer cancel":
        await armed.wait()
    if end == "timeout":
        await task
        assert call.followup.send.call_args.args == (SKIP_REPLIES[Outcome.UNAVAILABLE],)
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert guild.id not in bot.cooldowns
    assert not bot.announcements
    assert not bot.request_owners
    assert r.station.mixer.read() == pcm(1000)
    await r.tick()
    assert r.station.mixer.read() == pcm(1000)
    await r.station.close()


async def test_unsuccessful_skip_never_releases_a_newer_requests_cooldown(
    bot: SobaFM, clock: FakeClock
) -> None:
    r, guild, member = await attached(bot, ready=False)
    assert await bot.admit(member, "skip") is None
    old = bot.cooldowns[guild.id]
    clock.now += 31
    assert await bot.admit(member, "new request") is None
    newer = bot.cooldowns[guild.id]
    assert (
        await bot.skip(member, old, target=bot.skip_target(guild)) == SKIP_REPLIES[Outcome.NO_MATCH]
    )
    assert bot.cooldowns[guild.id] == newer
    await r.station.close()


@pytest.mark.parametrize("manage", [False, True])
async def test_skip_channel_admission_has_no_manage_server_bypass(
    bot: SobaFM, manage: bool
) -> None:
    enable_catalog(bot)
    guild = make_guild()
    member = in_voice(make_channel(guild, 11), make_channel(guild))
    member.guild_permissions = discord.Permissions(manage_guild=manage)
    call = make_interaction(member)
    command: Any = bot.tree.get_command("skip")
    await command.callback(call)
    assert call.response.send_message.call_args.kwargs["ephemeral"]
    assert "Join <#10>" in call.response.send_message.call_args.args[0]
    call.response.defer.assert_not_awaited()
    assert not bot.cooldowns


async def test_generated_only_skip_explains_without_model_or_cooldown(
    bot: SobaFM, gemini: FakeGemini
) -> None:
    enable_catalog(bot)
    guild = make_guild()
    channel = make_channel(guild)
    call = make_interaction(in_voice(channel, channel))
    command: Any = bot.tree.get_command("skip")
    await command.callback(call)
    assert "/freestyle" in call.followup.send.call_args.args[0]
    assert not bot.cooldowns
    assert not gemini.calls


@pytest.mark.parametrize("end", ["timeout", "cancel"])
async def test_incoming_read_winning_command_withdrawal_keeps_own_cooldown_and_heard_credit(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    r, guild, member = await attached(bot)
    assert await bot.admit(member, "skip") is None
    cooldown = bot.cooldowns[guild.id]
    target = bot.skip_target(guild)
    original = r.station.mixer.withdraw
    armed = asyncio.Event()
    original_skip = r.station.skip

    def arm(value: Any) -> asyncio.Future[Outcome]:
        result = original_skip(value)
        armed.set()
        return result

    def read_then_withdraw(value: Any) -> Any:
        assert r.station.mixer.read() == pcm(2000)
        return original(value)

    monkeypatch.setattr(r.station, "skip", arm)
    monkeypatch.setattr(r.station.mixer, "withdraw", read_then_withdraw)
    if end == "timeout":
        monkeypatch.setattr("sobafm.bot.START_TIMEOUT_S", 0.001)
    task = asyncio.create_task(bot.skip(member, cooldown, target=target))
    await armed.wait()
    if end == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task == SKIP_REPLIES[Outcome.PLAYING]
    assert bot.cooldowns[guild.id] == cooldown
    assert r.station.heard_source is r.selection.made[1]
    assert r.selection.history.excludes(r.selection.made[1].track)
    assert not bot.announcements
    await r.station.close()


@pytest.mark.parametrize("change", ["voice", "guild", "owner"])
async def test_exact_voice_and_station_owner_are_revalidated_after_defer(
    bot: SobaFM, change: str
) -> None:
    r, guild, member = await attached(bot)
    call = make_interaction(member)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(**kwargs: Any) -> None:
        entered.set()
        await release.wait()

    call.response.defer.side_effect = held
    command: Any = bot.tree.get_command("skip")
    task = asyncio.create_task(command.callback(call))
    await entered.wait()
    if change == "guild":
        make_guild(guild.voice_client)
    elif change == "voice":
        in_voice(member.voice.channel, guild.voice_client.channel)
    else:
        bot.station_owners[r.station] = (guild, None)
    release.set()
    await task
    expected = (
        SKIP_REPLIES[Outcome.DISCONNECTED]
        if change == "guild"
        else "No catalog track is playing to skip."
    )
    assert call.followup.send.call_args.args == (expected,)
    assert guild.id not in bot.cooldowns
    assert not r.selection.history.excludes(r.selection.made[1].track)
    await r.station.close()
