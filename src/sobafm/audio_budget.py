"""Shared audio reservations; unknown obligations fail catalog admission closed.

These are allocation ceilings, not measurements or qualification of a producer.
An owner reserves its complete reachable allocation (including growth/copies and
transport overshoot) before starting, and releases only after actual cleanup.
"""

from dataclasses import dataclass

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

    @property
    def size(self) -> AudioBytes | None:
        return self._size

    def release(self) -> None:
        """Refund after the owner verifies all resources covered by this lease are gone."""
        self._ledger._release(self)  # pyright: ignore[reportPrivateUsage]


class AudioLedger:
    """One server's leases, retained across replacement stations by its owner.

    All admission/refund runs synchronously on the event loop. A reserved envelope
    covers player-thread ownership until its final read/copy has released storage;
    deque pops and producer exit alone cannot refund it.
    """

    def __init__(self) -> None:
        self._leases: set[AudioLease] = set()

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

    def unbounded(self) -> AudioLease:
        """Track an unqualified obligation without changing generated behavior."""
        lease = AudioLease(self, None)
        self._leases.add(lease)
        return lease

    def _release(self, lease: AudioLease) -> None:
        self._leases.discard(lease)
