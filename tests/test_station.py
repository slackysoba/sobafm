import asyncio
from dataclasses import dataclass

import discord
import pytest

from sobafm.deck import PAUSE_AT_S, Deck, State
from sobafm.pcm import FRAME_SECONDS
from sobafm.plan import MusicPlan
from sobafm.station import (
    CONNECT_BACKOFF_S,
    FADE_IN_S,
    FADE_OUT_S,
    HANDOVER_BELOW_S,
    PLAYER_RETRY_S,
    REFUSAL_RETRY_S,
    SESSION_LIMIT_S,
    Outcome,
    SessionPool,
    Station,
)
from tests.doubles import FakeClock, FakeLyria, FakePlayer, FakeSession, settle

LOFI = MusicPlan.from_request("rainy lo-fi")
SYNTHWAVE = MusicPlan.from_request("synthwave")


@dataclass
class Rig:
    station: Station
    lyria: FakeLyria
    clock: FakeClock
    player: FakePlayer
    voice: list[FakePlayer]

    async def tick(self, seconds: float = 0.25) -> None:
        self.clock.now += seconds
        await settle()
        await self.station.reconcile()
        await settle()

    def listen(self, seconds: float) -> None:
        """Read the mixer as discord.py's player thread does, while it runs."""
        for _ in range(round(seconds / FRAME_SECONDS)):
            if self.player.running:
                self.station.mixer.read()

    def session(self, index: int) -> FakeSession:
        return self.lyria.sessions[index]

    async def feed(self, index: int, seconds: float) -> None:
        self.session(index).send_audio(seconds)
        await settle()

    def disconnect(self) -> None:
        self.voice.clear()


def make_station(
    lyria: FakeLyria, clock: FakeClock, pool: SessionPool
) -> tuple[Station, list[FakePlayer]]:
    voice = [FakePlayer()]  # emptied to simulate a lost voice connection
    station = Station(
        lambda: voice[0] if voice else None, lyria.connect, pool, volume=1.0, clock=clock
    )
    return station, voice


@pytest.fixture
def rig(lyria: FakeLyria, clock: FakeClock) -> Rig:
    station, voice = make_station(lyria, clock, SessionPool(4))
    return Rig(station, lyria, clock, voice[0], voice)


async def start_playing(rig: Rig, plan: MusicPlan = LOFI) -> None:
    """Start a program and listen through its fade-in, leaving 8 s on the live deck."""
    started = rig.station.play(plan, "Member")
    await rig.tick()
    await rig.feed(-2, 10)
    await rig.tick()
    rig.listen(2)
    assert started.result() is Outcome.PLAYING


def live_deck(rig: Rig) -> Deck:
    live = rig.station.mixer.live
    assert isinstance(live, Deck)
    return live


async def test_opens_the_live_and_next_decks(rig: Rig) -> None:
    rig.station.play(LOFI, "Member")
    await rig.tick()

    assert len(rig.lyria.sessions) == 2
    assert all(session.calls == ["prompts", "config", "play"] for session in rig.lyria.sessions)


async def test_fades_in_once_a_deck_has_its_preroll(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member")
    await rig.tick()
    await rig.feed(0, 5)
    await rig.tick()
    assert rig.player.plays == 0

    await rig.feed(0, 1)
    await rig.tick()
    rig.listen(2)

    assert started.result() is Outcome.PLAYING
    assert rig.player.plays == 1
    assert live_deck(rig).plan is LOFI


async def test_the_next_deck_pauses_once_full(rig: Rig) -> None:
    await start_playing(rig)

    await rig.feed(1, PAUSE_AT_S)
    await rig.tick()

    assert rig.session(1).calls[-1] == "pause"


async def test_hands_over_when_the_live_deck_runs_low(rig: Rig) -> None:
    await start_playing(rig)
    first = live_deck(rig)
    await rig.feed(1, 30)
    rig.listen(8 - HANDOVER_BELOW_S + 0.5)

    await rig.tick()
    rig.listen(4)

    assert live_deck(rig) is not first
    assert live_deck(rig).plan is LOFI
    assert rig.station.mixer.underruns == 0


async def test_retires_sessions_before_lyrias_limit(rig: Rig) -> None:
    await start_playing(rig)
    await rig.feed(1, 60)

    await rig.tick(SESSION_LIMIT_S)
    assert rig.session(0).closed
    assert rig.session(1).closed

    await rig.tick()  # new sessions open once the retired ones have closed
    assert len(rig.lyria.sessions) == 4
    assert live_deck(rig).buffered_seconds > 0


async def test_a_retired_live_deck_plays_out_its_buffer(rig: Rig) -> None:
    await start_playing(rig)
    first = live_deck(rig)
    await rig.feed(0, 20)
    await rig.feed(1, 30)

    await rig.tick(SESSION_LIMIT_S)
    await rig.tick()
    assert first.state is State.ENDED
    assert live_deck(rig) is first  # 28 s left to play

    rig.listen(28 - HANDOVER_BELOW_S + 0.5)
    await rig.tick()
    rig.listen(4)

    assert live_deck(rig) is not first
    assert rig.station.mixer.underruns == 0


async def test_keeps_an_ended_deck_the_mixer_still_plays(rig: Rig) -> None:
    await start_playing(rig)
    first = live_deck(rig)

    rig.session(0).close(1011)
    await rig.tick()
    await rig.tick()

    assert first in rig.station.decks


async def test_crossfades_to_a_new_request(rig: Rig) -> None:
    await start_playing(rig)
    await rig.feed(1, 60)

    replaced = rig.station.play(SYNTHWAVE, "Another member")
    await rig.tick()
    assert rig.session(1).closed
    assert not rig.session(0).closed  # the live deck plays until the new one is ready

    await rig.tick()
    assert len(rig.lyria.sessions) == 3
    await rig.feed(-1, 6)
    await rig.tick()
    rig.listen(4)

    assert replaced.result() is Outcome.PLAYING
    assert live_deck(rig).plan is SYNTHWAVE
    assert rig.session(0).closed
    assert rig.station.mixer.underruns == 0


async def test_keeps_playing_when_lyria_closes_the_live_session(rig: Rig) -> None:
    await start_playing(rig)
    first = live_deck(rig)
    await rig.feed(1, 30)

    rig.session(0).close(1011)
    await rig.tick()
    rig.listen(8 - HANDOVER_BELOW_S + 0.5)
    await rig.tick()
    rig.listen(4)

    assert live_deck(rig) is not first
    assert rig.station.mixer.underruns == 0
    assert len(rig.lyria.sessions) == 3  # a replacement for the closed session


async def test_retries_a_refused_start_once(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member")
    await rig.tick()
    rig.session(0).refuse()
    rig.session(1).refuse()
    await rig.tick()
    await rig.tick()
    assert not started.done()
    assert len(rig.lyria.sessions) == 2

    await rig.tick(REFUSAL_RETRY_S)
    assert len(rig.lyria.sessions) == 4

    rig.session(2).refuse()
    rig.session(3).refuse()
    await rig.tick()
    await rig.tick()

    assert started.result() is Outcome.REFUSED
    assert rig.station.program is None


async def test_backs_off_between_failed_starts(rig: Rig) -> None:
    rig.lyria.failure = OSError("unreachable")
    rig.station.play(LOFI, "Member")
    await rig.tick()
    await rig.tick()

    rig.lyria.failure = None
    await rig.tick(CONNECT_BACKOFF_S[0] - 0.5)
    assert rig.lyria.sessions == []

    await rig.tick(0.5)
    assert len(rig.lyria.sessions) == 2


async def test_reports_failed_connections_after_backing_off(rig: Rig) -> None:
    rig.lyria.failure = OSError("unreachable")
    started = rig.station.play(LOFI, "Member")

    for _ in range(8):
        await rig.tick(16)

    assert started.result() is Outcome.FAILED


async def test_reserves_two_sessions_for_each_program(lyria: FakeLyria, clock: FakeClock) -> None:
    pool = SessionPool(4)
    stations = [make_station(lyria, clock, pool)[0] for _ in range(3)]
    first, second = (station.play(LOFI, "Member") for station in stations[:2])

    third = stations[2].play(LOFI, "Member")
    assert third.result() is Outcome.BUSY
    assert not first.done()
    assert not second.done()

    stations[0].stop()
    await stations[0].reconcile()  # nothing was playing, so its sessions free at once
    assert not stations[2].play(LOFI, "Member").done()


async def test_holds_its_reservation_until_its_sessions_end(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    pool = SessionPool(2)
    first, _ = make_station(lyria, clock, pool)
    second, _ = make_station(lyria, clock, pool)
    first.play(LOFI, "Member")
    await first.reconcile()

    first.stop()
    await first.reconcile()
    assert second.play(LOFI, "Member").result() is Outcome.BUSY  # sessions still open

    await first.close()
    assert not second.play(LOFI, "Member").done()


async def test_reports_busy_without_session_capacity(lyria: FakeLyria, clock: FakeClock) -> None:
    station, _ = make_station(lyria, clock, SessionPool(1))

    assert station.play(LOFI, "Member").result() is Outcome.BUSY


async def test_stop_fades_out_then_stops_the_player(rig: Rig) -> None:
    await start_playing(rig)

    rig.station.stop()
    await rig.tick()
    rig.listen(3)
    await rig.tick()
    await rig.tick()

    assert rig.station.mixer.live is None
    assert not rig.player.playing
    assert all(session.closed for session in rig.lyria.sessions)


async def test_a_stop_during_the_fade_in_waits_for_it_before_fading_out(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member")
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

    rig.station.play(SYNTHWAVE, "Member")
    await rig.tick()
    await rig.tick()
    await rig.feed(-1, 10)
    await rig.tick(PLAYER_RETRY_S)
    assert rig.player.plays == 2
    rig.player.end_stopped_threads()  # the stopped player's `after` arrives only now
    await rig.tick(PLAYER_RETRY_S)

    assert rig.player.plays == 2  # the new player is still tracked, so none is restarted


async def test_a_request_during_a_fade_out_starts_once_it_ends(rig: Rig) -> None:
    await start_playing(rig)
    rig.station.stop()
    await rig.tick()
    assert rig.station.mixer.switching

    started = rig.station.play(SYNTHWAVE, "Member")
    await rig.tick()
    await rig.feed(-1, 10)
    rig.listen(3)
    await rig.tick()
    rig.listen(2)

    assert started.result() is Outcome.PLAYING
    assert live_deck(rig).plan is SYNTHWAVE


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


async def test_ends_the_program_when_the_player_cannot_start_at_all(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.player.failure = discord.DiscordException("the encoder failed")
    started = rig.station.play(LOFI, "Member")
    await rig.tick()
    await rig.feed(-2, 10)

    await rig.tick()
    await rig.tick()

    assert started.result() is Outcome.FAILED
    assert "Could not start the voice player" in caplog.text
    assert all(session.closed for session in rig.lyria.sessions)


async def test_waits_for_voice_to_reconnect_before_starting_the_player(rig: Rig) -> None:
    rig.player.connected = False
    started = rig.station.play(LOFI, "Member")
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
    started = rig.station.play(SYNTHWAVE, "Member")
    await rig.tick()
    await rig.tick()
    await rig.feed(-1, 10)
    await rig.tick()
    assert not started.done()  # nothing can be heard yet

    rig.player.connected = True
    await rig.tick(PLAYER_RETRY_S)

    assert started.result() is Outcome.PLAYING


async def test_stops_at_once_when_the_player_ends_during_the_fade_out(rig: Rig) -> None:
    await start_playing(rig)
    rig.station.stop()
    await rig.tick()
    assert rig.station.mixer.switching

    rig.player.end_thread()  # the thread gives up on a voice outage mid-fade
    await rig.tick()
    await rig.tick()

    assert rig.station.mixer.live is None
    assert not rig.station.mixer.switching
    assert all(session.closed for session in rig.lyria.sessions)


async def test_stops_at_once_when_no_player_can_run(rig: Rig) -> None:
    await start_playing(rig)
    rig.player.connected = False
    rig.player.end_thread()  # the thread gave up on a long voice outage

    rig.station.stop()
    await rig.tick()
    await rig.tick()

    assert rig.station.mixer.live is None
    assert all(session.closed for session in rig.lyria.sessions)


async def test_ends_the_program_when_the_voice_connection_is_lost(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member")
    rig.disconnect()

    await rig.tick()

    assert started.result() is Outcome.DISCONNECTED
    assert rig.station.program is None


async def test_close_settles_a_pending_request_with_its_outcome(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member")
    await rig.tick()

    await rig.station.close(Outcome.DISCONNECTED)

    assert started.result() is Outcome.DISCONNECTED


async def test_close_retires_everything(rig: Rig) -> None:
    await start_playing(rig)

    await rig.station.close()

    assert all(session.closed for session in rig.lyria.sessions)
    assert rig.station.decks == []
    assert not rig.player.playing
