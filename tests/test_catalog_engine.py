import asyncio
import weakref
from dataclasses import dataclass

import pytest

from sobafm.audio_budget import AudioBytes, AudioLedger
from sobafm.catalog import CatalogCycle, CatalogPage, CatalogQuery, CatalogTrack
from sobafm.catalog_engine import CatalogEngine, History
from sobafm.catalog_interpreter import CatalogPlan
from sobafm.failures import Failure
from sobafm.jamendo import CatalogError
from sobafm.mixer import Mixer
from sobafm.program import Outcome, Program
from tests.doubles import FakeCatalog, FakeClock, FakeSource, settle
from tests.test_catalog import credit, discovered

PLAN = CatalogPlan(title="quiet piano", query=CatalogQuery(tags=("piano",)))


def track(number: int, artist: str | None = None, *, eligible: bool = True) -> CatalogTrack:
    value = discovered(id=str(number), artist=artist or f"Artist {number}")
    if not eligible:
        return value
    return value.with_credits(
        credit(track_id=value.id, artist=value.artist, source_urls=(value.url,))
    )


class FixtureTrack(FakeSource):
    def __init__(self, value: CatalogTrack, program: Program) -> None:
        super().__init__(program)
        self.track = value
        self.title = value.title


@dataclass
class Rig:
    engine: CatalogEngine
    catalog: FakeCatalog
    made: list[FixtureTrack]
    clock: FakeClock
    history: History
    audio: AudioLedger

    async def generate(self, program: Program) -> None:
        self.engine.generate(program)
        await settle()
        self.engine.generate(program)


def rig(*results: tuple[CatalogTrack, ...] | CatalogPage | Exception) -> Rig:
    clock, audio = FakeClock(), AudioLedger()
    history = History(clock)
    made: list[FixtureTrack] = []
    catalog = FakeCatalog(*results)

    def create(value: CatalogTrack, program: Program) -> FixtureTrack:
        source = FixtureTrack(value, program)
        made.append(source)
        return source

    engine = CatalogEngine(
        catalog, create, audio=audio, history=history, wake=lambda: None, clock=clock
    )
    return Rig(engine, catalog, made, clock, history, audio)


async def test_local_exclusions_continue_the_original_page_cursor() -> None:
    values = tuple(track(i, eligible=False) for i in range(1, 11))
    r = rig(CatalogPage(tracks=values, received=10, exhausted=False), (track(11),))
    program = Program(PLAN, "Member", None)
    assert r.engine.reserve(program)
    await r.generate(program)
    await settle()
    r.engine.generate(program)
    assert [s.track.id for s in r.made] == ["11"]
    assert r.catalog.queries == [PLAN.query]
    await r.engine.close()


async def test_local_rights_exhaustion_never_opens_a_new_relaxation_budget() -> None:
    r = rig((track(1, eligible=False),), (track(2),))
    program = Program(PLAN, "Member", None)
    assert r.engine.reserve(program)
    await r.generate(program)
    for _ in range(5):
        r.engine.generate(program)
    assert not r.made
    assert r.catalog.queries == [PLAN.query]
    assert r.engine.terminal(program, True) is Outcome.NO_MATCH
    await r.engine.close()


async def test_only_one_unplayed_source_and_audible_acceptance_owns_history() -> None:
    first, second = track(1), track(2)
    r = rig((first,), (second,))
    program = Program(PLAN, "Member", None)
    assert r.engine.reserve(program)
    await r.generate(program)
    r.made[0].send_audio(6)
    await r.generate(program)
    assert len(r.made) == 1
    assert not r.history.excludes(first)
    r.engine.going_live(r.made[0])
    program.settle(Outcome.PLAYING)
    await r.generate(program)
    assert [s.track.id for s in r.made] == ["1", "2"]
    await r.generate(program)
    assert len(r.made) == 2
    assert r.history.excludes(first)
    await r.engine.close()


async def test_failed_candidates_share_three_attempts_and_backoff() -> None:
    r = rig(tuple(track(i) for i in range(1, 6)))
    program = Program(PLAN, "Member", None)
    assert r.engine.reserve(program)
    await r.generate(program)
    for index, delay in enumerate((2, 5, 15)):
        failed = r.made[-1]
        failed.finish(Outcome.FAILED)
        assert r.engine.ended_failures(program) == [failed]
        outcome = r.engine.count_failure(failed, program, True)
        assert r.history.excludes(failed.track)
        r.engine.drop_ended(program, Mixer(1))
        if index == 2:
            assert outcome is Outcome.UNAVAILABLE
            break
        assert outcome is None
        await r.generate(program)
        assert len(r.made) == index + 1
        r.clock.now += delay
        await r.generate(program)
        assert len(r.made) == index + 2
    assert len(r.made) == 3
    assert len(r.catalog.queries) == 1
    await r.engine.close()


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [(Failure.REJECTED, Outcome.REJECTED), (Failure.EXHAUSTED, Outcome.EXHAUSTED)],
)
async def test_auth_and_quota_are_terminal_without_refetch(
    failure: Failure, outcome: Outcome
) -> None:
    r = rig(CatalogError("api", failure), (track(1),))
    program = Program(PLAN, "Member", None)
    assert r.engine.reserve(program)
    await r.generate(program)
    assert r.engine.terminal(program, True) is outcome
    assert len(r.catalog.queries) == 1
    assert not r.made
    await r.engine.close()


async def test_playing_exhaustion_waits_15_30_60_then_keeps_the_ceiling() -> None:
    r = rig((), (), (), (), ())
    program = Program(PLAN, "Member", None)
    program.settle(Outcome.PLAYING)
    assert r.engine.reserve(program)
    for index, delay in enumerate((15, 30, 60, 60)):
        await r.generate(program)
        assert r.engine.terminal(program, True) is None
        deadline = r.clock.now + delay
        r.clock.now = deadline - 0.01
        await r.generate(program)
        assert len(r.catalog.queries) == index + 1
        r.clock.now = deadline
    await r.engine.close()


async def test_superseded_unheard_failure_still_excludes_id_without_charging_new_program() -> None:
    bad, good = track(1), track(2)
    r = rig((bad,), (bad, good))
    old, new = Program(PLAN, "Old", None), Program(PLAN, "New", None)
    assert r.engine.reserve(old)
    await r.generate(old)
    failed = r.made[0]
    assert r.engine.reserve(new)
    failed.finish(Outcome.FAILED)
    assert r.engine.ended_failures(new) == []
    assert r.history.excludes(bad)
    assert r.engine.count_failure(failed, new, True) is None
    r.engine.drop_ended(new, Mixer(1))
    await r.generate(new)
    assert r.made[-1].track.id == good.id
    assert r.engine.terminal(new, True) is None
    await r.engine.close()


async def test_prepared_artist_is_rechecked_without_restarting_its_cycle() -> None:
    b, c = track(2, "B"), track(3, "C")
    r = rig((b, c))
    program = Program(PLAN, "Member", None)
    assert r.engine.reserve(program)
    await r.generate(program)
    r.made[0].send_audio(6)
    r.history.heard(track(99, "\uff22"))
    assert r.engine.ready_source(program) is None
    assert r.made[0].retired
    await r.generate(program)
    assert r.made[-1].track.id == c.id
    assert len(r.catalog.queries) == 1
    await r.engine.close()


def test_history_uses_full_normalized_artist_and_expires_without_oldest_replay() -> None:
    clock = FakeClock()
    history = History(clock)
    history.heard(track(1, "\uff33\uff50\uff41\uff43\uff45\u3000\uff22\uff21\uff2e\uff24"))
    assert history.excludes(track(2, "space band"))
    history.heard(track(3, "x" * 499 + "A"))
    assert not history.excludes(track(4, "x" * 499 + "B"))
    assert not history.excludes(track(5, "Space Band"))  # anti-consecutive, not an artist ban
    for number in range(10, 75):
        history.heard(track(number))
    assert not history.excludes(track(1, "Other"))  # 64 id entries, not unbounded history
    history.failed(track(99))
    assert history.excludes(track(99))
    clock.now += 600
    assert not history.excludes(track(99))
    clock.now += 24 * 3600
    assert not history.excludes(track(74))


async def test_dropping_and_closing_sources_never_refunds_complete_audio_lease() -> None:
    r = rig((track(1),))
    program = Program(PLAN, "Member", None)
    assert r.engine.reserve(program)
    await r.generate(program)
    size = AudioBytes(pcm=2_000_000, other=100_000)
    lease = r.audio.reserve(size)
    assert lease is not None
    r.made[0].finish(Outcome.FAILED)
    r.engine.ended_failures(program)
    r.engine.drop_ended(None, Mixer(1))
    assert not r.engine.sources
    await r.engine.close()
    assert r.audio.reserved == size


async def test_unknown_audio_obligation_refuses_catalog_reservation_without_work() -> None:
    r = rig((track(1),))
    debt = r.audio.unbounded()
    assert debt.size is None
    assert not r.engine.reserve(Program(PLAN, "Member", None))
    assert not r.catalog.queries
    assert not r.made
    await r.engine.close()
    assert not r.audio.bounded


class HeldCycle:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def next_page(self) -> CatalogPage:
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()  # fixture-owned asynchronous close work
            raise
        raise AssertionError("held read unexpectedly returned")

    def close(self) -> None:
        self.closed = True


class HeldCatalog:
    def __init__(self, cycle: HeldCycle) -> None:
        self.cycle = cycle

    def open(self, query: CatalogQuery) -> CatalogCycle:
        return self.cycle

    async def search(self, query: CatalogQuery) -> tuple[CatalogTrack, ...]:
        raise AssertionError("engine must use its original cycle")


async def test_load_owner_survives_weak_program_loss_and_is_cancelled_joined_on_close() -> None:
    cycle = HeldCycle()
    made: list[FixtureTrack] = []

    def create(value: CatalogTrack, program: Program) -> FixtureTrack:
        source = FixtureTrack(value, program)
        made.append(source)
        return source

    engine = CatalogEngine(
        HeldCatalog(cycle), create, audio=AudioLedger(), history=History(), wake=lambda: None
    )
    program = Program(PLAN, "Member", None)
    assert engine.reserve(program)
    engine.generate(program)
    await cycle.entered.wait()
    ref = weakref.ref(program)
    del program
    assert ref() is None
    closing = asyncio.create_task(engine.close())
    await cycle.cancelled.wait()
    assert cycle.closed
    assert not closing.done()
    engine.release_if_ended(())  # cannot send a second cancel into close work
    await settle()
    assert not closing.done()
    cycle.release.set()
    await closing
    assert not made
