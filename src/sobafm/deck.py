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
from google.genai import errors, live_music, types

from sobafm.failures import Failure, as_token, call_failure, close_failure
from sobafm.pcm import BYTES_PER_SECOND, FRAME_SECONDS, FrameSplitter, matches_mime_type
from sobafm.plan import MusicPlan

log = logging.getLogger(__name__)

MODEL = "models/lyria-realtime-exp"
API_VERSION = "v1beta"

PREROLL_S = 6.0  # audio a deck needs before it can go live
PAUSE_AT_S = 60.0  # generation pauses at this much buffered audio...
RESUME_BELOW_S = 45.0  # ...and resumes below this
RATE_WINDOW_S = 10.0  # generating time before the rate is meaningful
CONNECT_TIMEOUT_S = 15.0  # connecting and starting playback
REGULATE_TIMEOUT_S = 0.5  # a pause or resume, which a closing session can stall


def close_detail(code: int, reason: object) -> str:
    """A close's code, with its reason only if that is a token, as free text could quote the key.

    Gemini's error messages can quote the API key (OPS-5), and Lyria's close reasons may too.
    """
    return f"code {code}: {token}" if (token := as_token(reason)) else f"code {code}"


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


class LyriaMessageError(Exception):
    """A message from Lyria RealTime that SobaFM can't use.

    It carries a fixed description: the original error could quote anything Lyria sent, such as
    the frame the SDK failed to parse, so neither its text nor its traceback is kept (OPS-5).
    """


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
        self.close_code: int | None = None  # how a closed session ended, such as 1008 on refusal
        self.failure: Failure | None = None  # why the session failed, when known
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
    def delivered_preroll(self) -> bool:
        """Whether the session produced the pre-roll's worth of audio, as one that went live did."""
        return self._audio_bytes >= PREROLL_S * BYTES_PER_SECOND

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
            self._task.add_done_callback(self._cancelled)

    def retire(self) -> None:
        """End the session; frames already buffered stay playable.

        A deck still connecting is cancelled, rather than finishing a setup that can stall,
        unless a cancellation is pending already, such as the connect timeout's: another would
        cut short the SDK's close of the socket. A generating deck ends through `_retiring`, so
        once the SDK has reported Lyria's close, the deck keeps its end reason and close code.
        """
        self._retiring.set()
        task = self._task
        if self.state is State.CONNECTING and task is not None and not task.cancelling():
            task.cancel()

    async def wait_ended(self) -> None:
        """Wait for the session to end, however it ends."""
        if self._task is not None:
            await asyncio.wait({self._task})

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
            # Bounded, so a socket whose close hangs cannot stall the station's ticks. A pause
            # abandoned on a backed-up socket still arrives, and the next tick sends it again.
            async with asyncio.timeout(REGULATE_TIMEOUT_S):
                await (session.pause() if pause else session.play())
        except Exception:  # noqa: BLE001 - the session is ending, or the next tick retries
            log.debug("Deck %d could not %s", self.number, "pause" if pause else "resume")
            return
        if self._session is session:
            self._set_generating(not pause)
            self.paused = pause

    async def _run(self) -> None:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S) as connecting:
                async with self._connect() as session:
                    await session.set_weighted_prompts(prompts=self.plan.weighted_prompts())
                    await session.set_music_generation_config(config=self.plan.to_config())
                    await session.play()
                    connecting.reschedule(None)
                    reason = await self._generate(session)
            self._end(reason)
        except errors.APIError as error:
            # A WebSocket close carries its reason in `details`, which the SDK leaves untyped.
            cause: object = error.message or getattr(error, "details", None)
            self.close_code = error.code
            self.failure = close_failure(error.code, cause)
            self._end(EndReason.CLOSED, close_detail(error.code, cause))
        except live_music.ConnectionClosed as error:
            # Closed during setup, as on a refusal. A traceback would quote the close reason.
            if (close := error.rcvd) is not None:
                self.close_code = close.code
                self.failure = close_failure(close.code, close.reason)
                detail = close_detail(close.code, close.reason)
            else:
                self.failure = Failure.UNAVAILABLE  # the connection dropped
                detail = "no close frame received"
            detail = f"{type(error).__name__}: {detail}"
            log.warning("Deck %d failed: %s", self.number, detail)
            self._end(EndReason.FAILED, detail)
        except Exception as error:  # a deck failure must never stop the station
            log.warning("Deck %d failed", self.number, exc_info=True)
            self.failure = call_failure(error)
            detail = type(error).__name__ + (f": {error}" if str(error) else "")
            self._end(EndReason.FAILED, detail[:200])

    def _cancelled(self, task: asyncio.Task[None]) -> None:
        """Record a deck cancelled by `retire()` while connecting, or at shutdown, as retired."""
        if task.cancelled():
            self._end(EndReason.RETIRED)

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
        messages = aiter(session.receive())
        while True:
            try:
                message = await anext(messages)
            except StopAsyncIteration:
                return EndReason.CLOSED
            except ValueError:  # the SDK's errors for a frame it can't parse or validate quote it
                message = None
            if message is None:  # raised outside the handler, so the SDK's error isn't attached
                raise LyriaMessageError("unreadable message")
            if message.filtered_prompt is not None and self._audio_bytes == 0:
                reason = message.filtered_prompt.filtered_reason
                log.debug("Deck %d: Lyria filtered the prompt: %s", self.number, reason)
                self.detail = as_token(reason)  # free text could quote the request
                return EndReason.FILTERED
            content = message.server_content
            for chunk in (content.audio_chunks or []) if content else []:
                if chunk.mime_type is not None and not matches_mime_type(chunk.mime_type):
                    raise LyriaMessageError("unexpected audio format")
                data = chunk.data or b""
                self._audio_bytes += len(data)
                self.frames.extend(self._splitter.split(data))

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
        notes = [self.detail] if self.detail else []
        if self.failure is not None and not self.delivered_preroll:  # why it couldn't go live
            notes.append(self.failure)
        log.info(
            "Deck %d %s%s after %.0f s: %.0f s of audio%s",
            self.number,
            reason,
            f" ({', '.join(notes)})" if notes else "",
            self.age,
            self._audio_bytes / BYTES_PER_SECOND,
            f" at {rate:.2f}x real time" if rate is not None else "",
        )
