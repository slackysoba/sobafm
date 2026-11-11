"""Fixed-format PCM banks and the non-blocking voice frame source.

Six banks form three exclusive two-bank station reservations (ADR 0012).
A bank lease is granted before its backing is
allocated, and the lease's capacity stays charged until the backing is actually
released. Cancellation records its first time and stops the stream; it never
frees capacity or resets recorded facts.

A bank has one producer and one consumer. The producer borrows a writable region
(for example for a decoder pipe ``readinto``) and commits what it filled. The
voice callback reads whole frames and never waits: it takes the bank lock without
blocking and copies at most one frame. Producer copies run outside that lock.

Transfer completion, decoder completion, valid PCM and audible drain are
independent facts. Only the producer's verified end of PCM can make a recording
shorter than the readiness target ready. A decoder's output length need not be
a multiple of the 20 ms frame: a stream of whole stereo samples holding at least
one whole frame is valid, its remainder shorter than one frame is never emitted
or padded with silence, and audible drain follows the last whole frame. A length
that is not a whole number of samples is truncated output and fails the stream
as a partial final frame; a stream with no whole frame is empty.
"""

from __future__ import annotations

import threading
import time
import weakref
from collections import deque
from collections.abc import Buffer, Callable
from dataclasses import dataclass
from typing import Literal

SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_BYTES = 2  # signed 16-bit little-endian, interleaved
STEREO_SAMPLE_BYTES = CHANNELS * SAMPLE_BYTES
FRAME_MILLISECONDS = 20
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_BYTES
FRAME_BYTES = BYTES_PER_SECOND * FRAME_MILLISECONDS // 1000
READY_SECONDS = 6
READY_BYTES = BYTES_PER_SECOND * READY_SECONDS
BANK_BYTES = 2_304_000
BANK_COUNT = 2
STATION_COUNT = 3
PCM_RESERVATION_BYTES = BANK_BYTES * BANK_COUNT * STATION_COUNT

assert BYTES_PER_SECOND == 192_000
assert FRAME_BYTES == 3_840
assert READY_BYTES == 1_152_000
assert BANK_BYTES % FRAME_BYTES == 0 and BANK_BYTES > READY_BYTES
assert PCM_RESERVATION_BYTES == 13_824_000

type PcmFailure = Literal["empty_pcm", "partial_final_frame"]


class PcmError(Exception):
    """Base class for PCM bank errors."""


class PcmCapacityError(PcmError):
    """No global bank lease is available; nothing was allocated."""


class PcmStateError(PcmError):
    """The requested operation is invalid in the bank's current state."""


@dataclass(frozen=True, slots=True)
class PcmFacts:
    """A consistent snapshot of one bank's separately tracked facts."""

    transfer_complete: bool
    decoder_complete: bool
    pcm_valid: bool
    audible_drain: bool
    ready: bool
    failure: PcmFailure | None
    cancelled_at: float | None
    released: bool
    written_bytes: int
    buffered_bytes: int


class VoiceFrameCredit:
    """One voice reader's scratch, including the previous frame during handoff."""

    def __init__(self, refund: Callable[[], None]) -> None:
        self._slots = deque([None, None])
        weakref.finalize(self, refund)

    @property
    def available(self) -> bool:
        return bool(self._slots)

    def handoff(self, frame: bytes) -> bytes:
        """Keep credit on the bytes actually returned, even after the player stops."""
        self._slots.pop()
        try:
            output = _VoiceFrame(frame)
            output.credit = self
        except BaseException:
            self._slots.append(None)
            raise
        return output

    def returned(self) -> None:
        self._slots.append(None)


class _VoiceFrame(bytes):
    credit: VoiceFrameCredit | None = None

    def __del__(self) -> None:
        if self.credit is not None:
            self.credit.returned()


class StationReservation:
    """Two producer/bank/copy slots and voice scratch belonging to one station."""

    def __init__(
        self,
        *,
        allocate: Callable[[int], bytearray] = bytearray,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._allocate = allocate
        self._clock = clock
        self._lock = threading.Lock()
        self._leased = 0
        self._closed = False
        # Independent of the bank leases: a retry can return a bank while
        # disnake still holds the frame passed to its previous send.
        self._voice_slots = deque([float("-inf")] * BANK_COUNT)

    @property
    def voice_readers(self) -> int:
        return BANK_COUNT - len(self._voice_slots)

    def claim_voice_reader(self) -> VoiceFrameCredit | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._closed:
                return None
            try:
                self._voice_slots.pop()
            except IndexError:
                return None
        finally:
            self._lock.release()
        try:
            return VoiceFrameCredit(self._return_voice_reader)
        except BaseException:
            self._return_voice_reader()
            raise

    def _return_voice_reader(self) -> None:
        self._voice_slots.append(time.monotonic())

    def voice_returned_by(self, deadline: float) -> bool:
        # The last actual return can arrive after the cutoff but before the
        # event loop gets to observe it. Snapshot both free slots together.
        returns = tuple(self._voice_slots)
        return len(returns) == BANK_COUNT and max(returns) <= deadline

    @property
    def last_voice_return(self) -> float:
        return max(self._voice_slots, default=float("-inf"))

    @property
    def leased(self) -> int:
        """Leases whose capacity is still charged, including cleanup debt."""
        with self._lock:
            return self._leased

    def acquire(self) -> PcmBank:
        """Lease one bank, refusing before allocation when none is available."""
        with self._lock:
            if self._closed or self._leased >= BANK_COUNT:
                raise PcmCapacityError("no PCM bank lease is available")
            self._leased += 1
        try:
            backing = self._allocate(BANK_BYTES)
            if len(backing) != BANK_BYTES:
                raise PcmError("allocated PCM bank has the wrong size")
        except BaseException:
            # Nothing usable was allocated, so the reservation itself is the
            # only debt and can be returned at once.
            self._refund()
            raise
        return PcmBank(backing, refund=self._refund, clock=self._clock)

    def _refund(self) -> None:
        with self._lock:
            if self._leased <= 0:
                raise PcmStateError("PCM lease refunded twice")
            self._leased -= 1

    def close(self) -> bool:
        """Retire the reservation only after its actual bank and voice debt returns."""
        with self._lock:
            if self._leased or self.voice_readers:
                return False
            self._closed = True
            return True


class PcmBankPool:
    """One process-wide budget of three exclusive station reservations.

    The direct bank/voice methods are a single two-slot reservation for offline
    producer tools. They share this budget; they cannot bypass guild admission.
    The adapter always reserves by guild before interpreting a request.
    Reservation changes are owned by the event loop (or a standalone tool).
    """

    def __init__(
        self,
        *,
        allocate: Callable[[int], bytearray] = bytearray,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._allocate = allocate
        self._clock = clock
        self._stations: dict[object, StationReservation] = {}
        self._direct_owner = object()
        self._last_voice_return = float("-inf")

    @property
    def owners(self) -> tuple[object, ...]:
        return tuple(self._stations)

    def reserve(self, owner: object) -> StationReservation:
        if (reservation := self._stations.get(owner)) is not None:
            return reservation
        if len(self._stations) >= STATION_COUNT:
            raise PcmCapacityError("three station reservations are in use")
        reservation = StationReservation(allocate=self._allocate, clock=self._clock)
        self._stations[owner] = reservation
        return reservation

    def release(self, owner: object) -> bool:
        reservation = self._stations.get(owner)
        if reservation is None:
            return True
        if not reservation.close():
            return False
        self._last_voice_return = max(self._last_voice_return, reservation.last_voice_return)
        del self._stations[owner]
        return True

    @property
    def leased(self) -> int:
        return sum(reservation.leased for reservation in self._stations.values())

    @property
    def voice_readers(self) -> int:
        return sum(reservation.voice_readers for reservation in self._stations.values())

    def voice_returned_by(self, deadline: float) -> bool:
        return self._last_voice_return <= deadline and all(
            reservation.voice_returned_by(deadline) for reservation in self._stations.values()
        )

    def acquire(self) -> PcmBank:
        return self.reserve(self._direct_owner).acquire()

    def claim_voice_reader(self) -> VoiceFrameCredit | None:
        try:
            return self.reserve(self._direct_owner).claim_voice_reader()
        except PcmCapacityError:
            return None


class PcmBank:
    """One leased 2,304,000-byte ring of prepared PCM.

    Construct banks only through ``PcmBankPool.acquire``.
    """

    def __init__(
        self,
        backing: bytearray,
        *,
        refund: Callable[[], None],
        clock: Callable[[], float],
    ) -> None:
        self._lock = threading.Lock()
        # Wakes a producer waiting for free space; notified with the lock held.
        self._changed = threading.Condition(self._lock)
        self._backing: bytearray | None = backing
        self._refund = refund
        self._clock = clock
        self._region: memoryview | None = None
        self._region_length = 0
        self._written = 0
        self._read = 0
        self._ended = False
        self._ready = False
        self._pcm_valid = False
        self._drained = False
        self._failure: PcmFailure | None = None
        self._transfer_complete = False
        self._decoder_complete = False
        self._cancelled_at: float | None = None

    # Producer side.

    def reserve_write(self, limit: int | None = None) -> memoryview | None:
        """Borrow the next contiguous free region, or return None when full.

        The producer fills a prefix and must settle the borrow with
        ``commit_write``, including ``commit_write(0)`` on error. The region may
        already be released by then. Until the borrow is settled and no view
        derived from the region exists, the bank cannot be released.
        """
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            backing = self._accepting_backing()
            buffered = self._written - self._read
            free = BANK_BYTES - buffered
            if free == 0:
                return None
            start = self._written % BANK_BYTES
            length = min(free, BANK_BYTES - start)
            if limit is not None:
                length = min(length, limit)
            with memoryview(backing) as whole:
                region = whole[start : start + length]
            self._region, self._region_length = region, length
            return region

    def commit_write(self, count: int) -> None:
        """Publish the first ``count`` bytes of the borrowed region.

        After cancellation this only settles the borrow; nothing is published.
        """
        with self._lock:
            region = self._region
            if region is None:
                raise PcmStateError("no write region is reserved")
            # The producer may have released the region (for example with a
            # ``with`` block), so its length is kept separately.
            if not 0 <= count <= self._region_length:
                raise ValueError("count exceeds the reserved region")
            region.release()  # idempotent on an already released view
            self._region, self._region_length = None, 0
            if self._cancelled_at is not None:
                return
            self._written += count
            if not self._ready and self._written - self._read >= READY_BYTES:
                self._ready = True

    def wait_writable(self, timeout: float | None = None) -> bool:
        """Wait until a write could proceed or fail at once.

        Returns True when there is free space or the stream was cancelled,
        ended or released, so the next ``reserve_write`` cannot block; False
        when ``timeout`` passed first. Never called by the voice callback.
        """
        with self._changed:
            return self._changed.wait_for(self._writable_or_stopped, timeout)

    def write(self, data: Buffer) -> int:
        """Copy as much of ``data`` as currently fits; return the bytes accepted."""
        with memoryview(data) as source, source.cast("B") as octets:
            accepted = 0
            while accepted < len(octets):
                region = self.reserve_write(len(octets) - accepted)
                if region is None:
                    break
                length = len(region)
                region[:] = octets[accepted : accepted + length]
                self.commit_write(length)
                accepted += length
            return accepted

    def end_pcm(self) -> None:
        """Declare the producer's verified end of PCM; no more bytes follow.

        Call this only after the source's completion policy is satisfied. The
        stream must be a whole number of stereo samples, or it fails as
        ``partial_final_frame`` (truncated output), and must hold at least one
        whole frame, or it fails as ``empty_pcm``. A valid stream's remainder
        shorter than one frame is never emitted; drain follows the last whole
        frame.
        """
        with self._lock:
            self._accepting_backing()
            self._ended = True
            if self._written == 0:
                self._failure = "empty_pcm"
            elif self._written % STEREO_SAMPLE_BYTES:
                self._failure = "partial_final_frame"
            elif self._written < FRAME_BYTES:
                self._failure = "empty_pcm"
            else:
                self._pcm_valid = True
                self._ready = True
            self._update_drain()

    def ready_now(self) -> bool:
        """Whether frames have become readable, without taking the bank lock.

        Readiness only ever changes from False to True, so the voice callback
        may read it without waiting; it says nothing about buffered frames.
        """
        return self._ready

    def mark_transfer_complete(self) -> None:
        """Record that the source transfer reached its clean end."""
        with self._lock:
            self._require_unreleased()
            self._transfer_complete = True

    def mark_decoder_complete(self) -> None:
        """Record that the decoder terminated successfully."""
        with self._lock:
            self._require_unreleased()
            self._decoder_complete = True

    # Consumer side.

    def read_frame(self) -> bytes | None:
        """Return one 3,840-byte frame without waiting.

        ``None`` means no frame is available now: not yet ready, underrun, or
        another operation briefly holds the bank. ``b""`` means no frame will
        follow: drained, cancelled, released or failed before readiness.
        """
        if not self._lock.acquire(blocking=False):
            return None
        try:
            backing = self._backing
            if backing is None or self._cancelled_at is not None:
                return b""
            if not self._ready:
                return b"" if self._failure is not None else None
            if self._written - self._read < FRAME_BYTES:
                self._update_drain()
                return b"" if self._drained else None
            # Reads stay frame-aligned and the bank size is a whole number of
            # frames, so a frame never straddles the ring boundary.
            start = self._read % BANK_BYTES
            with memoryview(backing) as whole, whole[start : start + FRAME_BYTES] as frame:
                data = bytes(frame)
            self._read += FRAME_BYTES
            self._update_drain()
            self._changed.notify_all()  # never blocks
            return data
        finally:
            self._lock.release()

    # Ownership.

    def cancel(self) -> None:
        """Stop the stream, keeping the first cancellation time and all debt."""
        with self._lock:
            if self._cancelled_at is None and self._backing is not None:
                self._cancelled_at = self._clock()
            self._changed.notify_all()

    def release(self) -> bool:
        """Free the backing and return the lease once nothing borrows it.

        Returns False, retaining the lease and its debt, while a producer
        region or any view of the backing still exists. Idempotent.
        """
        with self._lock:
            backing = self._backing
            if backing is None:
                return True
            if self._region is not None:
                return False
            try:
                # Clearing fails while any export survives, so success means
                # the bank's payload memory was actually given up.
                backing.clear()
            except BufferError:
                return False
            self._backing = None
            self._changed.notify_all()
        self._refund()
        return True

    def facts(self) -> PcmFacts:
        """Snapshot the bank's facts. Not for the voice callback."""
        with self._lock:
            return PcmFacts(
                transfer_complete=self._transfer_complete,
                decoder_complete=self._decoder_complete,
                pcm_valid=self._pcm_valid,
                audible_drain=self._drained,
                ready=self._ready,
                failure=self._failure,
                cancelled_at=self._cancelled_at,
                released=self._backing is None,
                written_bytes=self._written,
                buffered_bytes=self._written - self._read,
            )

    def _require_unreleased(self) -> bytearray:
        if self._backing is None:
            raise PcmStateError("the PCM bank was released")
        return self._backing

    def _accepting_backing(self) -> bytearray:
        backing = self._require_unreleased()
        if self._cancelled_at is not None:
            raise PcmStateError("the PCM stream was cancelled")
        if self._ended:
            raise PcmStateError("the PCM stream has ended")
        if self._region is not None:
            raise PcmStateError("a write region is already reserved")
        return backing

    def _writable_or_stopped(self) -> bool:
        return (
            self._backing is None
            or self._cancelled_at is not None
            or self._ended
            or self._written - self._read < BANK_BYTES
        )

    def _update_drain(self) -> None:
        if self._ended and self._ready and self._written - self._read < FRAME_BYTES:
            self._drained = True


GLOBAL_PCM_BANKS = PcmBankPool()
