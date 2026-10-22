"""Shared audio reservations; unknown obligations fail catalog admission closed.

These are allocation ceilings, not measurements or qualification of a producer.
An owner reserves its complete reachable allocation (including growth/copies and
transport overshoot) before starting, and releases only after actual cleanup.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sobafm.owned_pcm import PCMOwner

PCM_LIMIT = 34_560_000
PARENT_LIMIT = 38_000_000
TOTAL_LIMIT = 40_000_000  # strict: equality is ineligible


@dataclass(frozen=True)
class AudioBytes:
    """Disjoint allocations: PCM, other parent audio, and audited native audio."""

    pcm: int = 0
    other: int = 0
    native: int = 0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0 for value in (self.pcm, self.other, self.native)
        ):
            raise ValueError("audio reservations require nonnegative integer byte bounds")

    @property
    def parent(self) -> int:
        return self.pcm + self.other

    @property
    def total(self) -> int:
        return self.parent + self.native

    def __add__(self, other: AudioBytes) -> AudioBytes:
        return AudioBytes(
            self.pcm + other.pcm, self.other + other.other, self.native + other.native
        )


class AudioLease:
    """One backing allocation or envelope, shared by aliases and retained through cleanup.

    There is deliberately no context-manager or finalizer refund: cancellation,
    garbage collection and a cleanup deadline do not prove storage was released.
    Copies need distinct leases, even when their contents compare equal.
    """

    def __init__(self, ledger: AudioLedger, size: AudioBytes | None) -> None:
        self._ledger = ledger
        self._size = size
        self._released = False
        self._pcm_children: set[AudioLease] | None = None
        self._pcm_available = 0
        self._pcm_closed = False
        self._pcm_parent: AudioLease | None = None

    @property
    def size(self) -> AudioBytes | None:
        return self._size

    def reserve_pcm(self, pcm_bytes: int) -> AudioLease | None:
        """Borrow from this owner's precharged PCM capacity, without a new ledger charge."""
        _positive_pcm_bytes(pcm_bytes)
        children = self._pcm_children
        if children is None:
            raise RuntimeError("lease is not a PCM capacity")
        if self._released or self._pcm_closed or pcm_bytes > self._pcm_available:
            return None
        child = AudioLease(self._ledger, AudioBytes(pcm=pcm_bytes))
        child._pcm_parent = self
        children.add(child)
        self._pcm_available -= pcm_bytes
        return child

    def close_pcm_admission(self) -> None:
        """Permanently stop new children; existing storage and charges remain owned."""
        if self._pcm_children is None:
            raise RuntimeError("lease is not a PCM capacity")
        self._pcm_closed = True

    def release(self) -> None:
        """Refund after the owner verifies all resources covered by this lease are gone."""
        if self._released:
            return
        if self._pcm_children is not None and (not self._pcm_closed or self._pcm_children):
            raise RuntimeError("PCM capacity admission is open or children remain")
        parent = self._pcm_parent
        if parent is None:
            self._ledger._release(self)  # pyright: ignore[reportPrivateUsage]
        else:
            children, size = parent._pcm_children, self._size
            if children is None or self not in children or size is None:
                raise RuntimeError("PCM child ownership is unavailable")
            children.remove(self)
            parent._pcm_available += size.pcm
            self._pcm_parent = None
        self._released = True

    def _enable_pcm_capacity(self) -> None:
        size = self._size
        if (
            self._released
            or self._pcm_children is not None
            or self._pcm_parent is not None
            or size is None
            or size.pcm <= 0
            or size.other
            or size.native
        ):
            raise RuntimeError("lease cannot become a PCM capacity")
        self._pcm_children = set()
        self._pcm_available = size.pcm


def _positive_pcm_bytes(pcm_bytes: int) -> None:
    if type(pcm_bytes) is not int or pcm_bytes <= 0:
        raise ValueError("PCM reservations require positive integer byte bounds")


class AudioLedger:
    """One server's leases, retained across replacement stations by its owner.

    All admission/refund runs synchronously on the event loop. A reserved envelope
    covers player-thread ownership until its final read/copy has released storage;
    deque pops and producer exit alone cannot refund it.
    """

    def __init__(self) -> None:
        self._leases: set[AudioLease] = set()
        self._pcm: PCMOwner | None = None

    @property
    def pcm(self) -> PCMOwner:
        """The server's retained PCM owner, established before unknown producer work.

        Replacement stations reuse this parent and its outstanding children. It is
        deliberately not closed/refunded by a station or producer shutdown.
        """
        from sobafm.owned_pcm import PCMCapacityError, PCMOwner

        if self._pcm is None:
            capacity = self.reserve_pcm_capacity(PCM_LIMIT)
            if capacity is None:
                raise PCMCapacityError
            self._pcm = PCMOwner(capacity)
        return self._pcm

    @property
    def reserved(self) -> AudioBytes:
        result = AudioBytes()
        for lease in self._leases:
            if lease.size is not None:
                result += lease.size
        return result

    @property
    def bounded(self) -> bool:
        return all(lease.size is not None for lease in self._leases)

    def reserve(self, size: AudioBytes) -> AudioLease | None:
        """Reserve a complete qualified envelope, including its future reachable growth.

        Unknown Lyria/native/transport bounds and existing cleanup debt cannot be
        bypassed by a new station or by inspecting present queue lengths.
        """
        if not self.bounded:
            return None
        combined = self.reserved + size
        if (
            combined.pcm > PCM_LIMIT
            or combined.parent > PARENT_LIMIT
            or combined.total >= TOTAL_LIMIT
        ):
            return None
        lease = AudioLease(self, size)
        self._leases.add(lease)
        return lease

    def reserve_pcm_capacity(self, pcm_bytes: int) -> AudioLease | None:
        """Admit a fixed PCM owner before unknown work, using ordinary admission checks.

        Children can spend only this already-charged maximum. This supplies no
        exemption for unknown obligations and selects no runtime capacity size.
        """
        _positive_pcm_bytes(pcm_bytes)
        lease = self.reserve(AudioBytes(pcm=pcm_bytes))
        if lease is not None:
            lease._enable_pcm_capacity()  # pyright: ignore[reportPrivateUsage]
        return lease

    def unbounded(self) -> AudioLease:
        """Track an unqualified obligation without changing generated behavior."""
        lease = AudioLease(self, None)
        self._leases.add(lease)
        return lease

    def _release(self, lease: AudioLease) -> None:
        self._leases.discard(lease)
