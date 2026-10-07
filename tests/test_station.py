import asyncio
import gc
import logging
import threading
import weakref
from dataclasses import dataclass
from unittest.mock import MagicMock

import discord
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from sobafm.deck import PAUSE_AT_S, PREROLL_S, Deck, State
from sobafm.pcm import FRAME_SECONDS
from sobafm.plan import MusicPlan
from sobafm.station import (
    CONNECT_BACKOFF_S,
    EMPTY_GRACE_S,
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
from sobafm.status import RETRY_MAX_S, RETRY_S, Statuses
from tests.doubles import FakeClock, FakeLyria, FakePlayer, FakeSession, settle

HOUR_S = 3600.0
CHANNEL = 10  # SobaFM's voice channel
LOFI = MusicPlan.from_request("rainy lo-fi")
SYNTHWAVE = MusicPlan.from_request("synthwave")
JAZZ = MusicPlan.from_request("jazz")


@dataclass
class Rig:
    station: Station
    lyria: FakeLyria
    clock: FakeClock
    player: FakePlayer
    voice: list[FakePlayer]
    listeners: list[int]
    statuses: list[tuple[int, str | None]]  # each voice channel status request: where, and what
    channel: list[int]  # SobaFM's voice channel, while voice is connected

    async def tick(self, seconds: float = 0.25) -> None:
        self.clock.now += seconds
        await settle()
        await self.station.reconcile()
        await settle()

    def listen(self, seconds: float) -> None:
        """Read the mixer as discord.py's player thread does: while it runs and voice is up."""
        for _ in range(round(seconds / FRAME_SECONDS)):
            if self.player.running and self.player.connected:
                self.station.mixer.read()

    def session(self, index: int) -> FakeSession:
        return self.lyria.sessions[index]

    async def feed(self, index: int, seconds: float) -> None:
        self.session(index).send_audio(seconds)
        await settle()

    def disconnect(self) -> None:
        self.voice.clear()

    def set_listeners(self, count: int) -> None:
        self.listeners[0] = count

    def move(self, channel: int) -> None:
        self.channel[0] = channel


def make_station(
    lyria: FakeLyria, clock: FakeClock, pool: SessionPool
) -> tuple[Station, list[FakePlayer], list[int], list[tuple[int, str | None]], list[int]]:
    voice = [FakePlayer()]  # emptied to simulate a lost voice connection
    listeners = [1]
    statuses: list[tuple[int, str | None]] = []
    channel = [CHANNEL]

    async def show(channel: int, status: str | None) -> bool:
        statuses.append((channel, status))
        return True

    station = Station(
        lambda: voice[0] if voice else None,
        lambda: listeners[0],
        lyria.connect,
        pool,
        volume=1.0,
        statuses=Statuses(lambda: channel[0] if voice else None, show, clock=clock),
        clock=clock,
    )
    return station, voice, listeners, statuses, channel


@pytest.fixture
def rig(lyria: FakeLyria, clock: FakeClock) -> Rig:
    station, voice, listeners, statuses, channel = make_station(lyria, clock, SessionPool(4))
    return Rig(station, lyria, clock, voice[0], voice, listeners, statuses, channel)


async def start_playing(
    rig: Rig, plan: MusicPlan = LOFI, *, duration_seconds: float = HOUR_S
) -> None:
    """Start a program and listen through its fade-in, leaving 8 s on the live deck.

    The program is requested at the clock's current time, which is 0.5 s later on return.
    """
    started = rig.station.play(plan, "Member", duration_seconds=duration_seconds)
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
    rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()

    assert len(rig.lyria.sessions) == 2
    assert all(session.calls == ["prompts", "config", "play"] for session in rig.lyria.sessions)


async def test_fades_in_once_a_deck_has_its_preroll(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
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
    rig.listen(1)
    await rig.tick()  # the drained deck, now ended, is discarded
    assert rig.station.program is not None
    assert rig.station.program.failures == 0  # a handover isn't a failed session


async def test_retires_sessions_before_lyrias_limit(rig: Rig) -> None:
    await start_playing(rig)
    await rig.feed(1, 60)

    await rig.tick(SESSION_LIMIT_S)
    assert rig.session(0).closed
    assert rig.session(1).closed

    await rig.tick()  # new sessions open once the retired ones have closed
    assert len(rig.lyria.sessions) == 4
    assert live_deck(rig).buffered_seconds > 0
    assert rig.station.program is not None
    assert rig.station.program.failures == 0  # nor is a rotation


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
    rig.listen(3)  # leaving less than the pre-roll, so it couldn't go live again

    rig.session(0).close(1011)
    await rig.tick()
    await rig.tick()

    assert first in rig.station.decks


async def test_crossfades_to_a_new_request(rig: Rig) -> None:
    await start_playing(rig)
    await rig.feed(1, 60)

    replaced = rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
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
    assert len(rig.lyria.sessions) == 2
    await rig.tick(CONNECT_BACKOFF_S[0])
    assert len(rig.lyria.sessions) == 3  # a replacement for the closed session, after the backoff


async def test_retries_a_refused_start_once(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
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
    rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()

    rig.lyria.failure = None
    await rig.tick(CONNECT_BACKOFF_S[0] - 0.5)
    assert rig.lyria.sessions == []

    await rig.tick(0.5)
    assert len(rig.lyria.sessions) == 2


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [(OSError("unreachable"), Outcome.UNAVAILABLE), (ValueError("a bug"), Outcome.FAILED)],
    ids=["outage", "other"],
)
async def test_reports_failed_connections_after_backing_off(
    rig: Rig, failure: Exception, outcome: Outcome
) -> None:
    rig.lyria.failure = failure
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)

    for _ in range(3):
        await rig.tick(16)
    assert not started.done()  # still backing off

    for _ in range(5):
        await rig.tick(16)
    assert started.result() is outcome


def closed_during_setup(code: int, reason: str) -> ConnectionClosedError:
    """How connecting fails when Lyria RealTime closes the session during setup."""
    close = Close(code, reason)
    return ConnectionClosedError(close, close, rcvd_then_sent=True)


INVALID_KEY = (1007, "API key not valid. Please pass a valid API key.")
QUOTA = (1011, "You exceeded your current quota, please check your plan and billing details.")


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (closed_during_setup(*INVALID_KEY), Outcome.REJECTED),
        (closed_during_setup(*QUOTA), Outcome.EXHAUSTED),
        (InvalidStatus(Response(403, "Forbidden", Headers())), Outcome.REJECTED),
        (InvalidStatus(Response(429, "Too Many Requests", Headers())), Outcome.EXHAUSTED),
    ],
    ids=["rejected key", "quota", "upgrade forbidden", "upgrade rate-limited"],
)
async def test_fails_at_once_when_retrying_cannot_help(
    rig: Rig, failure: Exception, outcome: Outcome
) -> None:
    rig.lyria.failure = failure
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)

    await rig.tick()
    await rig.tick()

    assert started.result() is outcome
    assert rig.station.program is None


@pytest.mark.parametrize(
    ("audio_s", "ended", "outcome"),
    [(3, False, Outcome.PLAYING), (7, True, Outcome.PLAYING), (3, True, Outcome.EXHAUSTED)],
    ids=["still open", "ready", "short of its pre-roll"],
)
async def test_a_quota_close_waits_while_another_deck_may_start(
    rig: Rig, audio_s: float, ended: bool, outcome: Outcome
) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.feed(0, audio_s)
    if ended:
        rig.session(0).close(1011)  # an outage ends the first deck

    rig.session(1).close(1011, QUOTA[1])  # the second closes for the quota
    await rig.tick()
    await rig.tick()
    if not ended:
        assert not started.done()
        await rig.feed(0, 3)  # the open deck reaches its pre-roll
        await rig.tick()

    assert started.result() is outcome


async def test_a_playing_program_survives_a_rejected_next_session(rig: Rig) -> None:
    await start_playing(rig)
    heard = rig.station.program
    rig.session(0).close(1011)  # Lyria ends the live session; its buffer plays on
    rig.listen(3)  # leaving less than the pre-roll, so no other deck could start a program

    rig.session(1).close(*INVALID_KEY)
    await rig.tick()
    await rig.tick()

    assert rig.station.program is heard  # a started program keeps retrying instead
    assert live_deck(rig).plan is LOFI


async def test_logs_why_a_start_failed_without_lyrias_text(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.lyria.failure = closed_during_setup(*QUOTA)
    rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)

    with caplog.at_level(logging.DEBUG):
        await rig.tick()
        await rig.tick()

    assert "Could not open a Lyria RealTime session (exhausted)" in caplog.text
    assert "exceeded" not in caplog.text


@pytest.mark.parametrize(
    ("close", "outcome"),
    [(INVALID_KEY, Outcome.REJECTED), (QUOTA, Outcome.EXHAUSTED)],
    ids=["rejected key", "quota"],
)
async def test_a_start_that_backs_off_reports_the_cause_that_ends_it(
    rig: Rig, caplog: pytest.LogCaptureFixture, close: tuple[int, str], outcome: Outcome
) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()

    with caplog.at_level(logging.WARNING, logger="sobafm.station"):
        for _ in range(len(CONNECT_BACKOFF_S) + 1):
            # Each round, one session closes for the cause while the other is still open...
            first, second = rig.lyria.sessions[-2:]
            first.close(*close)
            await rig.tick()
            second.close(1011)  # ...and the other closes in an outage
            await rig.tick()
            await rig.tick(16)  # after the backoff

    assert started.result() is outcome
    assert f"Could not open a Lyria RealTime session ({outcome})" in caplog.text


@pytest.mark.parametrize("first", ["outage", "quota"])
async def test_the_last_rounds_cause_doesnt_depend_on_deck_order(rig: Rig, first: str) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    for _ in range(len(CONNECT_BACKOFF_S)):  # rounds lost to an outage
        for session in rig.lyria.sessions[-2:]:
            session.close(1011)
        await rig.tick()
        await rig.tick(16)

    earlier, later = rig.lyria.sessions[-2:]
    outage, quota = (earlier, later) if first == "outage" else (later, earlier)
    outage.close(1011)  # in the last round, both close in one tick
    quota.close(*QUOTA)
    await rig.tick()

    assert started.result() is Outcome.EXHAUSTED


@pytest.mark.parametrize("first", ["outage", "refusal"])
async def test_the_last_rounds_second_refusal_doesnt_depend_on_deck_order(
    rig: Rig, first: str
) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    for session in rig.lyria.sessions[-2:]:
        session.refuse()  # a refused round first
    await rig.tick()
    await rig.tick(REFUSAL_RETRY_S)
    for _ in range(len(CONNECT_BACKOFF_S)):  # then rounds lost to an outage
        for session in rig.lyria.sessions[-2:]:
            session.close(1011)
        await rig.tick()
        await rig.tick(16)

    earlier, later = rig.lyria.sessions[-2:]
    outage, refused = (earlier, later) if first == "outage" else (later, earlier)
    outage.close(1011)  # in the last round, one closes as the other is refused, in one tick
    refused.refuse()
    await rig.tick()

    assert started.result() is Outcome.REFUSED


@pytest.mark.parametrize("first", ["outage", "unrecognized"])
async def test_the_last_rounds_recognized_cause_comes_first(rig: Rig, first: str) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    for _ in range(len(CONNECT_BACKOFF_S)):
        for session in rig.lyria.sessions[-2:]:
            session.close(1011)
        await rig.tick()
        await rig.tick(16)

    earlier, later = rig.lyria.sessions[-2:]
    outage, other = (earlier, later) if first == "outage" else (later, earlier)
    outage.close(1011)
    other.close(1008, "Request contains an invalid argument.")  # a cause SobaFM doesn't name
    await rig.tick()

    assert started.result() is Outcome.UNAVAILABLE


async def test_a_failed_replacement_leaves_the_program_heard_its_own_count(rig: Rig) -> None:
    await start_playing(rig)
    heard = rig.station.program
    assert heard is not None
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()  # the replacement's session opens
    rig.session(0).close(1011)  # the heard program's live session ends early
    rig.session(-1).close(*INVALID_KEY)  # as the replacement's is rejected, in one tick
    await rig.tick()

    assert rig.station.program is heard
    assert heard.failures == 0  # seen while the replacement was current, so it never counts


async def test_a_rounds_refusals_count_once_however_far_apart(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    rig.session(0).refuse()
    await rig.tick()
    await rig.tick(10)  # each session's setup can take seconds
    rig.session(1).refuse()
    await rig.tick()

    assert not started.done()  # one refusal, so the start is retried once
    await rig.tick(REFUSAL_RETRY_S)
    assert len(rig.lyria.sessions) == 4


async def test_a_refusal_counts_after_a_failure_in_its_round(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    rig.session(0).send_audio(2)  # one session ends short, which counts first
    rig.session(0).close(1011)
    await rig.tick()
    rig.session(1).refuse()  # and the other is refused within the failure's window
    await rig.tick()

    await rig.tick(CONNECT_BACKOFF_S[0])
    assert len(rig.lyria.sessions) == 2  # the refusal's window holds the next round
    await rig.tick(REFUSAL_RETRY_S)
    assert len(rig.lyria.sessions) == 4

    rig.session(2).refuse()
    rig.session(3).refuse()
    await rig.tick()
    await rig.tick()

    assert started.result() is Outcome.REFUSED


async def test_a_replacement_failing_after_the_program_heard_ends_returns_to_nothing(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig, duration_seconds=300)
    replaced = rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()  # the replacement's session opens
    rig.clock.now += 300  # and the program heard's end time passes while it starts
    sessions = len(rig.lyria.sessions)

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.session(-1).close(*INVALID_KEY)
        await rig.tick()

    assert replaced.result() is Outcome.REJECTED
    assert rig.station.program is None
    assert "Program ended (stopped)" in caplog.text
    await rig.tick()
    assert len(rig.lyria.sessions) == sessions  # no session for a program that has ended


async def test_a_replacement_that_fails_in_an_outage_returns_to_the_program_heard(
    rig: Rig,
) -> None:
    await start_playing(rig)
    heard = rig.station.program
    rig.lyria.failure = OSError("unreachable")
    replaced = rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)

    for _ in range(10):  # the first round waits for the next deck's session to close
        await rig.tick(16)

    assert replaced.result() is Outcome.UNAVAILABLE
    assert rig.station.program is heard


@pytest.mark.parametrize("cause", ["refused", "rejected key"])
async def test_a_failed_replacement_returns_to_the_program_heard(rig: Rig, cause: str) -> None:
    await start_playing(rig, duration_seconds=300)
    heard = rig.station.program
    replaced = rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()  # retires the next deck, whose session the replacement takes
    if cause == "refused":
        await rig.tick()
        rig.session(2).refuse()
        await rig.tick()
        await rig.tick(REFUSAL_RETRY_S)
        rig.session(3).refuse()
    else:
        rig.lyria.failure = closed_during_setup(*INVALID_KEY)  # for the program heard too
        await rig.tick()
    await rig.tick()
    await rig.tick()  # the program heard opens a next session, or fails to and plays on

    assert replaced.result() is (Outcome.REFUSED if cause == "refused" else Outcome.REJECTED)
    assert rig.station.program is heard  # with its own end time
    assert live_deck(rig).plan is LOFI
    assert not rig.station.mixer.switching  # still playing, not fading out
    rig.listen(2)
    assert rig.station.mixer.underruns == 0


async def test_a_failed_request_returns_past_requests_that_never_played(rig: Rig) -> None:
    await start_playing(rig)
    heard = rig.station.program
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    rig.lyria.failure = closed_during_setup(*INVALID_KEY)
    latest = rig.station.play(JAZZ, "A third member", duration_seconds=HOUR_S)

    for _ in range(3):
        await rig.tick()

    assert latest.result() is Outcome.REJECTED
    assert rig.station.program is heard


async def test_a_failed_request_after_a_stop_stays_stopped(rig: Rig) -> None:
    await start_playing(rig)
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    rig.station.stop()
    rig.lyria.failure = closed_during_setup(*INVALID_KEY)
    latest = rig.station.play(JAZZ, "A third member", duration_seconds=HOUR_S)

    for _ in range(3):
        await rig.tick()

    assert latest.result() is Outcome.REJECTED
    assert rig.station.program is None
    assert rig.station.mixer.switching  # fading out


def cut_short(rig: Rig, seconds: float = 2.0) -> None:
    """Have each open session send `seconds` of audio, then close, as Lyria might in an outage."""
    for session in rig.lyria.sessions:
        if not session.closed and not session.ending:
            session.send_audio(seconds)
            session.close(1011)


@pytest.mark.parametrize("seconds", [2.0, PREROLL_S - FRAME_SECONDS], ids=["2 s", "a frame short"])
async def test_backs_off_from_sessions_that_end_short_of_the_preroll(
    rig: Rig, seconds: float
) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)

    for _ in range(40):  # ten seconds of ticks
        await rig.tick()
        cut_short(rig, seconds)
    assert len(rig.lyria.sessions) <= 6  # rounds after the backoff, not at every tick
    assert len(rig.station.decks) <= 2  # short decks can't go live, so none are kept

    for _ in range(8):
        await rig.tick(16)
        cut_short(rig, seconds)
    assert started.result() is Outcome.UNAVAILABLE


async def test_a_playing_program_survives_repeated_short_sessions(rig: Rig) -> None:
    await start_playing(rig)
    heard = rig.station.program

    for _ in range(12):  # more short sessions than the backoff has steps
        for session in rig.lyria.sessions[1:]:
            if not session.closed and not session.ending:
                session.send_audio(2)
                session.close(1011)
        await rig.tick(16)

    assert rig.station.program is heard  # a started program keeps retrying
    assert heard is not None
    assert heard.failures > 3


async def test_a_playing_program_survives_refused_next_sessions(rig: Rig) -> None:
    await start_playing(rig)
    heard = rig.station.program

    for _ in range(3):  # each round's next session is refused
        rig.lyria.sessions[-1].refuse()
        await rig.tick()
        await rig.tick(REFUSAL_RETRY_S)

    assert rig.station.program is heard


async def test_a_rejected_key_fails_a_start_when_its_decks_fail_in_turn(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    rig.session(0).close(*INVALID_KEY)  # while the other deck is still open
    await rig.tick()
    await rig.tick()
    assert not started.done()

    rig.session(1).close(*INVALID_KEY)  # inside the retry window the first one set
    await rig.tick()
    await rig.tick()

    assert started.result() is Outcome.REJECTED


async def test_a_playing_program_backs_off_from_a_short_session(rig: Rig) -> None:
    await start_playing(rig)
    rig.session(1).send_audio(2)  # the next deck's session ends short of the pre-roll
    rig.session(1).close(1011)

    await rig.tick()
    await rig.tick()
    assert len(rig.lyria.sessions) == 2  # no new session at once

    await rig.tick(CONNECT_BACKOFF_S[0])
    assert len(rig.lyria.sessions) == 3
    assert rig.station.program is not None


async def test_keeps_an_ended_deck_that_holds_its_preroll(rig: Rig) -> None:
    await start_playing(rig)
    await rig.feed(1, 10)
    next_deck = rig.station.decks[1]
    rig.session(1).close(1011)

    await rig.tick()
    await rig.tick()

    assert next_deck in rig.station.decks  # ready to go live, though its session ended
    assert len(rig.lyria.sessions) == 2  # its replacement opens after the backoff
    await rig.tick(CONNECT_BACKOFF_S[0])
    assert len(rig.lyria.sessions) == 3


async def test_keeps_one_ended_deck_ready_for_the_current_plan(rig: Rig) -> None:
    await start_playing(rig)
    await rig.feed(1, 20)
    rig.session(1).close(1011)
    await rig.tick()
    await rig.tick(CONNECT_BACKOFF_S[0])
    await rig.feed(2, 10)
    rig.session(2).close(1011)  # a second ended deck, newer but with less audio

    await rig.tick()
    await rig.tick()

    ended = [deck for deck in rig.station.decks if deck is not live_deck(rig)]
    assert [deck.buffered_seconds for deck in ended if deck.state is State.ENDED] == [20]


async def test_keeps_no_ended_deck_while_an_open_one_is_ready(rig: Rig) -> None:
    await start_playing(rig)
    await rig.feed(1, 20)
    ended = rig.station.decks[1]
    rig.session(1).close(1011)
    await rig.tick()
    await rig.tick(CONNECT_BACKOFF_S[0])  # its replacement opens
    assert ended in rig.station.decks

    await rig.feed(-1, PREROLL_S)  # and is ready to go live
    await rig.tick()

    assert ended not in rig.station.decks


async def test_drops_an_ended_deck_of_a_replaced_plan(rig: Rig) -> None:
    await start_playing(rig)
    await rig.feed(1, 10)  # the next deck is ready
    replaced = rig.station.decks[1]

    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()  # which retires it
    await rig.tick()

    assert replaced.state is State.ENDED
    assert replaced not in rig.station.decks


@pytest.mark.parametrize("end", ["closed", "failed", "unreadable"])
async def test_frees_a_dropped_deck_without_collecting_cycles(rig: Rig, end: str) -> None:
    await start_playing(rig)
    rig.session(1).send_audio(2)  # the next deck's session ends short of the pre-roll, on an error
    if end == "closed":
        rig.session(1).close(1011)
    elif end == "failed":
        rig.session(1).fail(RuntimeError("a frame the SDK couldn't read"))
    else:
        rig.session(1).fail(ValueError("a frame the SDK couldn't parse"))
    dropped = weakref.ref(rig.station.decks[1])
    gc.disable()  # so only reference counting can free it
    # pytest keeps each log record, whose traceback would hold the deck; SobaFM's handler doesn't.
    logging.disable(logging.CRITICAL)
    try:
        await rig.tick()
        await rig.tick()

        assert dropped() is None
    finally:
        logging.disable(logging.NOTSET)
        gc.enable()


async def test_frees_the_decks_it_drops(rig: Rig) -> None:
    await start_playing(rig)
    rig.session(1).send_audio(2)  # the next deck's session ends short of the pre-roll
    rig.session(1).close(1011)
    dropped = weakref.ref(rig.station.decks[1])

    await rig.tick()
    await rig.tick()
    gc.collect()

    assert dropped() is None


async def test_a_replaced_plans_sessions_leave_the_backoff_alone(rig: Rig) -> None:
    await start_playing(rig)
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()  # the replacement's deck opens
    rig.session(-1).send_audio(2)  # and ends short of the pre-roll
    rig.session(-1).close(1011)
    await rig.tick()
    await rig.tick(CONNECT_BACKOFF_S[0])  # its retry opens
    await rig.feed(-1, 10)
    await rig.tick()  # the replacement plays, retiring the replaced program's live deck
    await rig.tick()

    program = rig.station.program
    assert program is not None
    assert program.plan is SYNTHWAVE
    assert program.playing
    assert program.failures == 1


async def test_a_session_retired_at_a_handover_resets_the_backoff(rig: Rig) -> None:
    await start_playing(rig)
    heard = rig.station.program
    assert heard is not None
    heard.failures = len(CONNECT_BACKOFF_S)  # as after a start that needed every retry
    first = live_deck(rig)
    await rig.feed(1, 30)
    rig.listen(8 - HANDOVER_BELOW_S + 0.5)  # the live deck runs low while its session is open

    await rig.tick()  # so it hands over, retiring it
    rig.listen(4)
    await rig.tick()

    assert live_deck(rig) is not first
    assert heard.failures == 0


async def test_a_failure_seen_with_a_session_that_lasted_counts_from_the_first_step(
    rig: Rig,
) -> None:
    await start_playing(rig)
    heard = rig.station.program
    assert heard is not None
    heard.failures = 2
    await rig.feed(1, 10)
    rig.session(0).close(1011)  # the live session ends early, earlier in the deck list
    rig.station.decks[1].retire()  # and the next one is retired with its pre-roll, as at a handover
    sessions = len(rig.lyria.sessions)

    await rig.tick()  # which sees both ends at once

    assert heard.failures == 1
    await rig.tick(CONNECT_BACKOFF_S[0])
    assert len(rig.lyria.sessions) > sessions


async def test_a_session_that_lasts_until_retired_resets_the_backoff(rig: Rig) -> None:
    await start_playing(rig)
    heard = rig.station.program
    assert heard is not None
    heard.failures = len(CONNECT_BACKOFF_S)  # as after a start that needed every retry
    await rig.feed(1, 10)

    await rig.tick(SESSION_LIMIT_S)  # its sessions last until SobaFM retires them
    await rig.tick()

    assert heard.failures == 0


async def test_backs_off_from_sessions_retired_before_their_preroll(rig: Rig) -> None:
    rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()  # its sessions open, and never send audio
    await rig.tick(SESSION_LIMIT_S)  # so they are retired at the session limit

    await rig.tick()
    await rig.tick()
    assert len(rig.lyria.sessions) == 2  # no new sessions at once

    await rig.tick(CONNECT_BACKOFF_S[0])
    assert len(rig.lyria.sessions) == 4


def end_after_preroll(rig: Rig) -> None:
    """Have each open session send its pre-roll and a little more, then close."""
    cut_short(rig, PREROLL_S + 1)


async def test_a_playing_program_backs_off_from_sessions_that_end_after_the_preroll(
    rig: Rig,
) -> None:
    await start_playing(rig)

    for _ in range(40):  # ten seconds of ticks
        end_after_preroll(rig)
        await rig.tick()
        rig.listen(0.25)

    assert len(rig.lyria.sessions) <= 6  # rounds after the backoff, not at every tick
    assert len(rig.station.decks) <= 4  # the live deck, a spare, and two filling
    assert rig.station.program is not None
    assert rig.station.program.playing


async def test_a_start_that_cannot_be_heard_backs_off_from_sessions_that_end_after_the_preroll(
    rig: Rig,
) -> None:
    rig.player.connected = False
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)

    for _ in range(40):  # ten seconds of ticks
        await rig.tick()
        end_after_preroll(rig)
    assert len(rig.lyria.sessions) <= 6
    assert len(rig.station.decks) <= 3  # a spare and two filling

    for _ in range(8):
        await rig.tick(16)
        end_after_preroll(rig)
    assert started.result() is Outcome.UNAVAILABLE
    assert len(rig.station.decks) <= 3


async def test_reserves_two_sessions_for_each_program(lyria: FakeLyria, clock: FakeClock) -> None:
    pool = SessionPool(4)
    stations = [make_station(lyria, clock, pool)[0] for _ in range(3)]
    first, second = (
        station.play(LOFI, "Member", duration_seconds=HOUR_S) for station in stations[:2]
    )

    third = stations[2].play(LOFI, "Member", duration_seconds=HOUR_S)
    assert third.result() is Outcome.BUSY
    assert not first.done()
    assert not second.done()

    stations[0].stop()
    await stations[0].reconcile()  # nothing was playing, so its sessions free at once
    assert not stations[2].play(LOFI, "Member", duration_seconds=HOUR_S).done()


async def test_holds_its_reservation_until_its_sessions_end(
    lyria: FakeLyria, clock: FakeClock
) -> None:
    pool = SessionPool(2)
    first, *_ = make_station(lyria, clock, pool)
    second, *_ = make_station(lyria, clock, pool)
    first.play(LOFI, "Member", duration_seconds=HOUR_S)
    await first.reconcile()

    first.stop()
    await first.reconcile()
    # its sessions are still open
    assert second.play(LOFI, "Member", duration_seconds=HOUR_S).result() is Outcome.BUSY

    await first.close()
    assert not second.play(LOFI, "Member", duration_seconds=HOUR_S).done()


async def test_reports_busy_without_session_capacity(lyria: FakeLyria, clock: FakeClock) -> None:
    station, *_ = make_station(lyria, clock, SessionPool(1))

    assert station.play(LOFI, "Member", duration_seconds=HOUR_S).result() is Outcome.BUSY


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


async def test_a_request_during_a_fade_out_starts_once_it_ends(rig: Rig) -> None:
    await start_playing(rig)
    rig.station.stop()
    await rig.tick()
    assert rig.station.mixer.switching

    started = rig.station.play(SYNTHWAVE, "Member", duration_seconds=HOUR_S)
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
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.feed(-2, 10)

    await rig.tick()
    await rig.tick()

    assert started.result() is Outcome.FAILED
    assert "Could not start the voice player" in caplog.text
    assert all(session.closed for session in rig.lyria.sessions)


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


async def test_close_retires_everything(rig: Rig) -> None:
    await start_playing(rig)

    await rig.station.close()

    assert all(session.closed for session in rig.lyria.sessions)
    assert rig.station.decks == []
    assert not rig.player.playing


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


async def test_ends_the_program_after_the_channel_stays_empty(rig: Rig) -> None:
    await start_playing(rig)

    rig.set_listeners(0)
    await rig.tick()
    await rig.tick(EMPTY_GRACE_S - 1)
    assert rig.station.program is not None

    await rig.tick(1)
    assert rig.station.program is None
    assert rig.station.mixer.switching  # it fades out rather than cutting
    assert rig.player.playing

    rig.listen(FADE_OUT_S)
    await rig.tick()
    await rig.tick()
    assert rig.station.mixer.live is None
    assert not rig.player.playing
    assert all(session.closed for session in rig.lyria.sessions)


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


DRY = "The live deck ran dry; playing silence"


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


async def test_a_program_heard_again_after_a_failed_replacement_keeps_its_count(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig)
    heard = rig.station.program
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()  # the replacement's deck opens

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        rig.listen(9)  # the program heard runs dry while the replacement starts
        await rig.tick()
        rig.session(2).close(*INVALID_KEY)
        await rig.tick()
        await rig.tick()
        assert rig.station.program is heard
        assert silence_lines(caplog) == [DRY]  # it plays on, so nothing has ended
        rig.station.stop()

    assert silence_lines(caplog)[-2:] == [
        "Silence lasted 1.00 s",
        "Program ended (stopped) with 1.00 s of underrun",
    ]


async def test_a_program_heard_again_rotates_without_ending(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    await start_playing(rig)
    heard = rig.station.program
    rig.station.play(SYNTHWAVE, "Another member", duration_seconds=HOUR_S)
    await rig.tick()
    await rig.tick()  # the replacement's deck opens
    rig.session(2).close(*INVALID_KEY)
    await rig.tick()
    await rig.tick()  # the program heard is current again, and opens its next deck
    assert rig.station.program is heard

    with caplog.at_level(logging.INFO, logger="sobafm.station"):
        await rig.feed(-1, 10)
        rig.listen(8 - HANDOVER_BELOW_S + 0.5)  # its live deck runs low
        await rig.tick()  # so it hands over to its next deck
        rig.listen(4)
        await rig.tick()
        rig.station.stop()

    assert silence_lines(caplog) == ["Program ended (stopped) with 0.00 s of underrun"]


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
    return Station(
        lambda: rig.voice[0] if rig.voice else None,
        lambda: rig.listeners[0],
        rig.lyria.connect,
        SessionPool(4),
        volume=1.0,
        statuses=rig.station.statuses,
        clock=rig.clock,
    )


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


async def lose_rounds(rig: Rig, rounds: int) -> None:
    """End `rounds` rounds of a start's two sessions in an outage, each after the backoff."""
    for _ in range(rounds):
        for session in rig.lyria.sessions[-2:]:
            session.close(1011)
        await rig.tick()
        await rig.tick(16)


@pytest.mark.parametrize("ended", [False, True], ids=["still open", "ended but ready"])
async def test_the_last_rounds_outage_waits_while_another_deck_may_start(
    rig: Rig, ended: bool
) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await lose_rounds(rig, len(CONNECT_BACKOFF_S))
    await rig.feed(-2, 7)  # one deck of the last round holds more than the pre-roll
    if ended:
        rig.session(-2).close(1011)  # its session ends in an outage, with its audio kept
        await rig.tick()

    rig.session(-1).close(1011)  # as the other's session ends in an outage
    await rig.tick()
    await rig.tick()

    assert started.result() is Outcome.PLAYING


async def test_the_last_rounds_outage_gives_up_when_the_other_deck_ends_too(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await lose_rounds(rig, len(CONNECT_BACKOFF_S))

    rig.session(-1).close(1011)  # the last round's first session ends
    await rig.tick()
    assert not started.done()  # while the other is still open
    rig.session(-2).close(1011)
    await rig.tick()

    assert started.result() is Outcome.UNAVAILABLE


async def test_the_last_rounds_second_refusal_waits_while_another_deck_may_start(
    rig: Rig,
) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    for session in rig.lyria.sessions[-2:]:
        session.refuse()  # a refused round first
    await rig.tick()
    await rig.tick(REFUSAL_RETRY_S)
    await rig.feed(-2, 7)  # one deck of the next round holds more than the pre-roll

    rig.session(-1).refuse()  # as the other is refused a second time
    await rig.tick()
    await rig.tick()

    assert started.result() is Outcome.PLAYING


async def test_the_last_rounds_second_refusal_gives_up_when_the_other_deck_ends_too(
    rig: Rig,
) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    for session in rig.lyria.sessions[-2:]:
        session.refuse()
    await rig.tick()
    await rig.tick(REFUSAL_RETRY_S)

    rig.session(-1).refuse()  # the second refusal, while the other session is still open
    await rig.tick()
    assert not started.done()
    rig.session(-2).refuse()
    await rig.tick()

    assert started.result() is Outcome.REFUSED


async def test_the_last_rounds_outage_then_a_later_quota_close_reports_the_quota(
    rig: Rig,
) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await lose_rounds(rig, len(CONNECT_BACKOFF_S))

    rig.session(-1).close(1011)
    await rig.tick()
    assert not started.done()
    rig.session(-2).close(*QUOTA)  # a tick later, as in round 1
    await rig.tick()

    assert started.result() is Outcome.EXHAUSTED


async def sessions_opened_during_an_outage(rig: Rig, *, seconds: int) -> int:
    """Close every open session at each tick of 0.25 s, and count the sessions opened."""
    for _ in range(seconds * 4):
        for session in rig.lyria.sessions:
            if not session.closed:
                session.close(1011)
        await rig.tick(0.25)
    return len(rig.lyria.sessions)


async def test_a_started_programs_retries_stay_backed_off_in_a_long_outage(rig: Rig) -> None:
    await start_playing(rig)
    rig.listen(HOUR_S)  # its buffer outlasts the outage

    opened = await sessions_opened_during_an_outage(rig, seconds=60)

    assert opened <= 2 + 2 * 6  # a round of two every 15 seconds, after the first backoffs


async def test_a_waiting_starts_retries_stay_backed_off(rig: Rig) -> None:
    started = rig.station.play(LOFI, "Member", duration_seconds=HOUR_S)
    await rig.tick()
    await lose_rounds(rig, len(CONNECT_BACKOFF_S))
    await rig.feed(-2, 3)  # one deck of the last round is open and filling
    before = len(rig.lyria.sessions)

    open_deck = rig.session(-2)
    for _ in range(240):  # a minute, with a deck that is still open
        for session in rig.lyria.sessions:
            if session is not open_deck and not session.closed:
                session.close(1011)
        await rig.tick(0.25)

    assert not started.done()
    assert len(rig.lyria.sessions) - before <= 2 * 5  # a round every 15 seconds
