"""Deterministic PCM bank, lease and frame-source regressions; no sleeps or threads."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from sobafm import pcm
from sobafm.pcm import (
    BANK_BYTES,
    FRAME_BYTES,
    READY_BYTES,
    PcmBank,
    PcmBankPool,
    PcmCapacityError,
    PcmError,
    PcmStateError,
)


class CountingAllocator:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, size: int) -> bytearray:
        self.calls.append(size)
        return bytearray(size)


class SteppingClock:
    def __init__(self, *times: float) -> None:
        self.times = list(times)
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.times.pop(0)


def pattern(size: int, offset: int = 0) -> bytes:
    return bytes((offset + index) % 251 for index in range(size))


def fresh_bank(clock: Callable[[], float] = lambda: 0.0) -> tuple[PcmBankPool, PcmBank]:
    pool = PcmBankPool(clock=clock)
    return pool, pool.acquire()


def test_format_is_fixed_in_one_place() -> None:
    assert (pcm.SAMPLE_RATE, pcm.CHANNELS, pcm.SAMPLE_BYTES) == (48_000, 2, 2)
    assert pcm.BYTES_PER_SECOND == 192_000
    assert FRAME_BYTES == 3_840
    assert READY_BYTES == 1_152_000
    assert BANK_BYTES == 2_304_000
    assert pcm.BANK_COUNT * BANK_BYTES == pcm.PCM_RESERVATION_BYTES == 4_608_000
    assert pcm.GLOBAL_PCM_BANKS.leased == 0


def test_lease_is_refused_before_allocation() -> None:
    allocator = CountingAllocator()
    pool = PcmBankPool(allocate=allocator)
    first, second = pool.acquire(), pool.acquire()
    assert allocator.calls == [BANK_BYTES, BANK_BYTES]

    with pytest.raises(PcmCapacityError):
        pool.acquire()

    assert allocator.calls == [BANK_BYTES, BANK_BYTES]
    assert pool.leased == 2
    assert first is not second


def test_failed_allocation_returns_only_the_reservation() -> None:
    def failing(size: int) -> bytearray:
        raise MemoryError(size)

    pool = PcmBankPool(allocate=failing)
    with pytest.raises(MemoryError):
        pool.acquire()
    assert pool.leased == 0

    short = PcmBankPool(allocate=lambda size: bytearray(size - 1))
    with pytest.raises(PcmError):
        short.acquire()
    assert short.leased == 0


def test_debt_survives_cancellation_until_actual_release() -> None:
    allocator = CountingAllocator()
    pool = PcmBankPool(allocate=allocator, clock=lambda: 5.0)
    bank = pool.acquire()
    other = pool.acquire()
    bank.write(pattern(FRAME_BYTES))

    bank.cancel()
    assert pool.leased == 2
    with pytest.raises(PcmCapacityError):
        pool.acquire()
    assert len(allocator.calls) == 2

    assert bank.release() is True
    assert bank.facts().released
    assert pool.leased == 1
    assert bank.release() is True  # idempotent; refunds once
    assert pool.leased == 1

    replacement = pool.acquire()
    assert len(allocator.calls) == 3
    assert other.release() and replacement.release()
    assert pool.leased == 0


def test_borrowed_region_or_view_retains_the_lease() -> None:
    pool, bank = fresh_bank()
    region = bank.reserve_write(FRAME_BYTES)
    assert region is not None
    retained = region[:16]
    bank.cancel()

    assert bank.release() is False
    assert pool.leased == 1

    region[:16] = b"\x01" * 16
    bank.commit_write(16)  # settles the borrow; cancellation publishes nothing
    assert bank.facts().written_bytes == 0

    assert bank.release() is False  # a derived view still exports the backing
    assert pool.leased == 1
    assert not bank.facts().released

    retained.release()
    assert bank.release() is True
    assert pool.leased == 0


def test_region_released_by_the_producer_still_settles() -> None:
    pool, bank = fresh_bank()
    region = bank.reserve_write(FRAME_BYTES)
    assert region is not None
    with region:
        region[:] = pattern(FRAME_BYTES)
    bank.commit_write(FRAME_BYTES)
    assert bank.facts().written_bytes == FRAME_BYTES

    later = bank.reserve_write(16)
    assert later is not None
    with later:
        later[:] = b"\x02" * 16
    bank.cancel()
    with pytest.raises(ValueError):
        bank.commit_write(17)
    bank.commit_write(16)  # settles after cancel; nothing is published
    assert bank.facts().written_bytes == FRAME_BYTES

    assert bank.release() is True
    assert pool.leased == 0


def test_repeated_cancellation_keeps_the_original_clock_and_state() -> None:
    clock = SteppingClock(10.0, 20.0, 30.0)
    _, bank = fresh_bank(clock)
    bank.write(pattern(READY_BYTES))
    bank.mark_transfer_complete()
    assert bank.read_frame() == pattern(FRAME_BYTES)

    bank.cancel()
    first = bank.facts()
    bank.cancel()
    bank.cancel()

    assert clock.calls == 1
    assert first.cancelled_at == 10.0
    assert bank.facts() == first
    assert first.ready and first.transfer_complete
    assert first.buffered_bytes == READY_BYTES - FRAME_BYTES
    assert bank.read_frame() == b""
    with pytest.raises(PcmStateError):
        bank.write(b"\x00")
    with pytest.raises(PcmStateError):
        bank.end_pcm()


def test_frame_reads_never_block_when_empty_or_contended() -> None:
    _, bank = fresh_bank()
    assert bank.read_frame() is None  # empty, not ready

    bank.write(pattern(READY_BYTES))
    for _ in range(READY_BYTES // FRAME_BYTES):
        assert bank.read_frame() is not None
    assert bank.read_frame() is None  # underrun after readiness
    assert not bank.facts().audible_drain

    # cancel() calls the clock while it holds the bank lock; a blocking
    # acquire in read_frame would deadlock here instead of returning.
    observed: list[bytes | None] = []

    def contended_clock() -> float:
        observed.append(bank.read_frame())
        return 1.0

    _, contended = fresh_bank(contended_clock)
    contended.write(pattern(READY_BYTES))
    contended.cancel()
    assert observed == [None]


def test_readiness_boundary() -> None:
    _, bank = fresh_bank()
    data = pattern(READY_BYTES)
    assert bank.write(data[:-1]) == READY_BYTES - 1
    assert not bank.facts().ready
    assert bank.read_frame() is None

    assert bank.write(data[-1:]) == 1
    assert bank.facts().ready
    assert bank.read_frame() == data[:FRAME_BYTES]


def test_verified_short_completion_is_ready_and_drains() -> None:
    _, bank = fresh_bank()
    data = pattern(FRAME_BYTES * 10)
    bank.write(data)
    assert not bank.facts().ready
    assert bank.read_frame() is None

    bank.end_pcm()
    facts = bank.facts()
    assert facts.ready and facts.pcm_valid and facts.failure is None
    assert not facts.audible_drain

    frames = [bank.read_frame() for _ in range(10)]
    assert b"".join(frame or b"" for frame in frames) == data
    assert bank.facts().audible_drain
    assert bank.read_frame() == b""


def test_partial_final_frame_is_a_failure_not_silence() -> None:
    _, bank = fresh_bank()
    bank.write(pattern(FRAME_BYTES + 1))
    bank.end_pcm()
    facts = bank.facts()
    assert facts.failure == "partial_final_frame"
    assert not facts.ready and not facts.pcm_valid
    assert bank.read_frame() == b""


def test_partial_final_frame_after_readiness_emits_only_whole_frames() -> None:
    _, bank = fresh_bank()
    data = pattern(READY_BYTES + 101)  # not a whole number of stereo samples
    bank.write(data)
    bank.end_pcm()
    assert bank.facts().failure == "partial_final_frame"

    received = bytearray()
    while (frame := bank.read_frame()) not in (None, b""):
        received += frame
    assert bytes(received) == data[:READY_BYTES]
    assert bank.read_frame() == b""
    assert bank.facts().audible_drain


# Night Owl decoded to 37,260,508 bytes: 988 bytes past a whole frame.
REAL_REMAINDER = 37_260_508 % FRAME_BYTES


def test_sample_aligned_remainder_is_valid_and_never_emitted() -> None:
    assert REAL_REMAINDER == 988
    _, bank = fresh_bank()
    data = pattern(2 * FRAME_BYTES + REAL_REMAINDER)
    bank.write(data)
    assert not bank.ready_now()
    bank.end_pcm()
    facts = bank.facts()
    assert facts.failure is None and facts.pcm_valid and facts.ready
    assert bank.ready_now() and facts.cancelled_at is None
    assert bank.read_frame() == data[:FRAME_BYTES]
    assert not bank.facts().audible_drain
    assert bank.read_frame() == data[FRAME_BYTES : 2 * FRAME_BYTES]
    assert bank.facts().audible_drain  # the remainder is not a frame to drain
    assert bank.read_frame() == b""
    assert bank.facts().buffered_bytes == REAL_REMAINDER


def test_sample_aligned_remainder_after_readiness_drains_every_whole_frame() -> None:
    _, bank = fresh_bank()
    data = pattern(READY_BYTES + REAL_REMAINDER)
    bank.write(data)
    assert bank.ready_now()
    bank.end_pcm()
    assert bank.facts().failure is None and bank.facts().pcm_valid
    received = bytearray()
    while (frame := bank.read_frame()) not in (None, b""):
        received += frame
    assert bytes(received) == data[:READY_BYTES]
    assert bank.read_frame() == b"" and bank.facts().audible_drain


@pytest.mark.parametrize(
    ("length", "failure"),
    [(1, "partial_final_frame"), (3, "partial_final_frame"), (4, "empty_pcm"), (988, "empty_pcm")],
)
def test_stream_shorter_than_one_frame_fails(length: int, failure: str) -> None:
    _, bank = fresh_bank()
    bank.write(pattern(length))
    bank.end_pcm()
    facts = bank.facts()
    assert facts.failure == failure and not facts.pcm_valid and not facts.ready
    assert bank.read_frame() == b""


def test_empty_pcm_is_a_failure() -> None:
    _, bank = fresh_bank()
    bank.end_pcm()
    assert bank.facts().failure == "empty_pcm"
    assert not bank.facts().ready
    assert bank.read_frame() == b""


def completion_facts(bank: PcmBank) -> tuple[bool, bool, bool, bool]:
    facts = bank.facts()
    return (
        facts.transfer_complete,
        facts.decoder_complete,
        facts.pcm_valid,
        facts.audible_drain,
    )


def test_each_completion_fact_is_independent() -> None:
    _, transfer = fresh_bank()
    transfer.mark_transfer_complete()
    assert completion_facts(transfer) == (True, False, False, False)

    _, decoder = fresh_bank()
    decoder.mark_decoder_complete()
    assert completion_facts(decoder) == (False, True, False, False)

    _, valid = fresh_bank()
    valid.write(pattern(FRAME_BYTES * 2))
    valid.end_pcm()
    assert completion_facts(valid) == (False, False, True, False)

    _, drained = fresh_bank()
    drained.write(pattern(READY_BYTES + 1))
    drained.end_pcm()
    while drained.read_frame():
        pass
    assert completion_facts(drained) == (False, False, False, True)


def test_ring_wraps_and_refuses_overfill() -> None:
    _, bank = fresh_bank()
    data = pattern(BANK_BYTES + FRAME_BYTES * 3, offset=7)
    assert bank.write(data) == BANK_BYTES
    assert bank.reserve_write() is None
    assert bank.write(b"\x00") == 0

    received = bytearray()
    for _ in range(5):
        frame = bank.read_frame()
        assert frame is not None
        received += frame
    assert bank.write(data[BANK_BYTES:]) == FRAME_BYTES * 3
    region = bank.reserve_write()
    assert region is not None and len(region) == FRAME_BYTES * 2
    bank.commit_write(0)

    bank.end_pcm()
    while frame := bank.read_frame():
        received += frame
    assert bytes(received) == data
    assert bank.facts().audible_drain


def test_producer_misuse_is_rejected() -> None:
    _, bank = fresh_bank()
    with pytest.raises(PcmStateError):
        bank.commit_write(0)
    with pytest.raises(ValueError):
        bank.reserve_write(0)

    region = bank.reserve_write(8)
    assert region is not None and len(region) == 8
    with pytest.raises(PcmStateError):
        bank.reserve_write()
    with pytest.raises(PcmStateError):
        bank.end_pcm()
    with pytest.raises(ValueError):
        bank.commit_write(9)
    bank.commit_write(8)

    bank.end_pcm()
    with pytest.raises(PcmStateError):
        bank.write(b"\x00")
    with pytest.raises(PcmStateError):
        bank.end_pcm()

    assert bank.release()
    assert bank.read_frame() == b""
    with pytest.raises(PcmStateError):
        bank.mark_transfer_complete()
    with pytest.raises(PcmStateError):
        bank.mark_decoder_complete()
    bank.cancel()
    assert bank.facts().cancelled_at is None


def test_wait_writable_reports_space_or_a_stopped_stream() -> None:
    pool, bank = fresh_bank()
    assert bank.wait_writable(0)
    assert bank.write(pattern(BANK_BYTES)) == BANK_BYTES
    assert not bank.wait_writable(0)  # full: a producer would wait
    assert bank.read_frame() is not None
    assert bank.wait_writable(0)
    assert bank.write(pattern(FRAME_BYTES)) == FRAME_BYTES
    assert not bank.wait_writable(0)
    bank.cancel()
    assert bank.wait_writable(0)  # cancelled: the next write fails at once
    assert bank.release() and pool.leased == 0
    assert bank.wait_writable(0)
