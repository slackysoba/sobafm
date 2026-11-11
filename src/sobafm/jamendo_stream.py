"""Owned checked Jamendo streaming into an already leased bank; no station/model calls."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections.abc import Buffer, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sobafm.credits import RenderedCredits, render_credits
from sobafm.decode import DecodeProducer, DecoderCommand, DecodeReport, VerifiedStreamInput
from sobafm.jamendo_admission import Candidate, Policy
from sobafm.jamendo_catalog import Refusal, refresh, refresh_request
from sobafm.jamendo_journal import Attempt, Cleanup, Facts, Journal, JournalError, Outcome
from sobafm.jamendo_transport import (
    Kind,
    Locator,
    Report,
    StationTransport,
    StreamLimits,
    TransportError,
)
from sobafm.pcm import PcmBank, PcmFacts
from sobafm.stream_checks import Envelope, PcmScreen, StreamCheckError

HANDOFF_BYTES = 16_384


class Refresh(Protocol):
    async def __call__(self, candidate: Candidate) -> Locator | Refusal: ...


class AudioInput(Protocol):
    """The concrete owned transport surface; synthetic schedules implement the same facts."""

    @property
    def ready_deadline(self) -> float: ...

    @property
    def stopped(self) -> asyncio.Event: ...

    @property
    def transfer_complete(self) -> bool: ...

    @property
    def report(self) -> Report: ...

    async def read(self) -> bytes: ...

    def cancel(self, first_stop: float | None = None) -> None: ...

    async def close(self) -> None: ...


class AudioOpener(Protocol):
    async def __call__(
        self, locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
    ) -> AudioInput: ...


class RefreshCandidate:
    """Use the merged by-ID refresh exactly once; old decisions never authorize open."""

    def __init__(self, transport: StationTransport, policy: Policy) -> None:
        self.transport = transport
        self.policy = policy

    async def __call__(self, candidate: Candidate) -> Locator | Refusal:
        request = refresh_request(candidate.recording.key, self.policy)
        if isinstance(request, Refusal):
            return request
        payload = await self.transport.fetch(request)
        result = refresh(request, payload, candidate, self.policy, datetime.now(UTC))
        if isinstance(result, Refusal):
            return result
        report = self.transport.last_report
        if report is None or report.completed_at is None:
            raise TransportError(Kind.STALE)
        return Locator(result, report.completed_at)


@dataclass(frozen=True)
class Prepared:
    candidate: Candidate
    credits: RenderedCredits
    attempt: int


@dataclass(frozen=True)
class Completion:
    prepared: Prepared | None
    transfer_complete: bool
    envelope_complete: bool
    decoder: DecodeReport | None
    pcm: PcmFacts
    encoded_hash: str
    encoded_bytes: int
    decoded_bytes: int
    failure: Outcome | None
    refusal: Refusal | None
    transport: Report | None
    cleanup_started_at: float | None
    cleanup: Cleanup
    io_settled: bool
    settled: bool


class _Bridge:
    """One bounded handoff, one async writer and the existing decoder feeder."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._condition = threading.Condition()
        self._space = asyncio.Event()
        self._space.set()
        self._data = bytearray(HANDOFF_BYTES)
        self._start = self._end = 0
        self._eof = self._stopped = False

    async def put(self, data: memoryview) -> None:
        offset = 0
        while offset < len(data):
            await self._space.wait()
            with self._condition:
                if self._stopped:
                    raise OSError("stream stopped")
                if self._start != self._end:
                    self._space.clear()
                    continue
                count = min(HANDOFF_BYTES, len(data) - offset)
                self._data[:count] = data[offset : offset + count]
                self._start, self._end = 0, count
                self._space.clear()
                self._condition.notify_all()
            offset += count

    def readinto(self, buffer: Buffer, /) -> int:
        with memoryview(buffer).cast("B") as target, self._condition:
            self._condition.wait_for(lambda: self._stopped or self._eof or self._end > self._start)
            if self._stopped:
                raise OSError("stream stopped")
            count = min(len(target), self._end - self._start)
            with memoryview(self._data) as source:
                target[:count] = source[self._start : self._start + count]
            self._start += count
            if self._start == self._end:
                self._loop.call_soon_threadsafe(self._space.set)
            return count

    def end(self) -> None:
        with self._condition:
            self._eof = True
            self._condition.notify_all()

    def close(self) -> None:
        self.stop()

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        self._loop.call_soon_threadsafe(self._space.set)

    def release(self) -> None:
        """Only after both reader and writer have actually joined."""
        with self._condition:
            self._data.clear()


class JamendoStream:
    """Own preparation, one transfer and one decoder; caller retains the bank lease.

    Construct only when prefetch is eligible (<=30s remaining, skip or drain),
    then call ``prepare``/``run``. On retirement cancel, await ``run``, release the
    bank and await ``release``. The journal pin follows that final bank release.
    No automatic reopening, retries, lease acquisition or source switching occurs.
    """

    def __init__(
        self,
        candidate: Candidate,
        bank: PcmBank,
        *,
        refresh_candidate: Refresh,
        journal: Journal,
        open_audio: AudioOpener,
        command: DecoderCommand | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        bank.hold_readiness()
        self.bank = bank
        self._ranked = candidate
        self._refresh = refresh_candidate
        self._journal = journal
        self._opener = open_audio
        self._command = command
        self._clock = clock
        self._loop = asyncio.get_running_loop()
        self._changed = asyncio.Event()
        self._bridge = _Bridge()
        self._audio: AudioInput | None = None
        self._decode: DecodeProducer | None = None
        self._decode_report: DecodeReport | None = None
        self._attempt: Attempt | None = None
        self._prepared: Prepared | None = None
        self._snapshot: Prepared | None = None
        self._failure: Outcome | None = None
        self._refusal: Refusal | None = None
        self._conflict: str | None = None
        self._first_stop: float | None = None
        self._cleanup = Cleanup.PENDING
        self._io_settled = False
        self._released = False
        self._envelope_complete = False
        self._hash = hashlib.sha256()
        self._encoded = 0
        self._decoded = 0
        self._announced = False
        self._task: asyncio.Task[Completion] | None = None
        self._preparing: asyncio.Task[Locator | Refusal] | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._work: asyncio.Task[DecodeReport] | None = None
        self._opening: asyncio.Task[AudioInput] | None = None
        self._http_close: asyncio.Task[None] | None = None
        self._http_timer: asyncio.TimerHandle | None = None
        self._cleanup_timer: asyncio.TimerHandle | None = None
        self._conflict_write: asyncio.Task[None] | None = None
        self._io_timer: asyncio.TimerHandle | None = None

    def _start(self) -> asyncio.Task[Completion]:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        return self._task

    async def prepare(self) -> Prepared | None:
        work = self._start()
        try:
            while self._prepared is None and not work.done() and self._failure is None:
                self._changed.clear()
                await self._changed.wait()
        except asyncio.CancelledError:
            self.cancel()
            await _join(work)
            raise
        return self._prepared if self._failure is None else None

    async def run(self) -> Completion:
        work = self._start()
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            self.cancel()
            await _join(work)
            raise

    def cancel(self) -> None:
        self._stop(Outcome.CANCELLED)

    def _stop(self, failure: Outcome, at: float | None = None) -> None:
        now = self._clock() if at is None else at
        bank_stop = self.bank.facts().cancelled_at
        if bank_stop is not None:
            now = min(now, bank_stop)
        if self._audio is not None:
            report = self._audio.report
            if report.stopped_at is not None and (
                report.kind is not None or report.cleanup_expired
            ):
                now = min(now, report.stopped_at)
        if self._decode is not None:
            decoder = self._decode.report()
            if decoder.cleanup_started_at is not None and (
                decoder.failure is not None or decoder.cancelled_at is not None
            ):
                now = min(now, decoder.cleanup_started_at)
        self._first_stop = now if self._first_stop is None else min(now, self._first_stop)
        if self._cleanup_timer is not None:
            self._cleanup_timer.cancel()
        self._cleanup_timer = self._loop.call_at(self._first_stop + 3, self._expire_cleanup)
        if self._failure is None:
            self._failure = failure
        self.bank.cancel()
        self._bridge.stop()
        if self._decode is not None:
            self._decode.cancel(self._first_stop)
        if self._audio is not None:
            report = self._audio.report
            if not (
                self._audio.transfer_complete
                and report.settled_at is not None
                and report.kind is None
            ):
                self._audio.cancel(self._first_stop)
        if self._preparing is not None:
            self._preparing.cancel()
        if self._opening is not None:
            self._opening.cancel()
        if self._pump_task is not None:
            self._pump_task.cancel()
        self._changed.set()

    def _expire_cleanup(self) -> None:
        if not self._released:
            self._cleanup = Cleanup.EXPIRED

    def _expire_io(self, first: float) -> None:
        if not self._io_settled:
            self._cleanup = Cleanup.EXPIRED
            self._stop(Outcome.INTEGRITY, first)

    def _check_failure(self, error: StreamCheckError) -> None:
        if self._failure in (None, Outcome.INTEGRITY):
            self._failure = error.outcome
        if error.digest is not None:
            self._conflict = error.digest
            self._failure = error.outcome
        self._stop(error.outcome)
        if (
            self._conflict is not None
            and self._attempt is not None
            and self._conflict_write is None
        ):
            self._conflict_write = asyncio.create_task(self._commit(settled=False))
            self._conflict_write.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )

    def _progress(self, decoded_bytes: int) -> None:
        self._decoded = decoded_bytes
        if self.bank.ready_now() and not self._announced:
            self._announced = True
            self._loop.call_soon_threadsafe(self._announce)

    def _announce(self) -> None:
        if self._failure is None and self.bank.facts().cancelled_at is None:
            self._prepared = self._snapshot
        self._changed.set()

    async def _pump(self, envelope: Envelope, *, short: bool) -> None:
        audio = self._audio
        assert audio is not None
        while True:
            chunk = await audio.read()
            if not chunk:
                envelope.finish()
                self._envelope_complete = True
                self._bridge.end()
                return
            self._hash.update(chunk)
            self._encoded += len(chunk)
            with memoryview(chunk) as data:
                for checked in envelope.feed(data):
                    if envelope.leading_complete and not short:
                        self.bank.allow_readiness()
                    await self._bridge.put(checked)

    async def _run(self) -> Completion:
        timer: asyncio.TimerHandle | None = None
        wall: asyncio.TimerHandle | None = None
        stopped: asyncio.Task[bool] | None = None
        try:
            if self._failure is not None:
                raise asyncio.CancelledError
            self._preparing = asyncio.create_task(self._refresh(self._ranked))
            locator = await self._preparing
            self._preparing = None
            if self._failure is not None:
                raise asyncio.CancelledError
            if isinstance(locator, Refusal):
                self._refusal = locator
                self._stop(Outcome.RIGHTS)
                raise asyncio.CancelledError
            candidate = locator.refreshed.candidate
            credits = render_credits(candidate.recording)
            self._attempt = await self._journal.reserve_attempt(candidate)
            if self._failure is not None:
                raise asyncio.CancelledError
            self._snapshot = Prepared(candidate, credits, self._attempt.number)
            duration = candidate.recording.duration_seconds or 0
            screen = PcmScreen(duration)
            limits = StreamLimits.for_duration(duration)
            self._opening = asyncio.create_task(
                self._opener(locator, attempt=self._attempt, bank=self.bank)
            )
            self._audio = await self._opening
            self._opening = None
            if self._failure is not None:
                self._audio.cancel(self._first_stop)
                raise asyncio.CancelledError
            envelope = Envelope(candidate.recording)

            def check(action: Callable[[], None]) -> None:
                try:
                    action()
                except StreamCheckError as exc:
                    self._loop.call_soon_threadsafe(self._check_failure, exc)
                    raise

            source = VerifiedStreamInput(
                self._bridge,
                limits.encoded_bytes,
                screen.limit,
                lambda frame: check(lambda: screen.add(frame)),
                lambda count: check(lambda: screen.finish(count)),
                self._bridge.stop,
                self._progress,
            )
            self._decode = DecodeProducer(
                source, bank=self.bank, command=self._command, clock=self._clock
            )
            self._work = asyncio.create_task(asyncio.to_thread(_decode, self._decode))
            self._pump_task = asyncio.create_task(self._pump(envelope, short=duration < 6))
            timer = self._loop.call_at(self._audio.ready_deadline, self._ready_timeout)
            # HTTP's watchdog ends at transfer EOF; the whole-stream deadline does not.
            opened = self._audio.ready_deadline - 15
            wall = self._loop.call_at(opened + limits.wall_seconds, self._stop, Outcome.TRANSPORT)
            stopped = asyncio.create_task(self._audio.stopped.wait())
            await self._supervise(stopped)
            assert self._work is not None
            self._decode_report = await asyncio.shield(self._work)
            assert self._decode_report is not None
            if self._decode_report.failure is not None and self._failure is None:
                self._stop(Outcome.INTEGRITY, self._decode_report.cleanup_started_at)
            if self._failure is None:
                self.bank.allow_readiness()
                self._announce()
        except StreamCheckError as exc:
            self._check_failure(exc)
        except TransportError:
            self._stop(Outcome.TRANSPORT)
        except JournalError:
            self._stop(Outcome.JOURNAL)
        except asyncio.CancelledError:
            if self._failure is None:
                self._stop(Outcome.CANCELLED)
        except Exception:
            self._stop(Outcome.INTEGRITY)
        finally:
            for handle in (timer, wall):
                if handle is not None:
                    handle.cancel()
            if stopped is not None:
                stopped.cancel()
            await self._finish()
            self._changed.set()
        return self.report()

    def _ready_timeout(self) -> None:
        if not self.bank.ready_now():
            self._stop(Outcome.TRANSPORT)

    async def _supervise(self, stopped: asyncio.Task[bool]) -> None:
        pump, work, audio = self._pump_task, self._work, self._audio
        assert pump is not None and work is not None and audio is not None
        pending: set[asyncio.Task[object]] = {pump, work, stopped}
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if stopped in done:
                if not audio.transfer_complete or audio.report.kind is not None:
                    self._stop(Outcome.TRANSPORT, audio.report.stopped_at)
                self._http_close = asyncio.create_task(audio.close())
                pending.add(self._http_close)
                at = audio.report.stopped_at
                if at is not None:
                    self._http_timer = self._loop.call_at(at + 3, self._http_deadline)
            if self._http_close in done and (
                audio.report.kind is not None or audio.report.cleanup_expired
            ):
                self._stop(Outcome.TRANSPORT, audio.report.stopped_at)
            if work in done:
                self._decode_report = work.result()
                if not pump.done() or self._decode_report.failure is not None:
                    self._stop(Outcome.INTEGRITY, self._decode_report.cleanup_started_at)
            if pump.done() and work.done():
                return

    def _http_deadline(self) -> None:
        audio = self._audio
        if audio is not None and audio.report.settled_at is None:
            self._cleanup = Cleanup.EXPIRED
            self._stop(Outcome.TRANSPORT, audio.report.stopped_at)

    async def _finish(self) -> None:
        # Every task remains owned through repeated cancellation, even after +3s failure.
        if self._pump_task is not None:
            with suppress(asyncio.CancelledError, OSError, StreamCheckError, TransportError):
                await _join(self._pump_task)
        if self._work is not None:
            self._decode_report = await _join(self._work)
        if self._decode_report is not None and self._decode_report.cleanup_started_at is not None:
            first = self._decode_report.cleanup_started_at
            self._io_timer = self._loop.call_at(first + 3, self._expire_io, first)
        if self._audio is not None:
            try:
                if self._http_close is not None:
                    await _join(self._http_close)
                await self._audio.close()
            except JournalError:
                self._stop(Outcome.JOURNAL, self._audio.report.stopped_at)
            if self._audio.report.cleanup_expired:
                self._cleanup = Cleanup.EXPIRED
                self._stop(Outcome.TRANSPORT, self._audio.report.stopped_at)
        if self._http_timer is not None:
            self._http_timer.cancel()
        if self._decode_report is not None and self._decode_report.cleanup == "expired":
            self._cleanup = Cleanup.EXPIRED
            self._stop(Outcome.INTEGRITY, self._decode_report.cleanup_started_at)
        if self._first_stop is not None and self._clock() > self._first_stop + 3:
            self._cleanup = Cleanup.EXPIRED
        if self._cleanup != Cleanup.EXPIRED:
            self._cleanup = Cleanup.RELEASED
        self._bridge.release()
        self._pump_task = None
        self._work = None
        self._opening = None
        self._preparing = None
        if self._conflict_write is not None:
            try:
                await _join(self._conflict_write)
            except JournalError:
                self._stop(Outcome.JOURNAL)
        if self._attempt is not None:
            try:
                recorded = (self._cleanup, self._failure)
                await self._commit(settled=False)
                if recorded != (self._cleanup, self._failure):
                    # A delayed owned terminal write may cross its original
                    # cleanup deadline. Preserve that failed verdict durably.
                    await self._commit(settled=False)
            except JournalError:
                self._stop(Outcome.JOURNAL)
        self._io_settled = True
        if self._io_timer is not None:
            self._io_timer.cancel()

    async def _commit(self, *, settled: bool) -> None:
        assert self._attempt is not None
        decoder = self._decode_report
        pcm = self.bank.facts()
        facts = Facts(
            transfer=self._audio.transfer_complete if self._audio is not None else False,
            decode=decoder.decoder_complete if decoder is not None else False,
            pcm=pcm.pcm_valid,
            encoded_hash=self._hash.hexdigest(),
            encoded_bytes=self._encoded,
            decoded_samples=self._decoded // 4,
            failure=self._failure,
            cleanup=self._cleanup,
        )
        await self._journal.commit(
            self._attempt.number, facts, settled=settled, conflict_digest=self._conflict
        )

    async def release(self) -> bool:
        """Unpin only after I/O settlement AND the caller's actual bank release."""
        if self._released:
            return True
        if (
            self._task is None
            or not self._task.done()
            or not self._io_settled
            or not self.bank.facts().released
        ):
            return False
        if self._attempt is not None:
            await self._commit(settled=True)
            self._journal.release_pin(self._attempt.number)
        self._released = True
        if self._cleanup_timer is not None:
            self._cleanup_timer.cancel()
        return True

    def report(self) -> Completion:
        return Completion(
            self._prepared,
            self._audio.transfer_complete if self._audio is not None else False,
            self._envelope_complete,
            self._decode_report,
            self.bank.facts(),
            self._hash.hexdigest(),
            self._encoded,
            self._decoded,
            self._failure,
            self._refusal,
            self._audio.report if self._audio is not None else None,
            self._first_stop,
            self._cleanup,
            self._io_settled,
            self._released,
        )


async def _join[T](work: asyncio.Task[T]) -> T:
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            if work.done():
                break
    return work.result()


def _decode(producer: DecodeProducer) -> DecodeReport:
    producer.start()
    producer.finish()
    while not producer.release(1.0):
        pass
    return producer.report()


type StreamFactory = Callable[[Candidate, PcmBank], JamendoStream]
