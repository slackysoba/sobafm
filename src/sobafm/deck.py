"""A deck: one Lyria RealTime session filling a buffer of 20 ms frames (ADR-0004)."""

import asyncio
import itertools
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Protocol

from google import genai
from google.genai import errors, types

from sobafm.pcm import BYTES_PER_SECOND, FRAME_SECONDS, MIME_TYPE, FrameSplitter
from sobafm.plan import MusicPlan

log = logging.getLogger(__name__)

MODEL = "models/lyria-realtime-exp"
API_VERSION = "v1beta"

PREROLL_S = 6.0  # audio a deck needs before it can go live
PAUSE_AT_S = 60.0  # generation pauses at this much buffered audio...
RESUME_BELOW_S = 45.0  # ...and resumes below this
RATE_WINDOW_S = 10.0  # generating time before the rate is meaningful
CONNECT_TIMEOUT_S = 15.0  # connecting and starting playback


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
    CLOSED = "closed"  # the session closed while receiving, usually by Lyria
    FAILED = "failed"  # connecting, starting, or receiving failed
    FILTERED = "filtered"  # Lyria refused the prompt before producing audio


class Deck:
    """Fills `frames` from one session. Buffered frames stay playable after the session ends.

    Logs identify a deck by its number, because the plan's title can repeat the request text,
    which is logged only at debug level (OPS-5).
    """

    _numbers = itertools.count(1)

    def __init__(
        self, connect: Connect, plan: MusicPlan, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.plan = plan
        self.number = next(Deck._numbers)
        # Appended on the event loop and popped by the mixer on discord.py's player thread;
        # deque appends and pops are thread-safe.
        self.frames: deque[bytes] = deque()
        self.state = State.CONNECTING
        self.end_reason: EndReason | None = None
        self.detail: str | None = None
        self.paused = False
        self._connect = connect
        self._clock = clock
        self._created_at = clock()
        self._session: MusicSession | None = None
        self._splitter = FrameSplitter()
        self._audio_bytes = 0
        self._generated_s = 0.0
        self._generating_since: float | None = None
        self._retiring = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def age(self) -> float:
        """Seconds since the deck was created, slightly more than its session's age."""
        return self._clock() - self._created_at

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
        if self._task is None:
            log.debug("Deck %d plays %r", self.number, self.plan.title)
            self._task = asyncio.create_task(self._run(), name=f"deck {self.number}")

    def retire(self) -> None:
        """End the session; frames already buffered stay playable."""
        self._retiring.set()

    async def wait_ended(self) -> None:
        if self._task is not None:
            await self._task

    async def regulate(self) -> None:
        """Pause generation when the buffer is full and resume it as the buffer drains."""
        session = self._session
        if session is None:
            return
        if not self.paused and self.buffered_seconds >= PAUSE_AT_S:
            pause = True
        elif self.paused and self.buffered_seconds < RESUME_BELOW_S:
            pause = False
        else:
            return
        try:
            await (session.pause() if pause else session.play())
        except Exception:  # noqa: BLE001 - the session is ending, and the receive task records why
            log.debug("Deck %d could not %s", self.number, "pause" if pause else "resume")
            return
        if self._session is session:
            self._set_generating(not pause)
            self.paused = pause

    async def _run(self) -> None:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S) as connecting:
                async with self._connect() as session:
                    if self._retiring.is_set():  # retired while connecting: never start playback
                        reason = EndReason.RETIRED
                    else:
                        await session.set_weighted_prompts(prompts=self.plan.weighted_prompts())
                        await session.set_music_generation_config(config=self.plan.to_config())
                        await session.play()
                        connecting.reschedule(None)
                        reason = await self._generate(session)
            self._end(reason)
        except errors.APIError as error:
            # A WebSocket close carries its reason in `details`, which the SDK leaves untyped.
            cause: object = error.message or getattr(error, "details", None) or "no reason"
            self._end(EndReason.CLOSED, f"code {error.code}: {cause}")
        except asyncio.CancelledError:
            self._end(EndReason.RETIRED)
            raise
        except Exception as error:  # a deck failure must never stop the station
            log.warning("Deck %d failed", self.number, exc_info=True)
            self._end(EndReason.FAILED, f"{type(error).__name__}: {error}"[:200])

    async def _generate(self, session: MusicSession) -> EndReason:
        """Receive audio until Lyria ends or refuses the session, or the deck is retired."""
        self._session = session
        self.state = State.GENERATING
        self._set_generating(True)
        receiving = asyncio.create_task(self._receive(session))
        retiring = asyncio.create_task(self._retiring.wait())
        try:
            done, _ = await asyncio.wait({receiving, retiring}, return_when=asyncio.FIRST_COMPLETED)
            return receiving.result() if receiving in done else EndReason.RETIRED
        finally:
            self._session = None  # before the SDK closes the socket, so nothing else is sent
            for task in (receiving, retiring):
                task.cancel()
            await asyncio.gather(receiving, retiring, return_exceptions=True)

    async def _receive(self, session: MusicSession) -> EndReason:
        async for message in session.receive():
            if message.filtered_prompt is not None and self._audio_bytes == 0:
                self.detail = message.filtered_prompt.filtered_reason
                return EndReason.FILTERED
            content = message.server_content
            for chunk in (content.audio_chunks or []) if content else []:
                if chunk.mime_type not in (None, MIME_TYPE):
                    raise ValueError(f"unexpected audio format {chunk.mime_type}")
                data = chunk.data or b""
                self._audio_bytes += len(data)
                self.frames.extend(self._splitter.split(data))
        return EndReason.CLOSED

    def _set_generating(self, generating: bool) -> None:
        """Start or stop the clock behind `rate`."""
        now = self._clock()
        if not generating and self._generating_since is not None:
            self._generated_s += now - self._generating_since
            self._generating_since = None
        elif generating and self._generating_since is None:
            self._generating_since = now

    def _end(self, reason: EndReason, detail: str | None = None) -> None:
        self._set_generating(False)
        self._session = None
        self.state = State.ENDED
        self.end_reason = reason
        self.detail = detail or self.detail
        rate = self.rate
        log.info(
            "Deck %d %s%s after %.0f s: %.0f s of audio%s",
            self.number,
            reason,
            f" ({self.detail})" if self.detail else "",
            self.age,
            self._audio_bytes / BYTES_PER_SECOND,
            f" at {rate:.2f}x real time" if rate is not None else "",
        )
