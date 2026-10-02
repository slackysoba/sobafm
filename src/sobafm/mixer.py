"""The audio source discord.py plays: the live deck, crossfades, fades, and volume (ADR-0004)."""

import logging
import math
import threading
from collections import deque
from typing import Protocol, override

import audioop
import discord

from sobafm.pcm import FRAME_SECONDS, SAMPLE_WIDTH, SILENCE

log = logging.getLogger(__name__)

VOLUME_RAMP_S = 1.0  # a full-scale volume change takes this long, so it never clicks
VOLUME_STEP = FRAME_SECONDS / VOLUME_RAMP_S  # the most the gain moves per frame


class FrameSource(Protocol):
    """Anything that buffers frames for the mixer, such as a deck."""

    @property
    def frames(self) -> deque[bytes]: ...


class Mixer(discord.AudioSource):
    """Plays the live source and switches between sources with equal-power fades.

    `read()` runs on discord.py's player thread every 20 ms. It always returns one frame,
    never blocks, and never raises: silence covers underruns and errors, so the player never
    stops on its own. Fades are counted in frames, so they pause intact while voice reconnects.
    """

    def __init__(self, volume: float) -> None:
        self._lock = threading.Lock()  # held for one read's pops and mixing, or a setter
        self._live: FrameSource | None = None
        self._incoming: FrameSource | None = None
        self._switch_frames = 0
        self._switch_position = 0
        self._volume = volume
        self._gain = volume
        self._failing = False
        self.underruns = 0  # reads with a playing source but no frame from it
        self.errors = 0  # reads that failed; they play silence too

    @property
    def live(self) -> FrameSource | None:
        with self._lock:
            return self._live

    @property
    def switching(self) -> bool:
        with self._lock:
            return self._switch_frames > 0

    def set_volume(self, volume: float) -> None:
        """Change the volume (linear gain, 1.0 unchanged); it ramps to avoid clicks."""
        with self._lock:
            self._volume = volume

    def switch_to(self, source: FrameSource | None, seconds: float) -> None:
        """Fade from the live source to `source`, or to silence when `source` is None.

        A crossfade or fade-out never outlasts the audio the live source still holds, and a
        live source that has run dry is dropped, so `source` fades in from silence. Switching
        to the current target changes nothing. A new target during a switch makes the
        incoming source live at once, then fades from it.
        """
        with self._lock:
            if self._switch_frames:
                if source is self._incoming:
                    return
                self._live, self._incoming, self._switch_frames = self._incoming, None, 0
            if source is self._live:
                return
            if self._live is not None and not self._live.frames:
                self._live = None
            frames = round(seconds / FRAME_SECONDS)
            if self._live is not None:
                frames = min(frames, len(self._live.frames))
            if frames <= 0 or (self._live is None and source is None):
                self._live = source
                return
            self._incoming, self._switch_frames, self._switch_position = source, frames, 0

    @override
    def read(self) -> bytes:
        with self._lock:
            try:
                frame = self._next_frame()
            except Exception as error:  # noqa: BLE001 - an exception would stop discord.py's player
                self.errors += 1
                if self._switch_frames:  # a failing source must not hold a switch open
                    self._live, self._incoming, self._switch_frames = self._incoming, None, 0
                failure = error
            else:
                self._failing = False
                return frame
        if not self._failing:  # one traceback per run of failures, logged outside the lock
            log.error("Mixer read failed; playing silence", exc_info=failure)
            self._failing = True
        return SILENCE

    def _next_frame(self) -> bytes:
        self._gain += max(-VOLUME_STEP, min(VOLUME_STEP, self._volume - self._gain))
        if not self._switch_frames:
            sources = [(self._live, self._gain)]
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
        frames = [(f, gain) for s, gain in playing if (f := _pop(s)) is not None]
        if not frames:
            if playing:
                self.underruns += 1
            return SILENCE
        mixed = _scaled(*frames[0])
        for frame, gain in frames[1:]:
            mixed = audioop.add(mixed, _scaled(frame, gain), SAMPLE_WIDTH)
        return mixed


def _pop(source: FrameSource) -> bytes | None:
    try:
        return source.frames.popleft()
    except IndexError:
        return None


def _scaled(frame: bytes, gain: float) -> bytes:
    return frame if gain == 1.0 else audioop.mul(frame, SAMPLE_WIDTH, gain)
