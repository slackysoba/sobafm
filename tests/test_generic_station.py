"""Source-independent station coordination; Lyria policy stays in test_station."""

import asyncio
import logging
import threading
import weakref
from dataclasses import dataclass
from unittest.mock import MagicMock

import discord
import pytest

from sobafm.audio_budget import AudioBytes, AudioLedger
from sobafm.catalog import CatalogQuery
from sobafm.catalog_interpreter import CatalogPlan
from sobafm.mixer import Mixer
from sobafm.pcm import FRAME_SECONDS, SILENCE
from sobafm.plan import MusicPlan
from sobafm.program import Mode, Outcome, Program
from sobafm.source import State
from sobafm.station import (
    EMPTY_GRACE_S,
    FADE_IN_S,
    FADE_OUT_S,
    PLAYER_RETRY_S,
    SessionPool,
    Station,
)
from sobafm.status import RETRY_MAX_S, RETRY_S, Statuses
from tests.doubles import FakeClock, FakeEngine, FakePlayer, FakeSource, settle

HOUR_S = 3600.0
CHANNEL = 10
LOFI = MusicPlan.from_request("rainy lo-fi")
SYNTHWAVE = MusicPlan.from_request("synthwave")
DRY = "The live deck ran dry; playing silence"
CATALOG = CatalogPlan(title="acoustic", query=CatalogQuery(tags=("acoustic",)))


@dataclass
class Rig:
    station: Station
    generated: FakeEngine
    catalog: FakeEngine
    clock: FakeClock
    player: FakePlayer
    voice: list[FakePlayer]
    listeners: list[int]
    statuses: list[tuple[int, str | None]]
    channel: list[int]

    async def tick(self, seconds: float = 0.25) -> None:
        self.clock.now += seconds
        await settle()
        await self.station.reconcile()
        await settle()

    def listen(self, seconds: float) -> None:
        for _ in range(round(seconds / FRAME_SECONDS)):
            if self.player.running and self.player.connected:
                self.station.mixer.read()

    async def feed(self, index: int, seconds: float) -> None:
        self.generated.sources[index].send_audio(seconds)

    def disconnect(self) -> None:
        self.voice.clear()

    def set_listeners(self, count: int) -> None:
        self.listeners[0] = count

    def move(self, channel: int) -> None:
        self.channel[0] = channel


def make_rig(clock: FakeClock, pool: SessionPool | None = None) -> Rig:
    audio = AudioLedger()
    generated = FakeEngine(Mode.GENERATED, audio, pool if pool is not None else SessionPool(4))
    catalog = FakeEngine(Mode.CATALOG, audio)
    voice = [FakePlayer()]
    listeners = [1]
    statuses: list[tuple[int, str | None]] = []
    channel = [CHANNEL]

    async def show(channel: int, status: str | None) -> bool:
        statuses.append((channel, status))
        return True

    station = Station(
        lambda: voice[0] if voice else None,
        lambda: listeners[0],
        volume=1.0,
        statuses=Statuses(lambda: channel[0] if voice else None, show, clock=clock),
        engines=[generated, catalog],
        clock=clock,
    )
    return Rig(station, generated, catalog, clock, voice[0], voice, listeners, statuses, channel)


@pytest.fixture
def rig(clock: FakeClock) -> Rig:
    return make_rig(clock)


async def start_playing(
    rig: Rig, plan: MusicPlan = LOFI, *, duration_seconds: float = HOUR_S
) -> None:
    started = rig.station.play(plan, "Member", duration_seconds=duration_seconds)
    await rig.tick()
    await rig.feed(-2, 10)
    await rig.tick()
    rig.listen(2)
    assert started.result() is Outcome.PLAYING


async def test_reports_busy_without_session_capacity(clock: FakeClock) -> None:
    station = make_rig(clock, SessionPool(1)).station

    assert station.play(LOFI, "Member", duration_seconds=HOUR_S).result() is Outcome.BUSY


async def test_a_stop_during_the_fade_in_waits_for_it_before_fading_out(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.feed(-2, 10)
    await rig.tick()
    assert started.result() is Outcome.PLAYING

    rig.station.stop()
    await rig.tick()
    assert rig.player.playing  # still fading in
    rig.listen(FADE_IN_S)
    await rig.tick()
    rig.listen(FADE_OUT_S)
    await rig.tick()

    assert rig.station.mixer.live is None
    assert not rig.player.playing


async def test_a_late_callback_from_a_stopped_player_is_ignored(rig: Rig) -> None:
    await start_playing(rig)
    rig.station.stop()
    await rig.tick()
    rig.listen(FADE_OUT_S)
    await rig.tick()  # stops the player, whose thread has yet to end
    assert not rig.player.playing

    rig.station.play(SYNTHWAVE, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()
    await rig.feed(-1, 10)
    await rig.tick(PLAYER_RETRY_S)
    assert rig.player.plays == 2
    rig.player.end_stopped_threads()  # the stopped player's `after` arrives only now
    await rig.tick(PLAYER_RETRY_S)

    assert rig.player.plays == 2  # the new player is still tracked, so none is restarted


async def test_restarts_the_player_after_its_thread_ends(rig: Rig) -> None:
    await start_playing(rig)
    asyncio.get_running_loop().set_debug(True)  # flags loop calls from the player's thread

    rig.player.end_thread()  # a slow voice reconnect: is_playing() still reports True
    await rig.tick(PLAYER_RETRY_S)

    assert rig.player.plays == 2
    assert rig.player.playing


async def test_restarts_a_failing_player_at_most_once_per_interval(rig: Rig) -> None:
    await start_playing(rig)

    for _ in range(4):  # a player that dies as soon as it starts
        rig.player.end_thread()
        await rig.tick()

    assert rig.player.plays == 2


async def test_waits_for_voice_to_reconnect_before_starting_the_player(rig: Rig) -> None:
    rig.player.connected = False
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.feed(-2, 10)
    await rig.tick()
    assert rig.player.plays == 0
    assert not started.done()  # nothing can be heard yet

    rig.player.connected = True
    await rig.tick(PLAYER_RETRY_S)

    assert started.result() is Outcome.PLAYING
    assert rig.player.plays == 1


async def test_hands_over_to_a_new_request_only_once_the_player_runs(rig: Rig) -> None:
    await start_playing(rig)
    rig.player.connected = False
    rig.player.end_thread()  # the thread gave up on a voice outage
    started = rig.station.play(SYNTHWAVE, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()
    await rig.feed(-1, 10)
    await rig.tick()
    assert not started.done()  # nothing can be heard yet

    rig.player.connected = True
    await rig.tick(PLAYER_RETRY_S)

    assert started.result() is Outcome.PLAYING


async def test_hands_over_only_while_voice_is_connected(rig: Rig) -> None:
    await start_playing(rig)
    rig.player.connected = False  # the player thread waits for voice to reconnect
    started = rig.station.play(SYNTHWAVE, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()
    await rig.feed(-1, 10)
    await rig.tick()
    assert not started.done()  # nothing can be heard yet

    rig.player.connected = True
    await rig.tick()

    assert started.result() is Outcome.PLAYING


async def test_hands_over_only_once_a_player_runs_again(rig: Rig) -> None:
    await start_playing(rig)
    started = rig.station.play(SYNTHWAVE, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()
    await rig.feed(-1, 10)

    rig.player.end_thread()  # voice stays connected; the restart waits out PLAYER_RETRY_S
    await rig.tick()
    assert not started.done()  # nothing reads the mixer yet

    await rig.tick(PLAYER_RETRY_S)
    assert started.result() is Outcome.PLAYING


async def test_fades_in_only_while_voice_is_connected(rig: Rig) -> None:
    await start_playing(rig)
    rig.station.stop()
    await rig.tick()
    started = rig.station.play(SYNTHWAVE, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.feed(-1, 10)
    rig.listen(FADE_OUT_S)
    rig.player.connected = False  # voice drops as the fade-out ends
    await rig.tick()
    assert not started.done()

    rig.player.connected = True
    await rig.tick()

    assert started.result() is Outcome.PLAYING


async def test_handles_the_players_callback_on_the_loop_thread(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await start_playing(rig)
    woken_on: list[int] = []
    wake = rig.station.wake

    def record() -> None:
        woken_on.append(threading.get_ident())
        wake()

    monkeypatch.setattr(rig.station, "wake", record)
    rig.player.end_thread()
    await rig.tick()

    assert woken_on == [threading.get_ident()]


async def test_ends_the_program_when_the_voice_connection_is_lost(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    rig.disconnect()

    await rig.tick()

    assert started.result() is Outcome.DISCONNECTED
    assert rig.station.program is None


async def test_close_settles_a_pending_request_with_its_outcome(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()

    await rig.station.close(Outcome.DISCONNECTED)

    assert started.result() is Outcome.DISCONNECTED


async def test_ends_the_program_when_its_duration_elapses(rig: Rig) -> None:
    await start_playing(rig, duration_seconds=300)
    await rig.tick(299.25)
    assert rig.station.program is not None

    await rig.tick(0.25)  # 300 s after the request
    assert rig.station.program is None
    assert rig.station.mixer.switching  # it fades out rather than cutting
    assert rig.player.playing

    rig.listen(FADE_OUT_S)
    await rig.tick()
    await rig.tick()
    assert rig.station.mixer.live is None
    assert not rig.player.playing


async def test_a_new_request_restarts_the_duration(rig: Rig) -> None:
    rig.station.play(LOFI, "Member", duration_seconds=300)
    await rig.tick(200)
    rig.station.play(SYNTHWAVE, "Member", duration_seconds=300)
    await rig.tick()
    await rig.tick(299)
    assert rig.station.program is not None

    await rig.tick(1)
    assert rig.station.program is None


async def test_keeps_playing_when_a_listener_returns(rig: Rig) -> None:
    await start_playing(rig)

    rig.set_listeners(0)
    await rig.tick()
    await rig.tick(EMPTY_GRACE_S - 10)
    rig.set_listeners(1)
    await rig.tick()
    rig.set_listeners(0)
    await rig.tick()  # the grace period starts again here
    await rig.tick(EMPTY_GRACE_S - 1)
    assert rig.station.program is not None

    await rig.tick(1)
    assert rig.station.program is None


async def test_a_request_pending_when_the_grace_period_ends_is_stopped(rig: Rig) -> None:
    rig.set_listeners(0)
    started = rig.station.play(LOFI, "Deafened member", duration_seconds=HOUR_S)
    await rig.tick()

    await rig.tick(EMPTY_GRACE_S)

    assert started.result() is Outcome.STOPPED


async def test_a_lost_connection_outranks_an_empty_channel(rig: Rig) -> None:
    rig.set_listeners(0)
    started = rig.station.play(LOFI, "Deafened member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick(EMPTY_GRACE_S - 1)

    rig.disconnect()
    await rig.tick(1)

    assert started.result() is Outcome.DISCONNECTED


async def test_a_request_during_the_grace_period_restarts_it(rig: Rig) -> None:
    await start_playing(rig)
    rig.set_listeners(0)
    await rig.tick()
    await rig.tick(50)

    rig.station.play(SYNTHWAVE, "Deafened member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick(EMPTY_GRACE_S - 1)
    assert rig.station.program is not None

    await rig.tick(1)
    assert rig.station.program is None


async def test_a_new_request_restarts_the_grace_period(rig: Rig) -> None:
    await start_playing(rig)
    rig.set_listeners(0)
    await rig.tick()
    await rig.tick(EMPTY_GRACE_S)
    assert rig.station.program is None

    rig.station.play(SYNTHWAVE, "Deafened member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick(EMPTY_GRACE_S - 1)
    assert rig.station.program is not None

    await rig.tick(1)
    assert rig.station.program is None


def silence_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The station's lines about silence and programs' totals, in order."""
    prefixes = ("The live deck ran dry", "Silence lasted", "Program ended")
    return [message for message in caplog.messages if message.startswith(prefixes)]


async def test_logs_a_run_of_silence_once_and_its_length(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)  # a second past the live deck's audio
        await rig.tick()
        rig.listen(1)  # still dry
        await rig.tick()
        await rig.feed(0, 10)  # audio resumes
        rig.listen(1 - FRAME_SECONDS)
        await rig.tick()
        assert silence_lines(caplog) == [DRY]  # not over until audio has played a while
        rig.listen(FRAME_SECONDS)
        await rig.tick()
        assert silence_lines(caplog) == [DRY, "Silence lasted 2.00 s"]  # after a second of it
        rig.listen(1)
        await rig.tick()

    assert silence_lines(caplog) == [DRY, "Silence lasted 2.00 s"]
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record.getMessage() for record in warnings] == [DRY]


async def test_logs_a_single_dry_read(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    await start_playing(rig)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(8.02)  # one read past the live deck's audio
        await rig.tick()

    assert silence_lines(caplog) == [DRY]


async def test_logs_a_stutter_as_one_run(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    await start_playing(rig)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)  # a second of silence
        await rig.tick()
        await rig.feed(0, 0.5)
        rig.listen(0.5)  # half a second of audio
        await rig.tick()
        rig.listen(0.5)  # then half a second more of silence
        await rig.tick()
        await rig.feed(0, 0.5)
        rig.listen(0.5)  # and half a second of audio, which doesn't add to the earlier half
        await rig.tick()
        assert silence_lines(caplog) == [DRY]
        await rig.feed(0, 10)
        rig.listen(1)
        await rig.tick()

    assert silence_lines(caplog) == [DRY, "Silence lasted 1.50 s"]


async def test_a_run_stays_open_while_reads_pause(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)
        await rig.tick()
        for _ in range(4):  # discord.py reads nothing while voice reconnects
            await rig.tick()
        assert silence_lines(caplog) == [DRY]
        await rig.feed(0, 10)
        rig.listen(1)
        await rig.tick()

    assert silence_lines(caplog) == [DRY, "Silence lasted 1.00 s"]


@pytest.mark.parametrize("end", ["stop", "replace"])
async def test_logs_a_programs_total_when_it_stops_being_heard(
    rig: Rig, caplog: pytest.LogCaptureFixture, end: str
) -> None:
    await start_playing(rig)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)  # a second of silence
        await rig.tick()
        await rig.feed(0, 10)
        rig.listen(1)
        await rig.tick()
        if end == "stop":
            rig.station.stop()
        else:
            rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
            await rig.tick()
            await rig.tick()  # the replacement's deck opens
            assert not any(line.startswith("Program ended") for line in silence_lines(caplog))
            await rig.feed(-1, 10)
            await rig.tick()  # and plays

    outcome = "stopped" if end == "stop" else "replaced"
    assert silence_lines(caplog)[-1] == f"Program ended ({outcome}) with 1.00 s of underrun"


async def test_silence_while_a_replacement_starts_counts_for_the_program_heard(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig)
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()  # the replacement's deck opens

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)  # the program heard runs dry before the replacement plays
        await rig.tick()
        await rig.feed(-1, 10)
        await rig.tick()  # the replacement plays
        # The silence continues into the replacement, so its run is still open.
        assert silence_lines(caplog) == [DRY, "Program ended (replaced) with 1.00 s of underrun"]
        rig.listen(4)
        await rig.tick()
        await rig.feed(-1, 10)  # its next deck fills
        rig.listen(3)  # and its live deck runs low, so it hands over to its own next deck
        await rig.tick()
        rig.listen(4)
        await rig.tick()
        rig.station.stop()

    assert silence_lines(caplog) == [
        DRY,
        "Program ended (replaced) with 1.00 s of underrun",
        "Silence lasted 1.00 s",
        "Program ended (stopped) with 0.00 s of underrun",
    ]


async def test_silence_just_before_a_replacement_plays_is_logged_before_its_total(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig)
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()  # the replacement's deck opens
    await rig.feed(-1, 10)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)  # the program heard runs dry, which no tick has seen
        await rig.tick()  # and the replacement plays

    assert silence_lines(caplog) == [DRY, "Program ended (replaced) with 1.00 s of underrun"]


@pytest.mark.parametrize("end", ["stop", "close"])
async def test_a_program_ended_between_ticks_counts_its_latest_silence(
    rig: Rig, caplog: pytest.LogCaptureFixture, end: str
) -> None:
    await start_playing(rig)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)  # a second of silence that a tick sees
        await rig.tick()
        rig.listen(0.5)  # and half a second more before the next
        if end == "stop":
            rig.station.stop()
            await rig.tick()  # which counts nothing again
        else:
            await rig.station.close(Outcome.DISCONNECTED)

    outcome = "stopped" if end == "stop" else "disconnected"
    assert silence_lines(caplog) == [
        DRY,
        "Silence lasted 1.50 s",
        f"Program ended ({outcome}) with 1.50 s of underrun",
    ]


async def test_a_program_at_its_end_time_logs_its_silence_once(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig, duration_seconds=300)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)  # a second of silence
        await rig.tick(299.5)  # 300 s after the request
        assert rig.station.program is None
        await rig.tick()

    assert silence_lines(caplog) == [
        DRY,
        "Silence lasted 1.00 s",
        "Program ended (stopped) with 1.00 s of underrun",
    ]


async def test_a_program_ended_before_a_tick_saw_its_silence_logs_it(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(8.5)  # half a second of silence, which no tick has seen
        rig.station.stop()
        await rig.tick()

    assert silence_lines(caplog) == [
        DRY,
        "Silence lasted 0.50 s",
        "Program ended (stopped) with 0.50 s of underrun",
    ]


async def test_silence_with_nothing_heard_is_not_a_run(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)
        await rig.tick()
        rig.station.stop()
        rig.listen(1)  # discord.py reads the dry deck until a tick winds it down
        await rig.tick()
        await rig.tick()

    assert silence_lines(caplog) == [
        DRY,
        "Silence lasted 1.00 s",
        "Program ended (stopped) with 1.00 s of underrun",
    ]


async def test_silence_with_nothing_heard_counts_for_no_later_program(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig)
    rig.listen(8)  # the whole buffer, without silence
    rig.station.stop()

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(1)  # silence that no program is heard in, until a tick winds the deck down
        await rig.tick()
        await rig.tick()
        rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
        await rig.tick()
        await rig.feed(-2, 10)
        await rig.tick()
        rig.listen(2)
        await rig.tick()
        rig.station.stop()

    assert silence_lines(caplog) == ["Program ended (stopped) with 0.00 s of underrun"]


async def test_ending_while_a_replacement_starts_logs_the_program_heard(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig)
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)  # the program heard runs dry while the replacement starts
        await rig.tick()
        rig.station.stop()

    assert silence_lines(caplog) == [
        DRY,
        "Silence lasted 1.00 s",
        "Program ended (stopped) with 1.00 s of underrun",
    ]


async def test_failed_reads_end_no_run(
    rig: Rig, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    await start_playing(rig)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)
        await rig.tick()
        await rig.feed(0, 10)
        failing = [True]

        def scaled(frame: bytes, gain: float) -> bytes:
            if failing[0]:
                raise RuntimeError("a bug")
            return frame

        monkeypatch.setattr("sobafm.mixer._scaled", scaled)
        rig.listen(2)  # reads fail, playing silence
        await rig.tick()
        assert silence_lines(caplog) == [DRY]
        failing[0] = False
        rig.listen(1)
        await rig.tick()

    assert silence_lines(caplog) == [DRY, "Silence lasted 1.00 s"]


async def test_counts_no_underrun_without_a_program_heard(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        for _ in range(250):  # five seconds of reads before anything plays
            rig.station.mixer.read()
        await rig.tick()
        rig.station.stop()

    assert rig.station.mixer.underruns == 0
    assert silence_lines(caplog) == []


async def test_reports_the_time_left(rig: Rig) -> None:
    assert rig.station.time_left is None

    rig.station.play(LOFI, "Member", duration_seconds=300)
    await rig.tick(100)

    assert rig.station.time_left == pytest.approx(200)


async def test_shows_the_title_once_the_program_plays(rig: Rig) -> None:
    rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    assert rig.statuses == []  # nothing is heard yet

    await rig.feed(-2, 10)
    for _ in range(4):
        await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title)]  # once


async def test_shows_a_replacements_title_once_it_plays(rig: Rig) -> None:
    await start_playing(rig)
    await rig.feed(1, 60)
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()
    assert rig.statuses == [(CHANNEL, LOFI.title)]  # still what is heard

    await rig.feed(-1, 6)
    await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title), (CHANNEL, SYNTHWAVE.title)]


@pytest.mark.parametrize("end", ["stop", "duration"])
async def test_clears_the_status_when_the_program_ends(rig: Rig, end: str) -> None:
    await start_playing(rig, duration_seconds=300)
    if end == "stop":
        rig.station.stop()
    else:
        rig.clock.now += 300

    await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title), (CHANNEL, None)]


async def test_clears_the_status_once_back_in_the_channel(rig: Rig) -> None:
    await start_playing(rig)
    rig.disconnect()  # a new gateway session: discord.py forgets the voice client
    await rig.tick()
    assert rig.station.program is None
    assert rig.statuses == [(CHANNEL, LOFI.title)]  # nowhere to clear it from yet

    rig.voice.append(rig.player)  # SobaFM rejoins its channel
    await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title), (CHANNEL, None)]


def later_station(rig: Rig) -> Station:
    """Another station in the rig's server, as after a closed one, sharing its statuses."""
    station = Station(
        lambda: rig.voice[0] if rig.voice else None,
        lambda: rig.listeners[0],
        engines=[FakeEngine(Mode.GENERATED, rig.station.audio, SessionPool(4))],
        audio=rig.station.audio,
        volume=1.0,
        statuses=rig.station.statuses,
        clock=rig.clock,
    )
    engine = station.engines[Mode.GENERATED]
    assert isinstance(engine, FakeEngine)
    rig.generated = engine
    return station


async def test_keeps_a_title_a_closed_station_could_not_clear(rig: Rig) -> None:
    await start_playing(rig)
    rig.disconnect()  # SobaFM loses its voice connection

    await rig.station.close(Outcome.DISCONNECTED)

    assert rig.station.statuses.shown == {CHANNEL: LOFI.title}
    assert rig.statuses == [(CHANNEL, LOFI.title)]


async def test_a_later_station_clears_a_title_an_earlier_one_left(rig: Rig) -> None:
    await start_playing(rig)
    rig.disconnect()
    await rig.station.close(Outcome.DISCONNECTED)
    rig.voice.append(rig.player)  # SobaFM connects to the channel again
    rig.station = later_station(rig)

    await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title), (CHANNEL, None)]


async def test_a_new_program_shows_its_title_after_a_left_one_is_cleared(rig: Rig) -> None:
    await start_playing(rig)
    rig.disconnect()
    await rig.station.close(Outcome.DISCONNECTED)
    rig.voice.append(rig.player)
    rig.station = later_station(rig)

    await start_playing(rig, SYNTHWAVE)
    await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title), (CHANNEL, None), (CHANNEL, SYNTHWAVE.title)]


async def test_a_closed_stations_late_title_lands_before_a_new_one(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sobafm.status.TIMEOUT_S", 0.01)
    answered, events = answering_late(rig, monkeypatch)
    await start_playing(rig)  # the title's request is held, as by a rate limit
    rig.disconnect()
    await rig.station.close(Outcome.DISCONNECTED)
    rig.voice.append(rig.player)
    rig.station = later_station(rig)
    await start_playing(rig, SYNTHWAVE)  # while the old request is still in flight

    answered.set()
    for _ in range(3):
        await rig.tick()

    assert events[-2:] == [f"sent {SYNTHWAVE.title}", f"landed {SYNTHWAVE.title}"]
    assert rig.station.statuses.shown == {CHANNEL: SYNTHWAVE.title}


async def test_clears_a_title_left_where_sobafm_is_dragged_back(rig: Rig) -> None:
    await start_playing(rig)
    rig.move(11)  # an administrator drags SobaFM to another channel
    await rig.tick()
    rig.station.stop()  # where the program ends
    await rig.tick()
    assert rig.station.statuses.shown == {CHANNEL: LOFI.title}  # the first channel keeps it

    rig.move(CHANNEL)  # and SobaFM is dragged back
    await rig.tick()

    assert rig.statuses == [
        (CHANNEL, LOFI.title),
        (11, LOFI.title),
        (11, None),
        (CHANNEL, None),
    ]


async def test_never_clears_a_title_forgotten_when_its_channel_emptied(rig: Rig) -> None:
    await start_playing(rig)
    rig.disconnect()
    await rig.station.close(Outcome.DISCONNECTED)
    rig.station.statuses.forget(CHANNEL)  # the channel empties, and Discord clears its status
    rig.voice.append(rig.player)
    rig.station = later_station(rig)

    await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title)]


async def test_shows_the_title_where_sobafm_is_moved(rig: Rig) -> None:
    await start_playing(rig)

    rig.move(11)  # an administrator drags SobaFM to another channel
    await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title), (11, LOFI.title)]


async def test_clears_the_status_before_a_move(rig: Rig) -> None:
    await start_playing(rig)

    async with rig.station.moving():
        assert rig.statuses == [(CHANNEL, LOFI.title), (CHANNEL, None)]
        await rig.tick()  # nothing is shown while SobaFM is still in the old channel
        rig.move(11)
    await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title), (CHANNEL, None), (11, LOFI.title)]


async def test_shows_the_title_still_heard_where_sobafm_is_moved(rig: Rig) -> None:
    await start_playing(rig)
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()

    rig.move(11)
    await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title), (11, LOFI.title)]


async def test_a_request_after_a_stop_shows_nothing_until_it_plays(rig: Rig) -> None:
    await start_playing(rig)
    rig.station.stop()
    await rig.tick()

    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title), (CHANNEL, None)]


async def test_shows_the_title_still_heard_after_join_moves_sobafm(rig: Rig) -> None:
    await start_playing(rig)
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()

    async with rig.station.moving():
        rig.move(11)
    await rig.tick()

    assert rig.statuses == [(CHANNEL, LOFI.title), (CHANNEL, None), (11, LOFI.title)]


async def test_moving_skips_a_clear_sobafm_can_no_longer_send(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sobafm.status.TIMEOUT_S", 0.01)
    answered, events = answering_late(rig, monkeypatch)
    await start_playing(rig)  # the title's request is still in flight

    async with rig.station.moving():  # stops waiting for it
        rig.move(11)
    answered.set()
    await settle()

    # The title landed in the old channel, which SobaFM has left and can no longer clear.
    assert events == [f"sent {LOFI.title}", f"landed {LOFI.title}"]


async def test_clears_the_status_on_close(rig: Rig) -> None:
    await start_playing(rig)

    await rig.station.close()

    assert rig.statuses == [(CHANNEL, LOFI.title), (CHANNEL, None)]


async def test_close_leaves_a_status_it_never_showed(rig: Rig) -> None:
    rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()

    await rig.station.close()

    assert rig.statuses == []


async def test_close_leaves_a_channel_sobafm_was_dragged_out_of(rig: Rig) -> None:
    await start_playing(rig)
    rig.move(11)  # not yet caught up: SobaFM set no status there, and can't clear the old one

    await rig.station.close()

    assert rig.statuses == [(CHANNEL, LOFI.title)]


def answering_late(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, list[str]]:
    """Make Discord answer status requests once the event is set, recording each one."""
    answered = asyncio.Event()
    events: list[str] = []

    async def late(channel: int, status: str | None) -> bool:
        events.append(f"sent {status}")
        await answered.wait()
        events.append(f"landed {status}")
        return True

    monkeypatch.setattr(rig.station.statuses, "_set", late)
    return answered, events


async def test_sends_one_status_request_at_a_time(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await start_playing(rig)
    answered, events = answering_late(rig, monkeypatch)
    rig.station.stop()
    await rig.tick()
    await rig.tick()
    assert events == ["sent None"]  # in flight, and neither repeated nor cancelled

    answered.set()
    await rig.tick()

    assert events == ["sent None", "landed None"]


async def test_close_clears_a_title_that_lands_meanwhile(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    answered, events = answering_late(rig, monkeypatch)
    await start_playing(rig)  # the title's request is in flight
    closing = asyncio.create_task(rig.station.close())
    await settle()

    answered.set()
    async with asyncio.timeout(1):
        await closing

    assert events == [f"sent {LOFI.title}", f"landed {LOFI.title}", "sent None", "landed None"]


async def test_close_waits_a_bounded_time_for_the_status(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sobafm.status.TIMEOUT_S", 0.01)
    answered, events = answering_late(rig, monkeypatch)
    await start_playing(rig)  # Discord doesn't answer the title's request in time

    async with asyncio.timeout(1):
        await rig.station.close()
    answered.set()
    await settle()

    # Nothing was cancelled, and the clear followed the title.
    assert events == [f"sent {LOFI.title}", f"landed {LOFI.title}", "sent None", "landed None"]


async def test_checks_again_for_a_status_sobafm_may_not_set(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = [False]
    shown: list[tuple[int, str | None]] = []

    async def show(channel: int, status: str | None) -> bool:
        shown.append((channel, status))
        return allowed[0]

    monkeypatch.setattr(rig.station.statuses, "_set", show)
    await start_playing(rig)
    await rig.tick(RETRY_S - 0.25)
    assert shown == [(CHANNEL, LOFI.title)]

    allowed[0] = True  # a manager grants the permission
    await rig.tick(0.25)
    rig.station.stop()
    await rig.tick()

    assert shown == [(CHANNEL, LOFI.title), (CHANNEL, LOFI.title), (CHANNEL, None)]


async def test_never_clears_a_status_sobafm_did_not_set(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[tuple[int, str | None]] = []

    async def not_allowed(channel: int, status: str | None) -> bool:
        shown.append((channel, status))
        return False

    monkeypatch.setattr(rig.station.statuses, "_set", not_allowed)
    await start_playing(rig)

    rig.station.stop()
    await rig.tick()
    await rig.station.close()

    assert shown == [(CHANNEL, LOFI.title)]  # a member's own status stays


def refusing(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str | None]]:
    """Make Discord refuse every status request, recording each one."""
    sent: list[tuple[int, str | None]] = []

    async def refuse(channel: int, status: str | None) -> bool:
        sent.append((channel, status))
        raise discord.HTTPException(MagicMock(status=403), "Missing Access")

    monkeypatch.setattr(rig.station.statuses, "_set", refuse)
    return sent


async def test_backs_off_from_a_status_discord_keeps_refusing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sent = refusing(rig, monkeypatch)

    with caplog.at_level(logging.WARNING, logger="sobafm.status"):
        await start_playing(rig)
        for attempt, delay in enumerate([10, 20, 40, 80, 160, 300, 300], start=2):
            await rig.tick(delay - 0.25)
            assert len(sent) == attempt - 1
            await rig.tick(0.25)
            assert len(sent) == attempt

    assert caplog.text.count("Could not update the voice channel status") == 1
    assert rig.station.program is not None  # the status never affects playback


async def test_tries_a_new_status_at_once(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = refusing(rig, monkeypatch)
    await start_playing(rig)
    await rig.feed(1, 60)

    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    for _ in range(4):
        await rig.tick()
    await rig.feed(-1, 6)
    await rig.tick()

    # with no wait after the title that failed
    assert sent == [(CHANNEL, LOFI.title), (CHANNEL, SYNTHWAVE.title)]


async def test_tries_the_title_at_once_where_sobafm_is_dragged(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = refusing(rig, monkeypatch)
    await start_playing(rig)

    rig.move(11)
    await rig.tick()

    assert sent == [(CHANNEL, LOFI.title), (11, LOFI.title)]


async def test_keeps_backing_off_from_a_status_refused_for_days(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await start_playing(rig)
    sent = refusing(rig, monkeypatch)
    rig.station.stop()  # the clear is refused, while SobaFM sits idle in the channel

    for _ in range(1100):
        await rig.tick(RETRY_MAX_S)
    attempts = len(sent)
    await rig.tick()
    await rig.tick()

    assert attempts >= 1100
    assert len(sent) == attempts  # still waiting between attempts


async def test_counts_failures_afresh_once_a_status_shows(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    refusals = iter([True, False, False, True])  # whether Discord refuses each request
    sent: list[str | None] = []

    async def answer(channel: int, status: str | None) -> bool:
        sent.append(status)
        if next(refusals, False):
            raise discord.HTTPException(MagicMock(status=500), "unavailable")
        return True

    monkeypatch.setattr(rig.station.statuses, "_set", answer)
    again = MusicPlan.from_request("rainy lo-fi")  # a later program with the same title
    with caplog.at_level(logging.WARNING, logger="sobafm.status"):
        await start_playing(rig)
        await rig.tick(RETRY_S)
        rig.station.stop()
        await rig.tick()
        rig.station.play(again, "Member", duration_seconds=HOUR_S)
        await rig.tick()
        await rig.feed(-1, 10)
        rig.listen(3)
        await rig.tick()
        assert sent == [LOFI.title, LOFI.title, None, LOFI.title]

        await rig.tick(RETRY_S - 0.25)
        assert len(sent) == 4
        await rig.tick(0.25)

    assert sent == [LOFI.title, LOFI.title, None, LOFI.title, LOFI.title]
    assert caplog.text.count("Could not update the voice channel status") == 2


async def request(rig: Rig, mode: Mode) -> tuple[asyncio.Future[Outcome], Program, FakeSource]:
    started = (
        rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
        if mode is Mode.GENERATED
        else rig.station.play(CATALOG, "Member")
    )
    await rig.tick()
    program = rig.station.program
    assert program is not None
    engine = rig.generated if mode is Mode.GENERATED else rig.catalog
    source = next(s for s in engine.sources if s.program is program)
    return started, program, source


async def playing(rig: Rig, mode: Mode) -> tuple[Program, FakeSource]:
    started, program, source = await request(rig, mode)
    source.send_audio(10, sample=1000)
    await rig.tick()
    rig.listen(2)
    if mode is Mode.CATALOG:
        await rig.tick()  # accept only after its first successful PCM read
    assert started.result() is Outcome.PLAYING
    return program, source


@pytest.mark.parametrize("outgoing", list(Mode))
async def test_cross_mode_replacement_is_successive_without_fades(rig: Rig, outgoing: Mode) -> None:
    _, old = await playing(rig, outgoing)
    incoming = Mode.CATALOG if outgoing is Mode.GENERATED else Mode.GENERATED
    started, _, new = await request(rig, incoming)
    assert rig.station.heard_source is old
    old_frames = len(old.frames)
    new.send_audio(6, sample=2000)
    await rig.tick()
    assert not started.done()
    assert rig.station.heard_source is old
    assert rig.station.mixer.read() == (2000).to_bytes(2, "little", signed=True) * 1920
    assert not started.done()
    await rig.tick()
    assert started.result() is Outcome.PLAYING
    assert rig.station.heard_source is new
    assert rig.station.mixer.live is new
    assert not rig.station.mixer.switching
    assert len(old.frames) == old_frames
    assert rig.station.mixer.read() == (2000).to_bytes(2, "little", signed=True) * 1920
    assert old.retired


async def test_fade_settlement_is_not_announced_twice_when_the_player_finishes_during_a_tick(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, _, source = await request(rig, Mode.GENERATED)
    source.send_audio(10)
    await rig.tick()
    assert started.result() is Outcome.PLAYING
    assert rig.station.mixer.switching
    original = Mixer.live.fget
    assert original is not None

    def finish_fade_on_live_read(mixer: Mixer) -> object:
        live = original(mixer)
        if mixer.switching:
            rig.listen(FADE_IN_S)
        return live

    monkeypatch.setattr(Mixer, "live", property(finish_fade_on_live_read))
    await rig.tick()
    assert rig.generated.live_numbers == [source.number]


@pytest.mark.parametrize("outgoing", list(Mode))
async def test_failed_cross_mode_replacement_restores_heard_program(
    rig: Rig, outgoing: Mode
) -> None:
    heard, old = await playing(rig, outgoing)
    original_deadline = heard.ends_at
    incoming = Mode.CATALOG if outgoing is Mode.GENERATED else Mode.GENERATED
    started, _, failed = await request(rig, incoming)
    failed.finish(Outcome.UNAVAILABLE)
    await rig.tick()

    assert started.result() is Outcome.UNAVAILABLE
    assert rig.station.program is heard
    assert heard.ends_at == original_deadline
    assert rig.station.heard_source is old
    assert rig.station.mixer.live is old
    if outgoing is Mode.GENERATED:
        other = object()
        assert rig.generated.pool is not None
        assert rig.generated.pool.reserve(other)
        assert not rig.generated.pool.reserve(object())
        rig.generated.pool.release(other)


async def test_generated_busy_preserves_catalog_intent_audio_and_feedback(clock: FakeClock) -> None:
    rig = make_rig(clock, SessionPool(1))
    heard, source = await playing(rig, Mode.CATALOG)
    before = list(rig.statuses)
    result = rig.station.play(LOFI, "Other", duration_seconds=HOUR_S)
    await rig.tick()
    assert result.result() is Outcome.BUSY
    assert rig.station.program is heard
    assert rig.station.heard_source is source
    assert rig.statuses == before
    assert rig.generated.sources == []


async def test_releases_lyria_slots_after_departure_and_actual_cleanup(clock: FakeClock) -> None:
    pool = SessionPool(2)
    rig = make_rig(clock, pool)
    _, old = await playing(rig, Mode.GENERATED)
    old.hold_cleanup = True
    started, _, new = await request(rig, Mode.CATALOG)
    new.send_audio(6)
    await rig.tick()
    rig.listen(0.02)
    await rig.tick()
    assert started.result() is Outcome.PLAYING
    assert old.retired
    assert old.state is not State.ENDED
    contender = object()
    assert not pool.reserve(contender)
    old.release_cleanup()
    await rig.tick()
    assert pool.reserve(contender)
    pool.release(contender)


async def test_catalog_low_buffer_and_producer_exit_do_not_end_a_track(rig: Rig) -> None:
    program, old = await playing(rig, Mode.CATALOG)
    ready = next(s for s in rig.catalog.sources if s is not old)
    ready.send_audio(6, sample=2000)
    rig.listen(7)
    await rig.tick()
    assert rig.station.mixer.live is old
    old.release_cleanup()  # exit without strict completion does not prove success
    rig.listen(1)
    await rig.tick()
    assert rig.station.mixer.live is old
    assert rig.catalog.live_numbers == [old.number]
    assert program.playing


async def test_catalog_completion_waits_for_audible_drain_then_switches(rig: Rig) -> None:
    _, old = await playing(rig, Mode.CATALOG)
    ready = next(s for s in rig.catalog.sources if s is not old)
    ready.send_audio(6, sample=2000)
    ready.finish()
    old.finish()
    await rig.tick()
    assert rig.station.mixer.live is old
    rig.listen(8)
    assert rig.station.mixer.live is old  # final outgoing pop is not settlement
    assert rig.catalog.live_numbers == [old.number]
    # No station tick occurs between the final old frame and this incoming frame.
    assert rig.station.mixer.read() == (2000).to_bytes(2, "little", signed=True) * 1920
    await rig.tick()
    assert rig.station.mixer.live is ready
    assert not rig.station.mixer.switching
    assert rig.station.mixer.read() == (2000).to_bytes(2, "little", signed=True) * 1920
    assert rig.catalog.live_numbers == [old.number, ready.number]


async def test_late_catalog_failure_recovers_without_claiming_completion(rig: Rig) -> None:
    heard, failed = await playing(rig, Mode.CATALOG)
    ready = next(s for s in rig.catalog.sources if s is not failed)
    ready.send_audio(6, sample=2000)
    failed.finish(Outcome.UNAVAILABLE)
    await rig.tick()
    assert not failed.completed
    assert rig.station.mixer.live is failed
    rig.listen(8)
    await rig.tick()
    rig.listen(0.02)
    await rig.tick()
    assert rig.station.program is heard
    assert rig.station.mixer.live is ready
    assert failed.failure is Outcome.UNAVAILABLE
    assert not failed.completed
    assert not rig.station.mixer.switching
    assert rig.catalog.live_numbers == [failed.number, ready.number]


async def test_short_verified_catalog_source_starts_without_padding_or_fade(rig: Rig) -> None:
    started, _, source = await request(rig, Mode.CATALOG)
    source.send_audio(1, sample=2000)
    await rig.tick()
    assert not started.done()
    source.finish()
    await rig.tick()
    assert not started.done()
    assert source.buffered_seconds == 1
    assert rig.station.mixer.read() == (2000).to_bytes(2, "little", signed=True) * 1920
    await rig.tick()
    assert started.result() is Outcome.PLAYING


async def test_catalog_has_no_deadline_and_stop_ceases_without_a_fade(rig: Rig) -> None:
    program, _ = await playing(rig, Mode.CATALOG)
    await rig.tick(HOUR_S * 10)
    assert rig.station.program is program
    assert rig.station.time_left is None
    rig.station.stop()
    await rig.tick()
    assert rig.station.mixer.live is None
    assert not rig.station.mixer.switching
    assert not rig.player.running
    assert rig.station.mixer.read() == SILENCE


@pytest.mark.parametrize("first", list(Mode))
async def test_rapid_replacements_restore_a_and_ignore_stale_completion(
    rig: Rig, first: Mode
) -> None:
    heard, old = await playing(rig, first)
    other = Mode.CATALOG if first is Mode.GENERATED else Mode.GENERATED
    b, _, obsolete = await request(rig, other)
    c, _, failed = await request(rig, other)
    assert b.result() is Outcome.REPLACED
    obsolete.send_audio(6)
    obsolete.finish()
    failed.finish(Outcome.FAILED)
    await rig.tick()
    assert c.result() is Outcome.FAILED
    assert rig.station.program is heard
    assert rig.station.mixer.live is old
    assert rig.station.heard_source is old
    engine = rig.catalog if other is Mode.CATALOG else rig.generated
    assert obsolete.number not in engine.live_numbers


@pytest.mark.parametrize("end", ["stop", "disconnect", "close"])
async def test_stale_ready_catalog_work_cannot_restart_after_end(rig: Rig, end: str) -> None:
    started, _, source = await request(rig, Mode.CATALOG)
    if end == "stop":
        rig.station.stop()
    elif end == "disconnect":
        rig.disconnect()
    else:
        await rig.station.close()
    await rig.tick()
    source.send_audio(6)
    source.finish()
    await rig.tick()
    expected = Outcome.DISCONNECTED if end == "disconnect" else Outcome.STOPPED
    assert started.result() is expected
    assert rig.station.program is None
    assert rig.station.mixer.live is None
    assert rig.catalog.live_numbers == []


async def test_catalog_failure_cannot_restore_an_expired_generated_deadline(rig: Rig) -> None:
    heard, _ = await playing(rig, Mode.GENERATED)
    started, _, failed = await request(rig, Mode.CATALOG)
    assert heard.ends_at is not None
    rig.clock.now = heard.ends_at
    failed.finish(Outcome.FAILED)
    await rig.tick()
    assert started.result() is Outcome.FAILED
    assert rig.station.program is None


async def test_disconnected_readiness_preserves_heard_source_and_history(rig: Rig) -> None:
    _, old = await playing(rig, Mode.CATALOG)
    started, _, new = await request(rig, Mode.CATALOG)
    rig.player.connected = False
    new.send_audio(6)
    await rig.tick()
    assert not started.done()
    assert rig.station.heard_source is old
    assert rig.catalog.live_numbers == [old.number]
    rig.player.connected = True
    await rig.tick()
    assert not started.done()
    rig.listen(0.02)
    await rig.tick()
    assert started.result() is Outcome.PLAYING
    assert rig.catalog.live_numbers == [old.number, new.number]


async def test_close_keeps_capacity_through_repeated_caller_cancellation(rig: Rig) -> None:
    await playing(rig, Mode.GENERATED)
    _, _, source = await request(rig, Mode.CATALOG)
    source.hold_cleanup = True
    closing = asyncio.create_task(rig.station.close())
    await settle()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    again = asyncio.create_task(rig.station.close())
    await settle()
    again.cancel()
    with pytest.raises(asyncio.CancelledError):
        await again
    assert rig.catalog.close_calls == 1
    assert rig.station.audio.reserved.pcm > 0
    assert rig.station.audio.reserve(AudioBytes(pcm=34_560_000)) is None
    replacement = Station(
        lambda: rig.player,
        lambda: 1,
        engines=[FakeEngine(Mode.CATALOG, rig.station.audio)],
        audio=rig.station.audio,
        volume=1,
        statuses=rig.station.statuses,
    )
    assert replacement.audio is rig.station.audio
    assert rig.station.play(CATALOG, "late").result() is Outcome.STOPPED
    source.release_cleanup()
    await rig.station.close()
    assert rig.station.audio.reserved == AudioBytes()
    await replacement.close()


async def test_identical_plan_object_still_creates_a_distinct_request(rig: Rig) -> None:
    old_program, old = await playing(rig, Mode.CATALOG)
    started, new_program, new = await request(rig, Mode.CATALOG)
    assert new_program is not old_program
    assert new_program.plan is old_program.plan
    assert not started.done()
    assert rig.station.mixer.live is old
    new.send_audio(6)
    await rig.tick()
    assert rig.station.mixer.live is old
    assert not started.done()
    rig.listen(0.02)
    await rig.tick()
    assert rig.station.mixer.live is new


async def test_failed_catalog_source_and_program_release_without_cycle_collection(rig: Rig) -> None:
    started, program, source = await request(rig, Mode.CATALOG)
    program_ref, source_ref = weakref.ref(program), weakref.ref(source)
    source.finish(Outcome.FAILED)
    del program, source
    await rig.tick()
    assert started.result() is Outcome.FAILED
    assert source_ref() is None
    await rig.tick()  # the second producer has now finished retirement too
    assert program_ref() is None


@pytest.mark.parametrize("natural", [False, True])
async def test_delivered_then_invalidated_boundary_needs_a_new_valid_epoch_read(
    rig: Rig, natural: bool
) -> None:
    if natural:
        program, old = await playing(rig, Mode.CATALOG)
        started = program.started
        incoming = next(s for s in rig.catalog.sources if s is not old)
        incoming.send_audio(6, sample=2000)
        old.finish()
        await rig.tick()
        rig.listen(8)
    else:
        started, program, incoming = await request(rig, Mode.CATALOG)
        incoming.send_audio(6, sample=2000)
        await rig.tick()
    assert rig.station.mixer.read() == (2000).to_bytes(2, "little", signed=True) * 1920
    before = list(rig.catalog.live_numbers)
    rig.player.connected = False
    rig.station.invalidate_playback()
    await rig.tick()
    assert rig.catalog.live_numbers == before
    if not natural:
        assert not started.done()
    rig.player.connected = True
    await rig.tick()  # re-arm actual live PCM, without accepting the stale delivery
    assert rig.catalog.live_numbers == before
    assert rig.station.mixer.read() == (2000).to_bytes(2, "little", signed=True) * 1920
    await rig.tick()
    assert started.result() is Outcome.PLAYING
    assert rig.station.heard_source is incoming
    assert rig.catalog.live_numbers == [*before, incoming.number]
    assert not incoming.retired
    await rig.tick()
    assert rig.catalog.live_numbers == [*before, incoming.number]


async def test_stop_invalidates_an_armed_catalog_start_before_the_player_can_read(rig: Rig) -> None:
    started, _, incoming = await request(rig, Mode.CATALOG)
    incoming.send_audio(6)
    await rig.tick()
    assert not started.done()
    rig.station.stop()
    assert rig.station.mixer.read() == SILENCE
    await rig.tick()
    assert started.result() is Outcome.STOPPED
    assert not rig.catalog.live_numbers


@pytest.mark.parametrize("previous_generated", [False, True])
async def test_stop_detaches_actual_catalog_pcm_after_its_unaccepted_delivery_is_invalidated(
    rig: Rig, previous_generated: bool
) -> None:
    if previous_generated:
        await playing(rig, Mode.GENERATED)
    started, _, incoming = await request(rig, Mode.CATALOG)
    incoming.send_audio(6, sample=2000)
    await rig.tick()
    assert rig.station.mixer.read() == (2000).to_bytes(2, "little", signed=True) * 1920
    assert rig.station.mixer.live is incoming
    assert not started.done()
    rig.station.invalidate_playback()  # discards the receipt, not the actual live PCM owner
    rig.station.stop()
    assert rig.station.mixer.read() == SILENCE
    assert rig.station.mixer.live is None
    assert not rig.station.mixer.switching
    await rig.tick()
    assert started.result() is Outcome.STOPPED
    assert not rig.catalog.live_numbers
    assert rig.station.program is None


async def test_known_incoming_failure_between_arm_and_read_cannot_settle(rig: Rig) -> None:
    started, _, incoming = await request(rig, Mode.CATALOG)
    incoming.send_audio(6, sample=2000)
    await rig.tick()
    incoming.finish(Outcome.FAILED)
    assert incoming.frames  # a playable prefix is not a successful start after known failure
    assert rig.station.mixer.read() == SILENCE
    assert not started.done()
    await rig.tick()
    assert started.result() is Outcome.FAILED
    assert not rig.catalog.live_numbers


async def test_failed_pcm_construction_does_not_deliver_or_settle_an_armed_start(
    rig: Rig,
) -> None:
    started, _, incoming = await request(rig, Mode.CATALOG)
    incoming.send_audio(6, sample=2000)
    incoming.frames[0] = b"x"  # deliberate fixture corruption before non-unit scaling
    rig.station.mixer.set_volume(0.5)
    await rig.tick()
    assert rig.station.mixer.read() == SILENCE
    assert rig.station.mixer.errors == 1
    await rig.tick()
    assert not started.done()
    assert not rig.catalog.live_numbers
    incoming.send_audio(0.02)  # replenish the failed read before six-second admission
    await rig.tick()
    rig.station.mixer.read()
    await rig.tick()
    assert started.result() is Outcome.PLAYING
    assert rig.catalog.live_numbers == [incoming.number]


async def test_metadata_terminal_failure_restores_fb3_before_generation_and_stale_failures(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous, source = await playing(rig, Mode.GENERATED)
    snapshots: list[Program | None] = []
    original = rig.generated.ended_failures

    def ended(program: Program | None) -> list[FakeSource]:
        snapshots.append(program)
        return original(program)

    def terminal(program: Program | None, connected: bool) -> Outcome | None:
        return Outcome.NO_MATCH if program is not None else None

    monkeypatch.setattr(rig.generated, "ended_failures", ended)
    monkeypatch.setattr(rig.catalog, "terminal", terminal)
    source.finish(Outcome.FAILED)
    result = rig.station.play(CATALOG, "Other")
    await rig.tick()
    assert result.result() is Outcome.NO_MATCH
    assert snapshots == [None]
    assert rig.station.program is previous
    assert rig.station.heard_source is source
    assert not rig.catalog.sources  # metadata-only failure never fabricates a failed deck
