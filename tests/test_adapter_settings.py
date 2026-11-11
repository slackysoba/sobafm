"""`/settings`, volume and the change cooldown through the Discord adapter; no gateway or voice."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import disnake
import pytest

from sobafm import discord_adapter
from sobafm.catalog import Recording
from sobafm.discord_adapter import (
    MATCH_BUSY,
    NOT_A_MUSIC_REQUEST,
    NOTHING_ADMITTED,
    SETTINGS_INVALID,
    SETTINGS_UNAVAILABLE,
    SHUTTING_DOWN,
    SILENCE,
    SKIPPING,
    STOPPED_WHILE_MATCHING,
    Adapter,
    GuildPlayer,
    Inter,
    create_bot,
)
from sobafm.gemini_match import NO_RECORDINGS, MatchFailed, Refused
from sobafm.pcm import FRAME_BYTES, PcmBank, PcmBankPool
from sobafm.settings import ServerSettings, SettingsError, SettingsStore
from tests.test_discord_adapter import (
    MANY_NOTICES,
    FakeInteraction,
    FakeMatcher,
    FakeSettings,
    Harness,
    Producer,
    Tagged,
    harness,
    heard_id,
    inter,
    matcher_of,
    next_id,
    offline,
    prepared_next,
    program_harness,
    recording,
    run,
    settings_of,
    settle,
    skip,
    start_program,
    tag,
    until,
)

# 1,920 samples per frame: both extremes, values near zero and everything in between.
SAMPLES = (32767, -32768, 16384, -16384, 1000, -1000, 100, -100, 1, -1, 0, 12345)
assert len(SAMPLES) * 160 == FRAME_BYTES // 2
FRAME = struct.pack(f"<{len(SAMPLES)}h", *SAMPLES) * 160


def samples(frame: bytes) -> tuple[int, ...]:
    return struct.unpack(f"<{len(frame) // 2}h", frame)


class Patterned(Producer):
    """Writes a known frame, so what the voice player receives can be compared to it."""

    def __init__(self, frame: bytes = FRAME, frames: int = 400) -> None:
        super().__init__(frames, end=False)
        self.frame = frame

    async def produce(self, recording: Recording, bank: PcmBank) -> None:
        bank.write(self.frame * self.frames)
        await asyncio.Event().wait()  # producers stay alive until cancelled


def cooling(h: Harness, cooldown: int = 30) -> Harness:
    settings_of(h).current = ServerSettings(cooldown_seconds=cooldown)
    return h


async def settings(h: Harness, fake: FakeInteraction | None = None, /, **values: int) -> list[str]:
    def handler(interaction: Inter) -> Awaitable[None]:
        return h.adapter.settings(interaction, **values)

    return [t or "" for t in (await run(handler, fake or inter(h.room, manager=True))).texts()]


def player_of(h: Harness) -> GuildPlayer:
    return h.adapter.players[1]


async def until_settled(condition: Callable[[], bool]) -> None:
    """Wait for ``condition`` through a real timer or thread, within a generous bound."""
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.001)


async def heard_pattern(h: Harness) -> GuildPlayer:
    """Join, play and hear the known frame once."""
    await h.join()
    await h.play()
    await settle()
    player = player_of(h)
    assert player.output.read() != SILENCE
    await settle()
    assert player.station.heard is not None
    return player


# Registration and access.


@offline
async def test_settings_requires_manage_server_and_offers_the_required_ranges() -> None:
    for matcher in (FakeMatcher(recording("a")), None):
        adapter = Adapter(matcher, Producer(), FakeSettings(), pool=PcmBankPool())
        bodies = {c.name: c.body for c in create_bot(adapter).slash_commands}
        body = bodies["settings"]
        manage = body.default_member_permissions
        assert manage is not None and manage.value == type(manage)(manage_guild=True).value
        assert body.contexts is not None and body.contexts.guild
        assert not body.contexts.bot_dm and not body.contexts.private_channel
        assert {
            option.name: (option.required, option.min_value, option.max_value)
            for option in body.options
        } == {
            "duration": (False, 5, 240),
            "volume": (False, 1, 100),
            "cooldown": (False, 0, 600),
        }


@offline
async def test_settings_refuses_direct_messages_and_writes_nothing() -> None:
    h = harness()
    assert await settings(h, inter(h.room, manager=True, guild=False), volume=20) == [
        discord_adapter.SERVER_ONLY
    ]
    assert settings_of(h).writes == 0


# Showing and changing.


@offline
async def test_settings_shows_the_values_and_their_ranges_privately_without_writing() -> None:
    h = harness()
    settings_of(h).current = ServerSettings()
    fake = inter(h.room, manager=True)
    (text,) = await settings(h, fake)
    assert "Volume: 50% (1 to 100)" in text
    assert "Change cooldown: 30 seconds (0 to 600)" in text
    assert "Generated duration: 60 minutes (5 to 240)" in text
    assert fake.sent[0].options["ephemeral"] is True
    assert fake.sent[0].options["allowed_mentions"] is discord_adapter.NO_MENTIONS
    assert settings_of(h).writes == 0


def test_a_change_is_saved_and_a_restart_reads_it(tmp_path: Path) -> None:
    path = tmp_path / "sobafm.db"

    async def change() -> None:
        store = SettingsStore.open(path)
        h = harness(settings=store)
        fake = inter(h.room, manager=True)
        (text,) = await settings(h, fake, duration=90, volume=20, cooldown=5)
        assert text.startswith("Saved.") and "Volume: 20%" in text
        assert "Change cooldown: 5 seconds" in text and "Generated duration: 90 minutes" in text
        assert fake.sent[0].options["ephemeral"] is True
        assert await store.close() is True

    async def restart() -> None:
        store = SettingsStore.open(path)
        h = harness(settings=store)
        (text,) = await settings(h)
        assert "Volume: 20%" in text and "Change cooldown: 5 seconds" in text
        await h.join()
        assert player_of(h).output.volume == 0.2  # a new connection plays at the saved volume
        assert await h.adapter.shutdown() is True
        assert await store.close() is True

    asyncio.run(change())
    asyncio.run(restart())


@pytest.mark.parametrize(
    "values",
    [
        {"volume": 0},
        {"volume": 101},
        {"cooldown": -1},
        {"cooldown": 601},
        {"duration": 4},
        {"duration": 241},
        {"volume": 20, "cooldown": 601},
    ],
)
def test_a_value_out_of_range_changes_nothing(tmp_path: Path, values: dict[str, int]) -> None:
    path = tmp_path / "sobafm.db"

    async def scenario() -> None:
        store = SettingsStore.open(path)
        h = harness(Patterned(), settings=store)
        player = await heard_pattern(h)
        assert await settings(h, **values) == [SETTINGS_INVALID]
        assert store.get(1) == ServerSettings()
        assert player.output.volume == 0.5
        assert await h.adapter.shutdown() is True
        assert await store.close() is True

    asyncio.run(scenario())
    reader = sqlite3.connect(path, autocommit=True)
    assert reader.execute("SELECT count(*) FROM server_settings").fetchone() == (0,)
    reader.close()


def test_a_failed_save_changes_nothing_and_logs_only_its_kind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        h = harness(Patterned())
        player = await heard_pattern(h)
        settings_of(h).failure = SettingsError("SQLITE_FULL")
        assert await settings(h, volume=100) == [SETTINGS_UNAVAILABLE]
        assert player.output.volume == 0.5

    with caplog.at_level(logging.WARNING, logger="sobafm"):
        asyncio.run(scenario())
    assert "settings write failed: kind=SQLITE_FULL" in caplog.text


@offline
async def test_settings_refuses_a_change_during_shutdown_but_still_shows() -> None:
    h = harness()
    h.adapter.stopping.set()
    assert await settings(h, volume=20) == [SHUTTING_DOWN]
    assert settings_of(h).writes == 0
    (text,) = await settings(h)
    assert "Volume: 50%" in text


@offline
async def test_a_rejected_acknowledgement_writes_nothing() -> None:
    h = harness()
    fake = inter(h.room, manager=True)
    fake.rejected = True
    with pytest.raises(disnake.NotFound):
        await h.adapter.settings(cast(Inter, fake), volume=20)
    assert settings_of(h).writes == 0 and fake.sent == []


# Volume.


@pytest.mark.parametrize(
    ("percent", "top", "bottom"),
    [(100, 32767, -32768), (50, 16383, -16384), (1, 327, -328)],
)
def test_gain_is_linear_at_the_extremes_and_cannot_clip(
    percent: int, top: int, bottom: int
) -> None:
    async def scenario() -> None:
        h = harness(Patterned())
        settings_of(h).current = ServerSettings(volume_percent=percent, cooldown_seconds=0)
        await h.join()
        await h.play()
        await settle()
        output = player_of(h).output
        scaled = samples(output.read())
        assert len(scaled) == len(samples(FRAME))
        assert scaled[:2] == (top, bottom)
        for before, after in zip(samples(FRAME), scaled, strict=True):
            assert abs(after) <= abs(before)  # a gain of at most one cannot clip
            assert abs(after - before * percent / 100) <= 1  # linear, to within one step
        assert await h.adapter.shutdown() is True

    asyncio.run(scenario())


@offline
async def test_full_volume_leaves_every_byte_unchanged() -> None:
    h = harness(Patterned())
    settings_of(h).current = ServerSettings(volume_percent=100, cooldown_seconds=0)
    await h.join()
    await h.play()
    await settle()
    assert player_of(h).output.read() == FRAME
    assert await h.adapter.shutdown() is True


@offline
async def test_the_voice_player_receives_the_volume_output() -> None:
    h = harness(Patterned())
    player = await heard_pattern(h)
    voice = h.room.voice
    assert voice is not None and voice.plays[-1] is cast(Any, player.output)
    assert player.output.original is player.source
    assert await h.adapter.shutdown() is True


@offline
async def test_silence_stays_silent_and_the_end_of_audio_still_ends() -> None:
    h = harness(Producer(frames=1))
    await h.join()
    player = player_of(h)
    assert player.output.read() == b""  # idle: the player stops
    h.producer.end = False  # not ready: fewer than six seconds and no verified end
    await h.play()
    await settle()
    assert player.output.read() == SILENCE
    assert await h.adapter.shutdown() is True


@offline
async def test_a_change_reaches_current_audio_at_the_next_frame() -> None:
    h = harness(Patterned())
    player = await heard_pattern(h)
    heard = player.station.heard
    voice = h.room.voice
    assert voice is not None
    plays = len(voice.plays)
    assert samples(player.output.read())[0] == 16383
    assert (await settings(h, volume=100))[0].startswith("Saved.")
    assert samples(player.output.read())[0] == 32767
    assert (await settings(h, volume=1))[0].startswith("Saved.")
    assert samples(player.output.read())[0] == 327
    assert player.station.heard is heard and len(voice.plays) == plays  # no restart or switch
    assert await h.adapter.shutdown() is True


def test_the_voice_callback_never_waits_for_the_database(tmp_path: Path) -> None:
    """A write stuck behind a database lock leaves frames flowing at the previous gain."""
    path = tmp_path / "sobafm.db"

    async def scenario() -> None:
        store = SettingsStore.open(path)
        h = harness(Patterned(), settings=store)
        player = await heard_pattern(h)
        blocker = sqlite3.connect(path, autocommit=True)
        blocker.execute("BEGIN EXCLUSIVE")
        change = asyncio.create_task(settings(h, volume=100))
        for _ in range(20):
            await asyncio.sleep(0)
            assert samples(player.output.read())[0] == 16383  # the old gain, without waiting
        assert not change.done()
        blocker.execute("COMMIT")
        blocker.close()
        assert (await change)[0].startswith("Saved.")
        assert samples(player.output.read())[0] == 32767
        assert await h.adapter.shutdown() is True
        assert await store.close() is True

    asyncio.run(scenario())


# Cooldown: rapid requests.


@offline
async def test_rapid_requests_are_refused_before_interpretation_and_selection() -> None:
    h, tagged = program_harness("a", "b", "c", producer=Tagged(frames=50))
    cooling(h)
    await start_program(h)
    await prepared_next(h, "b")
    started = list(tagged.started)
    assert matcher_of(h).requests == ["piano"]

    assert await h.play("another") == [discord_adapter.COOLING.format("30 seconds")]
    assert await skip(h) == discord_adapter.COOLING.format("30 seconds")
    assert matcher_of(h).requests == ["piano"]  # no second interpretation
    assert tagged.started == started and next_id(h) == "b"  # no selection or cut
    station = player_of(h).station
    assert station.desired is not None and station.desired.request == "piano"

    h.clock.now += 29.5
    assert await skip(h) == discord_adapter.COOLING.format("1 second")
    h.clock.now += 0.5  # refusals did not extend it
    assert await skip(h) == SKIPPING
    assert await h.adapter.shutdown() is True


@offline
async def test_a_zero_cooldown_lets_every_request_through() -> None:
    h = cooling(harness(), 0)
    await h.join()
    for request in ("one", "two", "three"):
        (text,) = await h.play(request)
        assert "Request accepted" in text
    assert matcher_of(h).requests == ["one", "two", "three"]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_changed_setting_applies_to_the_running_cooldown() -> None:
    h = cooling(harness())
    await h.join()
    assert "Request accepted" in (await h.play("one"))[0]
    assert (await h.play("two"))[0].startswith("This server's change cooldown")
    await settings(h, cooldown=0)
    assert "Request accepted" in (await h.play("three"))[0]
    assert matcher_of(h).requests == ["one", "three"]
    assert await h.adapter.shutdown() is True


@offline
async def test_requests_during_matching_call_nothing_and_the_first_keeps_the_claim() -> None:
    h, _ = program_harness("a", "b", producer=Tagged(frames=50))
    cooling(h)
    await start_program(h)
    h.clock.now += 30
    fake = matcher_of(h)
    fake.gate = asyncio.Event()
    first = asyncio.create_task(h.play("first"))
    await settle()
    assert await h.play("second") == [MATCH_BUSY]
    assert await skip(h) == discord_adapter.COOLING.format("30 seconds")
    fake.gate.set()
    assert "Request accepted" in (await first)[0]
    assert fake.requests == ["piano", "first"]
    assert (await h.play("third"))[0].startswith("This server's change cooldown")
    assert await h.adapter.shutdown() is True


# Cooldown: failed and no-op requests give back their own claim.


@offline
async def test_a_request_without_a_plan_gives_back_its_claim() -> None:
    h = cooling(harness())
    await h.join()
    fake = matcher_of(h)
    for outcome, reply in (
        (Refused(), NOT_A_MUSIC_REQUEST),
        (
            MatchFailed("timeout"),
            "The interpretation request timed out. "
            "This request did not change playback. Try again shortly.",
        ),
        (MatchFailed(NO_RECORDINGS), NOTHING_ADMITTED),
    ):
        fake.outcome = outcome
        assert await h.play() == [reply]
    fake.outcome = None
    assert "Request accepted" in (await h.play())[0]
    assert len(fake.requests) == 4  # each reached the matcher: none was on cooldown
    assert (await h.play())[0].startswith("This server's change cooldown")
    assert await h.adapter.shutdown() is True


@offline
async def test_invalid_text_takes_no_claim() -> None:
    h = cooling(harness())
    await h.join()
    assert "printable characters" in (await h.play("   "))[0]
    assert "Request accepted" in (await h.play())[0]
    assert await h.adapter.shutdown() is True


@offline
async def test_recordings_with_unusable_credits_give_back_the_claim() -> None:
    too_long = recording("x", tuple(f"{n}: " + "x" * 1000 for n in range(40)))
    h = cooling(harness(recordings=(too_long,)))
    await h.join()
    assert "credits are incomplete" in (await h.play())[0]
    h.adapter.matcher = FakeMatcher(recording("a"))
    assert "Request accepted" in (await h.play())[0]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_refused_bank_lease_gives_back_the_claim() -> None:
    h = cooling(harness())
    await h.join()
    banks = [h.pool.reserve(1).acquire(), h.pool.reserve(1).acquire()]
    assert "capacity is busy" in (await h.play())[0]
    assert player_of(h).waiting_claim is None
    for bank in banks:
        assert bank.release()
    assert "Request accepted" in (await h.play())[0]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_voice_failure_gives_back_the_claim() -> None:
    h = cooling(harness())
    await h.join()
    voice = h.room.voice
    assert voice is not None
    play = voice.play

    def refuse(source: disnake.AudioSource) -> None:
        raise RuntimeError("synthetic: not connected")

    voice.play = refuse
    assert "could not start" in (await h.play())[0]
    voice.play = play
    assert "Request accepted" in (await h.play())[0]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_track_that_fails_to_start_gives_back_its_requests_claim() -> None:
    h, tagged = program_harness("a", producer=Tagged(frames=1, end=False))
    cooling(h)
    source = await h.join()
    assert "Request accepted" in (await h.play())[0]
    await until(lambda: "a" in tagged.banks)
    assert (await h.play())[0].startswith("This server's change cooldown")

    tagged.banks["a"].cancel()  # the decoder failed before readiness
    assert source.read() in (SILENCE, b"")
    await settle()
    assert player_of(h).station.desired is None
    assert "Request accepted" in (await h.play())[0]  # no wait after the failure
    assert await h.adapter.shutdown() is True


@offline
async def test_a_failed_start_after_a_deferred_claim_gives_it_back() -> None:
    """A deferred replacement returns its own claim when the capacity wait expires."""
    h, _ = program_harness("a", producer=Tagged(frames=50))
    cooling(h, 0)
    source = await h.join()
    await h.play()
    await settle()
    settings_of(h).current = ServerSettings(cooldown_seconds=30)
    h.clock.now += 30
    assert source.read() == tag("a")  # the switch is reported but not yet settled
    assert "Request accepted" in (await h.play("second"))[0]
    player = player_of(h)
    assert player.waiting_claim is not None and player.station.ready is not None
    banks: list[PcmBank] = []
    while h.pool.leased < 2:
        banks.append(h.pool.reserve(1).acquire())  # the settled report's start finds no capacity
    with pytest.MonkeyPatch.context() as bounds:
        bounds.setattr(discord_adapter, "CAPACITY_WAIT_SECONDS", 0.05)
        await until_settled(lambda: player.station.ready is None)
    assert player.waiting_claim is None
    for bank in banks:
        assert bank.release()
    assert "Request accepted" in (await h.play("third"))[0]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_skip_with_nothing_to_skip_gives_back_its_claim() -> None:
    h, _ = program_harness("a", producer=Tagged(frames=50))
    cooling(h)
    source = await h.join()
    assert "audible to skip" in await skip(h)
    await h.play()
    await settle()
    assert source.read() == tag("a")
    await until(lambda: heard_id(h) == "a")
    h.clock.now += 30
    # The one-recording plan is exhausted, so this skip does nothing either.
    await until(lambda: player_of(h).station.ending is not None)
    assert "Nothing follows" in await skip(h)
    assert "Nothing follows" in await skip(h)  # not on cooldown from the one before
    assert "Request accepted" in (await h.play("next"))[0]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_stop_during_matching_gives_back_the_claim_of_the_request_it_dropped() -> None:
    h = cooling(harness())
    await h.join()
    fake = matcher_of(h)
    fake.gate = asyncio.Event()
    played = asyncio.create_task(h.play())
    await settle()
    await run(h.adapter.stop, inter(h.room))
    fake.gate.set()
    assert await played == [STOPPED_WHILE_MATCHING]
    assert "Request accepted" in (await h.play())[0]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_stop_after_acceptance_leaves_the_cooldown_running() -> None:
    h = cooling(harness())
    await h.join()
    assert "Request accepted" in (await h.play())[0]
    await run(h.adapter.stop, inter(h.room))
    assert (await h.play())[0].startswith("This server's change cooldown")
    assert await h.adapter.shutdown() is True


def test_a_stop_leaves_a_waiting_skips_cooldown_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """The skip was accepted, so the stop is not its failure, even if its task ends later."""
    monkeypatch.setattr(discord_adapter, "SETTLE_SECONDS", 0.01)

    async def scenario() -> None:
        h, _ = program_harness("a", "b", producer=Tagged(frames=50))
        cooling(h)
        source = await h.join()
        await h.play()
        await settle()
        other = h.pool.reserve(1).acquire()  # the next track waits for capacity
        assert source.read() == tag("a")
        await until(lambda: heard_id(h) == "a")
        h.clock.now += 30
        assert await skip(h) == SKIPPING
        player = player_of(h)
        waiting = player.continuation
        assert waiting is not None and player.waiting_claim is not None

        await run(h.adapter.stop, inter(h.room))
        await asyncio.wait({waiting})
        assert player.waiting_claim is None
        assert (await h.play("next"))[0].startswith("This server's change cooldown")
        assert other.release()
        assert await h.adapter.shutdown() is True

    asyncio.run(scenario())


@offline
async def test_a_skip_whose_next_track_fails_gives_back_its_claim() -> None:
    h, tagged = program_harness("a", "b", "c", producer=Tagged(frames=50))
    cooling(h)
    source = await start_program(h)
    await prepared_next(h, "b")
    h.clock.now += 30
    assert await skip(h) == SKIPPING
    assert (await skip(h)).startswith("This server's change cooldown")

    tagged.banks["b"].cancel()  # the next track's decoder failed before it was heard
    assert source.read() == tag("a")  # the current recording continues
    await settle()
    await prepared_next(h, "c")
    assert await skip(h) == SKIPPING  # no wait after the failure
    assert await h.adapter.shutdown() is True


def test_a_skip_that_outlives_its_reply_gives_back_its_claim_when_it_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply already said it was preparing; the failure comes afterwards."""
    monkeypatch.setattr(discord_adapter, "SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(discord_adapter, "CAPACITY_WAIT_SECONDS", 0.01)

    async def scenario() -> None:
        h, tagged = program_harness("a", "b", producer=Tagged(frames=2))
        cooling(h)
        source = await h.join()
        await h.play()
        await settle()
        other = h.pool.reserve(1).acquire()  # this station retains its replacement slot
        # The heard track's own bank stays held by its retirement, so none ever comes free.
        gate = tagged.holding["a"] = asyncio.Event()
        assert source.read() == tag("a")
        await until(lambda: heard_id(h) == "a")
        h.clock.now += 30
        assert await skip(h) == SKIPPING  # still waiting for capacity
        player = player_of(h)
        assert player.waiting_claim is not None
        assert (await skip(h)).startswith("This server's change cooldown")

        assert source.read() == tag("a")
        assert source.read() == b""  # the heard track drains and nothing can follow
        await until_settled(lambda: player.waiting_claim is None)
        assert player.station.desired is None and tagged.started == ["a"]
        gate.set()
        assert other.release()
        await until_settled(lambda: h.pool.leased == 0)
        assert "Request accepted" in (await h.play("next"))[0]  # no wait after the failure
        assert await h.adapter.shutdown() is True

    asyncio.run(scenario())


# Cooldown: stale reports can't release a newer request's claim.


@offline
async def test_a_stale_match_failure_cannot_release_a_newer_requests_claim() -> None:
    h, _ = program_harness("a", "b", producer=Tagged(frames=50))
    cooling(h)
    await start_program(h)
    await prepared_next(h, "b")
    h.clock.now += 30
    fake = matcher_of(h)
    fake.gate = asyncio.Event()
    fake.outcome = MatchFailed("timeout")
    older = asyncio.create_task(h.play("older"))
    await settle()
    h.clock.now += 30  # the older request's time passes while it is still matching
    assert await skip(h) == SKIPPING  # a newer request holds the cooldown now
    fake.gate.set()
    assert await older == [
        "The interpretation request timed out. "
        "This request did not change playback. Try again shortly."
    ]  # its failure gives back only its own claim
    assert (await skip(h)).startswith("This server's change cooldown")
    assert (await h.play("newest"))[0].startswith("This server's change cooldown")
    assert await h.adapter.shutdown() is True


@offline
async def test_a_stale_track_failure_cannot_release_a_newer_requests_claim() -> None:
    h, tagged = program_harness("a", producer=Tagged(frames=1, end=False))
    cooling(h)
    source = await h.join()
    assert "Request accepted" in (await h.play("older"))[0]
    await until(lambda: "a" in tagged.banks)
    older_bank = tagged.banks["a"]

    h.clock.now += 30
    fake = matcher_of(h)
    fake.gate = asyncio.Event()
    newer = asyncio.create_task(h.play("newer"))  # claims, then waits for its match
    await settle()
    older_bank.cancel()  # the older track fails while the newer request is matching
    assert source.read() in (SILENCE, b"")
    await settle()
    fake.gate.set()
    assert "Request accepted" in (await newer)[0]
    assert (await h.play("newest"))[0].startswith("This server's change cooldown")
    assert fake.requests == ["older", "newer"]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_superseded_continuation_leaves_a_newer_requests_claim_alone() -> None:
    """An old program's next track, still waiting for a bank, ends after a newer `/play`."""
    h, _ = program_harness("a", "b", producer=Tagged(frames=2))
    cooling(h)
    source = await h.join()
    await h.play("old")
    await settle()
    blockers = [h.pool.reserve(1).acquire()]  # the old program's next track waits for capacity
    assert source.read() == tag("a")
    await until(lambda: heard_id(h) == "a")
    player = player_of(h)
    waiting = player.continuation
    assert waiting is not None and not waiting.done()
    assert source.read() == tag("a")
    assert source.read() == b""  # its track drains; its retirement frees that bank
    await until(lambda: h.pool.leased == 1)
    blockers.append(h.pool.reserve(1).acquire())  # keep it waiting, now with nothing audible
    await settle()  # it wakes for the freed bank and finds none, then sleeps again
    assert not waiting.done()

    # Free one bank without any report, so it sleeps on while a newer program takes it.
    assert blockers.pop().release()
    h.clock.now += 30
    assert "Request accepted" in (await h.play("replacement"))[0]
    await settle()
    assert blockers.pop().release()
    assert not waiting.done()

    # Another server's retirement wakes the old task. In the same turn of the loop that
    # track switches, and before its report settles a newer `/play` is accepted. It waits
    # for the report, so its claim is still waiting when the old task ends.
    h.adapter._capacity_changed()  # pyright: ignore[reportPrivateUsage]
    assert source.read() == tag("a")
    h.clock.now += 30
    assert "Request accepted" in (await h.play("newer"))[0]
    claim = player.waiting_claim
    assert claim is not None
    await asyncio.wait({waiting})
    await until(lambda: player.waiting_claim is None)
    pending = player.source._pending  # pyright: ignore[reportPrivateUsage]
    assert pending is not None and pending.claim is claim  # the newer track now holds it
    assert (await h.play("newest"))[0].startswith("This server's change cooldown")
    assert await h.adapter.shutdown() is True


@offline
async def test_a_track_from_before_a_rejoin_cannot_touch_the_new_players_cooldown() -> None:
    h, tagged = program_harness("a", producer=Tagged(frames=1, end=False))
    cooling(h)
    await h.join()
    assert "Request accepted" in (await h.play("older"))[0]
    await until(lambda: "a" in tagged.banks)
    stale = player_of(h).source._pending  # pyright: ignore[reportPrivateUsage]
    assert stale is not None
    await run(h.adapter.leave, inter(h.room, manager=True))
    await h.join()
    assert "Request accepted" in (await h.play("newer"))[0]  # a new connection starts afresh
    h.adapter._settle(1, "failed", stale)  # pyright: ignore[reportPrivateUsage]
    assert (await h.play("newest"))[0].startswith("This server's change cooldown")
    assert await h.adapter.shutdown() is True


# Now-playing wording stays with the heard source when a request is refused.


@offline
async def test_a_refused_request_leaves_the_program_and_credits_untouched() -> None:
    h = cooling(harness(recordings=(recording("a", MANY_NOTICES),)))
    await h.join()
    await h.play()
    await settle()
    player = player_of(h)
    player.source.read()
    await settle()
    heard = player.station.heard
    assert heard is not None
    desired = player.station.desired
    assert (await h.play("other"))[0].startswith("This server's change cooldown")
    assert player.station.heard is heard and player.station.desired == desired
    assert await h.adapter.shutdown() is True
