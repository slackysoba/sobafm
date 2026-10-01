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

VOLUME_STEP = FRAME_SECONDS  # gain moves at most this much per frame: full scale in one second


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
        self._lock = threading.Lock()
        self._live: FrameSource | None = None
        self._incoming: FrameSource | None = None
        self._switch_frames = 0
        self._switch_position = 0
        self._volume = volume
        self._gain = volume
        self.underruns = 0

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

        A crossfade or fade-out never outlasts the audio the live source still holds. Callers
        let a switch finish before starting another.
        """
        with self._lock:
            frames = round(seconds / FRAME_SECONDS)
            if self._live is not None:
                frames = min(frames, len(self._live.frames))
            if frames <= 0:
                self._live, self._incoming, self._switch_frames = source, None, 0
                return
            self._incoming = source
            self._switch_frames = frames
            self._switch_position = 0

    @override
    def read(self) -> bytes:
        try:
            with self._lock:
                return self._next_frame()
        except Exception:  # an exception would stop discord.py's player
            log.exception("Mixer read failed")
            return SILENCE

    @override
    def is_opus(self) -> bool:
        return False

    def _next_frame(self) -> bytes:
        self._gain += max(-VOLUME_STEP, min(VOLUME_STEP, self._volume - self._gain))
        if self._switch_frames == 0:
            return self._scaled(self._take(self._live), self._gain)
        progress = (self._switch_position + 0.5) / self._switch_frames
        outgoing = self._scaled(
            self._take(self._live), self._gain * math.cos(progress * math.pi / 2)
        )
        incoming = self._scaled(
            self._take(self._incoming), self._gain * math.sin(progress * math.pi / 2)
        )
        self._switch_position += 1
        if self._switch_position >= self._switch_frames:
            self._live, self._incoming, self._switch_frames = self._incoming, None, 0
        return audioop.add(outgoing, incoming, SAMPLE_WIDTH)

    def _take(self, source: FrameSource | None) -> bytes:
        if source is None:
            return SILENCE
        try:
            return source.frames.popleft()
        except IndexError:
            self.underruns += 1
            return SILENCE

    @staticmethod
    def _scaled(frame: bytes, gain: float) -> bytes:
        return frame if gain == 1.0 else audioop.mul(frame, SAMPLE_WIDTH, gain)
