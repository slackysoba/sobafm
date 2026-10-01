from dataclasses import dataclass

import pytest

from sobafm.deck import Deck, State
from sobafm.pcm import FRAME_SECONDS
from sobafm.plan import MusicPlan
from sobafm.station import (
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
        for _ in range(round(seconds / FRAME_SECONDS)):
            self.station.mixer.read()

    def session(self, index: int) -> FakeSession:
        return self.lyria.sessions[index]

    async def feed(self, index: int, seconds: float) -> None:
        self.session(index).send_audio(seconds)
        await settle()

    def disconnect(self) -> None:
        self.voice.clear()


@pytest.fixture
def rig(lyria: FakeLyria, clock: FakeClock) -> Rig:
    player = FakePlayer()
    voice = [player]  # emptied to simulate a lost voice connection
    station = Station(
        lambda: voice[0] if voice else None, lyria.connect, SessionPool(4), volume=1.0, clock=clock
    )
    return Rig(station, lyria, clock, player, voice)


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


async def test_hands_over_when_the_live_deck_runs_low(rig: Rig) -> None:
    await start_playing(rig)
    first = live_deck(rig)
    await rig.feed(1, 30)
    rig.listen(4.5)

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


async def test_keeps_playing_when_lyria_closes_the_live_session(rig: Rig) -> None:
    await start_playing(rig)
    first = live_deck(rig)
    await rig.feed(1, 30)

    rig.session(0).close(1011)
    await rig.tick()
    rig.listen(4.5)
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


async def test_reports_failed_connections_after_backing_off(rig: Rig) -> None:
    rig.lyria.failure = OSError("unreachable")
    started = rig.station.play(LOFI, "Member")

    for _ in range(8):
        await rig.tick(16)

    assert started.result() is Outcome.FAILED


async def test_reports_busy_without_session_capacity(
    rig: Rig, lyria: FakeLyria, clock: FakeClock
) -> None:
    station = Station(lambda: rig.player, lyria.connect, SessionPool(0), volume=1.0, clock=clock)

    started = station.play(LOFI, "Member")
    await station.reconcile()

    assert started.result() is Outcome.BUSY


async def test_stop_fades_out_then_stops_the_player(rig: Rig) -> None:
    await start_playing(rig)

    rig.station.stop()
    await rig.tick()
    rig.listen(3)
    await rig.tick()
    await rig.tick()

    assert rig.station.mixer.live is None
    assert not rig.player.playing
    assert all(deck.state is State.ENDED for deck in rig.station.decks)


async def test_restarts_the_player_after_discord_stops_it(rig: Rig) -> None:
    await start_playing(rig)

    rig.player.playing = False
    await rig.tick()

    assert rig.player.plays == 2


async def test_ends_the_program_when_the_voice_connection_is_lost(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member")
    rig.disconnect()

    await rig.tick()

    assert started.result() is Outcome.FAILED
    assert rig.station.program is None


async def test_close_retires_everything(rig: Rig) -> None:
    await start_playing(rig)

    await rig.station.close()

    assert all(session.closed for session in rig.lyria.sessions)
    assert rig.station.decks == []
    assert not rig.player.playing
