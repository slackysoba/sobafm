"""The audio source discord.py plays: the live deck, crossfades, fades, and volume (ADR-0004)."""

import asyncio
import logging
import math
import threading
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol, override

import audioop
import discord

from sobafm.owned_pcm import OwnedPCM, PCMOwner, PCMReceipt, QuiescentPCMSource
from sobafm.pcm import FRAME_BYTES, FRAME_SECONDS, SAMPLE_WIDTH, SILENCE
from sobafm.source import Source

log = logging.getLogger(__name__)

VOLUME_RAMP_S = 1.0  # a full-scale volume change takes this long, so it never clicks
VOLUME_STEP = FRAME_SECONDS / VOLUME_RAMP_S  # the most the gain moves per frame


class FrameSource(Protocol):
    """Anything that buffers frames for the mixer, such as a deck."""

    @property
    def frames(self) -> deque[OwnedPCM]: ...


@dataclass(frozen=True)
class Handoff:
    outgoing: Source | None
    incoming: Source
    program: object
    epoch: object
    drain: bool
    complete: bool


class Mixer(discord.AudioSource):
    """Plays the live source and switches between sources with equal-power fades.

    `read()` runs on discord.py's player thread every 20 ms. It always returns one frame,
    never blocks, and never raises: silence covers underruns and errors, so the player never
    stops on its own. Fades are counted in frames, so they pause intact while voice reconnects.
    """

    def __init__(
        self, volume: float, pcm: PCMOwner, loop: asyncio.AbstractEventLoop | None = None
    ) -> None:
        self._pcm, self._loop = pcm, loop
        self._player: QuiescentPCMSource | None = None
        self._blocked = False
        self._lock = threading.Lock()  # held for one read's pops and mixing, or a setter
        self._live: FrameSource | None = None
        self._incoming: FrameSource | None = None
        self._switch_frames = 0
        self._switch_position = 0
        self._volume = volume
        self._gain = volume
        self._failing = False
        self._program: object | None = None
        self._epoch: object | None = None
        self._armed: Handoff | None = None
        self._delivered: Handoff | None = None
        self._read_live: int | None = None  # identity held alive by _live after a successful read
        self._connected: Callable[[], bool] = lambda: False
        self._wake: Callable[[], None] = lambda: None
        self.underruns = 0  # reads with a playing source but no frame from it
        self.played = 0  # reads that returned audio
        self.errors = 0  # reads that failed; they play silence too

    def player_source(self) -> QuiescentPCMSource | None:
        """Prepay three mixing copies plus the supported caller's previous returned frame."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        source = self._pcm.wrap(self, self._loop, 4 * FRAME_BYTES)
        if source is not None:
            with self._lock:
                self._player = source
                self._blocked = False
        return source

    @override
    def cleanup(self) -> None:
        with self._lock:
            self._player = None

    @override
    def __del__(self) -> None:
        pass  # only the exact public wrapper cleanup acknowledges its player epoch

    @staticmethod
    def _released(receipts: tuple[PCMReceipt, ...]) -> None:
        for receipt in receipts:
            receipt.release()

    def _recover(self, player: QuiescentPCMSource) -> None:
        with self._lock:
            if self._player is player and player.renew_staging(4 * FRAME_BYTES):
                self._blocked = False

    @property
    def live(self) -> FrameSource | None:
        with self._lock:
            return self._live

    @property
    def switching(self) -> bool:
        with self._lock:
            return self._switch_frames > 0

    def uses(self, source: FrameSource) -> bool:
        """Whether `source` is playing or fading in."""
        with self._lock:
            return (
                source is self._live
                or source is self._incoming
                or (
                    self._armed is not None
                    and (source is self._armed.outgoing or source is self._armed.incoming)
                )
                or (
                    self._delivered is not None
                    and (source is self._delivered.outgoing or source is self._delivered.incoming)
                )
            )

    def bind(
        self,
        program: object | None,
        epoch: object | None,
        connected: Callable[[], bool],
        wake: Callable[[], None],
    ) -> None:
        """Install exact request/player identity and pure connected-state/wakeup callbacks."""
        with self._lock:
            if program is not self._program or epoch is not self._epoch:
                self._armed = self._delivered = None
            self._program, self._epoch = program, epoch
            self._connected, self._wake = connected, wake

    def invalidate_handoff(self, epoch: object | None = None) -> None:
        """Invalidate synchronously; a late old-player callback cannot cancel its successor."""
        with self._lock:
            if epoch is None or epoch is self._epoch:
                self._armed = self._delivered = None
                self._epoch = None
                self._program = None

    def arm(
        self,
        outgoing: Source | None,
        incoming: Source,
        program: object,
        epoch: object,
        *,
        drain: bool,
        complete: bool,
    ) -> Handoff | None:
        with self._lock:
            if (
                self._switch_frames
                or self._delivered is not None
                or self._live is not outgoing
                or self._program is not program
                or self._epoch is not epoch
            ):
                return None
            self._armed = Handoff(outgoing, incoming, program, epoch, drain, complete)
            return self._armed

    def withdraw(self, handoff: Handoff) -> Handoff | None:
        """Atomically withdraw this arm, or return its already-delivered PCM receipt."""
        with self._lock:
            if self._delivered is handoff:
                self._delivered = None
                return handoff
            if self._armed is handoff:
                self._armed = None
            return None

    def take_handoff(self) -> Handoff | None:
        with self._lock:
            result, self._delivered = self._delivered, None
            return result

    def set_volume(self, volume: float) -> None:
        """Change the volume (linear gain, 1.0 unchanged); it ramps to avoid clicks."""
        with self._lock:
            self._volume = volume

    def switch_to(self, source: FrameSource | None, seconds: float) -> None:
        """Fade from the live source to `source`, or to silence when `source` is None.

        A crossfade or fade-out never outlasts the audio the live source still holds, and a
        live source that has run dry is dropped, so `source` fades in from silence. Switching
        to the current target changes nothing, except that an instant switch finishes the one
        in progress. A new target during a switch makes the incoming source live at once,
        then fades from it.
        """
        frames = round(seconds / FRAME_SECONDS)
        with self._lock:
            self._armed = None
            self._read_live = None
            if self._switch_frames:
                if source is self._incoming and frames > 0:
                    return
                self._live, self._incoming, self._switch_frames = self._incoming, None, 0
            if source is self._live:
                return
            if self._live is not None and not self._live.frames:
                self._live = None
            if self._live is not None:
                frames = min(frames, len(self._live.frames))
            if frames <= 0 or (self._live is None and source is None):
                self._live = source
                return
            self._incoming, self._switch_frames, self._switch_position = source, frames, 0

    @override
    def read(self) -> bytes:
        wake: Callable[[], None] | None = None
        failure: Exception | None = None
        recovery: QuiescentPCMSource | None = None
        borrowed: list[OwnedPCM] = []
        frame = SILENCE
        loop = self._loop
        with self._lock:
            if self._player is None or self._blocked or loop is None:
                return frame
            previous_delivery = self._delivered
            try:
                frame = self._next_frame(borrowed)
            except Exception as error:  # noqa: BLE001 - an exception would stop discord.py's player
                self.errors += 1
                self._blocked = True  # another read cannot reuse potentially retained scratch
                self._player.uncertain = True
                recovery = self._player
                if self._switch_frames:  # a failing source must not hold a switch open
                    self._live, self._incoming, self._switch_frames = self._incoming, None, 0
                failure = error
            else:
                self._failing = False
                if self._delivered is not None and self._delivered is not previous_delivery:
                    wake = self._wake
        if failure is None:
            receipts = tuple(owned.receipt for owned in borrowed)
            borrowed.clear()  # callback owns receipts only, after _next_frame's aliases have gone
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._released, receipts)
        elif recovery is not None:
            # The exception/log owner may retain all borrowed PCM and working
            # copies. Recovery spends new prepaid scratch on the loop instead.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._recover, recovery)
        if wake is not None:
            with suppress(RuntimeError):  # the owning event loop may have closed
                wake()  # only a bounded loop wakeup, after successful PCM return construction
        if failure is None:
            return frame
        if not self._failing:  # one traceback per run of failures, logged outside the lock
            log.error("Mixer read failed; playing silence", exc_info=failure)
            self._failing = True
        del failure  # its traceback holds this frame, which would hold it in a cycle
        return SILENCE

    def _next_frame(self, borrowed: list[OwnedPCM]) -> bytes:
        handoff = self._armed
        selected: Source | None = None
        if handoff is not None:
            if (
                self._epoch is not handoff.epoch
                or self._program is not handoff.program
                or handoff.incoming.program is not handoff.program
                or handoff.incoming.failed
                or not self._connected()
                or self._live is not handoff.outgoing
            ):
                self._armed = None
            elif handoff.incoming.ready or (
                handoff.incoming is handoff.outgoing
                and id(handoff.incoming) == self._read_live
                and bool(handoff.incoming.frames)
            ):
                drained = handoff.outgoing is None or not handoff.outgoing.frames
                verified = not handoff.complete or (
                    handoff.outgoing is not None
                    and handoff.outgoing.completed
                    and not handoff.outgoing.failed
                )
                if (not handoff.drain or drained) and verified:
                    selected = handoff.incoming
        self._gain += max(-VOLUME_STEP, min(VOLUME_STEP, self._volume - self._gain))
        if not self._switch_frames:
            sources = [(selected if selected is not None else self._live, self._gain)]
        else:
            angle = (self._switch_position + 0.5) / self._switch_frames * math.pi / 2
            sources = [
                (self._live, self._gain * math.cos(angle)),
                (self._incoming, self._gain * math.sin(angle)),
            ]
            self._switch_position += 1
            if self._switch_position >= self._switch_frames:
                self._live, self._incoming, self._switch_frames = self._incoming, None, 0
        playing = [(s, gain) for s, gain in sources if s is not None]
        frames: list[tuple[bytes, float]] = []
        for source, gain in playing:
            if (owned := _pop(source)) is not None:
                borrowed.append(owned)
                frames.append((owned.data, gain))
        if not frames:
            if playing:
                self.underruns += 1
            return SILENCE
        mixed = _scaled(*frames[0])
        for frame, gain in frames[1:]:
            mixed = audioop.add(mixed, _scaled(frame, gain), SAMPLE_WIDTH)
        self.played += 1  # once it has mixed, so a failed read never counts
        if selected is not None and handoff is not None:
            self._live, self._armed, self._delivered = selected, None, handoff
            self._read_live = id(selected)
        return mixed


def _pop(source: FrameSource) -> OwnedPCM | None:
    try:
        return source.frames.popleft()
    except IndexError:
        return None


def _scaled(frame: bytes, gain: float) -> bytes:
    # Even unity gain returns new staging storage: the source receipt can then
    # finish independently of the caller retaining this output through its send.
    return memoryview(frame).tobytes() if gain == 1.0 else audioop.mul(frame, SAMPLE_WIDTH, gain)
