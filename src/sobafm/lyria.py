"""Lyria RealTime audio into PCM banks, over one bounded WebSocket per bank.

ADR 0008 records the transport choices and the copy budget. The station owns
each generated stream through settlement and release.

``LyriaSession`` owns one WebSocket feeding one leased ``pcm.py`` bank. It uses the
``aiohttp`` already in the lock rather than the Google SDK, whose import alone nearly
fills the 90 MB service limit and which cannot bound a message before allocating it.

The budget depends on three things aiohttp would not do by default. Reading is paused
between messages, so a second message never assembles while one is held. The socket's
receive buffer and the TLS read window are small, so transport copies stay inside the
producer's read slot. The base64 is scanned and decoded in place, never as a Python
string, so a stray non-ASCII byte can't widen the whole message to four bytes a
character. That conversion runs on the event loop on purpose: yielding would let the
next message assemble, and it takes about two milliseconds per two-second message.

Generated audio has no expected length, so its completion policy differs from a
recording's. PCM is valid once every message that produced it passed validation. A
session that ends after readiness declares the end of PCM so the buffered audio drains;
one that ends before readiness stops its bank and nothing is ever audible.

``GeneratedStream`` keeps a program playing across sessions: it renews on a timer or
when a session ends, pre-rolls the replacement on the second bank and crossfades in the
voice callback using prepaid scratch. ``read_frame`` has the same contract as
``PcmBank.read_frame``. Prompts, filter reasons and warnings are never logged, and the
key is only ever a request header.
"""

from __future__ import annotations

import asyncio
import binascii
import contextlib
import json
import logging
import math
import re
import socket
import ssl
import threading
import time
import unicodedata
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast

import aiohttp
import audioop

from sobafm.pcm import (
    BANK_BYTES,
    BYTES_PER_SECOND,
    FRAME_BYTES,
    GLOBAL_PCM_BANKS,
    READY_BYTES,
    SAMPLE_BYTES,
    STEREO_SAMPLE_BYTES,
    PcmBank,
    PcmBankPool,
    PcmCapacityError,
    PcmStateError,
)

if TYPE_CHECKING:
    from aiohttp.client_proto import ResponseHandler
    from aiohttp.connector import Connection

log = logging.getLogger(__name__)

# Pinned after checking official documentation on 2026-11-05; see ADR 0008. The model is
# experimental, so recheck behavior with an authorized live check before changing either.
MODEL = "models/lyria-realtime-exp"
ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateMusic"
)

# The copy budget in ADR 0008. aiohttp holds a frame's reads and their join together, so
# a message may be half the 1,048,576-byte network slot. Joining fragments or folding
# more than 2,048 reads of one frame can briefly need a third copy. Both operations use
# the shared 524,288-byte drain reservation synchronously on the event loop. Closing
# drops the connection without reading and uses no message drain. Two seconds of PCM
# is 512,000 base64 bytes, which leaves 12,288 for the JSON around them.
MAX_MESSAGE_BYTES = 524_288
MAX_MESSAGE_PCM_BYTES = MAX_MESSAGE_BYTES * 3 // 4
# Everything in a message except its audio. Bounding it bounds the JSON parse, including
# text that widens to four bytes a character when decoded.
MAX_ENVELOPE_BYTES = 16_384
MAX_CHUNKS_PER_MESSAGE = 8
# A multiple of four, so each piece decodes alone and stays a whole number of samples.
BASE64_SLICE_BYTES = 65_536
# The kernel doubles this, so one socket read returns at most 65,536 bytes.
RECEIVE_BUFFER_BYTES = 32_768
# Ciphertext asyncio may stage while reading is paused; its default is 256 KiB.
TLS_READ_HIGH_WATER_BYTES = 32_768

assert BASE64_SLICE_BYTES % 4 == 0
assert (BASE64_SLICE_BYTES * 3 // 4) % STEREO_SAMPLE_BYTES == 0
assert BANK_BYTES - MAX_MESSAGE_PCM_BYTES >= READY_BYTES

MAX_PROMPTS = 4
MAX_PROMPT_CHARACTERS = 200

PREPARE_SECONDS = 15.0  # connection to six seconds of pre-roll, per attempt
STALL_SECONDS = 5.0  # no audio while the bank has room, after readiness
CLEANUP_SECONDS = 3.0  # one original deadline from the first stop or cancellation
FORCE_CLOSE_SECONDS = 2.0  # interrupt unsettled cleanup, without abandoning its owner
CLOSE_HANDSHAKE_SECONDS = 1.0
MIN_SPACE_WAIT_SECONDS = 0.02  # one frame
# The music API documents no session limit; the Live API documents about ten minutes.
# Renewing early turns a server close into a planned crossfade. See ADR 0008.
RENEW_AFTER_SECONDS = 540.0
MAX_SESSION_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0
HEALTHY_SESSION_BYTES = 60 * BYTES_PER_SECOND  # a session shorter than this counts against it

CROSSFADE_FRAMES = 200  # four seconds, inside the six seconds of pre-roll
FADE_OUT_FRAMES = 100  # two seconds

type LyriaFailure = Literal[
    "capacity",
    "connect",
    "rate_limited",
    "service_error",
    "access_denied",
    "rejected",
    "prepare_timeout",
    "stalled",
    "truncated",
    "closed",
    "oversized",
    "malformed",
    "filtered",
    "internal",
]
type CleanupVerdict = Literal["clean", "expired"]

# The same request would be refused again, so these end recovery at once.
PERMANENT_FAILURES: frozenset[LyriaFailure] = frozenset({"rejected", "filtered", "access_denied"})

type Clock = Callable[[], float]
type Sleep = Callable[[float], Awaitable[None]]


class Scale(StrEnum):
    """The documented musical scales; each covers a major key and its relative minor."""

    C_MAJOR_A_MINOR = "C_MAJOR_A_MINOR"
    D_FLAT_MAJOR_B_FLAT_MINOR = "D_FLAT_MAJOR_B_FLAT_MINOR"
    D_MAJOR_B_MINOR = "D_MAJOR_B_MINOR"
    E_FLAT_MAJOR_C_MINOR = "E_FLAT_MAJOR_C_MINOR"
    E_MAJOR_D_FLAT_MINOR = "E_MAJOR_D_FLAT_MINOR"
    F_MAJOR_D_MINOR = "F_MAJOR_D_MINOR"
    G_FLAT_MAJOR_E_FLAT_MINOR = "G_FLAT_MAJOR_E_FLAT_MINOR"
    G_MAJOR_E_MINOR = "G_MAJOR_E_MINOR"
    A_FLAT_MAJOR_F_MINOR = "A_FLAT_MAJOR_F_MINOR"
    A_MAJOR_G_FLAT_MINOR = "A_MAJOR_G_FLAT_MINOR"
    B_FLAT_MAJOR_G_MINOR = "B_FLAT_MAJOR_G_MINOR"
    B_MAJOR_A_FLAT_MINOR = "B_MAJOR_A_FLAT_MINOR"


@dataclass(frozen=True, slots=True)
class Prompt:
    """One weighted musical direction."""

    text: str
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not 1 <= len(self.text.strip()) <= MAX_PROMPT_CHARACTERS:
            raise ValueError("prompt text has an unsupported length")
        if any(unicodedata.category(c).startswith("C") for c in self.text):
            raise ValueError("prompt text contains control characters")
        if not math.isfinite(self.weight) or self.weight <= 0:
            raise ValueError("prompt weight must be positive and finite")


@dataclass(frozen=True, slots=True)
class MusicRequest:
    """What one session asks for, checked against the documented ranges."""

    prompts: tuple[Prompt, ...]
    bpm: int | None = None
    scale: Scale | None = None
    density: float | None = None
    brightness: float | None = None
    mute_drums: bool = False
    vocalization: bool = False

    def __post_init__(self) -> None:
        if not 1 <= len(self.prompts) <= MAX_PROMPTS:
            raise ValueError("a request has one to four prompts")
        if self.bpm is not None and not 60 <= self.bpm <= 200:
            raise ValueError("bpm is outside 60 to 200")
        for value in (self.density, self.brightness):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError("density and brightness are outside 0 to 1")

    def prompts_message(self) -> dict[str, object]:
        prompts = [{"text": p.text, "weight": p.weight} for p in self.prompts]
        return {"clientContent": {"weightedPrompts": prompts}}

    def config_message(self) -> dict[str, object] | None:
        """The whole generation config, or None when the model should decide everything."""
        config: dict[str, object] = {}
        if self.bpm is not None:
            config["bpm"] = self.bpm
        if self.scale is not None:
            config["scale"] = self.scale.value
        if self.density is not None:
            config["density"] = self.density
        if self.brightness is not None:
            config["brightness"] = self.brightness
        if self.mute_drums:
            config["muteDrums"] = True
        if self.vocalization:
            config["musicGenerationMode"] = "VOCALIZATION"
        return {"musicGenerationConfig": config} if config else None


SETUP_MESSAGE: dict[str, object] = {"setup": {"model": MODEL}}
PLAY_MESSAGE: dict[str, object] = {"playbackControl": "PLAY"}


class MessageError(Exception):
    """A server message failed validation. It carries no provider text."""


@dataclass(frozen=True, slots=True)
class SetupComplete:
    pass


@dataclass(frozen=True, slots=True)
class Audio:
    """Where validated base64 sits in the message, and the PCM it holds."""

    spans: tuple[tuple[int, int], ...]
    pcm_bytes: int


@dataclass(frozen=True, slots=True)
class Filtered:
    pass


@dataclass(frozen=True, slots=True)
class Warned:
    pass


@dataclass(frozen=True, slots=True)
class Ignored:
    """Valid JSON holding no message this module acts on."""


type ServerMessage = SetupComplete | Audio | Filtered | Warned | Ignored

# The alphabet is part of the pattern, so one pass finds and validates the audio without
# copying it. A quote can't occur inside a base64 run, so matching stays linear.
_AUDIO_DATA = re.compile(rb'"data"\s*:\s*"([A-Za-z0-9+/]*={0,2})"')


def _pcm_size(encoded_bytes: int, padding: int) -> int:
    if encoded_bytes % 4:
        raise MessageError("base64 length")
    size = encoded_bytes // 4 * 3 - padding
    if size % STEREO_SAMPLE_BYTES:
        raise MessageError("not whole samples")
    return size


def _format_matches(mime_type: object) -> bool:
    """Whether a stated format could be ours. The docs give only a generic example."""
    if mime_type is None:
        return True
    if not isinstance(mime_type, str):
        return False
    kind, *parameters = (part.strip().lower() for part in mime_type.split(";"))
    if not kind.startswith("audio/"):
        return False
    expected = {"rate": "48000", "channels": "2"}
    for parameter in parameters:
        name, _, value = parameter.partition("=")
        if expected.get(name, value) != value:
            return False
    return True


def parse_message(payload: bytes) -> ServerMessage:
    """Validate one whole server message before anything changes state.

    The audio is matched in place and blanked, and only the small remainder is parsed as
    JSON. Blanking stays sound because every chunk's own ``data`` must then be empty, so
    it was matched, and the matches must number exactly those chunks: a match anywhere
    else, or an audio field the pattern didn't recognize, makes them disagree.
    """
    spans: list[tuple[int, int]] = []
    audio_bytes = 0
    pcm_bytes = 0
    for match in _AUDIO_DATA.finditer(payload):
        start, end = match.span(1)
        spans.append((start, end))
        if len(spans) > MAX_CHUNKS_PER_MESSAGE:
            raise MessageError("too many chunks")
        audio_bytes += end - start
        padding = payload.count(b"=", max(start, end - 2), end)
        pcm_bytes += _pcm_size(end - start, padding)
    if len(payload) - audio_bytes > MAX_ENVELOPE_BYTES:
        raise MessageError("envelope too large")

    envelope = bytearray()
    position = 0
    with memoryview(payload) as view:
        for start, end in spans:
            with view[position:start] as piece:
                envelope += piece
            position = end
        with view[position:] as piece:
            envelope += piece
    try:
        document = cast(object, json.loads(envelope))
    except (ValueError, RecursionError) as exc:
        raise MessageError("not JSON") from exc
    if not isinstance(document, dict):
        raise MessageError("not an object")
    fields = cast(dict[str, object], document)

    known = fields.keys() & {"setupComplete", "serverContent", "filteredPrompt", "warning"}
    if len(known) > 1:
        raise MessageError("more than one message type")
    if "serverContent" in known:
        return _audio(fields["serverContent"], tuple(spans), pcm_bytes)
    if spans:
        raise MessageError("audio outside a chunk")
    if "setupComplete" in known:
        return SetupComplete()
    if "filteredPrompt" in known:
        return Filtered()
    if "warning" in known:
        if not isinstance(fields["warning"], str):
            raise MessageError("warning is not text")
        return Warned()
    return Ignored()


def _audio(content: object, spans: tuple[tuple[int, int], ...], pcm_bytes: int) -> Audio:
    if not isinstance(content, dict):
        raise MessageError("content is not an object")
    chunks = cast(dict[str, object], content).get("audioChunks", [])
    if not isinstance(chunks, list):
        raise MessageError("chunks are not a list")
    with_data = 0
    for item in cast(list[object], chunks):
        if not isinstance(item, dict):
            raise MessageError("chunk is not an object")
        chunk = cast(dict[str, object], item)
        if "data" in chunk:
            with_data += 1
            if chunk["data"] != "":
                raise MessageError("chunk data was not valid base64")
        if not _format_matches(chunk.get("mimeType")):
            raise MessageError("unsupported audio format")
    if with_data != len(spans):
        raise MessageError("audio outside a chunk")
    return Audio(spans, pcm_bytes)


def receive_bounded_socket(
    addr_info: tuple[
        int | socket.AddressFamily, int | socket.SocketKind, int, str, tuple[Any, ...]
    ],
) -> socket.socket:
    """A socket whose reads stay inside the read slot, sized before the window is agreed."""
    family, kind, protocol, _, _ = addr_info
    sock = socket.socket(family, kind, protocol)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER_BYTES)
    return sock


async def _join[T](work: Coroutine[Any, Any, T]) -> T:
    """Finish ``work`` however often this task is cancelled, then deliver the cancellation."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            break  # reported through task.result() below
    if cancelled:
        raise asyncio.CancelledError
    return task.result()


class _MusicSocket:
    """One connection, with its session and the pacing the copy budget depends on."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        websocket: aiohttp.ClientWebSocketResponse[Literal[False]],
        protocol: ResponseHandler,
    ) -> None:
        self._session = session
        self._websocket = websocket
        self._protocol = protocol

    async def send(self, message: dict[str, object]) -> None:
        await self._websocket.send_str(json.dumps(message, separators=(",", ":")))

    async def receive(self) -> aiohttp.WSMessage:
        """One message; the transport stays paused until the next call."""
        self._protocol.resume_reading()
        try:
            return cast(aiohttp.WSMessage, await self._websocket.receive())
        finally:
            # A stopped producer can still own a pending close after this await is cancelled.
            self._protocol.pause_reading()

    async def close(self) -> None:
        """Drop the connection without resuming reads or draining more audio.

        A WebSocket close handshake keeps reading while awaiting the peer's close,
        retaining one message while the next assembles. That exceeds our network slot.
        """
        await _join(self._session.close())


async def _connect(
    api_key: str, endpoint: str, ssl_context: ssl.SSLContext | None = None
) -> _MusicSocket:
    connections: list[Connection] = []

    async def keep_connection(
        request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        response = await handler(request)
        if 300 <= response.status < 400:
            # aiohttp would follow it and send the key header to the new host.
            response.close()
            raise aiohttp.WSServerHandshakeError(
                response.request_info, response.history, status=response.status
            )
        if response.connection is not None:
            connections.append(response.connection)
        return response

    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            limit=1, force_close=True, socket_factory=receive_bounded_socket
        ),
        cookie_jar=aiohttp.DummyCookieJar(),
        middlewares=(keep_connection,),
        timeout=aiohttp.ClientTimeout(total=None),  # the session's watchdog bounds this
    )
    try:
        websocket = await session.ws_connect(
            endpoint,
            headers={"x-goog-api-key": api_key},
            max_msg_size=MAX_MESSAGE_BYTES,
            decode_text=False,
            compress=0,
            # Even an empty peer close must reach the pump directly. aiohttp's automatic
            # close would drain more audio while waiting for another close with a code.
            autoclose=False,
            # aiohttp builds this dataclass in a way Pyright can't see through.
            timeout=aiohttp.ClientWSTimeout(ws_close=CLOSE_HANDSHAKE_SECONDS),  # pyright: ignore[reportCallIssue]
            ssl=True if ssl_context is None else ssl_context,
        )
    except BaseException:
        await _join(session.close())
        raise
    transport, protocol = connections[-1].transport, connections[-1].protocol
    assert protocol is not None
    # Only the TLS transport has this. A plain socket has no staging buffer to limit.
    limit_tls = getattr(transport, "set_read_buffer_limits", None)
    if limit_tls is not None:
        limit_tls(high=TLS_READ_HIGH_WATER_BYTES)
    return _MusicSocket(session, websocket, protocol)


def _handshake_failure(status: int) -> LyriaFailure:
    if status in (401, 403):
        return "access_denied"
    if status == 429:
        return "rate_limited"
    if 500 <= status < 600:
        return "service_error"
    if 300 <= status < 500 and status != 408:
        return "rejected"
    return "connect"


class _SessionEnded(Exception):
    """The pump should stop; the reason is already recorded."""


@dataclass(frozen=True, slots=True)
class SessionReport:
    """One session's outcome. ``cleanup`` never changes once decided."""

    failure: LyriaFailure | None
    pcm_bytes: int
    close_code: int | None
    cleanup: CleanupVerdict


class LyriaSession:
    """One WebSocket feeding one bank. The bank's lease stays with whoever leased it.

    ``cancel`` and ``finish`` follow ``DecodeProducer``: the first of cancellation, a
    failure or the end of the stream fixes one original deadline, and repeating
    cancellation never moves it. Expiry is a failed verdict that a later settlement
    doesn't change, and the socket stays owned until it has actually settled.
    """

    def __init__(
        self,
        request: MusicRequest,
        bank: PcmBank,
        *,
        api_key: str,
        endpoint: str = ENDPOINT,
        ssl_context: ssl.SSLContext | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._request = request
        self._bank = bank
        self._api_key = api_key
        self._endpoint = endpoint
        self._ssl_context = ssl_context
        self._clock = clock
        self._sleep = sleep
        self._pump_task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._deadline = 0.0
        self._ready = asyncio.Event()
        self._stopped = asyncio.Event()
        self._prepared = asyncio.Event()
        self._stopped_at: float | None = None
        self._settled_at: float | None = None
        self._failure: LyriaFailure | None = None
        self._close_code: int | None = None
        self._pcm_bytes = 0
        self._cleanup: CleanupVerdict | None = None

    # What the owner can see.

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def failure(self) -> LyriaFailure | None:
        return self._failure

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    @property
    def pcm_bytes(self) -> int:
        return self._pcm_bytes

    async def wait_prepared(self) -> None:
        """Return once this session is ready or has stopped; check ``ready`` afterwards."""
        await self._prepared.wait()

    async def wait_stopped(self) -> None:
        await self._stopped.wait()

    # Owner operations.

    def start(self) -> None:
        self._deadline = self._clock() + PREPARE_SECONDS
        self._pump_task = asyncio.create_task(self._pump())
        self._pump_task.add_done_callback(self._pump_done)
        self._watch_task = asyncio.create_task(self._watch())

    def end_production(self) -> None:
        """Stop the pump now and let its bank play out the PCM already admitted."""
        if not self.stopped:
            self._stop(None)
            if self._pump_task is not None:
                self._pump_task.cancel()

    def cancel(self) -> None:
        """Stop the stream, keeping the first stop time and every owner. Idempotent."""
        self._bank.cancel()
        cancelled_at = self._bank.facts().cancelled_at
        assert cancelled_at is not None
        if self._stopped_at is None or cancelled_at < self._stopped_at:
            self._stopped_at = cancelled_at
        if self._stopped.is_set():
            return  # the pump already owns cleanup; don't interrupt it repeatedly
        self._mark_stopped()
        if self._pump_task is not None:
            self._pump_task.cancel()

    async def finish(self) -> SessionReport:
        """Wait for settlement by the original deadline and report the verdict.

        It returns ``expired`` at the deadline even though work remains; ``settle`` then
        joins it. Cancelling this call abandons nothing, and calling it again keeps the
        same deadline.
        """
        await self._stopped.wait()
        pump, watch = self._pump_task, self._watch_task
        assert self._stopped_at is not None
        if pump is not None and watch is not None:
            await self._wait_until({pump, watch}, self._stopped_at + FORCE_CLOSE_SECONDS)
            if not pump.done():
                pump.cancel()  # interrupt a stuck close; _join retains its actual owner
            await self._wait_until({pump, watch}, self._stopped_at + CLEANUP_SECONDS)
        return SessionReport(self._failure, self._pcm_bytes, self._close_code, self._verdict())

    async def settle(self) -> None:
        """Join the session's tasks however long that takes; never cancels them."""
        tasks = {t for t in (self._pump_task, self._watch_task) if t is not None}
        if tasks:
            await asyncio.wait(tasks)

    # Internals.

    def _pump_done(self, task: asyncio.Task[None]) -> None:
        # Cancellation before the first coroutine step skips _pump's finally entirely.
        # Do not overwrite the earlier settlement time on the ordinary path.
        if self._settled_at is None:
            self._settled_at = self._clock()
        if self._watch_task is not None:
            self._watch_task.cancel()

    def _verdict(self) -> CleanupVerdict:
        """Decide once, at the first report: a later settlement changes nothing."""
        if self._cleanup is None:
            assert self._stopped_at is not None
            deadline = self._stopped_at + CLEANUP_SECONDS
            settled_at = self._settled_at
            on_time = self._pump_task is None or (settled_at is not None and settled_at <= deadline)
            self._cleanup = "clean" if on_time else "expired"
        return self._cleanup

    async def _wait_until(self, tasks: set[asyncio.Task[None]], when: float) -> None:
        remaining = when - self._clock()
        if remaining <= 0 or all(t.done() for t in tasks):
            return
        timer = asyncio.ensure_future(self._sleep(remaining))
        try:
            pending = {*tasks}
            while pending and not timer.done():
                _, pending = await asyncio.wait(
                    {*pending, timer}, return_when=asyncio.FIRST_COMPLETED
                )
                pending.discard(timer)
        finally:
            timer.cancel()
            await asyncio.wait({timer})

    def _mark_stopped(self) -> None:
        self._stopped.set()
        self._prepared.set()

    def _stop(self, failure: LyriaFailure | None, *, transfer_complete: bool = False) -> None:
        """Record why the stream ended and leave the bank playable or stopped.

        Audio is only admitted after readiness, so a stream that never got there stops
        its bank. One that did has only validated PCM, which drains while a replacement
        prepares, so a provider failure doesn't cut off what listeners already have.
        """
        if self._stopped.is_set():
            return
        self._failure = failure
        self._stopped_at = self._clock()
        try:
            if transfer_complete:
                self._bank.mark_transfer_complete()
            if self._bank.ready_now():
                self._bank.end_pcm()
            else:
                self._bank.cancel()
        except PcmStateError:
            pass  # its owner already stopped the stream
        cancelled_at = self._bank.facts().cancelled_at
        if cancelled_at is not None:
            self._stopped_at = min(self._stopped_at, cancelled_at)
        self._mark_stopped()
        log.info(
            "lyria session stopped: failure=%s pcm_bytes=%d close_code=%s",
            failure or "none",
            self._pcm_bytes,
            self._close_code,
        )

    async def _watch(self) -> None:
        """Fail a session whose deadline passes without progress. One deadline at a time."""
        while not self._stopped.is_set():
            remaining = self._deadline - self._clock()
            if remaining > 0:
                # Readiness can bring the deadline forward to at most this far ahead.
                await self._sleep(min(remaining, STALL_SECONDS))
                continue
            self._stop("stalled" if self.ready else "prepare_timeout")
            assert self._pump_task is not None
            self._pump_task.cancel()

    async def _pump(self) -> None:
        connection: _MusicSocket | None = None
        try:
            connection = await _connect(self._api_key, self._endpoint, self._ssl_context)
            await connection.send(SETUP_MESSAGE)
            # The API requires setup to finish before anything else is sent.
            while not self._handle_setup(await connection.receive()):
                pass
            await connection.send(self._request.prompts_message())
            if (config := self._request.config_message()) is not None:
                await connection.send(config)
            await connection.send(PLAY_MESSAGE)
            while True:
                await self._wait_for_space()
                # A temporary, so nothing holds this message while the next assembles.
                self._handle_stream(await connection.receive())
        except _SessionEnded:
            pass
        except asyncio.CancelledError:
            pass  # `cancel` or the watchdog recorded why
        except aiohttp.WSServerHandshakeError as exc:
            self._stop(_handshake_failure(exc.status))
        except aiohttp.ClientError, OSError:
            self._stop("connect" if connection is None else "truncated")
        except Exception as exc:
            log.error("lyria session failed unexpectedly: %s", type(exc).__name__)
            self._stop("internal")
        finally:
            # Records that the stream stopped before anything else waits on it.
            self._stop(None)
            await self._close(connection)
            self._settled_at = self._clock()
            if self._watch_task is not None:
                self._watch_task.cancel()

    async def _close(self, connection: _MusicSocket | None) -> None:
        if connection is None:
            return
        try:
            await connection.close()
        except asyncio.CancelledError:
            pass  # aiohttp dropped the connection on this path; nothing is left open
        except Exception as exc:  # a failed close must not leave the session unsettled
            log.info("lyria close failed: %s", type(exc).__name__)

    async def _wait_for_space(self) -> None:
        """Take a message only when all of it fits, so no message is held across a wait."""
        while True:
            facts = self._bank.facts()
            if facts.cancelled_at is not None:
                raise _SessionEnded
            deficit = MAX_MESSAGE_PCM_BYTES - (BANK_BYTES - facts.buffered_bytes)
            if deficit <= 0:
                return
            delay = max(deficit / BYTES_PER_SECOND, MIN_SPACE_WAIT_SECONDS)
            # Not reading because the bank is full isn't a stall.
            self._deadline = self._clock() + delay + STALL_SECONDS
            await self._sleep(delay)

    def _handle_setup(self, message: aiohttp.WSMessage) -> bool:
        parsed = self._parse(message)
        if isinstance(parsed, SetupComplete):
            return True
        if isinstance(parsed, Audio):
            self._fail_malformed()
        return False

    def _handle_stream(self, message: aiohttp.WSMessage) -> None:
        parsed = self._parse(message)
        if isinstance(parsed, Audio):
            self._write_audio(message.data, parsed)
        elif isinstance(parsed, SetupComplete):
            self._fail_malformed()

    def _fail_malformed(self) -> NoReturn:
        self._stop("malformed")
        raise _SessionEnded

    def _parse(self, message: aiohttp.WSMessage) -> ServerMessage:
        """Validate a data message, or end the session on anything else."""
        kind = message.type
        if kind in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
            try:
                parsed = parse_message(message.data)
            except MessageError as exc:
                log.warning("lyria message rejected: %s", exc)
                self._fail_malformed()
            if isinstance(parsed, Filtered):
                self._stop("filtered")
                raise _SessionEnded
            if isinstance(parsed, Warned):
                log.warning("lyria warning received")
            return parsed
        if kind is aiohttp.WSMsgType.CLOSE:
            self._closed_by_server(cast(int, message.data))
        elif kind is aiohttp.WSMsgType.ERROR:
            self._failed_by_transport(message.data)
        else:
            self._stop("truncated")  # the connection ended with no close frame
        raise _SessionEnded

    def _closed_by_server(self, code: int) -> None:
        self._close_code = code
        if code == aiohttp.WSCloseCode.POLICY_VIOLATION:
            self._stop("rejected")
        elif code == aiohttp.WSCloseCode.INTERNAL_ERROR:
            self._stop("service_error")
        elif code in (aiohttp.WSCloseCode.OK, aiohttp.WSCloseCode.GOING_AWAY) and self.ready:
            self._stop(None, transfer_complete=True)
        else:
            self._stop("closed")

    def _failed_by_transport(self, error: object) -> None:
        if not isinstance(error, aiohttp.WebSocketError):
            self._stop("truncated")
        elif error.code == aiohttp.WSCloseCode.MESSAGE_TOO_BIG:
            self._stop("oversized")
        else:
            self._stop("malformed")

    def _write_audio(self, payload: bytes, audio: Audio) -> None:
        """Commit one validated message. Runs without yielding; see the module docstring."""
        try:
            with memoryview(payload) as view:
                for start, end in audio.spans:
                    for offset in range(start, end, BASE64_SLICE_BYTES):
                        with view[offset : min(offset + BASE64_SLICE_BYTES, end)] as piece:
                            pcm = binascii.a2b_base64(piece, strict_mode=True)
                        if self._bank.write(pcm) != len(pcm):
                            raise PcmStateError("the bank had less room than was checked")
        except PcmStateError:
            if self._bank.facts().cancelled_at is not None:
                raise _SessionEnded from None
            raise
        self._pcm_bytes += audio.pcm_bytes
        if not self.ready and self._bank.ready_now():
            self._ready.set()
            self._prepared.set()
        if self.ready and audio.pcm_bytes:
            self._deadline = self._clock() + STALL_SECONDS


@dataclass(eq=False, slots=True)
class _Feed:
    """A bank lease and the session writing to it, owned until the lease is returned."""

    bank: PcmBank
    session: LyriaSession
    cleanup: asyncio.Task[None]
    retirement: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class StreamReport:
    """What a stream did. ``retained_banks`` stay leased until ``release_debt`` succeeds."""

    failure: LyriaFailure | None
    sessions_started: int
    crossfades: int
    failed_cleanups: int
    retained_banks: int


@dataclass(slots=True)
class _Crossfade:
    """A replacement fading in, counted in frames."""

    incoming: _Feed
    frames: int
    position: int = 0


@dataclass(slots=True)
class _Voice:
    """What the voice callback reads, guarded by the stream's non-blocking lock."""

    audible: _Feed | None = None
    crossfade: _Crossfade | None = None
    fade_out_left: int | None = None
    silent: bool = False
    exhausted: bool = False
    ever_ready: bool = False
    recovering: bool = False


class GeneratedStream:
    """One generated program's audio across as many sessions as it takes.

    Uses a second bank during a renewal, so it overlaps within the existing two-bank
    reservation; when the other bank is in use elsewhere the session plays to its end and
    the replacement follows after a gap. Attempts are finite for each renewal, and once
    they run out the stream ends after its buffered audio drains.
    """

    def __init__(
        self,
        request: MusicRequest,
        *,
        api_key: str,
        endpoint: str = ENDPOINT,
        ssl_context: ssl.SSLContext | None = None,
        pool: PcmBankPool = GLOBAL_PCM_BANKS,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        renew_after: float = RENEW_AFTER_SECONDS,
        on_cleanup_expired: Callable[[], None] | None = None,
        on_bank_released: Callable[[], None] | None = None,
    ) -> None:
        self._request = request
        self._api_key = api_key
        self._endpoint = endpoint
        self._ssl_context = ssl_context
        self._pool = pool
        self._clock = clock
        self._sleep = sleep
        self._renew_after = renew_after
        self._on_cleanup_expired = on_cleanup_expired
        self._on_bank_released = on_bank_released
        self._lock = threading.Lock()
        self._voice = _Voice()
        self._feeds: set[_Feed] = set()
        self._retirements: set[asyncio.Task[None]] = set()
        self._debt: list[PcmBank] = []
        self._supervisor: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._changed = asyncio.Event()
        self._failure: LyriaFailure | None = None
        self._sessions_started = 0
        self._crossfades = 0
        self._failed_cleanups = 0

    # The voice callback's side. These never wait.

    @property
    def failure(self) -> LyriaFailure | None:
        return self._failure

    @property
    def recovering(self) -> bool:
        """A confirmed drain with no replacement frame handed to voice yet."""
        return self._voice.recovering

    @property
    def startup_failure(self) -> LyriaFailure | None:
        """Terminal failure before pre-roll, as observed on the owner loop."""
        if self._voice.ever_ready:
            return None
        if self._voice.exhausted:
            return self._failure
        for feed in self._feeds:
            if not feed.session.ready and (failure := feed.session.failure) in PERMANENT_FAILURES:
                return failure
        return None

    def ready_now(self) -> bool:
        """Whether frames have become readable. Only ever changes from False to True."""
        return self._voice.ever_ready

    def read_frame(self) -> bytes | None:
        """One frame, ``None`` for none available now, ``b""`` for none will follow."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            voice = self._voice
            if voice.silent:
                return b""
            frame = self._next_frame(voice)
            if frame:
                voice.recovering = False
            elif voice.audible is None and voice.ever_ready and not voice.exhausted:
                voice.recovering = True
            if not frame or voice.fade_out_left is None:
                return frame
            voice.fade_out_left -= 1
            gain = voice.fade_out_left / FADE_OUT_FRAMES
            if voice.fade_out_left == 0:
                voice.silent = True
                self._signal()
            return audioop.mul(frame, SAMPLE_BYTES, gain)
        finally:
            self._lock.release()

    def _next_frame(self, voice: _Voice) -> bytes | None:
        audible = voice.audible
        if audible is None:
            return b"" if voice.exhausted else None
        if voice.crossfade is not None:
            return self._crossfaded(voice, audible, voice.crossfade)
        frame = audible.bank.read_frame()
        if frame == b"":
            voice.audible = None
            self._signal()
            return b"" if voice.exhausted else None
        return frame

    def _crossfaded(self, voice: _Voice, old: _Feed, fade: _Crossfade) -> bytes | None:
        """Mix one frame with linear gains, which sum to one and so can't add clipping."""
        leaving = old.bank.read_frame()
        arriving = fade.incoming.bank.read_frame()
        if arriving == b"":  # the replacement died; keep what is playing
            voice.crossfade = None
            self._signal()
            if leaving == b"":
                voice.audible = None
                return b"" if voice.exhausted else None
            return leaving
        if arriving is None:  # time only advances with the replacement's audio
            gain = fade.position / fade.frames
            return None if not leaving else audioop.mul(leaving, SAMPLE_BYTES, 1.0 - gain)
        fade.position += 1
        gain = fade.position / fade.frames
        rising = audioop.mul(arriving, SAMPLE_BYTES, gain)
        mixed = (
            rising
            if not leaving
            else audioop.add(rising, audioop.mul(leaving, SAMPLE_BYTES, 1.0 - gain), SAMPLE_BYTES)
        )
        if fade.position >= fade.frames:
            voice.audible, voice.crossfade = fade.incoming, None
            self._signal()
        return mixed

    def _signal(self) -> None:
        """Wake the supervisor from the voice thread; never blocks on audio."""
        if self._loop is not None:
            with contextlib.suppress(RuntimeError):  # a closing loop has nothing waiting
                self._loop.call_soon_threadsafe(self._changed.set)

    # Owner operations.

    def start(self, first_bank: PcmBank | None = None) -> None:
        """Take ownership of an admitted first lease when the player supplies one."""
        self._loop = asyncio.get_running_loop()
        initial = self._start_feed(first_bank) if first_bank is not None else None
        self._supervisor = asyncio.create_task(self._supervise(initial))

    async def wait_ready(self) -> bool:
        """Wait for the first audio; False means the stream could not start."""
        await self._wait_until(lambda: self._voice.ever_ready or self._voice.exhausted)
        return self._voice.ever_ready

    @property
    def crossfading(self) -> bool:
        """Whether a replacement is fading in right now."""
        return self._voice.crossfade is not None

    def fade_out(self) -> None:
        """Ramp to silence over the next frames, for a stop. ``wait_silent`` follows it."""
        with self._lock:
            if self._voice.fade_out_left is None:
                self._voice.fade_out_left = FADE_OUT_FRAMES

    async def wait_silent(self) -> None:
        """Return when a fade-out reaches silence. It waits on frames being read."""
        await self._wait_until(lambda: self._voice.silent)

    def cancel(self) -> None:
        """Stop everything. Each bank keeps its own first cancellation time."""
        if self._supervisor is not None:
            self._supervisor.cancel()
        for feed in self._feeds:
            feed.session.cancel()
        with self._lock:
            self._voice.exhausted = True
        self._changed.set()

    async def finish(self) -> StreamReport:
        """Cancel, then join every owner and return every lease that actually released.

        A session that misses its deadline counts as a failed cleanup, and its lease stays
        charged until it settles, so this can outlast the deadline. Cancelling this call
        abandons nothing: the retirements keep running and a later call joins them.
        """
        self.cancel()
        if self._supervisor is not None:
            await asyncio.wait({self._supervisor})
        for feed in tuple(self._feeds):
            self._retire(feed)
        while self._retirements:
            await asyncio.wait(self._retirements)
        return StreamReport(
            self._failure,
            self._sessions_started,
            self._crossfades,
            self._failed_cleanups,
            len(self._debt),
        )

    def release_debt(self) -> bool:
        """Retry any bank that could not be released; True once none is retained."""
        self._debt = [bank for bank in self._debt if not self._release_bank(bank)]
        return not self._debt

    def _release_bank(self, bank: PcmBank) -> bool:
        if not bank.release():
            return False
        if self._on_bank_released is not None:
            self._on_bank_released()
        return True

    # The supervisor.

    async def _wait_until(self, condition: Callable[[], bool]) -> None:
        while True:
            self._changed.clear()
            with self._lock:
                if condition():
                    return
            await self._changed.wait()

    async def _supervise(self, initial: _Feed | None) -> None:
        try:
            feed = await self._prepare(initial=initial)
            if feed is None:
                self._exhaust()
                return
            with self._lock:
                self._voice.audible = feed
                self._voice.ever_ready = True
            self._changed.set()
            short_sessions = 0
            while True:
                await self._wait_for_renewal(feed)
                # Attempts restart with each renewal, so a service that lets every session
                # reach readiness and then drops it needs a bound of its own.
                short_sessions = short_sessions + 1 if self._was_short(feed) else 0
                if short_sessions >= MAX_SESSION_ATTEMPTS:
                    self._failure = feed.session.failure or "closed"
                    self._exhaust()
                    return
                replacement = await self._replacement(feed)
                if replacement is None:
                    self._exhaust()
                    return
                self._switch(feed, replacement)
                await self._wait_until(lambda: self._voice.crossfade is None)
                if self._voice.audible is replacement:
                    feed, retiring = replacement, feed
                else:
                    retiring = replacement  # it died mid-fade; the old audio plays on
                self._retire(retiring)
                if self._record_refusal(retiring):
                    feed.session.end_production()
                    self._exhaust()
                    return
        except asyncio.CancelledError:
            pass  # `cancel` already stopped every session this owns
        except Exception as exc:
            log.error("lyria stream failed unexpectedly: %s", type(exc).__name__)
            self._failure = "internal"
            self._exhaust()

    @staticmethod
    def _was_short(feed: _Feed) -> bool:
        return feed.session.stopped and feed.session.pcm_bytes < HEALTHY_SESSION_BYTES

    def _exhaust(self) -> None:
        with self._lock:
            self._voice.exhausted = True
        self._changed.set()
        log.warning("lyria stream cannot continue: failure=%s", self._failure or "none")

    async def _wait_for_renewal(self, feed: _Feed) -> None:
        """Return when the timer runs out or the session ends, whichever is first."""
        ended = asyncio.ensure_future(feed.session.wait_stopped())
        timer = asyncio.ensure_future(self._sleep(self._renew_after))
        try:
            await asyncio.wait({ended, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (ended, timer):
                waiter.cancel()
            await asyncio.wait({ended, timer})

    async def _replacement(self, feed: _Feed) -> _Feed | None:
        """A ready replacement, or None once this renewal's attempts are spent."""
        replacement = await self._prepare(feed)
        if replacement is not None or self._failure in PERMANENT_FAILURES:
            return replacement
        if not feed.session.stopped:
            # A planned renewal failed while audio is healthy. Keep playing and spend one
            # more set of attempts when this session really ends, then stop trying.
            await feed.session.wait_stopped()
            replacement = await self._prepare(feed)
            if replacement is not None or self._failure in PERMANENT_FAILURES:
                return replacement
        if self._failure != "capacity":
            return None
        # The other bank is in use elsewhere, so the only way forward is this session's
        # own bank: let it drain, return it, and accept the gap while the next pre-rolls.
        await self._wait_until(lambda: self._voice.audible is None)
        self._retire(feed)
        retirement = feed.retirement
        assert retirement is not None
        await asyncio.wait({retirement})
        return await self._prepare(feed)

    def _record_refusal(self, feed: _Feed | None) -> bool:
        if feed is not None and (failure := feed.session.failure) in PERMANENT_FAILURES:
            self._failure = failure
            return True
        return False

    def _start_feed(self, bank: PcmBank) -> _Feed:
        session = LyriaSession(
            self._request,
            bank,
            api_key=self._api_key,
            endpoint=self._endpoint,
            ssl_context=self._ssl_context,
            clock=self._clock,
            sleep=self._sleep,
        )
        feed = _Feed(bank, session, asyncio.create_task(self._observe_cleanup(session)))
        self._feeds.add(feed)
        self._sessions_started += 1
        session.start()
        return feed

    async def _prepare(
        self, replacing: _Feed | None = None, *, initial: _Feed | None = None
    ) -> _Feed | None:
        for attempt in range(MAX_SESSION_ATTEMPTS):
            if attempt:
                await self._sleep(RETRY_DELAY_SECONDS * attempt)
            # The audible feed can refuse the request after readiness, even while a
            # renewal is in backoff. Check before acquiring a lease or opening a socket.
            if self._record_refusal(replacing):
                return None
            if initial is not None:
                feed, initial = initial, None
            else:
                try:
                    feed = self._start_feed(self._pool.acquire())
                except PcmCapacityError:
                    self._failure = "capacity"
                    continue
            session = feed.session
            await session.wait_prepared()
            # A refusal can also arrive while the replacement is already preparing.
            # Keep the admitted audio playing and retire the unused candidate.
            if self._record_refusal(replacing) or (
                replacing is not None and self._record_refusal(feed)
            ):
                self._retire(feed)
                return None
            if session.ready:
                self._failure = None
                return feed
            self._failure = session.failure
            self._retire(feed)
            if self._failure in PERMANENT_FAILURES:
                break
        return None

    def _switch(self, old: _Feed, new: _Feed) -> None:
        """Start the crossfade, or go straight to the new audio if nothing is playing."""
        buffered = old.bank.facts().buffered_bytes // FRAME_BYTES
        frames = buffered if old.session.stopped else CROSSFADE_FRAMES
        frames = min(frames, CROSSFADE_FRAMES)
        with self._lock:
            voice = self._voice
            if voice.audible is None or frames == 0:
                voice.audible = new
            else:
                voice.crossfade = _Crossfade(new, frames)
                self._crossfades += 1
        self._changed.set()

    def _retire(self, feed: _Feed) -> None:
        """Start, once, the task that owns ``feed`` until its lease is actually returned."""
        if feed.retirement is None:
            feed.retirement = asyncio.create_task(self._settle_feed(feed))
            self._retirements.add(feed.retirement)
            feed.retirement.add_done_callback(self._retirements.discard)

    async def _observe_cleanup(self, session: LyriaSession) -> None:
        # A session may stop itself with playable PCM. Its cleanup clock starts
        # then, even if the player keeps draining and retirement is still far away.
        report = await session.finish()
        if report.cleanup == "expired":
            self._failed_cleanups += 1  # a failed verdict, whatever happens later
            if self._on_cleanup_expired is not None:
                self._on_cleanup_expired()
            log.error("lyria session missed its cleanup deadline; its bank stays leased")

    async def _settle_feed(self, feed: _Feed) -> None:
        feed.session.cancel()
        await feed.cleanup
        await feed.session.settle()
        self._feeds.discard(feed)
        if not self._release_bank(feed.bank):
            self._debt.append(feed.bank)
        self._changed.set()
