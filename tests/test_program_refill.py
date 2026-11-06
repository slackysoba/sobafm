"""A program's song list refills as it plays (ADR 0011), offline: no provider or voice."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import cast

import pytest

from sobafm.catalog import Recording, RecordingKey, Source, eligible, parse_catalog, parse_reviews
from sobafm.discord_adapter import (
    MAX_REFILL_ATTEMPTS,
    REFILL_RETRY_SECONDS,
    SKIPPING,
    Adapter,
    EndReason,
    GuildPlayer,
)
from sobafm.gemini_interactions import TIMEOUT_SECONDS
from sobafm.gemini_match import (
    MAX_RANKED,
    Fit,
    GeminiMatcher,
    Matched,
    MatchFailed,
    Ranked,
    Refused,
)
from sobafm.pcm import BYTES_PER_SECOND, READY_BYTES, PcmBankPool
from sobafm.settings import ServerSettings
from sobafm.station import Program, Station
from tests.test_discord_adapter import (
    BOT_ID,
    LISTENER_ID,
    FakeClock,
    FakeMatcher,
    FakeSettings,
    Harness,
    Tagged,
    heard_id,
    inter,
    matcher_of,
    next_id,
    now_texts,
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

SNAPSHOT = Path(__file__).resolve().parents[1] / "evaluations/catalog/fma-2026-11"
NOTHING_READY = "The next recording could not be prepared; the current recording continues."
GEMINI_ENDED = "Gemini did not supply more recordings"


def keys(*identifiers: str) -> frozenset[RecordingKey]:
    return frozenset(RecordingKey(Source.JAMENDO, i) for i in identifiers)


def player_of(h: Harness) -> GuildPlayer:
    return h.adapter.players[1]


def station_of(h: Harness) -> Station:
    return player_of(h).station


def desired_of(h: Harness) -> Program:
    program = station_of(h).desired
    assert program is not None
    return program


def one_at_a_time(*identifiers: str, frames: int = 50) -> tuple[Harness, Tagged, FakeMatcher]:
    """A program whose every list holds one recording, so each one starting asks for more."""
    h, tagged = program_harness(*identifiers, producer=Tagged(frames=frames))
    fake = matcher_of(h)
    fake.batch = 1
    return h, tagged, fake


async def first_heard(
    h: Harness, fake: FakeMatcher, outcome: Matched | Refused | MatchFailed | None = None
) -> None:
    """Join, play and hear the first recording, with ``outcome`` set for the refill's calls."""
    source = await h.join()
    await h.play()
    fake.outcome = outcome
    await settle()
    assert source.read() == tag("a")
    await until(lambda: heard_id(h) == "a")


async def calls_reach(fake: FakeMatcher, count: int) -> None:
    await until(lambda: len(fake.requests) == count)


async def retry_waiting(h: Harness) -> None:
    await until(lambda: h.clock.pending == [REFILL_RETRY_SECONDS])


# A refill at the last song.


@offline
async def test_the_last_song_starting_asks_for_more_while_it_keeps_playing() -> None:
    h, tagged = program_harness("a", "b", "c", "d", producer=Tagged(frames=2))
    tagged.plans["b"] = (50, True)
    fake = matcher_of(h)
    fake.batch = 2
    source = await start_program(h)
    await prepared_next(h, "b")
    assert fake.requests == ["piano"]  # a isn't the last on its list

    fake.gate = asyncio.Event()
    assert source.read() == tag("a")
    assert source.read() == tag("b")  # the last song on the list starts
    await until(lambda: heard_id(h) == "b")
    await calls_reach(fake, 2)
    assert fake.requests == ["piano", "piano"] and fake.played[1] == keys("a", "b")

    for _ in range(3):  # music keeps playing while the call waits
        assert source.read() == tag("b")
        await settle()
    assert heard_id(h) == "b" and next_id(h) is None and station_of(h).ending is None

    fake.gate.set()
    await prepared_next(h, "c")
    assert source.read() == tag("b")  # it follows b's last frame, with no cut
    assert len(fake.requests) == 2 and tagged.started == ["a", "b", "c"]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_list_of_one_refills_when_its_only_song_starts() -> None:
    h, tagged, fake = one_at_a_time("a", "b", frames=1)
    source = await start_program(h)
    await prepared_next(h, "b")
    assert fake.played == [frozenset(), keys("a")]
    assert source.read() == tag("b")  # the station isn't silent after one song
    await until(lambda: heard_id(h) == "b")
    assert tagged.started == ["a", "b"]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_refill_sends_the_same_checked_request_as_the_first_call() -> None:
    h, _, fake = one_at_a_time("a", "b")
    source = await h.join()
    await h.play("  piano  ")
    await settle()
    assert source.read() == tag("a")
    await calls_reach(fake, 2)
    assert fake.requests == ["piano", "piano"]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_refill_does_not_wait_for_a_free_bank() -> None:
    h, tagged, fake = one_at_a_time("a", "b")
    blocker = h.pool.acquire()  # another server holds the free bank
    await start_program(h)
    await calls_reach(fake, 2)
    await settle()
    assert station_of(h).plan(desired_of(h)) == (recording("b"),)
    assert tagged.started == ["a"] and h.pool.leased == 2  # the next track waits for its bank

    assert blocker.release()
    h.adapter._capacity_changed()  # pyright: ignore[reportPrivateUsage]
    await prepared_next(h, "b")
    assert await h.adapter.shutdown() is True


@offline
async def test_a_refill_can_bring_the_program_back_after_its_track_ended() -> None:
    h, _, fake = one_at_a_time("a", "b", frames=1)
    await h.join()
    await h.play()
    fake.gate = asyncio.Event()
    source = player_of(h).source
    await settle()
    assert source.read() == tag("a")
    await until(lambda: heard_id(h) == "a")
    await calls_reach(fake, 2)
    assert source.read() == b""  # a ends while the call waits, leaving a gap
    await until(lambda: heard_id(h) is None)
    assert station_of(h).desired is not None
    assert await now_texts(h) == [
        "Nothing is audible. The program's next recording is preparing and is not audible yet."
    ]
    assert len(fake.requests) == 2  # a's drain started no second refill

    fake.gate.set()
    await prepared_next(h, "b")
    assert source.read() == tag("b")
    await until(lambda: heard_id(h) == "b")
    assert await h.adapter.shutdown() is True


# Failure, the retry bound and an honest ending.


@offline
async def test_a_failing_refill_retries_within_its_bound_and_ends_honestly() -> None:
    h, tagged, fake = one_at_a_time("a", "b")
    await first_heard(h, fake, MatchFailed("http_503"))
    source = player_of(h).source
    for attempt in range(1, MAX_REFILL_ATTEMPTS):
        await calls_reach(fake, 1 + attempt)
        await retry_waiting(h)  # a timed wait between calls, so never a hot loop
        assert source.read() == tag("a") and station_of(h).ending is None
        assert await skip(h) == NOTHING_READY
        h.clock.expire()
    await calls_reach(fake, 1 + MAX_REFILL_ATTEMPTS)
    await until(lambda: station_of(h).ending is not None)
    assert station_of(h).ending is EndReason.REFILL_FAILED
    assert h.clock.pending == [] and player_of(h).refill is not None
    await settle()

    # The current song finishes, and the replies say why nothing follows it.
    assert source.read() == tag("a") and heard_id(h) == "a"
    (status, *_) = await now_texts(h)
    assert f"This is the program's last recording: {GEMINI_ENDED}." in status
    assert await skip(h) == f"Nothing follows: {GEMINI_ENDED}. The current recording continues."
    assert len(fake.requests) == 1 + MAX_REFILL_ATTEMPTS  # /now and /skip made no call

    while source.read() != b"":
        await settle()
    await until(lambda: station_of(h).desired is None)
    assert station_of(h).ended is EndReason.REFILL_FAILED
    assert await now_texts(h) == [
        f"Nothing is audible. The last program ended: {GEMINI_ENDED}. "
        "Use `/play` to start a new one."
    ]
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(fake.requests) == 1 + MAX_REFILL_ATTEMPTS and tagged.started == ["a"]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_refill_that_recovers_keeps_the_program_going() -> None:
    h, _, fake = one_at_a_time("a", "b")
    await first_heard(h, fake, MatchFailed("timeout"))
    await retry_waiting(h)
    fake.outcome = None
    h.clock.expire()
    await prepared_next(h, "b")
    assert fake.played == [frozenset(), keys("a"), keys("a")]
    assert station_of(h).ending is None
    assert await h.adapter.shutdown() is True


def test_a_refill_failing_with_nothing_audible_ends_at_once_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")

    async def scenario() -> None:
        h, _, fake = one_at_a_time("a", "b", frames=1)
        await first_heard(h, fake, MatchFailed("http_500"))
        source = player_of(h).source
        await retry_waiting(h)
        assert source.read() == b""  # a ends during the retries
        await until(lambda: heard_id(h) is None)
        assert station_of(h).desired is not None
        h.clock.expire()
        await retry_waiting(h)
        h.clock.expire()
        await until(lambda: station_of(h).desired is None)
        assert station_of(h).ended is EndReason.REFILL_FAILED
        assert await h.adapter.shutdown() is True

    asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records if r.name == "sobafm.discord_adapter"]
    assert [f"program refill failed: attempt={n}" for n in (1, 2, 3)] == [
        line for line in lines if line.startswith("program refill failed")
    ]
    assert lines.count("program ended: reason=refill_failed") == 1
    assert "piano" not in caplog.text


# Refusal and empty results.


@pytest.mark.parametrize(
    "outcome",
    [
        Refused(),
        Matched(()),
        Matched((Ranked(recording("a"), Fit.CLOSE),)),  # a played song, and nothing new
    ],
    ids=["refusal", "empty", "only_played"],
)
def test_a_refusal_or_nothing_new_cannot_start_a_hot_loop(outcome: Matched | Refused) -> None:
    async def scenario() -> None:
        h, tagged, fake = one_at_a_time("a", "b")
        await first_heard(h, fake, outcome)
        for attempt in range(1, MAX_REFILL_ATTEMPTS):
            await calls_reach(fake, 1 + attempt)
            await retry_waiting(h)
            for _ in range(50):  # nothing moves until the timer does
                await asyncio.sleep(0)
            assert len(fake.requests) == 1 + attempt
            h.clock.expire()
        await until(lambda: station_of(h).ending is EndReason.REFILL_FAILED)
        assert len(fake.requests) == 1 + MAX_REFILL_ATTEMPTS and tagged.started == ["a"]
        assert station_of(h).plan(desired_of(h)) == (recording("a"),)  # it stays as it was
        assert await h.adapter.shutdown() is True

    asyncio.run(scenario())


# Replacement, stop, leave, grace expiry and shutdown during a refill.


class LateMatcher(FakeMatcher):
    """Its call ignores cancellation and answers when released, so a stale result arrives."""

    def __init__(self, *recordings: Recording) -> None:
        super().__init__(*recordings)
        self.late = asyncio.Event()

    async def match(
        self, request: str, played: frozenset[RecordingKey] = frozenset()
    ) -> Matched | Refused:
        try:
            return await super().match(request, played)
        except asyncio.CancelledError:
            await self.late.wait()
            return Matched(
                tuple(Ranked(r, self.fit) for r in self.recordings if r.key not in played)
            )


ENDINGS = ["replacement", "stop", "leave", "grace", "shutdown"]


@pytest.mark.parametrize("late", [False, True], ids=["cancelled", "stale_result"])
@pytest.mark.parametrize("ending", ENDINGS)
def test_ending_or_replacing_a_program_cancels_its_refill(ending: str, late: bool) -> None:
    async def scenario() -> None:
        h, tagged = program_harness("a", producer=Tagged(frames=50))
        records = (recording("a"), recording("b"), recording("c"))
        fake = LateMatcher(*records) if late else FakeMatcher(*records)
        fake.batch = 1
        h.adapter.matcher = fake
        source = await h.join()
        await h.play()
        fake.gate = asyncio.Event()
        await settle()
        assert source.read() == tag("a")
        await until(lambda: heard_id(h) == "a")
        await calls_reach(fake, 2)
        player = player_of(h)
        station = player.station
        old = desired_of(h)
        refill = player.refill
        assert refill is not None and refill.program == old and not refill.task.done()

        finished: asyncio.Task[object] | None = None
        match ending:
            case "replacement":
                h.adapter.matcher = FakeMatcher(recording("d"))
                await h.play("other")
                await prepared_next(h, "d")
            case "stop":
                await run(h.adapter.stop, inter(h.room))
            case "leave":
                await run(h.adapter.leave, inter(h.room, manager=True))
            case "grace":
                h.voice_update(LISTENER_ID, None)
                await settle()
                h.clock.expire()
                await until(lambda: station.desired is None)
            case _:
                finished = asyncio.create_task(h.adapter.shutdown())
                await settle()
        assert player.refill is None
        await settle()

        if isinstance(fake, LateMatcher):
            assert not refill.task.done()  # still owned while its call hasn't returned
            fake.late.set()
        await asyncio.wait({refill.task}, timeout=5)  # bounded, so a regression fails
        assert refill.task.done() and refill.task.cancelled() is not late

        # Whatever arrived changed nothing: no track, list or ending came from it.
        await settle()
        assert len(fake.requests) == 2
        match ending:
            case "replacement":
                assert station.desired is not None and station.desired != old
                assert station.plan(station.desired) == (recording("d"),)
                assert tagged.started == ["a", "d"] and station.ending is None
            case "shutdown":
                assert finished is not None and await finished is True
            case _:
                assert station.desired is None and heard_id_or_none(h) is None
                assert tagged.started == ["a"]
                expected = EndReason.NO_LISTENERS if ending == "grace" else None
                assert station.ended is expected
        assert await h.adapter.shutdown() is True
        assert h.pool.leased == 0 and h.adapter.tasks == set()

    asyncio.run(scenario())


def heard_id_or_none(h: Harness) -> str | None:
    return heard_id(h) if 1 in h.adapter.players else None


@offline
async def test_a_superseded_program_makes_no_further_call() -> None:
    h, _, fake = one_at_a_time("a", "b", "c")
    await first_heard(h, fake)
    old = desired_of(h)
    await prepared_next(h, "b")
    await run(h.adapter.stop, inter(h.room))
    await h.play("other")
    new = desired_of(h)
    assert new != old
    calls = len(fake.requests)

    # A retry of the old program's refill wakes only now.
    await h.adapter._refill(player_of(h), old)  # pyright: ignore[reportPrivateUsage]
    assert len(fake.requests) == calls and desired_of(h) == new
    assert station_of(h).plan(new) == (recording("a"),) and station_of(h).ending is None
    assert await h.adapter.shutdown() is True


@pytest.mark.parametrize("ending", ["stop", "shutdown"])
def test_ending_a_program_cancels_a_refill_waiting_to_retry(ending: str) -> None:
    async def scenario() -> None:
        h, _, fake = one_at_a_time("a", "b")
        await first_heard(h, fake, MatchFailed("http_503"))
        await retry_waiting(h)
        if ending == "stop":
            await run(h.adapter.stop, inter(h.room))
            await settle()
            assert h.clock.pending == []  # the timer's task was cancelled, not left waiting
            assert len(fake.requests) == 2 and station_of(h).ended is None
        assert await h.adapter.shutdown() is True
        assert h.clock.pending == [] and h.adapter.tasks == set()

    asyncio.run(scenario())


@offline
async def test_losing_voice_cancels_a_refill() -> None:
    h, _, fake = one_at_a_time("a", "b")
    await h.join()
    await h.play()
    fake.gate = asyncio.Event()
    source = player_of(h).source
    await settle()
    assert source.read() == tag("a")
    await calls_reach(fake, 2)
    refill = player_of(h).refill
    assert refill is not None
    h.voice_update(BOT_ID, None)
    await asyncio.wait({refill.task}, timeout=5)  # bounded, so a regression fails
    assert refill.task.cancelled() and player_of(h).refill is None
    assert await h.adapter.shutdown() is True


@offline
async def test_a_failed_replacement_hands_the_refill_back_to_the_program() -> None:
    h, tagged, fake = one_at_a_time("a", "b")
    await h.join()
    await h.play()
    fake.gate = asyncio.Event()
    source = player_of(h).source
    await settle()
    assert source.read() == tag("a")
    await calls_reach(fake, 2)
    player = player_of(h)
    old = desired_of(h)
    first = player.refill
    assert first is not None
    blocker = h.pool.acquire()  # the replacement can't lease a bank

    h.adapter.matcher = FakeMatcher(recording("a"), recording("c"))
    (reply,) = await h.play("other")
    assert "capacity is busy" in reply and desired_of(h) == old and heard_id(h) == "a"
    assert first.task.cancelling() or first.task.cancelled()

    # The heard program stays desired and asks again, rather than waiting for its track's end.
    await until(lambda: station_of(h).plan(old) == (recording("c"),))
    assert first.task.cancelled() and station_of(h).ending is None
    assert blocker.release()
    h.adapter._capacity_changed()  # pyright: ignore[reportPrivateUsage]
    await prepared_next(h, "c")
    assert tagged.started == ["a", "c"]
    assert await h.adapter.shutdown() is True


# /skip during a refill.


@offline
async def test_skip_never_waits_on_the_model() -> None:
    h, tagged, fake = one_at_a_time("a", "b", "c")
    await h.join()
    await h.play()
    fake.gate = asyncio.Event()
    source = player_of(h).source
    await settle()
    assert source.read() == tag("a")
    await calls_reach(fake, 2)

    # The call hasn't returned, so nothing is ready: the existing wording applies at once.
    assert await skip(h) == NOTHING_READY
    assert await skip(h) == NOTHING_READY
    player = player_of(h)
    assert not player.skip_requested and player.waiting_claim is None
    assert len(fake.requests) == 2 and source.read() == tag("a") and heard_id(h) == "a"

    # When the call returns, the next track follows a's end, since that skip did nothing.
    fake.gate.set()
    await prepared_next(h, "b")
    assert source.read() == tag("a")

    # A skip with something ready cuts to it, and the next list is asked for in turn.
    assert await skip(h) == SKIPPING
    assert source.read() == tag("b")
    await until(lambda: heard_id(h) == "b")
    await prepared_next(h, "c")
    assert fake.played[-1] == keys("a", "b") and tagged.started == ["a", "b", "c"]
    assert await h.adapter.shutdown() is True


@offline
async def test_a_refill_neither_takes_nor_gives_back_the_cooldown() -> None:
    h, _, fake = one_at_a_time("a", "b")
    settings_of(h).current = ServerSettings(cooldown_seconds=30)
    await first_heard(h, fake, MatchFailed("http_503"))
    await calls_reach(fake, 2)

    # A failed refill doesn't free the request's cooldown.
    assert (await h.play("next"))[0].startswith("This server's change cooldown")
    # The wait passing frees it, and the refill's calls don't renew it.
    h.clock.now += 30
    fake.outcome = None
    h.clock.expire()
    await prepared_next(h, "b")
    assert await skip(h) == SKIPPING
    assert await h.adapter.shutdown() is True


@offline
async def test_a_skip_given_nothing_gives_back_its_claim() -> None:
    h, _, fake = one_at_a_time("a", "b")
    settings_of(h).current = ServerSettings(cooldown_seconds=30)
    await h.join()
    await h.play()
    fake.gate = asyncio.Event()
    source = player_of(h).source
    await settle()
    assert source.read() == tag("a")
    await calls_reach(fake, 2)
    h.clock.now += 30
    assert await skip(h) == NOTHING_READY
    assert await skip(h) == NOTHING_READY  # the one before holds no cooldown
    assert await h.adapter.shutdown() is True


# Catalog exhaustion and the total call bound.


@offline
async def test_every_song_played_ends_the_program_with_no_extra_call_or_repeat() -> None:
    identifiers = "abcde"
    h, tagged, fake = one_at_a_time(*identifiers, frames=1)
    source = await start_program(h)
    for played, identifier in enumerate(identifiers[1:], start=1):
        await prepared_next(h, identifier)
        assert fake.played[-1] == keys(*identifiers[:played])
        assert source.read() == tag(identifier)
        await until(partial(lambda expected: heard_id(h) == expected, identifier))
    await until(lambda: station_of(h).ending is not None)
    assert station_of(h).ending is EndReason.EXHAUSTED

    # One call per list, even with a model that returns one song at a time.
    assert len(fake.requests) == len(identifiers)
    assert tagged.started == list(identifiers)
    assert source.read() == b""
    await until(lambda: station_of(h).desired is None)
    assert station_of(h).ended is EndReason.EXHAUSTED
    assert await now_texts(h) == [
        "Nothing is audible. The last program ended: every recording in the catalog has "
        "been played or tried. Use `/play` to start a new one."
    ]
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(fake.requests) == len(identifiers) and h.clock.pending == []
    assert await h.adapter.shutdown() is True


class ChoosingTransport:
    """Answers each call by choosing the first songs it was sent, as Gemini's list would."""

    def __init__(self) -> None:
        self.sent: list[list[str]] = []

    async def post(self, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        sent = cast(dict[str, object], json.loads(body))
        assert sent["store"] is False
        text = cast(str, sent["input"])
        songs = text.removeprefix("SONGS:\n").split("\n\nREQUEST:\n")[0]
        titles = [cast(str, song["title"]) for song in json.loads(songs)]
        self.sent.append(titles)
        chosen = [{"song": n, "fit": "loose"} for n in range(min(MAX_RANKED, len(titles)))]
        output = json.dumps({"outcome": "match", "ranked": chosen})
        reply = {
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": output}]}],
        }
        return 200, json.dumps(reply).encode()

    async def close(self) -> None:
        pass


@offline
async def test_a_whole_program_through_the_real_matcher_stays_within_its_call_bound() -> None:
    identifiers = [f"song{n:02d}" for n in range(20)]
    records = tuple(recording(i) for i in identifiers)
    transport = ChoosingTransport()
    matcher = GeminiMatcher(records, "placeholder-gemini-value", transport=transport)
    tagged = Tagged(frames=1)
    pool = PcmBankPool()
    clock = FakeClock()
    adapter = Adapter(
        matcher, tagged, FakeSettings(), pool=pool, sleep=clock.sleep, clock=clock.time
    )
    h = Harness(adapter, pool, tagged, clock)
    source = await h.join()
    await h.play()
    station = station_of(h)
    for _ in range(5_000):
        source.read()
        await asyncio.sleep(0)
        if station.desired is None:
            break
    assert station.ended is EndReason.EXHAUSTED

    # Eight songs at a time, with no repeats, and nothing played is ever sent again.
    assert tagged.started == identifiers
    assert len(transport.sent) == math.ceil(len(identifiers) / MAX_RANKED) == 3
    assert [len(titles) for titles in transport.sent] == [20, 12, 4]
    assert transport.sent[1] == [f"Song {i}" for i in identifiers[8:]]
    assert transport.sent[2] == [f"Song {i}" for i in identifiers[16:]]
    assert clock.pending == []
    assert await h.adapter.shutdown() is True


def test_the_retry_bound_leaves_time_on_the_shortest_admitted_recording() -> None:
    admitted = eligible(
        parse_catalog((SNAPSHOT / "catalog.json").read_text(encoding="utf-8")),
        parse_reviews((SNAPSHOT / "reviews.json").read_text(encoding="utf-8")),
    )
    durations = [r.duration_seconds for r in admitted if r.duration_seconds is not None]
    assert len(durations) == len(admitted)
    calls = MAX_REFILL_ATTEMPTS * TIMEOUT_SECONDS
    waits = (MAX_REFILL_ATTEMPTS - 1) * REFILL_RETRY_SECONDS
    # Six seconds of pre-roll, even at a decoder only as fast as real time.
    assert calls + waits + READY_BYTES / BYTES_PER_SECOND < min(durations)
    # ADR 0011's estimate: about 21 calls for the current catalog, with no retries.
    assert math.ceil(len(admitted) / MAX_RANKED) == 21
