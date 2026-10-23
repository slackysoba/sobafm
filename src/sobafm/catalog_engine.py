"""Bounded catalog selection and track lifetime; only station accepts audible starts."""

import asyncio
import logging
import time
import unicodedata
import weakref
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sobafm.audio_budget import AudioLedger
from sobafm.catalog import Catalog, CatalogCycle, CatalogPage, CatalogQuery, CatalogTrack
from sobafm.catalog_interpreter import CatalogPlan
from sobafm.failures import Failure
from sobafm.jamendo import CatalogError
from sobafm.mixer import Mixer
from sobafm.program import Mode, Outcome, Program
from sobafm.source import Source, State

log = logging.getLogger(__name__)
TRACK_RETRY_S = (2.0, 5.0, 15.0)
EXHAUSTION_RETRY_S = (15.0, 30.0, 60.0)
HISTORY_SECONDS = 24 * 3600
FAILED_SECONDS = 10 * 60
MAX_CANDIDATES = 3
METADATA_OUTCOMES: dict[Failure | None, Outcome] = {
    Failure.REJECTED: Outcome.REJECTED,
    Failure.EXHAUSTED: Outcome.EXHAUSTED,
    Failure.UNAVAILABLE: Outcome.UNAVAILABLE,
    None: Outcome.FAILED,
}


def artist_key(label: str) -> str:
    """Compare the complete validated display label, without claiming performer identity."""
    return " ".join(unicodedata.normalize("NFKC", label).split()).casefold()


class History:
    """One server's process-local history, shared by replacement engine owners."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._played: deque[tuple[str, float]] = deque(maxlen=64)
        self._failed: deque[tuple[str, float]] = deque(maxlen=128)
        self._artist: tuple[str, float] | None = None

    def _expire(self) -> None:
        now = self._clock()
        while self._played and now - self._played[0][1] >= HISTORY_SECONDS:
            self._played.popleft()
        while self._failed and now - self._failed[0][1] >= FAILED_SECONDS:
            self._failed.popleft()
        if self._artist is not None and now - self._artist[1] >= HISTORY_SECONDS:
            self._artist = None

    def excludes(self, track: CatalogTrack) -> bool:
        self._expire()
        return any(track.id == key for key, _ in (*self._played, *self._failed)) or (
            self._artist is not None and artist_key(track.artist) == self._artist[0]
        )

    def heard(self, track: CatalogTrack) -> None:
        self._expire()
        self._played.append((track.id, self._clock()))
        self._artist = (artist_key(track.artist), self._clock())

    def failed(self, track: CatalogTrack) -> None:
        self._expire()
        self._failed.append((track.id, self._clock()))


class TrackSource(Source, Protocol):
    @property
    def track(self) -> CatalogTrack: ...

    @property
    def failure(self) -> object | None: ...

    @property
    def completed(self) -> bool: ...


@dataclass
class Selection:
    owner: weakref.ReferenceType[Program] | None = None
    epoch: int = 0
    cursor: CatalogCycle | None = None
    task: asyncio.Task[None] | None = None
    result: CatalogPage | None = None
    candidates: deque[CatalogTrack] = field(default_factory=deque[CatalogTrack])
    seen: set[str] = field(default_factory=set[str])
    exhausted: bool = False
    attempted: int = 0
    failures: int = 0
    exhaustion_count: int = 0
    retry_at: float = 0
    outcome: Outcome | None = None
    live_number: int | None = None


class CatalogEngine:
    """One audible track plus one prepared track, using the station's existing ledger.

    `create` constructs an unstarted TrackDeck (or a fixture source), with that
    same ledger and the process-wide decoder supervisor. The server owner keeps
    `history` across replacement stations. Neither membership removal nor close
    refunds a TrackDeck's complete audio allocation: its storage owner must prove
    release independently of producer cleanup and player/deque drain.
    """

    def __init__(
        self,
        catalog: Catalog,
        create: Callable[[CatalogTrack, Program], TrackSource | None],
        *,
        audio: AudioLedger,
        history: History,
        wake: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        cancel_admission_wait: Callable[[], None] | None = None,
    ) -> None:
        self.audio, self.history = audio, history
        self._catalog, self._create, self._wake, self._clock = catalog, create, wake, clock
        self._sources: list[TrackSource] = []
        self._states: weakref.WeakKeyDictionary[Program, Selection] = weakref.WeakKeyDictionary()
        self._seen_failed: weakref.WeakSet[TrackSource] = weakref.WeakSet()
        self._retired: weakref.WeakSet[TrackSource] = weakref.WeakSet()
        self._tasks: dict[asyncio.Task[None], Selection] = {}
        self._closed = False
        self._cancel_admission_wait = cancel_admission_wait

    @property
    def mode(self) -> Mode:
        return Mode.CATALOG

    @property
    def sources(self) -> Sequence[Source]:
        return self._sources

    def reserve(self, program: Program) -> bool:
        if self._closed or program.mode is not self.mode or not self.audio.bounded:
            return False
        self._state(program)
        return True

    def _state(self, program: Program) -> Selection:
        if program not in self._states:
            self._states[program] = Selection(owner=weakref.ref(program))
        return self._states[program]

    def _owned_states(self) -> list[Selection]:
        # A load outlives its weak Program key until cancellation/join completes.
        values = [*self._states.values(), *self._tasks.values()]
        return list({id(state): state for state in values}.values())

    def release_if_ended(self, retained: Sequence[Program]) -> None:
        for state in self._owned_states():
            program = state.owner() if state.owner is not None else None
            if program not in retained:
                self._end_cycle(state)

    def _end_cycle(self, state: Selection) -> None:
        state.epoch += 1
        if state.cursor is not None:
            state.cursor.close()
        if state.task is not None and not state.task.cancelling():
            state.task.cancel()
        state.cursor = state.result = None
        state.candidates.clear()
        state.seen.clear()
        state.attempted = 0
        state.exhausted = False

    def _recover(self, state: Selection) -> None:
        self._end_cycle(state)
        state.exhaustion_count += 1
        state.retry_at = (
            self._clock()
            + EXHAUSTION_RETRY_S[min(state.exhaustion_count, len(EXHAUSTION_RETRY_S)) - 1]
        )
        state.outcome = None

    def terminal(self, program: Program | None, connected: bool) -> Outcome | None:
        if program is None or (state := self._states.get(program)) is None:
            return None
        outcome = state.outcome
        if outcome is None:
            return None
        if outcome not in (Outcome.REJECTED, Outcome.EXHAUSTED):
            if program.playing:
                self._recover(state)
                return None
            if any(
                source.program is program
                and source not in self._retired
                and source.failure is None
                and (source.state is not State.ENDED or (connected and source.ready))
                for source in self._sources
            ):
                return None
        state.outcome = None
        self._end_cycle(state)
        return outcome

    async def _load(self, state: Selection, query: CatalogQuery, epoch: int) -> None:
        try:
            if state.cursor is None:
                state.cursor = self._catalog.open(query)
            result = CatalogPage.model_validate(await state.cursor.next_page())
            if state.epoch == epoch:
                state.result = result
        except CatalogError as error:
            outcome = METADATA_OUTCOMES[error.failure]
            if state.epoch == epoch:
                state.outcome = outcome
            log.warning(
                "Catalog selection failed category=%s failure=%s", error.category, error.failure
            )
        except Exception as error:  # noqa: BLE001 - never retain provider/validation details
            error.__traceback__ = error.__context__ = error.__cause__ = None
            if state.epoch == epoch:
                state.outcome = Outcome.FAILED
        finally:
            if state.task is asyncio.current_task():
                state.task = None
            self._wake()

    def generate(self, program: Program) -> None:
        if self._closed or not isinstance(program.plan, CatalogPlan):
            return
        state = self._state(program)
        if state.outcome is not None or self._clock() < state.retry_at:
            return
        if any(
            source.program is program
            and source.number != state.live_number
            and source not in self._retired
            and source.failure is None
            and (source.state is not State.ENDED or source.ready)
            for source in self._sources
        ):
            return
        if state.result is not None:
            page, state.result = state.result, None
            state.candidates.extend(page.tracks)
            state.exhausted = page.exhausted
        while state.candidates and state.attempted < MAX_CANDIDATES:
            track = state.candidates[0]
            if track.id in state.seen:
                state.candidates.popleft()
                continue
            if len(state.seen) >= 60:
                state.outcome = Outcome.FAILED
                return
            if (
                not track.eligible
                or self.history.excludes(track)
                or any(source.track.id == track.id for source in self._sources)
            ):
                state.candidates.popleft()
                state.seen.add(track.id)
                continue
            source = self._create(track, program)
            if source is None:
                return  # temporary admission refusal keeps this exact candidate and budget
            state.candidates.popleft()
            state.seen.add(track.id)
            state.attempted += 1
            self._sources.append(source)
            source.start()
            return
        if state.exhausted or state.attempted >= MAX_CANDIDATES:
            state.outcome = Outcome.NO_MATCH
        elif state.task is None:
            task = asyncio.create_task(
                self._load(state, program.plan.query, state.epoch), name="catalog selection"
            )
            state.task = task
            self._tasks[task] = state
            task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.pop(task, None)

    def ended_failures(self, program: Program | None) -> Sequence[Source]:
        failed = [
            source
            for source in self._sources
            if source.state is State.ENDED
            and source.failure is not None
            and source not in self._seen_failed
        ]
        self._seen_failed.update(failed)
        for source in failed:
            self.history.failed(source.track)
        return [source for source in failed if source.program is program]

    def count_failure(
        self, source: Source, program: Program | None, connected: bool
    ) -> Outcome | None:
        if program is None or source.program is not program:
            return None
        track = next((s for s in self._sources if s is source), None)
        if track is None:
            return None
        state = self._state(program)
        state.failures += 1
        state.retry_at = self._clock() + TRACK_RETRY_S[min(state.failures, 3) - 1]
        if state.attempted >= MAX_CANDIDATES:
            state.outcome = Outcome.UNAVAILABLE
            return self.terminal(program, connected)
        return None

    def drop_ended(self, program: Program | None, mixer: Mixer) -> None:
        kept = [
            source
            for source in self._sources
            if mixer.uses(source)
            or source.state is not State.ENDED
            or (source.program is program and source not in self._retired and source.ready)
        ]
        dropped = [source for source in self._sources if source not in kept]
        self._sources = kept
        for source in dropped:
            source.discard_pcm()

    def retire(self, program: Program | None, mixer: Mixer) -> None:
        for source in self._sources:
            if source.program is not program and not mixer.uses(source):
                self._retired.add(source)
                source.retire()

    def ready_source(self, program: Program, exclude: Source | None = None) -> Source | None:
        for source in self._sources:
            if source.program is not program or source is exclude or source in self._retired:
                continue
            state = self._states.get(program)
            if (state is None or source.number != state.live_number) and self.history.excludes(
                source.track
            ):
                self._retired.add(source)
                source.retire()
                continue
            if source.ready:
                return source
        return None

    @staticmethod
    def handover_due(source: Source) -> bool:
        return source.completed or (source.failed and not source.frames)

    def going_live(self, source: Source) -> None:
        track = next((s for s in self._sources if s is source), None)
        if track is None or (program := track.program) is None:
            return
        state = self._state(program)
        if state.live_number == source.number:
            return
        self.history.heard(track.track)
        for previous in self._sources:
            if previous is not track and previous.number == state.live_number:
                self._retired.add(previous)
                previous.retire()
        self._end_cycle(state)
        state.live_number = source.number
        state.failures = state.exhaustion_count = 0
        state.retry_at = 0

    async def close(self) -> None:
        self._closed = True
        if self._cancel_admission_wait is not None:
            self._cancel_admission_wait()
        for state in self._owned_states():
            self._end_cycle(state)
        for source in self._sources:
            source.retire()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.gather(*(source.wait_ended() for source in self._sources))
        for source in self._sources:
            source.discard_pcm()
        self._sources.clear()
