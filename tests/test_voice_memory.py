"""Remembering, rejoining and losing the voice channel; no gateway, Discord or voice.

Most schedules use the adapter's fakes. The section at the end runs disnake's real
`VoiceClient` state machine against a scripted gateway and websocket, because whether a
reconnect counts as a disconnection is a fact about that code, not about a fake of it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import aiohttp
import disnake
import disnake.voice_client
import pytest

from sobafm import discord_adapter
from sobafm.discord_adapter import (
    JOIN_FAILED,
    JOIN_NOT_ALLOWED,
    JOIN_NOT_SAVED,
    LEAVE_NOT_SAVED,
    MAX_CONCURRENT_REJOINS,
    NOT_CONNECTED,
    REJOIN_ATTEMPTS,
    SHUTTING_DOWN,
    SILENCE,
    VOICE_BUSY,
    Adapter,
    GuildPlayer,
    create_bot,
)
from sobafm.pcm import PcmBankPool
from sobafm.settings import ServerSettings, SettingsError, SettingsStore
from tests.test_discord_adapter import (
    BOT_ID,
    LISTENER_ID,
    FakeChannel,
    FakeChannelPermissions,
    FakeGuild,
    FakeInteraction,
    FakeMatcher,
    FakeSettings,
    FakeVoice,
    FakeVoiceState,
    Harness,
    Producer,
    harness,
    heard_track,
    inter,
    join_retirements,
    matcher_of,
    offline,
    recording,
    run,
    settings_of,
    settle,
)

GUILD = 1


def player_of(h: Harness, guild: int = GUILD) -> GuildPlayer:
    return h.adapter.players[guild]


async def until_settled(condition: Callable[[], bool]) -> None:
    """Wait for ``condition`` through real tasks and timers, within a generous bound."""
    async with asyncio.timeout(10):
        while not condition():
            await asyncio.sleep(0.001)


def remembered(h: Harness) -> int | None:
    return h.adapter._settings.channel(GUILD)  # pyright: ignore[reportPrivateUsage]


async def leave(h: Harness) -> FakeInteraction:
    return await run(h.adapter.leave, inter(h.room, manager=True))


def adapter_log(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "sobafm.discord_adapter"]


# /join and /leave.


def test_join_remembers_and_leave_forgets_across_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "sobafm.db"
    channel_id: list[int] = []

    async def joined() -> None:
        store = SettingsStore.open(path)
        h = harness(settings=store)
        await h.join()
        assert store.channel(GUILD) == h.room.id
        channel_id.append(h.room.id)
        assert await h.adapter.shutdown() is True  # shutting down forgets nothing
        assert store.channel(GUILD) == h.room.id
        assert await store.close() is True

    async def still_remembered_then_left() -> None:
        store = SettingsStore.open(path)
        assert store.channel(GUILD) == channel_id[0]
        h = harness(settings=store)
        await h.join()
        await leave(h)
        assert store.channel(GUILD) is None
        assert await h.adapter.shutdown() is True
        assert await store.close() is True

    async def forgotten() -> None:
        store = SettingsStore.open(path)
        assert store.channels() == {}
        assert await store.close() is True

    asyncio.run(joined())
    asyncio.run(still_remembered_then_left())
    asyncio.run(forgotten())


@offline
async def test_moving_with_join_remembers_the_new_channel_and_reports_a_request() -> None:
    """A move only asks Discord, so the reply can't say it has happened."""
    h = harness()
    await h.join()
    voice = h.room.voice
    assert voice is not None
    other = FakeChannel("other")
    fake = await run(h.adapter.join, inter(other, manager=True))
    assert fake.texts() == [f"Moving to {other.mention}."]
    assert remembered(h) == other.id and voice.channel is other


@offline
async def test_join_in_the_same_channel_says_so_and_saves_nothing() -> None:
    h = harness()
    await h.join()
    writes = settings_of(h).channel_writes
    fake = await run(h.adapter.join, inter(h.room, manager=True))
    assert fake.texts() == [f"SobaFM is already in {h.room.mention}."]
    assert settings_of(h).channel_writes == writes


@pytest.mark.parametrize(
    "allowed", [FakeChannelPermissions(connect=False), FakeChannelPermissions(speak=False)]
)
def test_join_refuses_a_channel_it_cant_connect_and_speak_in(
    allowed: FakeChannelPermissions,
) -> None:
    async def scenario() -> None:
        h = harness()
        locked = FakeChannel("locked", allowed=allowed)
        fake = await run(h.adapter.join, inter(locked, manager=True))
        assert fake.texts() == [JOIN_NOT_ALLOWED]
        assert h.adapter.players == {} and locked.voice is None and remembered(h) is None

        # And a move there changes nothing either: not the channel, and not what's saved.
        await h.join()
        voice = h.room.voice
        assert voice is not None
        fake = await run(h.adapter.join, inter(locked, manager=True))
        assert fake.texts() == [JOIN_NOT_ALLOWED]
        assert voice.channel is h.room and remembered(h) == h.room.id

    asyncio.run(scenario())


@offline
async def test_a_failed_save_on_join_changes_nothing_and_says_so() -> None:
    h = harness()
    settings_of(h).failure = SettingsError("SQLITE_FULL")
    fake = await run(h.adapter.join, inter(h.room, manager=True))
    assert fake.texts() == [JOIN_NOT_SAVED]
    assert h.adapter.players == {} and h.room.voice is None and remembered(h) is None


@offline
async def test_a_failed_save_on_a_move_leaves_the_bot_where_it_was() -> None:
    h = harness()
    await h.join()
    voice = h.room.voice
    assert voice is not None
    settings_of(h).failure = SettingsError("SQLITE_FULL")
    other = FakeChannel("other")
    fake = await run(h.adapter.join, inter(other, manager=True))
    assert fake.texts() == [JOIN_NOT_SAVED]
    assert voice.channel is h.room and remembered(h) == h.room.id


@offline
async def test_a_failed_connection_on_join_restores_what_was_remembered() -> None:
    @dataclass(eq=False)
    class Refusing(FakeChannel):
        async def connect(self, *, timeout: float, cls: object) -> FakeVoice:
            raise TimeoutError

    h = harness()
    settings_of(h).remembered[GUILD] = 777
    fake = await run(h.adapter.join, inter(Refusing("refusing"), manager=True))
    assert fake.texts() == [JOIN_FAILED]
    assert remembered(h) == 777 and h.adapter.players == {}

    h = harness()
    fake = await run(h.adapter.join, inter(Refusing("refusing"), manager=True))
    assert fake.texts() == [JOIN_FAILED]
    assert remembered(h) is None

    # Failing to reach the channel it already remembers keeps it, so it's tried at next start.
    h = harness()
    refusing = Refusing("refusing")
    settings_of(h).remembered[GUILD] = refusing.id
    fake = await run(h.adapter.join, inter(refusing, manager=True))
    assert fake.texts() == [JOIN_FAILED]
    assert remembered(h) == refusing.id and settings_of(h).channel_writes == 0


@offline
async def test_a_connection_finished_during_shutdown_is_dropped() -> None:
    h = harness()

    @dataclass(eq=False)
    class Late(FakeChannel):
        async def connect(self, *, timeout: float, cls: object) -> FakeVoice:
            h.adapter.stopping.set()
            return await super().connect(timeout=timeout, cls=cls)

    late = Late("late")
    fake = await run(h.adapter.join, inter(late, manager=True))
    assert fake.texts() == [SHUTTING_DOWN]
    assert late.voice is not None and late.voice.disconnects == 1 and h.adapter.players == {}


@offline
async def test_a_failed_save_on_leave_keeps_the_bot_connected_and_says_so() -> None:
    h = harness()
    source = await heard_track(h)
    player = player_of(h)
    heard = player.station.heard
    settings_of(h).failure = SettingsError("SQLITE_FULL")
    fake = await leave(h)
    assert fake.texts() == [LEAVE_NOT_SAVED]
    assert player_of(h) is player and remembered(h) == h.room.id
    assert h.room.voice is not None and h.room.voice.disconnects == 0
    assert player.station.heard is heard and source.read() != SILENCE  # audio continues
    assert h.pool.leased == 1 and not player.closing

    settings_of(h).failure = None  # a retry then works
    assert (await leave(h)).texts() == ["Stopped and left."]
    assert h.adapter.players == {} and remembered(h) is None


@offline
async def test_leave_with_a_rejected_acknowledgement_still_forgets() -> None:
    h = harness()
    await heard_track(h)
    fake = inter(h.room, manager=True)
    fake.rejected = True
    await run(h.adapter.leave, fake)
    assert fake.sent == [] and h.adapter.players == {} and remembered(h) is None


@offline
async def test_leave_forgets_a_channel_with_no_connection() -> None:
    h = harness()
    settings_of(h).remembered[GUILD] = 777
    fake = await leave(h)
    assert fake.texts() == [f"{NOT_CONNECTED} It won't rejoin after a restart."]
    assert remembered(h) is None
    assert (await leave(h)).texts() == [NOT_CONNECTED]  # nothing left to do


@offline
async def test_two_voice_changes_cant_interleave() -> None:
    h = harness()
    gate = asyncio.Event()

    @dataclass(eq=False)
    class Slow(FakeChannel):
        async def connect(self, *, timeout: float, cls: object) -> FakeVoice:
            await gate.wait()
            return await super().connect(timeout=timeout, cls=cls)

    slow = Slow("slow")
    joining = asyncio.create_task(run(h.adapter.join, inter(slow, manager=True)))
    await settle()
    assert (await run(h.adapter.join, inter(h.room, manager=True))).texts() == [VOICE_BUSY]
    assert (await leave(h)).texts() == [VOICE_BUSY]
    gate.set()
    assert (await joining).texts() == [f"Joined {slow.mention}."]
    assert remembered(h) == slow.id
    assert (await leave(h)).texts() == ["Stopped and left."]  # free again


# Rejoining after a restart.


@dataclass(eq=False)
class VocalChannel(FakeChannel):
    type: disnake.ChannelType = disnake.ChannelType.voice
    failures: int = 0
    failure: type[Exception] = TimeoutError
    gate: asyncio.Event | None = None
    connects: int = 0

    async def connect(self, *, timeout: float, cls: object) -> FakeVoice:
        self.connects += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.failures > 0:
            self.failures -= 1
            raise self.failure
        return await super().connect(timeout=timeout, cls=cls)


@dataclass
class VocalGuild(FakeGuild):
    channels: dict[int, FakeChannel] = field(default_factory=lambda: {})
    unavailable: bool = False

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)


def rejoin_harness(
    channel: FakeChannel | None = None, *, guild_id: int = GUILD
) -> tuple[Harness, VocalGuild, VocalChannel]:
    """A restarted service remembering one channel."""
    h = harness()
    vocal = cast(VocalChannel, channel) if channel is not None else VocalChannel("remembered")
    guild = VocalGuild(guild_id, channels={vocal.id: vocal})
    vocal.guild = guild
    settings_of(h).remembered[guild_id] = vocal.id
    return h, guild, vocal


def guilds(*known: FakeGuild) -> Callable[[int], disnake.Guild | None]:
    by_id = {guild.id: guild for guild in known}

    def get_guild(guild_id: int) -> disnake.Guild | None:
        return cast(Any, by_id.get(guild_id))

    return get_guild


async def rejoin(h: Harness, *known: VocalGuild) -> None:
    h.adapter.start_rejoins(guilds(*known))
    await until_settled(lambda: h.adapter.tasks == set())


def test_an_eligible_channel_is_rejoined_with_no_program(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")

    async def scenario() -> None:
        h, guild, channel = rejoin_harness()
        settings_of(h).current = ServerSettings(volume_percent=20, cooldown_seconds=0)
        await rejoin(h, guild)
        assert channel.connects == 1
        player = player_of(h)
        voice = channel.voice
        assert voice is not None and player.voice is cast(Any, voice)
        # Connected and idle: nothing asked for, played or leased.
        assert player.station.desired is None and player.station.heard is None
        assert voice.plays == [] and h.pool.leased == 0
        assert matcher_of(h).requests == [] and player.source.read() == b""
        assert h.clock.pending == []  # and so no listener grace period either
        assert player.output.volume == 0.2  # at the saved volume
        assert remembered(h) == channel.id

        # It works as an ordinary connection afterwards.
        h.room = channel
        (text,) = await h.play()
        assert "Request accepted" in text
        assert await h.adapter.shutdown() is True

    asyncio.run(scenario())
    assert "voice rejoin: outcome=joined" in adapter_log(caplog)


def missing_channel() -> tuple[Harness, VocalGuild, VocalChannel]:
    h, guild, channel = rejoin_harness()
    guild.channels.clear()
    return h, guild, channel


def text_channel() -> tuple[Harness, VocalGuild, VocalChannel]:
    return rejoin_harness(VocalChannel("words", type=disnake.ChannelType.text))


def no_connect() -> tuple[Harness, VocalGuild, VocalChannel]:
    return rejoin_harness(VocalChannel("locked", allowed=FakeChannelPermissions(connect=False)))


def no_speak() -> tuple[Harness, VocalGuild, VocalChannel]:
    return rejoin_harness(VocalChannel("muted", allowed=FakeChannelPermissions(speak=False)))


@pytest.mark.parametrize(
    ("prepare", "reason"),
    [
        (missing_channel, "channel_gone"),
        (text_channel, "channel_gone"),
        (no_connect, "no_permission"),
        (no_speak, "no_permission"),
    ],
)
def test_an_ineligible_channel_is_forgotten_with_a_safe_reason(
    prepare: Callable[[], tuple[Harness, VocalGuild, VocalChannel]],
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")

    async def scenario() -> None:
        h, guild, channel = prepare()
        await rejoin(h, guild)
        assert channel.connects == 0  # refused without trying
        assert h.adapter.players == {} and remembered(h) is None
        assert str(channel.id) not in caplog.text

    asyncio.run(scenario())
    assert f"voice rejoin: outcome=forgotten reason={reason}" in adapter_log(caplog)


def test_a_server_the_bot_left_is_forgotten(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")

    async def scenario() -> None:
        h, _, channel = rejoin_harness()
        await rejoin(h)  # the bot is no longer in that server
        assert channel.connects == 0 and remembered(h) is None

    asyncio.run(scenario())
    assert "voice rejoin: outcome=forgotten reason=guild_gone" in adapter_log(caplog)


def test_an_unavailable_server_is_kept_for_the_next_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")

    async def scenario() -> None:
        h, guild, channel = rejoin_harness()
        guild.unavailable = True
        await rejoin(h, guild)
        assert channel.connects == 0 and remembered(h) == channel.id
        assert h.adapter.players == {}

    asyncio.run(scenario())
    assert "voice rejoin: outcome=kept reason=guild_unavailable" in adapter_log(caplog)


def test_connection_attempts_are_bounded_and_then_the_channel_is_forgotten(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")

    async def scenario() -> None:
        h, guild, channel = rejoin_harness()
        channel.failures = 1_000
        await rejoin(h, guild)
        await until_settled(lambda: remembered(h) is None)
        assert channel.connects == REJOIN_ATTEMPTS
        assert h.adapter.players == {}

    asyncio.run(scenario())
    lines = adapter_log(caplog)
    assert lines.count("voice rejoin attempt failed: TimeoutError") == REJOIN_ATTEMPTS
    assert "voice rejoin: outcome=forgotten reason=connect_failed" in lines


@offline
async def test_a_second_attempt_can_succeed() -> None:
    h, guild, channel = rejoin_harness()
    channel.failures = 1
    await rejoin(h, guild)
    await until_settled(lambda: GUILD in h.adapter.players)
    assert channel.connects == 2 and remembered(h) == channel.id
    assert await h.adapter.shutdown() is True


def test_an_attempt_that_hangs_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discord_adapter, "CONNECT_ATTEMPT_SECONDS", 0.01)

    async def scenario() -> None:
        h, guild, channel = rejoin_harness()
        channel.gate = asyncio.Event()  # never set: the connection never completes
        await rejoin(h, guild)
        await until_settled(lambda: remembered(h) is None)
        assert channel.connects == REJOIN_ATTEMPTS and h.adapter.players == {}

    asyncio.run(scenario())


@offline
async def test_rejoining_happens_once_per_process() -> None:
    h, guild, channel = rejoin_harness()
    bot = create_bot(h.adapter)
    bot.get_guild = guilds(guild)  # pyright: ignore[reportAttributeAccessIssue]
    (ready,) = bot.extra_events["on_ready"]
    await ready()
    await settle()
    assert channel.connects == 1

    # Discord reports ready again after a new gateway session; nothing happens.
    await ready()
    await settle()
    assert channel.connects == 1

    # Even if the first connection has since gone, and the channel is remembered again.
    await run(h.adapter.leave, inter(channel, manager=True))
    settings_of(h).remembered[GUILD] = channel.id
    await ready()
    await settle()
    assert channel.connects == 1 and h.adapter.players == {}

    # A gateway resume has no handler that could start anything.
    assert "on_resumed" not in bot.extra_events
    assert await h.adapter.shutdown() is True


@offline
async def test_a_later_ready_finds_nothing_even_when_the_first_had_nothing() -> None:
    h = harness()
    h.adapter.start_rejoins(guilds())
    settings_of(h).remembered[GUILD] = 777
    h.adapter.start_rejoins(guilds())
    await settle()
    assert h.adapter.tasks == set() and remembered(h) == 777


@offline
async def test_rejoins_are_limited_in_how_many_run_at_once() -> None:
    h = harness()
    gate = asyncio.Event()
    count = MAX_CONCURRENT_REJOINS * 3
    known: list[VocalGuild] = []
    channels: list[VocalChannel] = []
    for guild_id in range(10, 10 + count):
        channel = VocalChannel(f"channel-{guild_id}", gate=gate)
        guild = VocalGuild(guild_id, channels={channel.id: channel})
        channel.guild = guild
        settings_of(h).remembered[guild_id] = channel.id
        known.append(guild)
        channels.append(channel)
    h.adapter.start_rejoins(guilds(*known))
    await settle()
    assert sum(c.connects for c in channels) == MAX_CONCURRENT_REJOINS
    gate.set()
    await until_settled(lambda: len(h.adapter.players) == count)
    assert all(c.connects == 1 for c in channels)
    assert await h.adapter.shutdown() is True


@offline
async def test_one_failed_rejoin_doesnt_stop_the_others() -> None:
    h = harness()
    good = VocalChannel("good")
    bad = VocalChannel("bad", failures=1_000)
    known = [VocalGuild(10, channels={bad.id: bad}), VocalGuild(11, channels={good.id: good})]
    bad.guild, good.guild = known
    settings_of(h).remembered.update({10: bad.id, 11: good.id})
    h.adapter.start_rejoins(guilds(*known))
    await until_settled(lambda: 11 in h.adapter.players and 10 not in settings_of(h).remembered)
    assert list(h.adapter.players) == [11]
    assert await h.adapter.shutdown() is True


def test_a_failed_save_while_forgetting_is_logged_and_retried_next_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        h, guild, channel = missing_channel()
        settings_of(h).failure = SettingsError("SQLITE_FULL")
        await rejoin(h, guild)
        await until_settled(lambda: h.adapter.tasks == set())
        assert remembered(h) == channel.id and h.adapter.players == {}

    with caplog.at_level(logging.WARNING, logger="sobafm"):
        asyncio.run(scenario())
    assert "settings write failed: kind=SQLITE_FULL" in adapter_log(caplog)


def test_a_managers_join_or_leave_before_the_rejoin_wins(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")

    async def scenario() -> None:
        h, guild, channel = rejoin_harness()
        # /join got in first: already connected, so there is nothing to rejoin.
        h.room = channel
        await h.join()
        connects = channel.connects
        await rejoin(h, guild)
        assert channel.connects == connects

        h, guild, channel = rejoin_harness()
        # /leave got in first: nothing is remembered any more.
        settings_of(h).remembered.clear()
        await h.adapter._rejoin(GUILD, cast(Any, guild))  # pyright: ignore[reportPrivateUsage]
        assert channel.connects == 0 and h.adapter.players == {}

    asyncio.run(scenario())
    assert not [line for line in adapter_log(caplog) if line.startswith("voice rejoin")]


@offline
async def test_join_and_leave_wait_for_a_rejoin_under_way() -> None:
    h, guild, channel = rejoin_harness()
    channel.gate = asyncio.Event()
    h.adapter.start_rejoins(guilds(guild))
    await settle()
    assert channel.connects == 1
    assert (await run(h.adapter.join, inter(channel, manager=True))).texts() == [VOICE_BUSY]
    assert (await run(h.adapter.leave, inter(channel, manager=True))).texts() == [VOICE_BUSY]
    channel.gate.set()
    await until_settled(lambda: GUILD in h.adapter.players)
    assert await h.adapter.shutdown() is True


@offline
async def test_shutdown_during_a_rejoin_cancels_it_and_keeps_the_channel() -> None:
    h, guild, channel = rejoin_harness()
    channel.gate = asyncio.Event()  # never set
    h.adapter.start_rejoins(guilds(guild))
    await settle()
    # Bounded well under one attempt's limit, so only cancelling it can finish in time.
    assert await asyncio.wait_for(h.adapter.shutdown(), timeout=5) is True
    assert h.adapter.players == {} and h.adapter.tasks == set()
    assert channel.connects == 1
    assert remembered(h) == channel.id  # the next start tries again


@offline
async def test_a_rejoin_after_shutdown_starts_nothing() -> None:
    h, guild, channel = rejoin_harness()
    await h.adapter.shutdown()
    h.adapter.start_rejoins(guilds(guild))
    assert h.adapter.tasks == set()  # no new owner appears after shutdown has joined them
    await settle()
    assert channel.connects == 0 and remembered(h) == channel.id


# An administrative move.


def moved(h: Harness, channel: FakeChannel) -> None:
    voice = h.room.voice
    assert voice is not None
    voice.channel = channel
    h.voice_update(BOT_ID, FakeVoiceState(None), channel)


def test_a_move_is_adopted_and_audio_carries_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")
    path = tmp_path / "sobafm.db"

    async def scenario() -> None:
        store = SettingsStore.open(path)
        h = harness(settings=store)
        source = await heard_track(h)
        player = player_of(h)
        heard = player.station.heard
        desired = player.station.desired
        other = FakeChannel("other")
        moved(h, other)
        await until_settled(lambda: store.channel(GUILD) == other.id)
        # The same program, track and bank: only the channel changed.
        assert player_of(h) is player
        assert player.station.heard is heard and player.station.desired == desired
        assert h.pool.leased == 1 and source.read() != SILENCE
        assert await h.adapter.shutdown() is True
        assert await store.close() is True

    asyncio.run(scenario())
    assert "voice channel adopted" in adapter_log(caplog)


@offline
async def test_a_move_to_a_channel_with_no_listener_starts_the_grace_period() -> None:
    h = harness()
    await heard_track(h)
    assert h.clock.pending == []
    empty = FakeChannel("empty", voice_states={})
    moved(h, empty)
    await settle()
    assert h.clock.pending == [discord_adapter.LISTENER_GRACE_SECONDS]
    assert remembered(h) == empty.id
    assert player_of(h).station.heard is not None  # still playing during the grace period


@offline
async def test_a_move_to_a_channel_with_a_listener_cancels_the_grace_period() -> None:
    h = harness()
    await heard_track(h)
    h.voice_update(LISTENER_ID, None)
    await settle()
    assert h.clock.pending == [discord_adapter.LISTENER_GRACE_SECONDS]
    moved(h, FakeChannel("busy"))  # holds one listener by default
    await settle()
    assert h.clock.pending == []


@offline
async def test_the_bots_state_in_the_same_channel_saves_nothing() -> None:
    h = harness()
    await h.join()
    writes = settings_of(h).channel_writes
    moved(h, h.room)
    await settle()
    assert settings_of(h).channel_writes == writes


def test_a_failed_save_on_a_move_keeps_playing_and_tries_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        h = harness()
        await heard_track(h)
        old = h.room.id
        other = FakeChannel("other")
        settings_of(h).failure = SettingsError("SQLITE_FULL")
        moved(h, other)
        await settle()
        assert remembered(h) == old and player_of(h).station.heard is not None
        settings_of(h).failure = None
        h.voice_update(BOT_ID, FakeVoiceState(None), other)  # the next state update retries
        await settle()
        assert remembered(h) == other.id

    with caplog.at_level(logging.WARNING, logger="sobafm"):
        asyncio.run(scenario())
    assert "settings write failed: kind=SQLITE_FULL" in adapter_log(caplog)


@offline
async def test_a_move_during_leave_cant_bring_the_channel_back() -> None:
    h = harness()
    await h.join()
    player = player_of(h)
    h.adapter._voice_busy.add(GUILD)  # pyright: ignore[reportPrivateUsage]
    moved(h, FakeChannel("other"))
    await settle()
    assert remembered(h) == h.room.id
    h.adapter._voice_busy.discard(GUILD)  # pyright: ignore[reportPrivateUsage]
    player.closing = True  # and neither can a move while the connection is ending
    moved(h, FakeChannel("another"))
    await settle()
    assert remembered(h) == h.room.id
    player.closing = False


# Disconnection and channel deletion.


def lost(h: Harness) -> None:
    """The library reports that the connection ended, as `WatchedVoiceClient` does."""
    voice = player_of(h).voice
    voice.connected = False  # pyright: ignore[reportAttributeAccessIssue]
    h.adapter._voice_gone(GUILD, voice)  # pyright: ignore[reportPrivateUsage]


def test_a_disconnection_ends_the_program_and_forgets_the_channel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")

    async def scenario() -> None:
        h = harness()
        source = await heard_track(h)
        voice = h.room.voice
        assert voice is not None
        lost(h)
        await until_settled(lambda: GUILD not in h.adapter.players and remembered(h) is None)
        await join_retirements(h)
        assert h.pool.leased == 0 and len(h.producer.settled) == 1
        assert source.read() == b""
        assert voice.disconnects == 0  # the library already closed it; not closed again
        fake = await run(h.adapter.now, inter(None))
        assert fake.texts() == [NOT_CONNECTED]

    asyncio.run(scenario())
    assert "voice connection ended: reason=disconnected" in adapter_log(caplog)


@offline
async def test_a_disconnection_leaves_no_program_to_rejoin_after_a_restart() -> None:
    h = harness()
    await heard_track(h)
    lost(h)
    await until_settled(lambda: remembered(h) is None)
    assert settings_of(h).channels() == {}


@offline
async def test_a_lost_connections_retirement_keeps_its_owner_and_debt() -> None:
    h = harness()
    await heard_track(h)
    h.producer.hold = True
    track = player_of(h).source._current  # pyright: ignore[reportPrivateUsage]
    assert track is not None
    lost(h)
    await until_settled(lambda: GUILD not in h.adapter.players)
    assert track.retirement is not None and not track.retirement.done()
    assert h.pool.leased == 1 and h.producer.settled == []  # the bank stays charged
    assert track.retirement in h.adapter.tasks  # and the adapter still owns its retirement
    h.producer.release.set()
    await track.retirement
    assert h.pool.leased == 0 and len(h.producer.settled) == 1
    assert await h.adapter.shutdown() is True


@offline
async def test_a_retained_bank_after_a_loss_stays_debt_until_released() -> None:
    h = harness(Producer(frames=1, end=False))
    await h.join()
    await h.play()
    await settle()
    pending = player_of(h).source._pending  # pyright: ignore[reportPrivateUsage]
    assert pending is not None
    view = pending.bank.reserve_write(1)  # a borrowed region blocks actual release
    assert view is not None
    lost(h)
    await until_settled(lambda: h.adapter.debt == [pending.bank])
    assert h.pool.leased == 1
    assert await h.adapter.shutdown() is False  # the failed verdict isn't lost
    pending.bank.commit_write(0)
    assert await h.adapter.shutdown() is True and h.pool.leased == 0


def test_a_missed_cleanup_deadline_after_a_loss_stays_a_failed_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discord_adapter, "CLEANUP_SECONDS", 0.01)

    async def scenario() -> None:
        h = harness()
        await heard_track(h)
        h.producer.hold = True
        lost(h)
        await until_settled(lambda: h.adapter.cleanup_failures == 1)
        assert h.pool.leased == 1
        h.producer.release.set()
        assert await h.adapter.shutdown() is False
        assert h.pool.leased == 0

    asyncio.run(scenario())


def test_a_failed_forget_after_a_loss_still_ends_the_program(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        h = harness()
        await heard_track(h)
        settings_of(h).failure = SettingsError("SQLITE_FULL")
        lost(h)
        await until_settled(lambda: GUILD not in h.adapter.players)
        await until_settled(lambda: h.adapter.tasks == set())
        assert h.pool.leased == 0 and remembered(h) == h.room.id

    with caplog.at_level(logging.WARNING, logger="sobafm"):
        asyncio.run(scenario())
    assert "settings write failed: kind=SQLITE_FULL" in adapter_log(caplog)


@offline
async def test_a_stale_clients_report_changes_nothing() -> None:
    h = harness()
    await heard_track(h)
    stale = cast(Any, FakeVoice(FakeChannel("old")))
    h.adapter._voice_gone(GUILD, stale)  # pyright: ignore[reportPrivateUsage]
    await settle()
    assert GUILD in h.adapter.players and remembered(h) == h.room.id


def reports_cleanup(h: Harness) -> None:
    """Make the fake's `disconnect` report through `cleanup`, as the real client does."""
    voice = player_of(h).voice
    original = voice.disconnect

    async def reporting(*, force: bool) -> None:
        await original(force=force)
        h.adapter._voice_gone(GUILD, voice)  # pyright: ignore[reportPrivateUsage]

    voice.disconnect = reporting  # pyright: ignore[reportAttributeAccessIssue]


@offline
async def test_the_adapters_own_disconnect_isnt_a_loss() -> None:
    h = harness()
    await h.join()
    reports_cleanup(h)
    player = player_of(h)
    await h.adapter._disconnect(GUILD, player)  # pyright: ignore[reportPrivateUsage]
    await settle()
    assert remembered(h) == h.room.id and h.adapter.tasks == set()


@offline
async def test_a_shutdown_forgets_nothing_or_nothing_would_rejoin() -> None:
    h = harness()
    await h.join()
    reports_cleanup(h)
    assert await h.adapter.shutdown() is True
    assert remembered(h) == h.room.id


@offline
async def test_a_report_during_shutdown_forgets_nothing() -> None:
    h = harness()
    await h.join()
    voice = player_of(h).voice
    h.adapter.stopping.set()
    h.adapter._voice_gone(GUILD, voice)  # pyright: ignore[reportPrivateUsage]
    await settle()
    assert remembered(h) == h.room.id


@offline
async def test_disconnecting_twice_cant_touch_a_newer_connection() -> None:
    h = harness()
    await h.join()
    old = player_of(h)
    tracks = await h.adapter._disconnect(GUILD, old)  # pyright: ignore[reportPrivateUsage]
    assert tracks == set()
    await h.join()
    newer = player_of(h)
    assert newer is not old
    voice = cast(FakeVoice, old.voice)
    disconnects = voice.disconnects
    assert await h.adapter._disconnect(GUILD, old) == set()  # pyright: ignore[reportPrivateUsage]
    assert player_of(h) is newer and voice.disconnects == disconnects
    assert await h.adapter.shutdown() is True


def test_deleting_the_channel_ends_the_program_and_forgets_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")

    async def scenario() -> None:
        h = harness()
        await heard_track(h)
        voice = h.room.voice
        assert voice is not None
        h.adapter.channel_deleted(GUILD, h.room.id)
        await until_settled(lambda: GUILD not in h.adapter.players and remembered(h) is None)
        await join_retirements(h)
        assert voice.disconnects == 1  # this one the adapter closes itself
        assert h.pool.leased == 0 and len(h.producer.settled) == 1

    asyncio.run(scenario())
    assert "voice connection ended: reason=channel_deleted" in adapter_log(caplog)


@offline
async def test_deleting_another_channel_changes_nothing() -> None:
    h = harness()
    await heard_track(h)
    h.adapter.channel_deleted(GUILD, h.room.id + 1)
    await settle()
    assert GUILD in h.adapter.players and remembered(h) == h.room.id
    assert player_of(h).station.heard is not None


@offline
async def test_deleting_a_remembered_channel_with_no_connection_forgets_it() -> None:
    h = harness()
    settings_of(h).remembered[GUILD] = 777
    h.adapter.channel_deleted(GUILD, 777)
    await until_settled(lambda: remembered(h) is None)


@offline
async def test_the_deletion_event_is_wired_to_the_adapter() -> None:
    h = harness()
    await h.join()
    bot = create_bot(h.adapter)
    (deleted,) = bot.extra_events["on_guild_channel_delete"]
    await deleted(SimpleNamespace(guild=SimpleNamespace(id=GUILD), id=h.room.id))
    await until_settled(lambda: GUILD not in h.adapter.players)
    assert remembered(h) is None


# disnake's real voice client against a scripted gateway and websocket.


class WireState:
    """The parts of disnake's connection state that a real voice client uses."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.user = SimpleNamespace(id=BOT_ID)
        self.clients: dict[int, disnake.VoiceProtocol] = {}
        self.client = SimpleNamespace(_connection=self)

    def _get_voice_client(self, guild_id: int) -> disnake.VoiceProtocol | None:
        return self.clients.get(guild_id)

    def _add_voice_client(self, guild_id: int, voice: disnake.VoiceProtocol) -> None:
        self.clients[guild_id] = voice

    def _remove_voice_client(self, guild_id: int) -> None:
        self.clients.pop(guild_id, None)

    def _get_client(self) -> object:
        return self.client


class ScriptedWebSocket:
    """One voice websocket: a close code a test sends ends the current poll.

    `close` behaves as aiohttp's does: it feeds a closing message to a waiting `receive`
    and awaits that before it returns (`ClientWebSocketResponse.close`), so the library's
    polling loop wakes in the middle of `disconnect`, before `cleanup` has run.
    """

    _keep_alive = None

    def __init__(self) -> None:
        self.closes: asyncio.Queue[int] = asyncio.Queue()
        self.closed_with: int | None = None

    async def poll_event(self) -> None:
        code = await self.closes.get() if self.closed_with is None else self.closed_with
        raise disnake.ConnectionClosed(
            cast(Any, SimpleNamespace(close_code=code)), shard_id=None, code=code
        )

    async def close(self, code: int = 1000) -> None:
        if self.closed_with is None:
            self.closed_with = code
            self.closes.put_nowait(code)
            for _ in range(5):
                await asyncio.sleep(0)

    async def speak(self, speaking: bool) -> None:
        pass


class Wire:
    """The network edges of the real voice client, which a test scripts."""

    def __init__(self) -> None:
        self.sockets: list[ScriptedWebSocket] = []
        self.backoff_seconds = 0.0
        # The next websocket connection raises this.
        self.failure: Exception | None = None
        # The next websocket connection never finishes, as with a lost UDP discovery packet.
        self.hangs = False

    @property
    def closes(self) -> asyncio.Queue[int]:
        return self.sockets[-1].closes


@dataclass
class WireGuild(FakeGuild):
    """The gateway's side: a voice state change comes back as VOICE_STATE_UPDATE."""

    channels: dict[int, FakeChannel] = field(default_factory=lambda: {})
    state: WireState = field(default_factory=WireState)
    adapter: Adapter | None = None
    # Whether Discord answers a connection with VOICE_SERVER_UPDATE.
    answers: bool = True
    changes: list[FakeChannel | None] = field(default_factory=lambda: [])
    unavailable: bool = False

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)

    @property
    def voice_client(self) -> disnake.VoiceProtocol | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self.state.clients.get(self.id)

    async def change_voice_state(self, *, channel: FakeChannel | None) -> None:
        self.changes.append(channel)
        asyncio.get_running_loop().call_soon(self.deliver, channel)

    def deliver(self, channel: FakeChannel | None) -> None:
        """Mirror `ConnectionState.parse_voice_state_update`: the client, then the event."""
        voice = self.state.clients.get(self.id)
        payload: Any = {
            "session_id": "synthetic",
            "channel_id": None if channel is None else channel.id,
        }
        if voice is not None:
            asyncio.create_task(voice.on_voice_state_update(payload))  # noqa: RUF006
            if channel is not None and self.answers:
                update: Any = {
                    "token": "synthetic",
                    "guild_id": self.id,
                    "endpoint": "voice.invalid",
                }
                asyncio.create_task(voice.on_voice_server_update(update))  # noqa: RUF006
        assert self.adapter is not None
        self.adapter.voice_state_changed(self.id, BOT_ID, cast(Any, channel))


@dataclass(eq=False)
class WireChannel(FakeChannel):
    """Connects through disnake's own `Connectable.connect`."""

    type: disnake.ChannelType = disnake.ChannelType.voice

    @property
    def _state(self) -> WireState:
        return cast(WireGuild, self.guild).state

    def _get_voice_client_key(self) -> tuple[int, str]:
        return self.guild.id, "guild_id"

    async def connect(self, *, timeout: float, cls: Any) -> Any:  # noqa: ANN401
        connect = disnake.abc.Connectable.connect
        return await connect(cast(Any, self), timeout=timeout, cls=cls)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Generator[Wire]:
    """Patch only the network edges of the real voice client: its websocket and backoff."""
    edges = Wire()

    async def connect_websocket(self: disnake.VoiceClient, *, resume: bool = False) -> None:
        if edges.failure is not None:
            failure, edges.failure = edges.failure, None
            raise failure
        ws = ScriptedWebSocket()
        edges.sockets.append(ws)
        self.ws = cast(Any, ws)
        if edges.hangs:
            await asyncio.Event().wait()
        self._connected.set()  # pyright: ignore[reportPrivateUsage]

    played: set[int] = set()

    def play(self: disnake.VoiceClient, source: disnake.AudioSource) -> None:
        played.add(id(self))

    def is_playing(self: disnake.VoiceClient) -> bool:
        return id(self) in played

    monkeypatch.setattr(disnake.VoiceClient, "connect_websocket", connect_websocket)
    monkeypatch.setattr(disnake.VoiceClient, "play", play)
    monkeypatch.setattr(disnake.VoiceClient, "is_playing", is_playing)
    monkeypatch.setattr(
        disnake.voice_client,
        "ExponentialBackoff",
        lambda: SimpleNamespace(delay=lambda: edges.backoff_seconds),
    )
    yield edges


def wire_harness() -> tuple[Harness, WireGuild, WireChannel, WireChannel]:
    h = harness()
    guild = WireGuild(GUILD)
    guild.adapter = h.adapter
    room, other = WireChannel("room", guild=guild), WireChannel("other", guild=guild)
    guild.channels = {room.id: room, other.id: other}
    h.room = room
    return h, guild, room, other


async def wire_program(h: Harness) -> disnake.VoiceClient:
    """Connect for real and start a program that is actually heard."""
    source = await h.join()
    (text,) = await h.play()
    assert "Request accepted" in text
    await settle()
    assert source.read() != SILENCE
    await settle()
    assert player_of(h).station.heard is not None
    return player_of(h).voice


async def wire_finish(h: Harness, ws: Wire) -> None:
    """A normal closure ends the real polling task and closes its socket."""
    if GUILD in h.adapter.players:
        ws.closes.put_nowait(1000)
        await until_settled(lambda: GUILD not in h.adapter.players)
    await until_settled(lambda: h.adapter.tasks == set())


def test_a_transient_reconnect_is_not_a_disconnection(wire: Wire) -> None:
    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        voice = await wire_program(h)
        player = player_of(h)
        heard = player.station.heard
        desired = player.station.desired
        guild.changes.clear()

        wire.closes.put_nowait(4006)  # the websocket drops, and the library recovers
        await until_settled(lambda: guild.changes == [None, room] and voice.is_connected())

        # SobaFM's own state really did report no channel, and nothing was ended by it.
        assert player_of(h) is player and player.voice is cast(Any, voice)
        assert player.station.heard is heard and player.station.desired == desired
        assert remembered(h) == room.id and guild.state.clients[GUILD] is voice
        assert h.pool.leased == 1 and not player.closing

        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_listeners_are_reviewed_when_a_reconnect_completes(wire: Wire) -> None:
    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        voice = await wire_program(h)
        room.voice_states.clear()  # everyone left during the outage
        guild.changes.clear()
        wire.closes.put_nowait(4006)
        await until_settled(lambda: guild.changes == [None, room] and voice.is_connected())
        await until_settled(lambda: h.clock.pending == [discord_adapter.LISTENER_GRACE_SECONDS])
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_a_normal_closure_is_a_disconnection(wire: Wire) -> None:
    async def scenario() -> None:
        h, guild, _, _ = wire_harness()
        await wire_program(h)
        wire.closes.put_nowait(1000)
        await until_settled(lambda: GUILD not in h.adapter.players and remembered(h) is None)
        await join_retirements(h)
        assert h.pool.leased == 0 and guild.state.clients == {}
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_being_kicked_is_a_disconnection(wire: Wire) -> None:
    """The state update arrives first, while the client still counts as connected."""

    async def scenario() -> None:
        h, guild, _, _ = wire_harness()
        await wire_program(h)
        guild.deliver(None)
        await until_settled(lambda: GUILD not in h.adapter.players and remembered(h) is None)
        await join_retirements(h)
        assert h.pool.leased == 0 and guild.state.clients == {}
        wire.closes.put_nowait(1000)  # ends the polling task
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_being_kicked_after_a_forced_close_is_a_disconnection_once_the_library_gives_up(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close code 4014 first: the library waits for a server update that never comes."""
    monkeypatch.setattr(discord_adapter, "VOICE_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> None:
        h, guild, _, _ = wire_harness()
        voice = await wire_program(h)
        guild.answers = False
        wire.closes.put_nowait(4014)
        await until_settled(lambda: not voice.is_connected())
        guild.deliver(None)
        await until_settled(lambda: GUILD not in h.adapter.players and remembered(h) is None)
        await join_retirements(h)
        assert h.pool.leased == 0 and guild.state.clients == {}
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_a_forced_close_with_a_new_channel_is_an_adopted_move(wire: Wire) -> None:
    async def scenario() -> None:
        h, guild, _, other = wire_harness()
        voice = await wire_program(h)
        player = player_of(h)
        heard = player.station.heard
        wire.closes.put_nowait(4014)
        await until_settled(lambda: not voice.is_connected())
        guild.deliver(other)  # the channel and the new voice server arrive
        await until_settled(lambda: voice.is_connected() and remembered(h) == other.id)
        assert voice.channel is cast(Any, other)
        assert player_of(h) is player and player.station.heard is heard and h.pool.leased == 1
        await wire_finish(h, wire)

    asyncio.run(scenario())


@pytest.mark.parametrize("code", [4017, 4021])
def test_a_close_the_library_abandons_is_a_disconnection(wire: Wire, code: int) -> None:
    """These codes end its polling loop without disconnecting, leaving a dead client."""

    async def scenario() -> None:
        h, guild, _, _ = wire_harness()
        voice = await wire_program(h)
        assert voice.is_connected()
        wire.closes.put_nowait(code)
        await until_settled(lambda: GUILD not in h.adapter.players and remembered(h) is None)
        await join_retirements(h)
        assert h.pool.leased == 0 and guild.state.clients == {}
        assert not voice.is_connected()
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_a_reconnect_that_gives_up_is_a_disconnection(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discord_adapter, "VOICE_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> None:
        h, guild, _, _ = wire_harness()
        await wire_program(h)
        guild.answers = False
        wire.closes.put_nowait(4006)
        await until_settled(lambda: GUILD not in h.adapter.players and remembered(h) is None)
        await join_retirements(h)
        assert h.pool.leased == 0 and guild.state.clients == {}
        wire.closes.put_nowait(1000)  # the library's loop carries on until it sees a closure
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_leaving_for_real_reports_nothing_as_a_loss(wire: Wire) -> None:
    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        await wire_program(h)
        assert (await run(h.adapter.leave, inter(room, manager=True))).texts() == [
            "Stopped and left."
        ]
        assert h.adapter.players == {} and remembered(h) is None and guild.state.clients == {}
        wire.closes.put_nowait(1000)
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_shutdown_for_real_keeps_the_channel(wire: Wire) -> None:
    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        await wire_program(h)
        assert await h.adapter.shutdown() is True
        assert remembered(h) == room.id and guild.state.clients == {}
        wire.closes.put_nowait(1000)
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_a_hung_rejoin_doesnt_leave_a_client_blocking_the_next_attempt(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled connect stays registered, and would refuse any later connection."""
    monkeypatch.setattr(discord_adapter, "CONNECT_ATTEMPT_SECONDS", 0.05)
    monkeypatch.setattr(discord_adapter, "VOICE_TIMEOUT_SECONDS", 30.0)

    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        guild.answers = False
        settings_of(h).remembered[GUILD] = room.id
        h.adapter.start_rejoins(guilds(guild))
        await until_settled(lambda: remembered(h) is None)
        assert guild.changes.count(room) == REJOIN_ATTEMPTS  # the second attempt really ran
        assert guild.state.clients == {} and h.adapter.players == {}
        await until_settled(lambda: h.adapter.tasks == set())

    asyncio.run(scenario())


def test_rejoining_for_real_connects_with_no_program(wire: Wire) -> None:
    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        settings_of(h).remembered[GUILD] = room.id
        h.adapter.start_rejoins(guilds(guild))
        await until_settled(lambda: GUILD in h.adapter.players)
        player = player_of(h)
        assert player.station.desired is None and h.pool.leased == 0
        assert guild.state.clients[GUILD] is cast(Any, player.voice)
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_the_rejoin_listener_is_registered_with_every_configuration() -> None:
    async def scenario() -> None:
        for matcher in (FakeMatcher(recording("a")), None):
            adapter = Adapter(matcher, Producer(), FakeSettings(), pool=PcmBankPool())
            bot = create_bot(adapter)
            assert len(bot.extra_events["on_ready"]) == 1
            assert len(bot.extra_events["on_guild_channel_delete"]) == 1

    asyncio.run(scenario())


# A closed client's library loop must not outlive it, and a failed connection leaves nothing.


async def old_loop_finished(voice: disnake.VoiceClient) -> None:
    """Wait until the client's own polling task can't touch the gateway or its slot again."""
    runner = voice._runner  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType, reportUnknownVariableType]
    await until_settled(runner.done)
    await settle()


def test_a_join_within_a_forced_close_keeps_the_new_connection(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kick arrives after close code 4014, and a manager joins before the old timer fires."""
    monkeypatch.setattr(discord_adapter, "VOICE_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> None:
        h, guild, _, other = wire_harness()
        old = await wire_program(h)
        guild.answers = False
        wire.closes.put_nowait(4014)
        await until_settled(lambda: not old.is_connected())
        guild.deliver(None)
        await settle()

        guild.answers = True
        fake = await run(h.adapter.join, inter(other, manager=True))
        assert fake.texts() == [f"Joined {other.mention}."]
        new = player_of(h).voice
        assert new is not old and new.is_connected()

        await old_loop_finished(old)
        assert guild.state.clients[GUILD] is cast(Any, new)  # the old timer left it alone
        assert player_of(h).voice is new and remembered(h) == other.id
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_a_join_during_reconnect_backoff_keeps_the_new_connection(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discord_adapter, "VOICE_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        old = await wire_program(h)
        wire.backoff_seconds = 0.05
        wire.closes.put_nowait(4006)
        await until_settled(lambda: not old.is_connected())

        guild.changes.clear()
        fake = await run(h.adapter.join, inter(room, manager=True))
        assert fake.texts() == [f"Joined {room.mention}."]
        new = player_of(h).voice
        await old_loop_finished(old)
        assert guild.changes == [None, room]  # the old client's close, then the new connection
        assert guild.state.clients[GUILD] is cast(Any, new) and remembered(h) == room.id
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_a_leave_during_reconnect_backoff_stays_gone(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discord_adapter, "VOICE_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        old = await wire_program(h)
        wire.backoff_seconds = 0.05
        wire.closes.put_nowait(4006)
        await until_settled(lambda: not old.is_connected())

        guild.changes.clear()
        fake = await run(h.adapter.leave, inter(room, manager=True))
        assert fake.texts() == ["Stopped and left."]
        await old_loop_finished(old)
        assert guild.changes == [None]  # it never came back into the channel
        assert guild.state.clients == {} and remembered(h) is None

    asyncio.run(scenario())


def test_a_healthy_leave_closes_once_though_closing_wakes_the_loop(wire: Wire) -> None:
    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        old = await wire_program(h)
        guild.changes.clear()
        await run(h.adapter.leave, inter(room, manager=True))
        await old_loop_finished(old)
        assert guild.changes == [None]
        assert guild.state.clients == {} and not old.is_connected()

    asyncio.run(scenario())


def test_a_join_that_hangs_is_bounded_and_leaves_nothing_behind(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost UDP discovery packet: disnake's own timeout covers only the handshake."""
    monkeypatch.setattr(discord_adapter, "CONNECT_ATTEMPT_SECONDS", 0.05)

    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        wire.hangs = True
        fake = await asyncio.wait_for(run(h.adapter.join, inter(room, manager=True)), timeout=5)
        assert fake.texts() == [JOIN_FAILED]
        assert guild.state.clients == {} and h.adapter.players == {} and remembered(h) is None

        # The server isn't stuck: the next /join works, and so does /leave.
        wire.hangs = False
        fake = await run(h.adapter.join, inter(room, manager=True))
        assert fake.texts() == [f"Joined {room.mention}."]
        assert (await run(h.adapter.leave, inter(room, manager=True))).texts() == [
            "Stopped and left."
        ]

    asyncio.run(scenario())


def test_a_join_that_fails_otherwise_leaves_nothing_behind(wire: Wire) -> None:
    """Neither disnake layer closes the client for a failure other than a handshake timeout."""

    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        wire.failure = aiohttp.ClientConnectionError("synthetic: the voice server refused")
        fake = await run(h.adapter.join, inter(room, manager=True))
        assert fake.texts() == [JOIN_FAILED]
        assert guild.state.clients == {} and remembered(h) is None
        assert guild.changes[-1] is None  # it asked Discord to take it out of the channel again

        assert (await run(h.adapter.leave, inter(room, manager=True))).texts() == [NOT_CONNECTED]
        fake = await run(h.adapter.join, inter(room, manager=True))
        assert fake.texts() == [f"Joined {room.mention}."]  # no "already connected"
        await wire_finish(h, wire)

    asyncio.run(scenario())


def test_cancelling_the_loop_closes_the_websocket_it_holds(wire: Wire) -> None:
    """`cleanup` can end the loop just after a reconnect opened a new websocket."""

    async def scenario() -> None:
        h, _, _, _ = wire_harness()
        await h.join()
        voice = player_of(h).voice
        runner = voice._runner  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType, reportUnknownVariableType]
        runner.cancel()
        await until_settled(runner.done)
        assert wire.sockets[-1].closed_with == 1000
        await run(h.adapter.leave, inter(h.room, manager=True))

    asyncio.run(scenario())


def test_an_ended_client_reports_disconnected_and_stays_ended(wire: Wire) -> None:
    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        await h.join()
        voice = player_of(h).voice
        await run(h.adapter.leave, inter(room, manager=True))
        assert not voice.is_connected()
        guild.changes.clear()
        await voice.disconnect(force=True)
        voice.cleanup()
        assert guild.changes == []  # nothing further reaches the gateway

    asyncio.run(scenario())


def test_callers_during_a_disconnect_wait_for_it_to_finish(wire: Wire) -> None:
    """A manager's /join can arrive while the library is already closing the client."""

    async def scenario() -> None:
        h, guild, _, _ = wire_harness()
        await h.join()
        voice = player_of(h).voice
        gate = asyncio.Event()
        original = guild.change_voice_state

        async def held(*, channel: FakeChannel | None) -> None:
            await gate.wait()
            await original(channel=channel)

        guild.change_voice_state = held
        first = asyncio.create_task(voice.disconnect(force=True))
        await settle()
        second = asyncio.create_task(voice.disconnect(force=True))
        await settle()
        assert not first.done() and not second.done()  # the second is waiting, not acting
        gate.set()
        await asyncio.gather(first, second)
        assert guild.changes == [None] and guild.state.clients == {}
        await until_settled(lambda: h.adapter.tasks == set())

    asyncio.run(scenario())


class Frames(disnake.AudioSource):
    """A source that has audio, so the player thread goes on to send a frame."""

    def read(self) -> bytes:
        return SILENCE


def test_a_player_thread_waiting_for_a_lost_connection_ends_with_it(wire: Wire) -> None:
    """`AudioPlayer.stop` doesn't wake a thread parked on a connection that's already down."""

    async def scenario() -> None:
        h, _, _, _ = wire_harness()
        await h.join()
        voice = player_of(h).voice
        voice._connected.clear()  # pyright: ignore[reportPrivateUsage]
        thread = disnake.player.AudioPlayer(Frames(), voice)
        voice._player = thread  # pyright: ignore[reportPrivateUsage]
        thread.start()

        await voice.disconnect(force=True)

        await asyncio.to_thread(thread.join, 5)
        assert not thread.is_alive()  # it used up one of the service's tasks until a restart
        # It read one more frame on the way out, which went nowhere and raised nothing.
        assert thread._current_error is None  # pyright: ignore[reportPrivateUsage]
        assert not voice.is_connected()
        await until_settled(lambda: GUILD not in h.adapter.players)
        await settle()

    asyncio.run(scenario())


def test_a_frame_with_nowhere_to_go_is_dropped_rather_than_ending_the_thread(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A voice server update while connected clears the endpoint until the reconnect is done."""
    sent: list[bytes] = []

    def send(self: disnake.VoiceClient, data: bytes, *, encode: bool = True) -> None:
        sent.append(data)

    monkeypatch.setattr(disnake.VoiceClient, "send_audio_packet", send)

    async def scenario() -> None:
        h, _, room, _ = wire_harness()
        await h.join()
        voice = player_of(h).voice
        voice.endpoint_ip = "192.0.2.1"
        voice.send_audio_packet(b"first")
        assert sent == [b"first"]

        update: Any = {"token": "synthetic", "guild_id": GUILD, "endpoint": "voice.invalid"}
        await voice.on_voice_server_update(update)
        assert voice.endpoint_ip is disnake.utils.MISSING
        voice.send_audio_packet(b"second")
        assert sent == [b"first"]

        voice.endpoint_ip = "192.0.2.1"  # the reconnect's discovery found it again
        voice.send_audio_packet(b"third")
        assert sent == [b"first", b"third"]

        await run(h.adapter.leave, inter(room, manager=True))
        voice.send_audio_packet(b"fourth")  # its socket is closed
        assert sent == [b"first", b"third"]

    asyncio.run(scenario())


def test_a_local_limit_at_rejoin_keeps_the_channel(
    wire: Wire, caplog: pytest.LogCaptureFixture
) -> None:
    """No task left for a voice keep-alive thread says nothing about the channel."""
    caplog.set_level(logging.INFO, logger="sobafm")

    async def scenario() -> None:
        h, guild, room, _ = wire_harness()
        settings_of(h).remembered[GUILD] = room.id
        wire.failure = RuntimeError("can't start new thread")
        h.adapter.start_rejoins(guilds(guild))
        await until_settled(lambda: h.adapter.tasks == set())
        assert remembered(h) == room.id and h.adapter.players == {}
        assert guild.state.clients == {}  # and the failed attempt's client is gone
        assert guild.changes.count(room) == 1  # another attempt wouldn't help

    asyncio.run(scenario())
    lines = adapter_log(caplog)
    assert "voice rejoin: outcome=kept reason=local_failure" in lines
    assert not [line for line in lines if "outcome=forgotten" in line]


def test_a_new_gateway_session_ends_with_the_channel_forgotten(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`parse_ready` clears the library's table, so its next reconnect can't hear an answer.

    The library says that reconnect never succeeds, and the requirements' "on disconnection
    forget the channel" covers it, so `docs/operations.md` tells operators to expect it.
    """
    monkeypatch.setattr(discord_adapter, "VOICE_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> None:
        h, guild, _, _ = wire_harness()
        await wire_program(h)
        guild.state.clients.clear()  # `ConnectionState.clear`, called by `parse_ready`
        wire.closes.put_nowait(4006)
        await until_settled(lambda: GUILD not in h.adapter.players and remembered(h) is None)
        await join_retirements(h)
        assert h.pool.leased == 0

    asyncio.run(scenario())


@offline
async def test_a_failure_at_rejoin_other_than_a_local_limit_still_forgets() -> None:
    h, guild, channel = rejoin_harness(VocalChannel("x", failures=1_000, failure=OSError))
    await rejoin(h, guild)
    assert channel.connects == REJOIN_ATTEMPTS and remembered(h) is None


@offline
async def test_a_local_limit_with_a_fake_channel_tries_once_and_keeps_it() -> None:
    h, guild, channel = rejoin_harness(VocalChannel("x", failures=1_000, failure=RuntimeError))
    await rejoin(h, guild)
    assert channel.connects == 1 and remembered(h) == channel.id
