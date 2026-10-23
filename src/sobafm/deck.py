"""A deck: one Lyria RealTime session filling a buffer of 20 ms frames (ADR-0004)."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, closing, suppress
from enum import StrEnum
from typing import Protocol

import websockets.asyncio.client
from google import genai
from google.genai import errors, live_music, types
from google.genai.json_shape_preflight import ShapeLimits

# ADR-0010 documents this public SDK re-export; its export annotation is absent.
from google.genai.live_music import (
    BoundedReceiveOptions as BoundedReceiveOptions,  # pyright: ignore[reportPrivateImportUsage]
)
from websockets.exceptions import InvalidStatus, WebSocketException

from sobafm.audio_budget import LYRIA_RECEIVE_BYTES, PCM_LIMIT, AudioLedger, ReceiveBacking
from sobafm.failures import Failure, as_token, call_failure, close_failure
from sobafm.owned_pcm import OwnedPCM, PCMCapacityError
from sobafm.pcm import BYTES_PER_SECOND, FRAME_SECONDS, FrameSplitter, matches_mime_type
from sobafm.plan import MusicPlan
from sobafm.program import Mode, Program
from sobafm.source import State as State
from sobafm.source import next_source_number

log = logging.getLogger(__name__)

MODEL = "models/lyria-realtime-exp"
API_VERSION = "v1beta"

PREROLL_S = 6.0  # audio a deck needs before it can go live
PAUSE_AT_S = 60.0  # generation pauses at this much buffered audio...
RESUME_BELOW_S = 45.0  # ...and resumes below this
RATE_WINDOW_S = 10.0  # generating time before the rate is meaningful
CONNECT_TIMEOUT_S = 15.0  # connecting and starting playback
REGULATE_TIMEOUT_S = 0.5  # a pause or resume, which a closing session can stall
CLEANUP_SECONDS = 3.0  # one verdict across receive/setup joins and context closure


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


type Connect = Callable[[BoundedReceiveOptions], AbstractAsyncContextManager[MusicSession]]


# The SDK sends the API key in a header, and websockets follows redirects while connecting, taking
# that header to whatever server a redirect names. With a limit of one connection it follows none:
# a redirect fails as SecurityError, or as InvalidURI or ValueError if its Location isn't a valid
# WebSocket URI (#85).
websockets.asyncio.client.MAX_REDIRECTS = 1


def lyria(api_key: str) -> Connect:
    """Open Lyria RealTime sessions with the operator's API key."""
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version=API_VERSION))
    return lambda options: client.aio.live.music.connect(
        model=MODEL, compression=None, bounded_receive=options
    )


class LyriaMessageError(Exception):
    """A message from Lyria RealTime that SobaFM can't use.

    It carries a fixed description: the original error could quote anything Lyria sent, such as
    the frame the SDK failed to parse, so neither its text nor its traceback is kept (OPS-5).
    """


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

    def __init__(
        self,
        connect: Connect,
        plan: MusicPlan,
        *,
        audio: AudioLedger,
        clock: Callable[[], float] = time.monotonic,
        program: Program | None = None,
    ) -> None:
        self.plan = plan
        self.program = program
        self.number = next_source_number()
        # Appended on the event loop and popped by the mixer on discord.py's player thread;
        # deque appends and pops are thread-safe.
        self.frames: deque[OwnedPCM] = deque()
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
        self.pcm = audio.pcm.storage(PCM_LIMIT)
        self._receive_storage = audio.lyria_receive
        self._backing: ReceiveBacking | None = None
        self._splitter = FrameSplitter(self.pcm)
        self._audio_bytes = 0
        self._generated_s = 0.0
        self._generating_since: float | None = None
        self._retiring = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._producer: asyncio.Task[EndReason | BaseException] | None = None
        self._context: AbstractAsyncContextManager[MusicSession] | None = None
        self._entering: asyncio.Task[MusicSession | BaseException] | None = None
        self._iterator_close: asyncio.Task[None] | None = None
        self._cleanup: asyncio.Task[None] | None = None
        self._cleanup_at: float | None = None
        self._cleanup_timer: asyncio.TimerHandle | None = None
        self._cleanup_decision: asyncio.Future[bool] | None = None
        self.cleanup_failed = False
        self._uncertain = False
        self._quiescent = True

    @property
    def quiescent(self) -> bool:
        """The actual receive/setup/context owners released; a deadline is insufficient."""
        return self._quiescent

    @property
    def mode(self) -> Mode:
        return Mode.GENERATED

    @property
    def title(self) -> str:
        return self.plan.title

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
    def failed(self) -> bool:
        return self.end_reason not in (None, EndReason.RETIRED)

    @property
    def completed(self) -> bool:
        return False  # generated sessions have no strict finite-track completion

    @property
    def credits(self) -> None:
        return None

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
            if self._retiring.is_set():
                self._end(EndReason.RETIRED)
                return
            storage = self._receive_storage
            self._backing = storage.claim(self) if storage is not None else None
            if self._backing is None:
                self._end(EndReason.FAILED, "Receive capacity exhausted")
                return
            self._quiescent = False
            self._cleanup_decision = asyncio.get_running_loop().create_future()
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
        if task is None:
            if self.end_reason is None:
                self._end(EndReason.RETIRED)
            return
        self._begin_cleanup()
        if self.state is State.CONNECTING and not task.cancelling():
            task.cancel()

    async def wait_ended(self) -> None:
        """Wait for actual closure or the one original failed cleanup verdict."""
        decision = self._cleanup_decision
        if self._task is not None and decision is not None and await asyncio.shield(decision):
            await asyncio.wait({self._task})

    async def wait_closed(self) -> None:
        """Actual cleanup acknowledgement, independent of its original deadline verdict."""
        if self._cleanup is not None:
            await asyncio.shield(self._cleanup)
        if not self.quiescent:
            await asyncio.Future[None]()  # failed/unknown closure retains its owner, never retries

    def discard_pcm(self) -> None:
        """Only the engine's permanent unused-source drop calls this, after producer end."""
        if self.state is not State.ENDED:
            raise RuntimeError("producer still owns PCM")
        while self.frames:
            frame = self.frames.popleft()
            receipt = frame.receipt
            del frame
            receipt.release()
        self._splitter.discard()

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
            self._producer = asyncio.create_task(self._produce())
            reason = await asyncio.shield(self._producer)
            if isinstance(reason, BaseException):
                raise reason
            if self.end_reason is None:
                self._end(reason)
        except asyncio.CancelledError:
            if self.end_reason is None:
                self._end(EndReason.RETIRED)
            raise
        except (PCMCapacityError, LyriaMessageError) as error:
            self._local_failed(error)
        except errors.APIError as error:
            self._remote_closed(error)
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
            self._end(EndReason.FAILED, detail, error)
        except OSError as error:
            # A certificate error is also a ValueError, and its text is OpenSSL's.
            self._fail(error)
        except (WebSocketException, ValueError) as error:
            # A failed handshake, or a redirect: its message, or its cause's, can quote the
            # server's reply. Any other ValueError, such as one from a bug in SobaFM, is
            # described by its type too.
            self.failure = call_failure(error)
            detail = type(error).__name__
            if isinstance(error, InvalidStatus):
                detail += f": HTTP {error.response.status_code}"
            log.warning("Deck %d failed: %s", self.number, detail)
            self._end(EndReason.FAILED, detail, error)
        except Exception as error:  # noqa: BLE001 - a deck failure must never stop the station
            self._uncertain = True
            self._end(EndReason.FAILED, "Lyria receive failed", error)
        finally:
            self._session = None
            self._begin_cleanup()
            if self._cleanup is not None:
                await asyncio.shield(self._cleanup)

    async def _produce(self) -> EndReason | BaseException:
        # A failed Task keeps a separate traceback even after its exception is sanitized.
        # Return the error to the root owner so setup/receive frames do not form a cycle.
        try:
            return await self._connect_and_generate()
        except BaseException as error:  # noqa: BLE001 - transfer sanitized outcome to root owner
            error.__traceback__ = error.__context__ = error.__cause__ = None
            return error

    async def _connect_and_generate(self) -> EndReason:
        backing = self._backing
        if backing is None:
            raise LyriaMessageError("Receive capacity unavailable")
        options = BoundedReceiveOptions(
            storage=backing.storage,
            max_bytes=LYRIA_RECEIVE_BYTES,
            max_fragments=64,
            max_empty_fragments=8,
            max_size=LYRIA_RECEIVE_BYTES,
            max_queue=1,
            shape_limits=ShapeLimits(LYRIA_RECEIVE_BYTES, 1024, 16, 64),
        )
        async with asyncio.timeout(CONNECT_TIMEOUT_S) as connecting:
            self._context = self._connect(options)
            self._entering = asyncio.create_task(self._enter())
            session = await asyncio.shield(self._entering)
            if isinstance(session, BaseException):
                raise session
            await session.set_weighted_prompts(prompts=self.plan.weighted_prompts())
            await session.set_music_generation_config(config=self.plan.to_config())
            await session.play()
            connecting.reschedule(None)
            return await self._generate(session)

    async def _enter(self) -> MusicSession | BaseException:
        context = self._context
        if context is None:
            return LyriaMessageError("Missing receive owner")
        try:
            return await context.__aenter__()
        except RuntimeError, TypeError:
            pass
        except BaseException as error:  # noqa: BLE001 - transfer sanitized outcome to root owner
            error.__traceback__ = error.__context__ = error.__cause__ = None
            return error
        return LyriaMessageError("unreadable setup message")

    def _cleanup_failure(self) -> None:
        if self.cleanup_failed:
            return
        self.cleanup_failed = True
        if self._cleanup_timer is not None:
            self._cleanup_timer.cancel()
        self._end(EndReason.FAILED, "Lyria cleanup failed")
        decision = self._cleanup_decision
        if decision is not None and not decision.done():
            decision.set_result(False)

    def _start_cleanup_clock(self) -> None:
        if self._cleanup_at is None:
            loop = asyncio.get_running_loop()
            self._cleanup_at = loop.time()
            self._cleanup_timer = loop.call_at(
                self._cleanup_at + CLEANUP_SECONDS, self._cleanup_failure
            )

    def _begin_cleanup(self) -> None:
        self._start_cleanup_clock()
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._close_owned())

    async def _close_owned(self) -> None:
        """One retained owner joins setup/receive and exits the exact SDK context once."""
        try:
            producer = self._producer
            if producer is not None:
                if not producer.done():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            entering = self._entering
            if entering is not None:
                if not entering.done():
                    entering.cancel()
                await asyncio.gather(entering, return_exceptions=True)
            if self._iterator_close is not None:
                await asyncio.shield(self._iterator_close)
            context = self._context
            if (
                context is not None
                and entering is not None
                and not entering.cancelled()
                and not isinstance(entering.result(), BaseException)
            ):
                await context.__aexit__(None, None, None)
            if self._uncertain:
                self._cleanup_failure()
                return
            self._context = None
            self._entering = None
            self._producer = None
            self._iterator_close = None
            backing, storage = self._backing, self._receive_storage
            if backing is not None and storage is not None:
                storage.released(backing, self)
            self._backing = None
            self._quiescent = True
            if self._cleanup_at is not None and (
                asyncio.get_running_loop().time() >= self._cleanup_at + CLEANUP_SECONDS
            ):
                self._cleanup_failure()
            if self._cleanup_timer is not None:
                self._cleanup_timer.cancel()
            decision = self._cleanup_decision
            if decision is not None and not decision.done():
                decision.set_result(not self.cleanup_failed)
        except BaseException as error:  # noqa: BLE001 - failed closure remains owned, never reusable
            error.__traceback__ = error.__context__ = error.__cause__ = None
            self._uncertain = True
            self._cleanup_failure()

    def _local_failed(self, error: PCMCapacityError | LyriaMessageError) -> None:
        if isinstance(error, PCMCapacityError):
            detail = "PCM capacity exhausted"
        else:
            detail = f"LyriaMessageError: {error}"
            log.warning("Deck %d failed: %s", self.number, detail)
        self._end(EndReason.FAILED, detail, error)

    def _remote_closed(self, error: errors.APIError) -> None:
        # Record an observed remote close before iterator/context cleanup can suspend.
        # A later retirement must not hide its refusal, quota or outage category.
        cause: object = error.message or getattr(error, "details", None)
        self.close_code = error.code
        self.failure = close_failure(error.code, cause)
        self._end(EndReason.CLOSED, close_detail(error.code, cause), error)

    def _fail(self, error: Exception) -> None:
        """End the deck after an error not expected to quote the server, with its traceback."""
        log.warning("Deck %d failed", self.number, exc_info=error)
        self.failure = call_failure(error)
        detail = type(error).__name__ + (f": {error}" if str(error) else "")
        self._end(EndReason.FAILED, detail[:200], error)

    def _cancelled(self, task: asyncio.Task[None]) -> None:
        """Record a deck cancelled by `retire()` while connecting, or at shutdown, as retired."""
        if task.cancelled():
            # The task keeps its CancelledError, whose traceback holds this deck's frames, and the
            # deck holds the task: a cycle that would keep the deck until a collection (#89).
            with suppress(asyncio.CancelledError):
                task.result()
            if self.end_reason is None:
                self._end(EndReason.RETIRED)
            self._begin_cleanup()  # also covers cancellation before _run first advances

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
        try:
            while True:
                try:
                    message = await anext(messages)
                except StopAsyncIteration:
                    self._end(EndReason.CLOSED)
                    return EndReason.CLOSED
                except errors.APIError as error:
                    self._remote_closed(error)
                    return EndReason.CLOSED
                except ValueError, RuntimeError, TypeError:
                    message = None
                if message is None:  # outside the handler: retain no provider exception graph
                    raise LyriaMessageError("unreadable message")
                try:
                    end_reason = self._process_message(message)
                finally:
                    del message
                if end_reason is not None:
                    self._end(end_reason)
                    return end_reason
        except (PCMCapacityError, LyriaMessageError) as error:
            # Preserve an already-observed local failure before iterator closure suspends.
            self._local_failed(error)
            return EndReason.FAILED
        finally:
            self._start_cleanup_clock()
            if isinstance(messages, AsyncGenerator):
                self._iterator_close = asyncio.create_task(messages.aclose())
                await asyncio.shield(self._iterator_close)
            else:
                self._uncertain = True
                raise LyriaMessageError("Unclosable receive iterator")

    def _process_message(self, message: types.LiveMusicServerMessage) -> EndReason | None:
        """Process one update without suspending its content/chunk/data locals at an await.

        An escaping exception can still own this frame until its traceback is cleared by `_end`.
        """
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
            with closing(self._splitter.split(data)) as frames:
                self.frames.extend(frames)
        return None

    def _set_generating(self, generating: bool) -> None:
        """Start or stop the clock behind `rate`."""
        now = self._clock()
        if not generating and self._generating_since is not None:
            self._generated_s += now - self._generating_since
            self._generating_since = None
        elif generating and self._generating_since is None:
            self._generating_since = now

    def _end(
        self, reason: EndReason, detail: str | None = None, error: BaseException | None = None
    ) -> None:
        """Record how the session ended, logging the deck's summary.

        Drops `error`'s traceback, whose frames hold this deck, once it is recorded and logged
        where SobaFM logs it. Something holds the error in turn: `_generate()`'s receive task
        after a failure while streaming, websockets' `ClientProtocol.handshake_exc` after a
        failed handshake. Either cycle would keep the deck and its audio until a garbage
        collection (#89).
        """
        if error is not None:
            error.__traceback__ = error.__context__ = error.__cause__ = None
        if self.cleanup_failed:
            reason, detail = EndReason.FAILED, "Lyria cleanup failed"
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
