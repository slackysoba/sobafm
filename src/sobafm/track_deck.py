"""A bounded catalog producer; production completion is distinct from audible drain."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sobafm.audio_budget import AudioBytes, AudioLease, AudioLedger
from sobafm.catalog import CatalogTrack
from sobafm.catalog_audio import (
    AudioStream,
    TrackError,
    TrackFailure,
    audio_session,
)
from sobafm.decoder_process import Boundary, Child
from sobafm.native import VerifiedNativeRuntime
from sobafm.pcm import BYTES_PER_SECOND, FRAME_BYTES, FRAME_SECONDS, FrameSplitter
from sobafm.program import Mode, Program
from sobafm.source import State, next_source_number

log = logging.getLogger(__name__)
PREROLL_SECONDS = 6
MAX_BUFFER_FRAMES = 3000
MAX_SAMPLES = 43_200_000
LIFETIME_SECONDS = 1830.0
PROGRESS_SECONDS = 15.0
CLEANUP_SECONDS = 3.0
PIPE_ENVELOPE = 256 * 1024
DIAGNOSTIC_LIMIT = 4096


class EndReason(StrEnum):
    FINISHED = "finished"
    RETIRED = "retired"
    FAILED = "failed"


class Input(Protocol):
    @property
    def complete(self) -> bool: ...
    @property
    def closed(self) -> bool: ...
    def chunks(self) -> AsyncGenerator[bytes]: ...
    async def aclose(self) -> None: ...


class Process(Protocol):
    def bind(self) -> None: ...
    async def write(self, chunk: bytes) -> None: ...
    async def close_input(self) -> None: ...
    async def read(self, pipe: int, size: int) -> bytes: ...
    async def wait(self) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def empty(self) -> bool: ...
    def close_pipes(self) -> bool: ...


@dataclass(frozen=True)
class Envelope:
    """Caller-supplied audited whole-allocation bounds, never a runtime measurement.

    No production envelope is supplied here. None refuses admission. In particular,
    neither 128MiB containment nor current deque length supplies native headroom.
    PCM includes splitter/temporary/player copies beyond the configured deque;
    other includes HTTP, all three pipe transports and encoded/in-flight copies.
    #175 must qualify each actual runtime/platform envelope before production use.
    """

    allocation: AudioBytes | None = None
    buffer_frames: int = MAX_BUFFER_FRAMES

    def __post_init__(self) -> None:
        if type(self.buffer_frames) is not int or not 1 <= self.buffer_frames <= MAX_BUFFER_FRAMES:
            raise ValueError("catalog frame capacity outside accepted bounds")
        if self.allocation is not None and (
            self.allocation.pcm < (self.buffer_frames + 8) * FRAME_BYTES
            or self.allocation.other < 5 * PIPE_ENVELOPE + DIAGNOSTIC_LIMIT
        ):
            raise ValueError("envelope omits retained PCM/copy or transport reservations")


class Slots:
    """Process-wide child owners, including pending starts and incomplete cleanup."""

    def __init__(self) -> None:
        self._owners: dict[object, AudioLedger] = {}

    def acquire(self, ledger: AudioLedger) -> object | None:
        if len(self._owners) >= 4 or sum(owner is ledger for owner in self._owners.values()) >= 2:
            return None
        token = object()
        self._owners[token] = ledger
        return token

    def release(self, token: object) -> None:
        self._owners.pop(token, None)

    @property
    def count(self) -> int:
        return len(self._owners)


_PROCESS_SLOTS = Slots()


class DecoderSupervisor:
    """One bot's cleanup registry, independent of station lifetime.

    It retains unfinished cleanup tasks; removing/replacing a station cannot
    clear slots, leases or resources. Default production admission is unavailable.
    """

    def __init__(
        self,
        runtime: VerifiedNativeRuntime,
        boundary: Callable[[], Boundary],
        *,
        envelope: Envelope | None = None,
        slots: Slots = _PROCESS_SLOTS,
        spawn: Callable[[VerifiedNativeRuntime, Boundary], Awaitable[Process]] = Child.launch,
    ) -> None:
        self.runtime, self.boundary = runtime, boundary
        self.envelope = envelope if envelope is not None else Envelope()
        self.slots, self.spawn = slots, spawn
        self.cleanups: set[asyncio.Task[None]] = set()
        self._debts: dict[object, TrackDeck] = {}

    def retain(self, task: asyncio.Task[None]) -> None:
        self.cleanups.add(task)
        task.add_done_callback(self.cleanups.discard)

    def register(self, token: object, source: TrackDeck) -> None:
        self._debts[token] = source

    def released(self, token: object) -> None:
        self.slots.release(token)
        self._debts.pop(token, None)

    @property
    def debt_count(self) -> int:
        return len(self._debts)


def _input(track: CatalogTrack) -> Input:
    return AudioStream(audio_session(), track)


class TrackDeck:
    def __init__(
        self,
        track: CatalogTrack,
        program: Program,
        audio: AudioLedger,
        supervisor: DecoderSupervisor,
        *,
        input_factory: Callable[[CatalogTrack], Input] = _input,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.track = CatalogTrack.model_validate(track)
        if not self.track.eligible or program.mode is not Mode.CATALOG:
            raise TrackError(TrackFailure.INELIGIBLE)
        self.program = program
        self.credits = self.track.credits
        self.number = next_source_number()
        self.frames: deque[bytes] = deque()
        self.state = State.CONNECTING
        self.end_reason: EndReason | None = None
        self.failure: TrackFailure | None = None
        self.exit_status: int | None = None
        self.cleanup_failed = False
        self.allocation: AudioLease | None = None
        self._audio, self._supervisor = audio, supervisor
        self._input_factory, self._clock = input_factory, clock
        self._created = clock()
        self._started_at = self._created
        self._splitter = FrameSplitter()
        self._raw_bytes = 0
        self._encoded_bytes = 0
        self._stderr_bytes = 0
        self._eof = {1: False, 2: False}
        self._locks = {1: asyncio.Lock(), 2: asyncio.Lock()}
        self._space = asyncio.Event()
        self._retired = False
        self._verified = False
        self._backpressure = False
        self._last_input = self._last_output = self._last_terminal = clock()
        self._task: asyncio.Task[None] | None = None
        self._spawn_task: asyncio.Task[Process] | None = None
        self._workers: list[asyncio.Task[object]] = []
        self._child: Process | None = None
        self._input: Input | None = None
        self._boundary: Boundary | None = None
        self._slot: object | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._cleanup_at: float | None = None
        self._cleanup_result: asyncio.Future[bool] | None = None
        self._cleanup_timer: asyncio.TimerHandle | None = None

    @property
    def mode(self) -> Mode:
        return Mode.CATALOG

    @property
    def title(self) -> str:
        return self.track.title

    @property
    def buffered_seconds(self) -> float:
        return len(self.frames) * FRAME_SECONDS

    @property
    def ready(self) -> bool:
        return (
            self.failure is None
            and bool(self.frames)
            and (self.buffered_seconds >= PREROLL_SECONDS or self.end_reason is EndReason.FINISHED)
        )

    @property
    def completed(self) -> bool:
        return self.end_reason is EndReason.FINISHED

    @property
    def drained(self) -> bool:
        """Not an encoder-storage release proof or an automatic lease refund."""
        return self.completed and not self.frames

    def start(self) -> None:
        if self._retired or self._task is not None or self.end_reason is not None:
            return
        # Until six seconds, nothing may consume this source. A smaller buffer
        # must hold every duration-valid short output (including the one-second allowance).
        needed = min(300, math.ceil((self.track.duration + 1) / FRAME_SECONDS))
        if self._supervisor.envelope.buffer_frames < needed:
            self.failure, self.end_reason, self.state = (
                TrackFailure.CAPACITY,
                EndReason.FAILED,
                State.ENDED,
            )
            return
        envelope = self._supervisor.envelope.allocation
        if envelope is None or (lease := self._audio.reserve(envelope)) is None:
            self.failure, self.end_reason, self.state = (
                TrackFailure.CAPACITY,
                EndReason.FAILED,
                State.ENDED,
            )
            return
        slot = self._supervisor.slots.acquire(self._audio)
        if slot is None:
            lease.release()  # no allocation/producer was created
            self.failure, self.end_reason, self.state = (
                TrackFailure.CAPACITY,
                EndReason.FAILED,
                State.ENDED,
            )
            return
        self.allocation, self._slot = lease, slot
        self._supervisor.register(slot, self)
        self._cleanup_result = asyncio.get_running_loop().create_future()
        self._started_at = self._clock()
        self._task = asyncio.create_task(self._run(), name=f"track {self.number}")

    def retire(self) -> None:
        if self._retired:
            return
        self._retired = True
        if self._task is None:
            if self.end_reason is None:
                self.end_reason = EndReason.RETIRED
            self.state = State.ENDED
            return
        self._cleanup_at = self._cleanup_at if self._cleanup_at is not None else self._clock()
        self._space.set()
        if self._cleanup_task is None:
            self._begin_cleanup()
            self._task.cancel()

    async def wait_ended(self) -> None:
        if self._task is not None:
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                if not self._task.cancelled():
                    raise
            if self._cleanup_result is not None:
                await asyncio.shield(self._cleanup_result)

    async def regulate(self) -> None:
        if len(self.frames) < self._supervisor.envelope.buffer_frames:
            self._space.set()

    async def _room(self) -> None:
        while len(self.frames) >= self._supervisor.envelope.buffer_frames:
            self._backpressure = True
            self._space.clear()
            await self._space.wait()
        if self._backpressure:
            self._last_input = self._last_output = self._clock()
            self._backpressure = False

    async def _feed(self, child: Process, source: Input) -> None:
        iterator = source.chunks()
        try:
            async for chunk in iterator:
                if (
                    not 0 < len(chunk) <= 16 * 1024
                    or self._encoded_bytes + len(chunk) > 16 * 1024**2
                ):
                    raise TrackError(TrackFailure.ENCODED_LIMIT)
                self._encoded_bytes += len(chunk)
                await child.write(chunk)
                self._last_input = self._clock()
            if not source.complete or self._encoded_bytes == 0:
                raise TrackError(TrackFailure.TRANSFER)
            await child.close_input()
        finally:
            await iterator.aclose()

    async def _output(self, child: Process) -> None:
        while True:
            async with self._locks[1]:
                chunk = await child.read(1, FRAME_BYTES)
            if not chunk:
                self._eof[1] = True
                return
            self._last_output = self._clock()
            if len(chunk) > FRAME_BYTES or self._raw_bytes + len(chunk) > MAX_SAMPLES * 4:
                raise TrackError(TrackFailure.OUTPUT)
            self._raw_bytes += len(chunk)
            if self._raw_bytes > (self.track.duration + 1) * BYTES_PER_SECOND:
                raise TrackError(TrackFailure.DURATION)
            frames = self._splitter.split(chunk)
            if frames:
                await self._room()
                self.frames.extend(frames)

    async def _diagnostics(self, child: Process) -> None:
        while True:
            async with self._locks[2]:
                chunk = await child.read(2, DIAGNOSTIC_LIMIT)
            if not chunk:
                self._eof[2] = True
                return
            # Count a capped diagnostic prefix, but keep no untrusted diagnostic
            # content. The separately reserved transport is still drained to EOF.
            self._stderr_bytes = min(DIAGNOSTIC_LIMIT, self._stderr_bytes + len(chunk))
            self._last_terminal = self._clock()
            await asyncio.sleep(0)

    async def _monitor(
        self,
        feed: asyncio.Task[object],
        output: asyncio.Task[object],
        diagnostics: asyncio.Task[object],
        exited: asyncio.Task[object],
    ) -> None:
        started = self._started_at
        while not all(task.done() for task in (feed, output, diagnostics, exited)):
            now = self._clock()
            if self._raw_bytes == 0 and now - started >= PROGRESS_SECONDS:
                raise TrackError(TrackFailure.DEADLINE)
            if not self._backpressure and (
                (not feed.done() and now - self._last_input >= PROGRESS_SECONDS)
                or (not output.done() and now - self._last_output >= PROGRESS_SECONDS)
            ):
                raise TrackError(TrackFailure.DEADLINE)
            if (
                feed.done()
                and output.done()
                and now - max(self._last_output, self._last_terminal) >= PROGRESS_SECONDS
            ):
                raise TrackError(TrackFailure.DEADLINE)
            await asyncio.sleep(0.05)

    async def _run(self) -> None:
        try:
            self._boundary = self._supervisor.boundary()
            self._spawn_task = asyncio.create_task(self._spawn(self._boundary))
            # Pending spawn is already charged. Caller cancellation cannot lose a
            # child materializing after cancellation; cleanup retains this task.
            child = await asyncio.wait_for(asyncio.shield(self._spawn_task), PROGRESS_SECONDS)
            self._child = child
            child.bind()
            source = self._input = self._input_factory(self.track)
            self.state = State.GENERATING
            self._last_input = self._last_output = self._clock()
            feed = asyncio.create_task(self._feed(child, source))
            output = asyncio.create_task(self._output(child))
            diagnostics = asyncio.create_task(self._diagnostics(child))
            exited = asyncio.create_task(child.wait())
            monitor = asyncio.create_task(self._monitor(feed, output, diagnostics, exited))
            self._workers = [feed, output, diagnostics, exited, monitor]
            # Reserve the cleanup interval inside the absolute lifetime ceiling.
            remaining = LIFETIME_SECONDS - CLEANUP_SECONDS - (self._clock() - self._started_at)
            async with asyncio.timeout(max(0, remaining)):
                results = await asyncio.gather(*self._workers)
                if type(results[3]) is int:
                    self.exit_status = results[3]
                if results[3] != 0 or not all(self._eof.values()) or not source.complete:
                    raise TrackError(TrackFailure.DECODER)
                if not self._raw_bytes or self._raw_bytes % 4:
                    raise TrackError(TrackFailure.OUTPUT)
                if abs(self._raw_bytes / BYTES_PER_SECOND - self.track.duration) > 1:
                    raise TrackError(TrackFailure.DURATION)
                if tail := self._raw_bytes % FRAME_BYTES:
                    padded = FRAME_BYTES - tail
                    if self._raw_bytes + padded > MAX_SAMPLES * 4:
                        raise TrackError(TrackFailure.OUTPUT)
                    await self._room()
                    self.frames.extend(self._splitter.split(bytes(padded)))
                self._verified = True
        except asyncio.CancelledError:
            self._retired = True
        except TrackError as error:
            self.failure = error.category
            error.__traceback__ = error.__context__ = error.__cause__ = None
        except TimeoutError:
            self.failure = TrackFailure.DEADLINE
        except Exception as error:  # noqa: BLE001 - one native/transport failure must not escape its source
            self.failure = TrackFailure.DECODER
            error.__traceback__ = error.__context__ = error.__cause__ = None
        finally:
            self._begin_cleanup()
            result = self._cleanup_result
            if result is not None and not await asyncio.shield(result):
                self.failure = TrackFailure.CLEANUP
            if not self._verified and not self._retired and self.failure is None:
                self.failure = TrackFailure.DECODER
            self.end_reason = (
                EndReason.FAILED
                if self.failure
                else (EndReason.RETIRED if self._retired else EndReason.FINISHED)
            )
            if self.exit_status is not None:
                log.info("Track %d decoder exit status %d", self.number, self.exit_status)
            log.info(
                "Deck %d %s%s after %.0f s: %.0f s of audio%s",
                self.number,
                self.end_reason,
                f" ({self.failure})" if self.failure else "",
                self._clock() - self._created,
                self._raw_bytes / BYTES_PER_SECOND,
                "",
            )

    def _begin_cleanup(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_at = self._cleanup_at if self._cleanup_at is not None else self._clock()
            self._cleanup_timer = asyncio.get_running_loop().call_later(
                max(0, self._cleanup_at + CLEANUP_SECONDS - self._clock()), self._cleanup_failure
            )
            task = self._cleanup_task = asyncio.create_task(
                self._cleanup(), name=f"track {self.number} cleanup"
            )
            self._supervisor.retain(task)

    def _cleanup_failure(self) -> None:
        self.cleanup_failed = True
        self.failure, self.end_reason = TrackFailure.CLEANUP, EndReason.FAILED
        decision = self._cleanup_result
        if decision is not None and not decision.done():
            decision.set_result(False)

    async def _discard(self, child: Process, pipe: int) -> None:
        async with self._locks[pipe]:
            while await child.read(pipe, FRAME_BYTES):  # noqa: ASYNC110 - yield bounded drain work fairly
                await asyncio.sleep(0)
            self._eof[pipe] = True

    @staticmethod
    async def _join(tasks: list[asyncio.Task[object]]) -> None:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for value in results:
            if isinstance(value, BaseException):
                value.__traceback__ = value.__context__ = value.__cause__ = None

    def _attempt(self, action: Callable[[], bool | None]) -> bool:
        try:
            result = action()
            return result is not False
        except Exception as error:  # noqa: BLE001 - uncertainty retains charged ownership
            error.__traceback__ = error.__context__ = error.__cause__ = None
            self._cleanup_failure()
            return False

    async def _cleanup(self) -> None:
        deadline = (
            self._cleanup_at if self._cleanup_at is not None else self._clock()
        ) + CLEANUP_SECONDS
        decision = self._cleanup_result
        for task in self._workers:
            task.cancel()
        if self._spawn_task is not None:
            while not self._spawn_task.done():
                self._deadline(deadline)
                await asyncio.sleep(0.01)
            if self._spawn_task.cancelled():
                # An externally cancelled launch cannot prove no child appeared.
                self._cleanup_failure()
                await asyncio.Future[None]()
            else:
                try:
                    self._child = self._spawn_task.result()
                except Exception as error:  # noqa: BLE001 - failed launch still owns its prepared boundary
                    error.__traceback__ = error.__context__ = error.__cause__ = None
        child = self._child
        cleanup: list[asyncio.Task[object]] = []
        if child is not None:
            cleanup = [asyncio.create_task(self._discard(child, pipe)) for pipe in (1, 2)]
            cleanup.extend(
                [
                    asyncio.create_task(child.close_input()),
                    asyncio.create_task(self._join(self._workers)),
                ]
            )
            if self._input is not None:
                cleanup.append(asyncio.create_task(self._input.aclose()))
            # Start drains before waiting for joins/exit, including inherited pipes.
            self._attempt(child.terminate)
            reap = asyncio.create_task(child.wait())
            cleanup.append(reap)
            await asyncio.wait([reap], timeout=max(0, min(1, deadline - self._clock())))
            if not reap.done() or not self._attempt(child.empty):
                self._attempt(child.kill)
            kill_deadline = min(deadline, self._clock() + 1)
            last_kill = self._clock()
            while True:
                self._deadline(deadline)
                if not reap.done() and self._clock() >= kill_deadline:
                    self._cleanup_failure()
                tasks_done = all(task.done() for task in cleanup)
                if tasks_done:
                    if not reap.cancelled() and reap.exception() is None:
                        self.exit_status = reap.result()
                    for task in cleanup:
                        try:
                            task.result()
                        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - retrieve owned task failures, including cancellation
                            error.__traceback__ = error.__context__ = error.__cause__ = None
                            self._cleanup_failure()
                empty = self._attempt(child.empty)
                if not empty and self._clock() - last_kill >= 0.1:
                    self._attempt(child.kill)
                    last_kill = self._clock()
                pipes_closed = empty and self._attempt(child.close_pipes)
                http_closed = self._input is None or self._input.closed
                if tasks_done and empty and pipes_closed and http_closed:
                    break
                await asyncio.sleep(0.01)
        while self._boundary is not None and not self._attempt(self._boundary.close):
            self._deadline(deadline)
            await asyncio.sleep(0.01)
        if self._slot is not None:
            self._supervisor.released(self._slot)
            self._slot = None
        if self._spawn_task is None and self.allocation is not None:
            self.allocation.release()  # retired before a producer/allocation was created
            self.allocation = None
        self._deadline(deadline)
        if self._cleanup_timer is not None:
            self._cleanup_timer.cancel()
        self.state = State.ENDED
        if self._retired and self.end_reason is None:
            self.end_reason = EndReason.RETIRED
        if decision is not None and not decision.done():
            decision.set_result(not self.cleanup_failed)
        # PCM remains charged: deque pops/producer cleanup do not prove release
        # of mixer/encoder copies. Its owner needs that separate storage proof.

    def _deadline(self, deadline: float) -> None:
        if self._clock() >= deadline:
            self._cleanup_failure()

    async def _spawn(self, boundary: Boundary) -> Process:
        return await self._supervisor.spawn(self._supervisor.runtime, boundary)
