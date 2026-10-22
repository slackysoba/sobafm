"""Allocation admission and cleanup debt, independent of provider/native qualification."""

import asyncio

import pytest

from sobafm.audio_budget import PARENT_LIMIT, PCM_LIMIT, TOTAL_LIMIT, AudioBytes, AudioLedger


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


def test_larger_parent_ceiling_includes_pcm_without_raising_its_limit() -> None:
    ledger = AudioLedger()
    parent = ledger.reserve(AudioBytes(pcm=34_560_000, other=13_440_000))
    assert parent is not None
    assert ledger.reserved.parent == 48_000_000
    assert ledger.reserve(AudioBytes(other=1)) is None
    parent.release()
    assert ledger.reserve(AudioBytes(pcm=34_560_001)) is None


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
    parent = ledger.reserve(AudioBytes(other=48_000_000))
    native = ledger.reserve(AudioBytes(native=21_999_999))
    assert parent is not None
    assert native is not None
    assert ledger.reserved.total == 69_999_999
    assert ledger.reserve(AudioBytes(native=1)) is None
    unknown = ledger.unbounded()
    native.release()
    assert ledger.reserved.parent == 48_000_000
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
