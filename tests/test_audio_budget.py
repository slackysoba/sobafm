"""Allocation admission and cleanup debt, independent of provider/native qualification."""

import asyncio
from collections import deque

import pytest

from sobafm.audio_budget import (
    PARENT_LIMIT,
    PCM_LIMIT,
    TOTAL_LIMIT,
    AudioBytes,
    AudioLease,
    AudioLedger,
)


@pytest.mark.parametrize("size", [AudioBytes(pcm=PCM_LIMIT), AudioBytes(other=PARENT_LIMIT)])
def test_admits_the_inclusive_pcm_and_parent_ceilings(size: AudioBytes) -> None:
    ledger = AudioLedger()
    assert ledger.reserve(size) is not None
    assert ledger.reserve(AudioBytes(pcm=1, other=1)) is None


@pytest.mark.parametrize(
    "size",
    [
        AudioBytes(pcm=PCM_LIMIT + 1),
        AudioBytes(other=PARENT_LIMIT + 1),
        AudioBytes(other=PARENT_LIMIT, native=TOTAL_LIMIT - PARENT_LIMIT),
        AudioBytes(native=TOTAL_LIMIT),
    ],
)
def test_refuses_overflow_and_equality_at_the_strict_total(size: AudioBytes) -> None:
    ledger = AudioLedger()
    assert ledger.reserve(size) is None
    assert ledger.reserved == AudioBytes()


def test_native_reservation_reduces_parent_headroom() -> None:
    ledger = AudioLedger()
    assert ledger.reserve(AudioBytes(native=TOTAL_LIMIT - PARENT_LIMIT)) is not None
    assert ledger.reserve(AudioBytes(other=PARENT_LIMIT)) is None
    assert ledger.reserve(AudioBytes(other=PARENT_LIMIT - 1)) is not None


def test_parent_bound_follows_the_strict_total_without_raising_pcm() -> None:
    ledger = AudioLedger()
    parent = ledger.reserve(AudioBytes(pcm=34_560_000, other=35_439_999))
    assert parent is not None
    assert ledger.reserved.parent == 69_999_999
    assert PARENT_LIMIT == TOTAL_LIMIT - 1
    assert ledger.reserve(AudioBytes(other=1)) is None
    parent.release()
    assert ledger.reserve(AudioBytes(pcm=34_560_001)) is None


def test_parent_allowance_follows_still_charged_native_reservations() -> None:
    ledger = AudioLedger()
    parent = ledger.reserve(AudioBytes(pcm=34_560_000, other=15_440_000))
    outside = ledger.reserve(AudioBytes(native=4 * 1024 * 1024))
    assert parent is not None
    assert outside is not None
    assert ledger.reserved.parent == 50_000_000
    assert ledger.reserved.total == 54_194_304
    # Hypothetical complete reservations verify admission, not profile qualification.
    equality = TOTAL_LIMIT - ledger.reserved.total
    assert ledger.reserve(AudioBytes(native=equality)) is None
    first_child = ledger.reserve(AudioBytes(native=8 * 1024 * 1024))
    assert first_child is not None
    assert ledger.reserved.total == 62_582_912
    assert ledger.reserve(AudioBytes(native=8 * 1024 * 1024)) is None
    assert ledger.reserved.total == 62_582_912


def test_two_child_overlap_and_retained_lease_fit_only_after_actual_release() -> None:
    ledger = AudioLedger()
    parent = ledger.reserve(AudioBytes(pcm=34_560_000, other=13_440_000))
    first = ledger.reserve(AudioBytes(native=8 * 1024 * 1024))
    second = ledger.reserve(AudioBytes(native=8 * 1024 * 1024))
    outside = ledger.reserve(AudioBytes(native=4 * 1024 * 1024))
    assert parent is not None
    assert first is not None
    assert second is not None
    assert outside is not None
    # Synthetic whole reservations test arithmetic, not a qualified native or
    # outside-storage classification/profile. Both closing and incoming count.
    assert ledger.reserved.total == 68_971_520
    live = closing = first
    assert live is closing
    replacement_station_ledger = ledger
    assert replacement_station_ledger.reserve(AudioBytes(native=8 * 1024 * 1024)) is None
    assert ledger.reserved.total == 68_971_520
    closing.release()
    assert ledger.reserved.native == 12 * 1024 * 1024
    assert replacement_station_ledger.reserve(AudioBytes(native=8 * 1024 * 1024)) is not None
    assert ledger.reserved.total == 68_971_520


def test_total_boundary_and_unknown_debt_remain_effective_with_larger_caps() -> None:
    ledger = AudioLedger()
    parent = ledger.reserve(AudioBytes(other=50_000_000))
    native = ledger.reserve(AudioBytes(native=19_999_999))
    assert parent is not None
    assert native is not None
    assert ledger.reserved.total == 69_999_999
    assert ledger.reserve(AudioBytes(native=1)) is None
    unknown = ledger.unbounded()
    native.release()
    assert ledger.reserved.parent == 50_000_000
    assert ledger.reserve(AudioBytes(native=1)) is None
    unknown.release()
    assert ledger.reserve(AudioBytes(native=1)) is not None


def test_future_growth_and_each_transport_copy_are_reserved_before_admission() -> None:
    ledger = AudioLedger()
    # A synthetic full Lyria envelope includes future replacements, rather than
    # subtracting today's 8/60/2/2-second payload from the total allowance.
    growth = ledger.reserve(AudioBytes(pcm=30_000_000, other=1_000_000))
    assert growth is not None
    catalog = ledger.reserve(
        AudioBytes(pcm=4_000_000, other=256 * 1024 * 4 + 16 * 1024 + 3840, native=500_000)
    )
    assert catalog is not None
    assert ledger.reserve(AudioBytes(pcm=600_000)) is None
    # A producer-capacity release does not refund retained bytes/future growth.
    assert ledger.reserved.pcm == 34_000_000
    catalog.release()
    assert ledger.reserved.pcm == 30_000_000
    growth.release()
    assert ledger.reserved == AudioBytes()


def test_aliases_share_one_allocation_but_a_copy_requires_its_own_lease() -> None:
    ledger = AudioLedger()
    allocation = ledger.reserve(AudioBytes(pcm=18_000_000))
    assert allocation is not None
    live = incoming = allocation
    assert ledger.reserved.pcm == 18_000_000
    assert live is incoming
    assert ledger.reserve(AudioBytes(pcm=18_000_000)) is None
    allocation.release()
    allocation.release()  # an idempotent explicit release never refunds another lease
    assert ledger.reserved == AudioBytes()


def test_deque_pop_or_producer_exit_does_not_release_in_flight_mixer_storage() -> None:
    ledger = AudioLedger()
    envelope = ledger.reserve(AudioBytes(pcm=PCM_LIMIT))
    assert envelope is not None
    # The owner's fixed envelope covers a popped frame and all mixing/output
    # copies until the player has stopped using them, even with an empty deque.
    assert ledger.reserve(AudioBytes(pcm=3840)) is None
    envelope.release()
    assert ledger.reserve(AudioBytes(pcm=3840)) is not None


async def test_cleanup_debt_survives_timeout_cancellation_and_replacement_owner() -> None:
    ledger = AudioLedger()
    debt = ledger.reserve(AudioBytes(pcm=PCM_LIMIT, other=1_000_000, native=500_000))
    assert debt is not None

    async def incomplete_cleanup() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(incomplete_cleanup())
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.001):
            await task
    assert task.cancelled()
    new_station_ledger = ledger
    assert new_station_ledger.reserve(AudioBytes(pcm=3840)) is None
    assert ledger.reserved.total > PCM_LIMIT
    debt.release()  # only a later owner verifying actual release may refund
    assert ledger.reserve(AudioBytes(pcm=3840)) is not None


def test_unknown_obligations_block_catalog_even_with_empty_queues() -> None:
    ledger = AudioLedger()
    unknown = ledger.unbounded()
    assert ledger.reserved == AudioBytes()
    assert not ledger.bounded
    assert ledger.reserve(AudioBytes(pcm=1)) is None
    unknown.release()
    assert ledger.bounded
    assert ledger.reserve(AudioBytes(pcm=1)) is not None


@pytest.mark.parametrize("value", [-1, 0.5, True])
def test_byte_bounds_must_be_nonnegative_integers(value: int | float) -> None:
    with pytest.raises(ValueError, match="integer"):
        AudioBytes(pcm=value)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("value", [-1, 0, 0.5, True])
def test_pcm_capacity_and_children_require_positive_plain_integers(value: int | float) -> None:
    ledger = AudioLedger()
    capacity = ledger.reserve_pcm_capacity(4)
    assert capacity is not None
    with pytest.raises(ValueError, match="positive integer"):
        ledger.reserve_pcm_capacity(value)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="positive integer"):
        capacity.reserve_pcm(value)  # pyright: ignore[reportArgumentType]
    assert ledger.reserved == AudioBytes(pcm=4)


@pytest.mark.parametrize(
    "existing",
    [
        AudioBytes(pcm=PCM_LIMIT - 1),
        AudioBytes(other=PARENT_LIMIT - 1),
        AudioBytes(native=TOTAL_LIMIT - 2),
    ],
)
def test_pcm_capacity_minting_keeps_each_existing_numeric_gate(existing: AudioBytes) -> None:
    ledger = AudioLedger()
    assert ledger.reserve(existing) is not None
    assert ledger.reserve_pcm_capacity(2) is None
    assert ledger.reserved == existing
    assert ledger.reserve_pcm_capacity(1) is not None
    assert ledger.reserved == existing + AudioBytes(pcm=1)


def test_retained_unknown_owner_prevents_replacement_capacity_minting() -> None:
    ledger = AudioLedger()
    old_owner = ledger.unbounded()
    replacement_ledger = ledger
    assert replacement_ledger.reserve_pcm_capacity(4) is None
    assert replacement_ledger.reserve(AudioBytes(pcm=4)) is None
    assert not ledger.bounded
    old_owner.release()
    assert replacement_ledger.reserve_pcm_capacity(4) is not None


def test_later_unknown_work_cannot_expand_or_replace_precharged_capacity() -> None:
    ledger = AudioLedger()
    capacity = ledger.reserve_pcm_capacity(4)
    assert capacity is not None
    unknown = ledger.unbounded()
    assert not ledger.bounded
    assert ledger.reserve_pcm_capacity(1) is None
    assert ledger.reserve(AudioBytes(pcm=1)) is None
    child = capacity.reserve_pcm(4)
    assert child is not None
    assert child.size == AudioBytes(pcm=4)
    assert capacity.reserve_pcm(1) is None
    assert ledger.reserved == AudioBytes(pcm=4)
    capacity.close_pcm_admission()
    child.release()
    assert ledger.reserved == AudioBytes(pcm=4)
    assert capacity.reserve_pcm(4) is None
    assert ledger.reserve_pcm_capacity(4) is None
    capacity.release()
    assert ledger.reserved == AudioBytes()
    assert not ledger.bounded
    assert ledger.reserve_pcm_capacity(4) is None
    unknown.release()
    assert ledger.reserve_pcm_capacity(4) is not None


def test_child_overflow_refuses_allocation_and_explicit_release_reuses_same_capacity() -> None:
    ledger = AudioLedger()
    capacity = ledger.reserve_pcm_capacity(4)
    assert capacity is not None
    allocations: list[bytes] = []

    def retain(size: int) -> AudioLease | None:
        child = capacity.reserve_pcm(size)
        if child is not None:
            allocations.append(bytes(range(size)))
        return child

    first = retain(4)
    assert first is not None
    assert retain(1) is None
    assert len(allocations) == 1
    allocations.clear()
    first.release()
    replacement = retain(4)
    assert replacement is not None
    first.release()  # a stale acknowledgement cannot refund the replacement
    assert retain(1) is None
    assert len(allocations) == 1
    assert ledger.reserved == AudioBytes(pcm=4)


def test_independent_pcm_owners_cannot_spend_each_others_capacity() -> None:
    ledger = AudioLedger()
    left = ledger.reserve_pcm_capacity(3)
    right = ledger.reserve_pcm_capacity(5)
    assert left is not None
    assert right is not None
    assert left.reserve_pcm(4) is None
    first = left.reserve_pcm(3)
    second = right.reserve_pcm(5)
    assert first is not None
    assert second is not None
    assert ledger.reserved == AudioBytes(pcm=8)
    first.release()
    assert right.reserve_pcm(1) is None
    assert left.reserve_pcm(3) is not None
    assert ledger.reserved == AudioBytes(pcm=8)


def test_flat_unknown_and_child_leases_cannot_issue_pcm_children() -> None:
    ledger = AudioLedger()
    capacity = ledger.reserve_pcm_capacity(4)
    flat = ledger.reserve(AudioBytes(pcm=1))
    assert capacity is not None
    assert flat is not None
    child = capacity.reserve_pcm(1)
    assert child is not None
    unknown = ledger.unbounded()
    for lease in (flat, unknown, child):
        with pytest.raises(RuntimeError, match="not a PCM capacity"):
            lease.reserve_pcm(1)
        with pytest.raises(RuntimeError, match="not a PCM capacity"):
            lease.close_pcm_admission()
    assert ledger.reserved == AudioBytes(pcm=5)


def test_parent_release_requires_permanent_closure_and_explicit_child_release() -> None:
    ledger = AudioLedger()
    capacity = ledger.reserve_pcm_capacity(4)
    assert capacity is not None
    with pytest.raises(RuntimeError, match="admission is open"):
        capacity.release()
    child = capacity.reserve_pcm(4)
    assert child is not None
    capacity.close_pcm_admission()
    capacity.close_pcm_admission()
    assert capacity.reserve_pcm(1) is None
    assert child.size == AudioBytes(pcm=4)
    with pytest.raises(RuntimeError, match="children remain"):
        capacity.release()
    assert ledger.reserved == AudioBytes(pcm=4)
    child.release()
    child.release()
    assert ledger.reserved == AudioBytes(pcm=4)  # zero children is not parent release
    assert capacity.reserve_pcm(1) is None
    capacity.release()
    capacity.release()
    capacity.close_pcm_admission()
    assert capacity.reserve_pcm(1) is None
    assert ledger.reserved == AudioBytes()


async def test_held_mixer_receipt_survives_cancellation_timeout_and_replacement() -> None:
    ledger = AudioLedger()
    capacity = ledger.reserve_pcm_capacity(4)
    assert capacity is not None
    child = capacity.reserve_pcm(4)
    assert child is not None
    queued = deque([bytes(range(4))])
    held = [queued.popleft()]
    capacity.close_pcm_admission()  # producer ended; its already-admitted frame remains owned
    unknown_debt = ledger.unbounded()
    storage_released = asyncio.Event()

    async def cleanup_owner() -> None:
        await storage_released.wait()
        held.clear()
        child.release()

    cleanup = asyncio.create_task(cleanup_owner())

    async def wait_cleanup() -> None:
        await asyncio.shield(cleanup)

    for _ in range(2):
        waiter = asyncio.create_task(wait_cleanup())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.001):
            await asyncio.shield(cleanup)
    assert not queued
    assert held
    assert not cleanup.done()
    assert ledger.reserved == AudioBytes(pcm=4)
    replacement_ledger = ledger
    assert replacement_ledger.reserve_pcm_capacity(4) is None
    with pytest.raises(RuntimeError, match="children remain"):
        capacity.release()
    storage_released.set()
    await cleanup
    assert not held
    assert ledger.reserved == AudioBytes(pcm=4)
    capacity.release()
    assert ledger.reserved == AudioBytes()
    assert not ledger.bounded
    assert replacement_ledger.reserve_pcm_capacity(4) is None
    unknown_debt.release()
    assert replacement_ledger.reserve_pcm_capacity(4) is not None
