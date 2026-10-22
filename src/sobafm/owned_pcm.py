"""Explicit event-loop ownership of SobaFM's Python PCM backings and player staging.

Receipts contain no audio. There is no GC refund: a queue transfers ownership to
its reader, which acknowledges only after dropping every backing alias it owns.
SDK, native and transport allocations are separate, unqualified obligations.
"""

import asyncio
from dataclasses import dataclass
from typing import override

import discord

from sobafm.audio_budget import AudioLease


class PCMCapacityError(Exception):
    """Fixed local resource failure; carries no input or provider exception."""


@dataclass
class _Backing:
    storage: PCMStorage
    lease: AudioLease
    size: int
    aliases: int = 0


class PCMReceipt:
    """An idempotent alias acknowledgement of a shared backing's one child lease."""

    def __init__(self, backing: _Backing) -> None:
        self._backing = backing
        self._released = False
        backing.aliases += 1

    def borrow(self) -> PCMReceipt:
        if self._released:
            raise RuntimeError("PCM storage already released")
        return PCMReceipt(self._backing)

    def release(self) -> None:
        backing = self._backing
        if self._released or backing.storage.uncertain:
            return
        self._released = True
        backing.aliases -= 1
        if backing.aliases == 0:
            backing.lease.release()
            backing.storage.used -= backing.size


@dataclass(frozen=True)
class OwnedPCM:
    data: bytes
    receipt: PCMReceipt

    def borrow(self) -> OwnedPCM:
        return OwnedPCM(self.data, self.receipt.borrow())


class PCMStorage:
    """A source's children of the one prepaid parent; limit includes popped borrows."""

    def __init__(self, capacity: AudioLease, limit: int) -> None:
        self._capacity, self.limit = capacity, limit
        self.used = 0
        self.uncertain = False

    def reserve(self, size: int) -> PCMReceipt:
        if self.uncertain or size <= 0 or self.used + size > self.limit:
            raise PCMCapacityError
        lease = self._capacity.reserve_pcm(size)
        if lease is None:
            raise PCMCapacityError
        self.used += size
        return PCMReceipt(_Backing(self, lease, size))

    def copy(self, data: memoryview) -> OwnedPCM:
        receipt = self.reserve(len(data))
        try:
            return OwnedPCM(data.tobytes(), receipt)
        except BaseException:
            # An escaping allocation error may own audio in its traceback/attributes.
            self.uncertain = True
            raise

    def join(self, prefix: bytes, suffix: memoryview) -> OwnedPCM:
        receipt = self.reserve(len(prefix) + len(suffix))
        try:
            # join accepts buffers: no separately allocated suffix copy is needed.
            return OwnedPCM(b"".join((prefix, suffix)), receipt)
        except BaseException:
            self.uncertain = True
            raise

    def silence(self, size: int) -> OwnedPCM:
        receipt = self.reserve(size)
        try:
            return OwnedPCM(bytes(size), receipt)
        except BaseException:
            self.uncertain = True
            raise


class PCMOwner:
    """One server's parent and exact public-cleanup player epoch, retained by its ledger."""

    def __init__(self, capacity: AudioLease) -> None:
        from sobafm.pcm import SILENCE

        self.capacity = capacity
        # This immutable global backing outlives every player. Keep its child
        # separate from transient mixing/caller scratch, once per server owner.
        self._silence = self.storage(len(SILENCE)).reserve(len(SILENCE))
        self._epoch: object | None = None
        self._active: QuiescentPCMSource | None = None

    def storage(self, limit: int) -> PCMStorage:
        return PCMStorage(self.capacity, limit)

    def wrap(
        self, source: discord.AudioSource, loop: asyncio.AbstractEventLoop, staging_bytes: int
    ) -> QuiescentPCMSource | None:
        if self._epoch is not None:
            return None
        try:
            receipt = self.storage(staging_bytes).reserve(staging_bytes)
        except PCMCapacityError:
            return None
        epoch = self._epoch = object()
        self._active = QuiescentPCMSource(source, loop, self, epoch, receipt)
        return self._active

    def finished(self, epoch: object, receipt: PCMReceipt, uncertain: bool) -> None:
        if self._epoch is not epoch:
            return  # an old callback never releases a successor or its admission gate
        self._epoch = None
        self._active = None
        if not uncertain:
            receipt.release()


class QuiescentPCMSource(discord.AudioSource):
    """The public caller boundary; clean cleanup drops the delegate before loop admission.

    stop/after are insufficient. Failed cleanup or a closed loop keeps the epoch
    closed. Unknown errors keep staging debt even after a clean public cleanup.
    """

    def __init__(
        self,
        source: discord.AudioSource,
        loop: asyncio.AbstractEventLoop,
        owner: PCMOwner,
        epoch: object,
        receipt: PCMReceipt,
    ) -> None:
        self._source: discord.AudioSource | None = source
        self._loop, self._owner = loop, owner
        self._epoch, self._receipt = epoch, receipt
        self.uncertain = False
        self._cleaned = False
        self._extra_staging: list[PCMReceipt] = []

    def renew_staging(self, size: int) -> bool:
        """After an uncertain read, fund recovery on the loop; never reuse its old scratch."""
        if self._cleaned:
            return False
        try:
            receipt = self._owner.storage(size).reserve(size)
        except PCMCapacityError:
            return False
        self._extra_staging.append(receipt)
        return True

    @override
    def read(self) -> bytes:
        source = self._source
        return b"" if source is None else source.read()

    @override
    def __del__(self) -> None:
        # AudioSource's default finalizer calls cleanup. Collection is not the
        # public player's quiescence certificate and must never acknowledge debt.
        pass

    @override
    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        source, self._source = self._source, None
        try:
            if source is not None:
                source.cleanup()
        finally:
            source = None
        self._loop.call_soon_threadsafe(
            self._owner.finished, self._epoch, self._receipt, self.uncertain
        )
