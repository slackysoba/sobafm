"""A deck: one Lyria RealTime session filling a buffer of 20 ms frames (ADR-0004)."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Protocol

from google import genai
from google.genai import errors, types

from sobafm.pcm import BYTES_PER_SECOND, FRAME_SECONDS, FrameSplitter
from sobafm.plan import MusicPlan

log = logging.getLogger(__name__)

MODEL = "models/lyria-realtime-exp"
API_VERSION = "v1beta"

PREROLL_S = 6.0  # audio a deck needs before it can go live
PAUSE_AT_S = 60.0  # generation pauses at this much buffered audio...
RESUME_BELOW_S = 45.0  # ...and resumes below this
RATE_WINDOW_S = 10.0  # generating time before the rate is meaningful


class MusicSession(Protocol):
    """The part of the Google Gen AI SDK's live music session a deck uses."""

    async def set_weighted_prompts(self, prompts: list[types.WeightedPrompt]) -> None: ...

    async def set_music_generation_config(
        self, config: types.LiveMusicGenerationConfig
    ) -> None: ...

    async def play(self) -> None: ...

    async def pause(self) -> None: ...

    def receive(self) -> AsyncIterator[types.LiveMusicServerMessage]: ...


type Connect = Callable[[], AbstractAsyncContextManager[MusicSession]]


def lyria(api_key: str) -> Connect:
    """Open Lyria RealTime sessions with the operator's API key."""
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version=API_VERSION))
    return lambda: client.aio.live.music.connect(model=MODEL)


class State(StrEnum):
    CONNECTING = "connecting"
    GENERATING = "generating"
    ENDED = "ended"


class EndReason(StrEnum):
    RETIRED = "retired"  # SobaFM ended the session
    CLOSED = "closed"  # Lyria ended the session
    FAILED = "failed"  # connecting or receiving failed
    FILTERED = "filtered"  # Lyria refused the prompt before producing audio


class Deck:
    """Fills `frames` from one session. Buffered frames stay playable after the session ends."""

    def __init__(
        self, connect: Connect, plan: MusicPlan, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.plan = plan
        # Appended on the event loop and popped by the mixer on discord.py's player thread;
        # deque appends and pops are thread-safe.
        self.frames: deque[bytes] = deque()
        self.state = State.CONNECTING
        self.end_reason: EndReason | None = None
        self.detail: str | None = None
        self.paused = False
        self._connect = connect
        self._clock = clock
        self._opened_at = clock()
        self._session: MusicSession | None = None
        self._splitter = FrameSplitter()
        self._audio_bytes = 0
        self._generated_s = 0.0
        self._generating_since: float | None = None
        self._retiring = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def age(self) -> float:
        return self._clock() - self._opened_at

    @property
    def buffered_seconds(self) -> float:
        return len(self.frames) * FRAME_SECONDS

    @property
    def ready(self) -> bool:
        return self.buffered_seconds >= PREROLL_S

    @property
    def rate(self) -> float | None:
        """Seconds of audio received per second of unpaused generation."""
        generating = self._generated_s
        if self._generating_since is not None:
            generating += self._clock() - self._generating_since
        if generating < RATE_WINDOW_S:
            return None
        return self._audio_bytes / BYTES_PER_SECOND / generating

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"deck {self.plan.title!r}")

    def retire(self) -> None:
        """End the session; frames already buffered stay playable."""
        self._retiring.set()

    async def wait_ended(self) -> None:
        if self._task is not None:
            await self._task

    async def regulate(self) -> None:
        """Pause generation when the buffer is full and resume it as the buffer drains."""
        session = self._session
        if session is None or self.state is not State.GENERATING:
            return
        if not self.paused and self.buffered_seconds >= PAUSE_AT_S:
            await session.pause()
            self._set_paused(True)
        elif self.paused and self.buffered_seconds < RESUME_BELOW_S:
            await session.play()
            self._set_paused(False)

    async def _run(self) -> None:
        try:
            async with self._connect() as session:
                await session.set_weighted_prompts(prompts=self.plan.weighted_prompts())
                await session.set_music_generation_config(config=self.plan.to_config())
                await session.play()
                self._session = session
                self.state = State.GENERATING
                self._generating_since = self._clock()
                reason = await self._generate(session)
            self._end(reason)
        except errors.APIError as error:
            # A WebSocket close carries its reason in `details`, which the SDK leaves untyped.
            cause: object = error.message or getattr(error, "details", None) or "no reason"
            self._end(EndReason.CLOSED, f"code {error.code}: {cause}")
        except Exception as error:  # a deck failure must never stop the station
            log.warning("Deck for %r failed", self.plan.title, exc_info=True)
            self._end(EndReason.FAILED, type(error).__name__)

    async def _generate(self, session: MusicSession) -> EndReason:
        """Receive audio until Lyria ends or refuses the session, or the deck is retired."""
        receiving = asyncio.create_task(self._receive(session))
        retiring = asyncio.create_task(self._retiring.wait())
        done, _ = await asyncio.wait({receiving, retiring}, return_when=asyncio.FIRST_COMPLETED)
        retiring.cancel()
        if receiving in done:
            return receiving.result()
        receiving.cancel()
        await asyncio.gather(receiving, return_exceptions=True)
        return EndReason.RETIRED

    async def _receive(self, session: MusicSession) -> EndReason:
        async for message in session.receive():
            if message.filtered_prompt is not None and self._audio_bytes == 0:
                self.detail = message.filtered_prompt.filtered_reason
                return EndReason.FILTERED
            content = message.server_content
            for chunk in (content.audio_chunks or []) if content else []:
                data = chunk.data or b""
                self._audio_bytes += len(data)
                self.frames.extend(self._splitter.split(data))
        return EndReason.CLOSED

    def _set_paused(self, paused: bool) -> None:
        now = self._clock()
        if paused and self._generating_since is not None:
            self._generated_s += now - self._generating_since
            self._generating_since = None
        elif not paused:
            self._generating_since = now
        self.paused = paused

    def _end(self, reason: EndReason, detail: str | None = None) -> None:
        self._set_paused(True)
        self._session = None
        self.state = State.ENDED
        self.end_reason = reason
        self.detail = detail or self.detail
        rate = self.rate
        log.info(
            "Deck for %r %s%s after %.0f s: %.0f s of audio%s",
            self.plan.title,
            reason,
            f" ({self.detail})" if self.detail else "",
            self.age,
            self._audio_bytes / BYTES_PER_SECOND,
            f" at {rate:.2f}x real time" if rate is not None else "",
        )
