"""Lyria path regressions against a loopback fake, with the real aiohttp client.

Time is a manual clock and every schedule waits on a condition, so nothing here sleeps
until a race happens. The fake speaks the documented protocol over real sockets, which
exercises aiohttp's framing, limits and close paths. It can't establish how the real
service behaves; ADR 0008 lists what still needs a live check.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import tracemalloc
from asyncio import sslproto
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

import aiohttp
import pytest
from aiohttp import web
from aiohttp.client_proto import ResponseHandler

from sobafm.lyria import (
    CROSSFADE_FRAMES,
    FADE_OUT_FRAMES,
    MAX_CHUNKS_PER_MESSAGE,
    MAX_ENVELOPE_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_MESSAGE_PCM_BYTES,
    PLAY_MESSAGE,
    RECEIVE_BUFFER_BYTES,
    SETUP_MESSAGE,
    STALL_SECONDS,
    TLS_READ_HIGH_WATER_BYTES,
    Audio,
    Filtered,
    GeneratedStream,
    Ignored,
    LyriaSession,
    MessageError,
    MusicRequest,
    Prompt,
    Scale,
    SessionReport,
    SetupComplete,
    Warned,
    parse_message,
    receive_bounded_socket,
)
from sobafm.pcm import (
    BANK_BYTES,
    BYTES_PER_SECOND,
    FRAME_BYTES,
    READY_BYTES,
    STEREO_SAMPLE_BYTES,
    PcmBank,
    PcmBankPool,
)

KEY = "test-placeholder-key"
PROMPT_TEXT = "PRIVATE-PROMPT-TEXT"
REQUEST = MusicRequest((Prompt(PROMPT_TEXT, 1.5),), bpm=90, scale=Scale.D_MAJOR_B_MINOR)
MESSAGE_SECONDS = 2.0
MESSAGE_PCM = int(MESSAGE_SECONDS * BYTES_PER_SECOND)
PRE_ROLL_MESSAGES = READY_BYTES // MESSAGE_PCM
PRE_ROLL_FRAMES = READY_BYTES // FRAME_BYTES
RENEW_SECONDS = 3.0  # below the stall limit, so advancing to a renewal stalls nothing

assert READY_BYTES % MESSAGE_PCM == 0


def run[T](scenario: Coroutine[Any, Any, T]) -> T:
    """Run one schedule. The limit is a failsafe for a hang, not part of any schedule."""

    async def bounded() -> T:
        async with asyncio.timeout(60):
            return await scenario

    return asyncio.run(bounded())


async def until(condition: Callable[[], bool]) -> None:
    """Let the loop run, including socket I/O, until ``condition`` holds."""
    while not condition():
        await asyncio.sleep(0)


async def advancing_until(time: ManualTime, condition: Callable[[], bool]) -> None:
    """Move the clock in small steps until ``condition`` holds, for retries and backoff."""
    while not condition():
        time.advance(0.25)
        for _ in range(20):
            await asyncio.sleep(0)


class ManualTime:
    """A clock and sleep the test advances."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self._sleepers: list[tuple[float, float, asyncio.Future[None]]] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (self.now + delay, delay, waiter)
        self._sleepers.append(entry)
        try:
            await waiter
        finally:
            self._sleepers.remove(entry)

    def delays(self) -> list[float]:
        return [delay for _, delay, _ in self._sleepers]

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for when, _, waiter in self._sleepers:
            if when <= self.now and not waiter.done():
                waiter.set_result(None)


@cache
def pcm_of(value: int, seconds: float = MESSAGE_SECONDS) -> bytes:
    return struct.pack("<hh", value, value) * int(seconds * 48_000)


def audio_message(value: int = 1_000, seconds: float = MESSAGE_SECONDS) -> str:
    return json.dumps(
        {
            "serverContent": {
                "audioChunks": [
                    {
                        "data": base64.b64encode(pcm_of(value, seconds)).decode(),
                        "mimeType": "audio/l16;rate=48000;channels=2",
                    }
                ]
            }
        }
    )


def sample_of(frame: bytes) -> int:
    return struct.unpack("<h", frame[:2])[0]


class Peer:
    """The server side of one connection."""

    def __init__(self, request: web.Request, websocket: web.WebSocketResponse) -> None:
        self.request = request
        self.websocket = websocket
        self.received: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.disconnected = asyncio.Event()
        self.played = asyncio.Event()
        self.reader: asyncio.Task[None] | None = None
        self.ignoring = False

    async def read_forever(self) -> None:
        try:
            async for message in self.websocket:
                if message.type is aiohttp.WSMsgType.TEXT:
                    self.received.put_nowait(json.loads(message.data))
        finally:
            if not self.ignoring:
                self.disconnected.set()

    def stop_reading(self) -> None:
        """Stop answering, so a close frame from the client gets no reply."""
        assert self.reader is not None
        self.ignoring = True
        self.reader.cancel()

    async def setup(self) -> None:
        assert await self.received.get() == SETUP_MESSAGE
        await self.websocket.send_str('{"setupComplete":{}}')

    async def handshake(self) -> None:
        await self.setup()
        assert await self.received.get() == REQUEST.prompts_message()
        assert await self.received.get() == REQUEST.config_message()
        assert await self.received.get() == PLAY_MESSAGE
        self.played.set()

    async def pre_roll(self, value: int = 1_000) -> None:
        for _ in range(PRE_ROLL_MESSAGES):
            await self.websocket.send_str(audio_message(value))

    def write_raw(self, data: bytes) -> None:
        assert self.request.transport is not None
        self.request.transport.write(data)

    def abort(self) -> None:
        assert self.request.transport is not None
        self.request.transport.abort()


type Script = Callable[[Peer], Awaitable[None]]


@dataclass(frozen=True)
class Refuse:
    """Answer the handshake with this status instead of upgrading."""

    status: int
    location: str | None = None


class FakeLyria:
    """A loopback endpoint, scripted per connection in order."""

    def __init__(
        self, *scripts: Script | Refuse, ssl_context: ssl.SSLContext | None = None
    ) -> None:
        self.scripts = deque(scripts)
        self.peers: list[Peer] = []
        self.handshake_seen = asyncio.Event()
        self.hold_handshake: asyncio.Event | None = None
        self.requests: list[web.Request] = []
        self.ssl_context = ssl_context
        self.url = ""
        self.runner: web.AppRunner | None = None
        self.errors: list[BaseException] = []

    async def __aenter__(self) -> Self:
        app = web.Application()
        app.router.add_get("/", self.handle)
        self.runner = web.AppRunner(app, shutdown_timeout=0.1)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=self.ssl_context)
        await site.start()
        scheme = "wss" if self.ssl_context is not None else "ws"
        self.url = f"{scheme}://127.0.0.1:{self.runner.addresses[0][1]}/"
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self.runner is not None
        await self.runner.cleanup()
        if exc is None and self.errors:
            raise self.errors[0]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(request)
        self.handshake_seen.set()
        if self.hold_handshake is not None:
            await self.hold_handshake.wait()
        script = self.scripts.popleft()
        if isinstance(script, Refuse):
            headers = {} if script.location is None else {"Location": script.location}
            return web.Response(status=script.status, headers=headers)
        websocket = web.WebSocketResponse()
        try:
            await websocket.prepare(request)
        except ConnectionError:
            return websocket  # the client gave up while the handshake was held
        peer = Peer(request, websocket)
        self.peers.append(peer)
        peer.reader = asyncio.create_task(peer.read_forever())
        running = asyncio.ensure_future(script(peer))
        gone = asyncio.create_task(peer.disconnected.wait())
        try:
            await asyncio.wait({running, gone}, return_when=asyncio.FIRST_COMPLETED)
            if running.done() and running.exception() is not None:
                peer.abort()  # a broken script must fail the test rather than hang it
            elif running.done():
                await peer.disconnected.wait()
        finally:
            running.cancel()
            gone.cancel()
            peer.reader.cancel()
            await asyncio.wait({running, gone, peer.reader})
            peer.disconnected.set()
        if not running.cancelled():
            error = running.exception()
            if error is not None and not isinstance(error, ConnectionError):
                self.errors.append(error)
        return websocket


class Harness:
    """One session on one bank, with everything a schedule needs to look at."""

    def __init__(self, fake: FakeLyria, time: ManualTime | None = None) -> None:
        self.time = time if time is not None else ManualTime()
        self.pool = PcmBankPool(clock=self.time.clock)
        self.bank: PcmBank = self.pool.acquire()
        self.session = LyriaSession(
            REQUEST,
            self.bank,
            api_key=KEY,
            endpoint=fake.url,
            clock=self.time.clock,
            sleep=self.time.sleep,
        )

    async def finished(self) -> SessionReport:
        report = await self.session.finish()
        await self.session.settle()
        return report

    def released(self) -> bool:
        return self.bank.release() and self.pool.leased == 0


def drain(bank: PcmBank) -> list[bytes]:
    """Read frames until none is available right now."""
    frames: list[bytes] = []
    while frame := bank.read_frame():
        frames.append(frame)
    return frames


# Requests and wire messages.


@pytest.mark.parametrize(
    "fields",
    [
        {"prompts": ()},
        {"prompts": (Prompt("a"),) * 5},
        {"prompts": (Prompt("a"),), "bpm": 59},
        {"prompts": (Prompt("a"),), "bpm": 201},
        {"prompts": (Prompt("a"),), "density": 1.5},
        {"prompts": (Prompt("a"),), "density": -0.1},
        {"prompts": (Prompt("a"),), "brightness": 1.1},
    ],
)
def test_request_rejects_undocumented_values(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        MusicRequest(**fields)


@pytest.mark.parametrize(
    "fields",
    [
        {"text": ""},
        {"text": "   "},
        {"text": "x" * 201},
        {"text": "a\nb"},
        {"text": "a", "weight": 0.0},
        {"text": "a", "weight": -1.0},
        {"text": "a", "weight": float("inf")},
        {"text": "a", "weight": float("nan")},
    ],
)
def test_prompt_rejects_invalid_values(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Prompt(**fields)


def test_messages_use_the_documented_shape() -> None:
    request = MusicRequest(
        (Prompt("piano", 2.0), Prompt("rain")),
        bpm=120,
        scale=Scale.F_MAJOR_D_MINOR,
        density=0.25,
        brightness=0.75,
        mute_drums=True,
        vocalization=True,
    )
    assert SETUP_MESSAGE == {"setup": {"model": "models/lyria-realtime-exp"}}
    assert PLAY_MESSAGE == {"playbackControl": "PLAY"}
    assert request.prompts_message() == {
        "clientContent": {
            "weightedPrompts": [{"text": "piano", "weight": 2.0}, {"text": "rain", "weight": 1.0}]
        }
    }
    assert request.config_message() == {
        "musicGenerationConfig": {
            "bpm": 120,
            "scale": "F_MAJOR_D_MINOR",
            "density": 0.25,
            "brightness": 0.75,
            "muteDrums": True,
            "musicGenerationMode": "VOCALIZATION",
        }
    }
    assert MusicRequest((Prompt("a"),)).config_message() is None


# Message validation.

AAAA = "AAAAAAAA"  # six bytes: not a whole number of stereo samples
WHOLE = base64.b64encode(bytes(STEREO_SAMPLE_BYTES)).decode()


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b"3",
        b'{"warning":"\xff"}',
        b'{"serverContent":[]}',
        b'{"serverContent":{"audioChunks":{}}}',
        b'{"serverContent":{"audioChunks":[3]}}',
        b'{"serverContent":{"audioChunks":[{"data":"****"}]}}',
        b'{"serverContent":{"audioChunks":[{"data":"AA-_"}]}}',
        b'{"serverContent":{"audioChunks":[{"data":3}]}}',
        b'{"serverContent":{"audioChunks":[{"data":"AAAAA"}]}}',
        b'{"serverContent":{"audioChunks":[{"data":"A==="}]}}',
        b'{"serverContent":{"audioChunks":[{"data":"AAAAAAAA"}]}}',  # six PCM bytes
        b'{"serverContent":{"audioChunks":[{"data":"AAA="}]}}',  # two PCM bytes
        (f'{{"serverContent":{{"audioChunks":[{{"d\\u0061ta":"{WHOLE}"}}]}}}}').encode(),
        (f'{{"x\\"data":"{WHOLE}","serverContent":{{"audioChunks":[]}}}}').encode(),
        (f'{{"serverContent":{{"audioChunks":[{{"data":""}}]}},"x\\"data":"{WHOLE}"}}').encode(),
        (
            f'{{"serverContent":{{"audioChunks":[{{"sourceMetadata":{{"data":"{WHOLE}"}}}},'
            '{"data":""}]}}'
        ).encode(),
        (f'{{"warning":"text","x\\"data":"{WHOLE}"}}').encode(),
        b'{"serverContent":{"audioChunks":[{"data":"","mimeType":"audio/l16;rate=44100"}]}}',
        b'{"serverContent":{"audioChunks":[{"data":"","mimeType":"audio/l16;channels=1"}]}}',
        b'{"serverContent":{"audioChunks":[{"data":"","mimeType":"video/mp4"}]}}',
        b'{"serverContent":{"audioChunks":[{"data":"","mimeType":3}]}}',
        b'{"serverContent":{},"warning":"both"}',
        b'{"setupComplete":{},"filteredPrompt":{}}',
        b'{"warning":3}',
        json.dumps({"warning": "x" * (MAX_ENVELOPE_BYTES + 1)}).encode(),
        b"[" * MAX_ENVELOPE_BYTES,
        json.dumps(
            {"serverContent": {"audioChunks": [{"data": ""}] * (MAX_CHUNKS_PER_MESSAGE + 1)}}
        ).encode(),
    ],
)
def test_parse_rejects_malformed_messages(payload: bytes) -> None:
    with pytest.raises(MessageError):
        parse_message(payload)


def test_parse_finds_audio_in_place_and_validates_it() -> None:
    first, second = base64.b64encode(pcm_of(5, 0.01)), base64.b64encode(pcm_of(6, 0.02))
    payload = (
        b'{\n  "serverContent": {\n    "audioChunks": [\n      {\n        "data" : "'
        + first
        + b'",\n        "mimeType": "audio/l16;rate=48000;channels=2",\n'
        + '        "sourceMetadata": {"clientContent": {"text": "\U0001f3b9"}}\n'.encode()
        + b'      },\n      {"sourceMetadata": {}},\n      {"data":"'
        + second
        + b'"}\n    ]\n  }\n}'
    )
    parsed = parse_message(payload)
    assert isinstance(parsed, Audio)
    assert [payload[a:b] for a, b in parsed.spans] == [first, second]
    assert parsed.pcm_bytes == len(pcm_of(5, 0.01)) + len(pcm_of(6, 0.02))


def test_parse_recognizes_the_other_messages() -> None:
    assert isinstance(parse_message(b'{"setupComplete":{}}'), SetupComplete)
    assert isinstance(
        parse_message(b'{"filteredPrompt":{"text":"x","filteredReason":"y"}}'), Filtered
    )
    assert isinstance(parse_message(b'{"warning":"careful"}'), Warned)
    assert isinstance(parse_message(b"{}"), Ignored)
    assert isinstance(parse_message(b'{"usageMetadata":{"tokens":1}}'), Ignored)
    assert parse_message(b'{"serverContent":{}}') == Audio((), 0)


def test_non_ascii_text_cannot_widen_the_message() -> None:
    """One emoji must not make the 512,000 bytes of base64 four bytes a character."""
    encoded = base64.b64encode(pcm_of(1)).decode()
    payload = json.dumps(
        {
            "serverContent": {
                "audioChunks": [
                    {"data": encoded, "sourceMetadata": {"clientContent": {"text": "\U0001f3b9"}}}
                ]
            }
        },
        ensure_ascii=False,
    ).encode()
    tracemalloc.start()
    try:
        parse_message(payload)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < MAX_ENVELOPE_BYTES * 8


# A session's normal path.


@pytest.mark.parametrize("binary", [False, True])
def test_audio_stays_unreadable_until_six_seconds_then_plays_exactly(binary: bool) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        sent = asyncio.Event()

        async def script(peer: Peer) -> None:
            await peer.handshake()
            for _ in range(PRE_ROLL_MESSAGES - 1):
                await peer.websocket.send_str(audio_message(7))
            sent.set()
            await release.wait()
            payload = audio_message(7)
            if binary:
                await peer.websocket.send_bytes(payload.encode())
            else:
                await peer.websocket.send_str(payload)

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await sent.wait()
            await until(lambda: harness.session.pcm_bytes == (PRE_ROLL_MESSAGES - 1) * MESSAGE_PCM)
            assert harness.bank.read_frame() is None
            assert not harness.session.ready
            release.set()
            await harness.session.wait_prepared()
            assert harness.session.ready
            frames = drain(harness.bank)
            assert len(frames) == PRE_ROLL_FRAMES
            assert frames[0] == frames[-1] == pcm_of(7)[:FRAME_BYTES]
            assert harness.session.failure is None
            harness.session.cancel()
            report = await harness.finished()
            assert report == SessionReport(None, READY_BYTES, None, "clean")
            assert harness.released()

    run(scenario())


def test_pings_warnings_and_unknown_messages_leave_the_stream_running() -> None:
    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            await peer.handshake()
            await peer.websocket.ping(b"are you there")
            await peer.websocket.send_str('{"warning":"careful"}')
            await peer.websocket.send_str('{"usageMetadata":{"tokens":1}}')
            await peer.websocket.send_str("{}")
            await peer.pre_roll()

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await harness.session.wait_prepared()
            assert harness.session.ready
            assert harness.session.failure is None
            harness.session.cancel()
            await harness.finished()
            assert harness.released()

    run(scenario())


def test_several_chunks_in_one_message_are_written_in_order() -> None:
    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            await peer.handshake()
            chunks = [{"data": base64.b64encode(pcm_of(v, 0.5)).decode()} for v in (31, 32)]
            await peer.websocket.send_str(json.dumps({"serverContent": {"audioChunks": chunks}}))
            await peer.pre_roll(33)

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await until(lambda: harness.session.pcm_bytes == BYTES_PER_SECOND + READY_BYTES)
            levels = [sample_of(frame) for frame in drain(harness.bank)]
            assert levels == [31] * 25 + [32] * 25 + [33] * PRE_ROLL_FRAMES
            harness.session.cancel()
            await harness.finished()
            assert harness.released()

    run(scenario())


def test_setup_finishes_before_anything_else_is_sent() -> None:
    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            assert await peer.received.get() == SETUP_MESSAGE
            for _ in range(50):
                await asyncio.sleep(0)
            assert peer.received.empty()
            await peer.websocket.send_str('{"warning":"before setup"}')
            for _ in range(50):
                await asyncio.sleep(0)
            assert peer.received.empty()
            await peer.websocket.send_str('{"setupComplete":{}}')
            assert await peer.received.get() == REQUEST.prompts_message()
            assert await peer.received.get() == REQUEST.config_message()
            assert await peer.received.get() == PLAY_MESSAGE
            await peer.pre_roll()

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await harness.session.wait_prepared()
            assert harness.session.ready
            harness.session.cancel()
            assert (await harness.finished()).cleanup == "clean"
            assert harness.released()

    run(scenario())


def test_only_the_key_header_carries_the_credential_and_logs_stay_safe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            await peer.handshake()
            await peer.websocket.send_str(f'{{"warning":"{PROMPT_TEXT}"}}')
            await peer.websocket.send_str(
                json.dumps({"filteredPrompt": {"text": PROMPT_TEXT, "filteredReason": PROMPT_TEXT}})
            )

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await harness.session.wait_stopped()
            assert harness.session.failure == "filtered"
            await harness.finished()
            assert harness.released()
            request = fake.requests[0]
            assert request.headers["x-goog-api-key"] == KEY
            assert KEY not in str(request.url)

    run(scenario())
    assert PROMPT_TEXT not in caplog.text
    assert KEY not in caplog.text


# Failure schedules before readiness: nothing was audible and the bank stops.


def frame_header(length: int, *, opcode: int = 0x1, fin: bool = True) -> bytes:
    first = (0x80 if fin else 0) | opcode
    if length < 126:
        return bytes([first, length])
    if length < 65_536:
        return bytes([first, 126]) + struct.pack("!H", length)
    return bytes([first, 127]) + struct.pack("!Q", length)


async def setup_only(peer: Peer) -> None:
    await peer.setup()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("abort", "truncated"),
        ("abort_mid_frame", "truncated"),
        ("close_1000", "closed"),
        ("close_1011", "closed"),
        ("close_1008", "rejected"),
        ("oversized_header", "oversized"),
        ("oversized_fragments", "oversized"),
        ("compressed_frame", "malformed"),
        ("unknown_opcode", "malformed"),
        ("not_json", "malformed"),
        ("bad_base64", "malformed"),
        ("filtered", "filtered"),
    ],
)
def test_failures_before_readiness_stop_the_bank(name: str, expected: str) -> None:
    async def fail(peer: Peer) -> None:
        match name:
            case "abort":
                peer.abort()
            case "abort_mid_frame":
                peer.write_raw(frame_header(1_000) + b"x" * 10)
                peer.abort()
            case "close_1000":
                await peer.websocket.close(code=1000)
            case "close_1011":
                await peer.websocket.close(code=1011)
            case "close_1008":
                await peer.websocket.close(code=1008)
            case "oversized_header":
                # Rejected from the declared length alone, before any payload is buffered.
                peer.write_raw(frame_header(MAX_MESSAGE_BYTES))
            case "oversized_fragments":
                part = b"x" * (MAX_MESSAGE_BYTES // 2)
                peer.write_raw(frame_header(len(part), fin=False) + part)
                peer.write_raw(frame_header(len(part), opcode=0x0) + part)
            case "compressed_frame":
                peer.write_raw(bytes([0xC1, 2]) + b"{}")
            case "unknown_opcode":
                peer.write_raw(bytes([0x83, 2]) + b"{}")
            case "not_json":
                await peer.websocket.send_str("not json")
            case "bad_base64":
                await peer.websocket.send_str('{"serverContent":{"audioChunks":[{"data":"****"}]}}')
            case _:
                await peer.websocket.send_str('{"filteredPrompt":{"filteredReason":"x"}}')

    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            await peer.handshake()
            await peer.websocket.send_str(audio_message())
            await fail(peer)

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await harness.session.wait_prepared()
            assert not harness.session.ready
            assert harness.session.failure == expected
            report = await harness.finished()
            assert report.failure == expected
            assert report.cleanup == "clean"
            facts = harness.bank.facts()
            assert facts.cancelled_at is not None
            assert not facts.pcm_valid
            assert not facts.ready
            assert harness.bank.read_frame() == b""
            assert harness.released()

    run(scenario())


# After readiness: the validated audio drains and the facts stay separate.


@pytest.mark.parametrize(
    ("name", "expected", "transfer_complete"),
    [
        ("close_1000", None, True),
        ("close_1001", None, True),
        ("close_1011", "closed", False),
        ("abort", "truncated", False),
        ("abort_mid_message", "truncated", False),
        ("oversized", "oversized", False),
        ("not_json", "malformed", False),
        ("setup_again", "malformed", False),
    ],
)
def test_failures_after_readiness_keep_only_whole_validated_messages(
    name: str, expected: str | None, transfer_complete: bool
) -> None:
    async def fail(peer: Peer) -> None:
        match name:
            case "close_1000":
                await peer.websocket.close(code=1000)
            case "close_1001":
                await peer.websocket.close(code=1001)
            case "close_1011":
                await peer.websocket.close(code=1011)
            case "abort":
                peer.abort()
            case "abort_mid_message":
                encoded = audio_message(9).encode()
                peer.write_raw(frame_header(len(encoded)) + encoded[: len(encoded) // 2])
                peer.abort()
            case "oversized":
                peer.write_raw(frame_header(MAX_MESSAGE_BYTES))
            case "not_json":
                await peer.websocket.send_str("not json")
            case _:
                await peer.websocket.send_str('{"setupComplete":{}}')

    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            await peer.handshake()
            await peer.pre_roll(3)
            await fail(peer)

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await harness.session.wait_stopped()
            assert harness.session.failure == expected
            report = await harness.finished()
            assert report.failure == expected
            assert report.pcm_bytes == READY_BYTES
            assert report.cleanup == "clean"
            facts = harness.bank.facts()
            assert facts.transfer_complete is transfer_complete
            assert facts.pcm_valid
            assert not facts.decoder_complete
            assert facts.cancelled_at is None
            assert not facts.audible_drain
            frames = drain(harness.bank)
            assert len(frames) == PRE_ROLL_FRAMES
            assert {sample_of(frame) for frame in frames} == {3}
            assert harness.bank.facts().audible_drain
            assert harness.released()

    run(scenario())


def test_a_corrupt_message_commits_none_of_its_audio() -> None:
    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            await peer.handshake()
            await peer.websocket.send_str(audio_message())
            good = base64.b64encode(pcm_of(2)).decode()
            await peer.websocket.send_str(
                json.dumps({"serverContent": {"audioChunks": [{"data": good}, {"data": "AAA="}]}})
            )

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await harness.session.wait_stopped()
            assert harness.session.failure == "malformed"
            assert harness.bank.facts().written_bytes == MESSAGE_PCM
            await harness.finished()
            assert harness.released()

    run(scenario())


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "rejected"),
        (403, "rejected"),
        (404, "rejected"),
        (408, "connect"),
        (429, "connect"),
        (503, "connect"),
    ],
)
def test_a_refused_handshake_is_classified(status: int, expected: str) -> None:
    async def scenario() -> None:
        async with FakeLyria(Refuse(status)) as fake:
            harness = Harness(fake)
            harness.session.start()
            await harness.session.wait_stopped()
            assert harness.session.failure == expected
            assert (await harness.finished()).cleanup == "clean"
            assert harness.bank.facts().cancelled_at is not None
            assert harness.released()

    run(scenario())


def test_a_refused_connection_is_a_connect_failure() -> None:
    async def scenario() -> None:
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
        time = ManualTime()
        pool = PcmBankPool(clock=time.clock)
        bank = pool.acquire()
        session = LyriaSession(
            REQUEST,
            bank,
            api_key=KEY,
            endpoint=f"ws://127.0.0.1:{port}/",
            clock=time.clock,
            sleep=time.sleep,
        )
        session.start()
        await session.wait_stopped()
        assert session.failure == "connect"
        assert (await session.finish()).cleanup == "clean"
        assert bank.release()

    run(scenario())


def test_a_redirect_is_refused_so_the_key_never_reaches_another_host() -> None:
    async def scenario() -> None:
        async with FakeLyria() as elsewhere, FakeLyria() as fake:
            fake.scripts.append(Refuse(302, elsewhere.url))
            harness = Harness(fake)
            harness.session.start()
            await harness.session.wait_stopped()
            assert harness.session.failure == "rejected"
            assert (await harness.finished()).cleanup == "clean"
            assert elsewhere.requests == []
            assert harness.released()

    run(scenario())


def test_a_message_at_the_limit_is_refused_and_one_below_it_is_accepted() -> None:
    def padded(length: int) -> bytes:
        encoded = base64.b64encode(pcm_of(4)).decode()
        body = f'{{"serverContent":{{"audioChunks":[{{"data":"{encoded}"}}]}}}}'
        return (body + " " * (length - len(body))).encode()

    async def scenario() -> None:
        async def accepted(peer: Peer) -> None:
            await peer.handshake()
            await peer.websocket.send_bytes(padded(MAX_MESSAGE_BYTES - 1))

        async def refused(peer: Peer) -> None:
            await peer.handshake()
            await peer.websocket.send_bytes(padded(MAX_MESSAGE_BYTES))

        async with FakeLyria(accepted, refused) as fake:
            harness = Harness(fake)
            harness.session.start()
            await until(lambda: harness.session.pcm_bytes == MESSAGE_PCM)
            harness.session.cancel()
            await harness.finished()
            assert harness.released()
            second = Harness(fake)
            second.session.start()
            await second.session.wait_stopped()
            assert second.session.failure == "oversized"
            await second.finished()
            assert second.released()

    run(scenario())


# Stalls, on the manual clock.


def test_preparation_has_one_firm_deadline_however_slowly_audio_arrives() -> None:
    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            await peer.handshake()
            await peer.websocket.send_str(audio_message())

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await until(lambda: harness.session.pcm_bytes == MESSAGE_PCM)
            harness.time.advance(14.0)  # audio arrived, but readiness hasn't
            for _ in range(20):
                await asyncio.sleep(0)
            assert not harness.session.stopped
            harness.time.advance(1.0)
            await harness.session.wait_stopped()
            assert harness.session.failure == "prepare_timeout"
            assert harness.bank.facts().cancelled_at is not None
            assert (await harness.finished()).cleanup == "clean"
            assert harness.released()

    run(scenario())


def test_a_held_setup_fails_as_a_preparation_timeout() -> None:
    async def scenario() -> None:
        async def never_answers(peer: Peer) -> None:
            await peer.received.get()

        async with FakeLyria(never_answers) as fake:
            harness = Harness(fake)
            harness.session.start()
            await until(lambda: len(fake.peers) == 1)
            harness.time.advance(15.0)
            await harness.session.wait_stopped()
            assert harness.session.failure == "prepare_timeout"
            await harness.finished()
            await fake.peers[0].disconnected.wait()
            assert harness.released()

    run(scenario())


@pytest.mark.parametrize("mid_frame", [False, True])
def test_silence_after_readiness_is_a_stall_and_the_audio_still_drains(mid_frame: bool) -> None:
    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            await peer.handshake()
            await peer.pre_roll()
            if mid_frame:
                peer.write_raw(frame_header(1_000) + b"x" * 10)

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await harness.session.wait_prepared()
            harness.time.advance(STALL_SECONDS - 0.5)
            for _ in range(20):
                await asyncio.sleep(0)
            assert not harness.session.stopped
            harness.time.advance(0.5)
            await harness.session.wait_stopped()
            assert harness.session.failure == "stalled"
            report = await harness.finished()
            assert report.cleanup == "clean"
            await fake.peers[0].disconnected.wait()
            assert len(drain(harness.bank)) == PRE_ROLL_FRAMES
            assert harness.released()

    run(scenario())


def test_a_full_bank_stops_reading_without_counting_as_a_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        total = 40
        sent = [0]

        async def script(peer: Peer) -> None:
            await peer.handshake()
            for index in range(total):
                await peer.websocket.send_str(audio_message(index + 1))
                sent[0] += 1
            await peer.websocket.close(code=1000)

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await until(lambda: bool(harness.time.delays()) and min(harness.time.delays()) < 3.0)
            # Pacing leaves room for one whole message, and nothing past the bank is held.
            facts = harness.bank.facts()
            assert BANK_BYTES - MAX_MESSAGE_PCM_BYTES < facts.buffered_bytes <= BANK_BYTES
            assert harness.session.pcm_bytes == facts.written_bytes
            assert sent[0] < total
            # A wait far longer than the stall limit, with the bank full, isn't a stall.
            full_until = harness.time.now + 3 * STALL_SECONDS
            await advancing_until(harness.time, lambda: harness.time.now >= full_until)
            assert not harness.session.stopped

            # Reading makes room, reading resumes, and every message arrives in order. The
            # clock only needs to move for pacing here, so stalls are out of the way.
            monkeypatch.setattr("sobafm.lyria.STALL_SECONDS", 1e9)
            samples: list[int] = []
            while not harness.bank.facts().audible_drain:
                for frame in drain(harness.bank):
                    samples.append(sample_of(frame))
                harness.time.advance(0.02)
                await asyncio.sleep(0)
            assert harness.session.failure is None
            assert harness.bank.facts().transfer_complete
            assert sent[0] == total
            assert len(samples) == total * MESSAGE_PCM // FRAME_BYTES
            assert samples == sorted(samples)
            assert set(samples) == set(range(1, total + 1))
            await harness.finished()
            assert harness.released()

    run(scenario())


# Cancellation at each await, then the original deadline.


class SendGate:
    """Blocks the nth WebSocket send, so cancellation lands on that exact await."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, nth: int) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        original = aiohttp.ClientWebSocketResponse.send_str
        count = [0]

        async def gated(
            websocket: aiohttp.ClientWebSocketResponse[Any], data: str, compress: int | None = None
        ) -> None:
            count[0] += 1
            if count[0] == nth:
                self.entered.set()
                await self.release.wait()
            await original(websocket, data, compress)

        monkeypatch.setattr(aiohttp.ClientWebSocketResponse, "send_str", gated)


@pytest.mark.parametrize(
    "point",
    [
        "connect",
        "setup_send",
        "setup_receive",
        "prompts_send",
        "config_send",
        "play_send",
        "audio_receive",
        "audio_receive_mid_frame",
        "space_wait",
        "close",
    ],
)
def test_cancellation_at_each_await_settles_with_one_owner_and_one_deadline(
    point: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sends = {"setup_send": 1, "prompts_send": 2, "config_send": 3, "play_send": 4}

    async def scenario() -> None:
        gate = SendGate(monkeypatch, sends[point]) if point in sends else None

        async def script(peer: Peer) -> None:
            if point == "setup_receive":
                await peer.received.get()
                return
            if point not in sends:
                await peer.handshake()
            elif point != "setup_send":
                await peer.setup()
            if point == "audio_receive_mid_frame":
                peer.write_raw(frame_header(1_000) + b"x" * 10)
            if point in ("space_wait", "close"):
                for _ in range(BANK_BYTES // MESSAGE_PCM + 1):
                    await peer.websocket.send_str(audio_message())

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            time = harness.time
            if point == "connect":
                fake.hold_handshake = asyncio.Event()
            harness.session.start()
            match point:
                case "connect":
                    await fake.handshake_seen.wait()
                case "setup_receive":
                    await until(lambda: len(fake.peers) == 1)
                case "audio_receive":
                    await until(lambda: bool(fake.peers))
                    await fake.peers[0].played.wait()
                case "audio_receive_mid_frame":
                    await until(lambda: bool(fake.peers))
                    await fake.peers[0].played.wait()
                    for _ in range(50):
                        await asyncio.sleep(0)
                case "space_wait" | "close":
                    await until(lambda: 0.0 < min(time.delays(), default=9.0) < 3.0)
                case _:
                    assert gate is not None
                    await gate.entered.wait()
            if point == "close":
                fake.peers[0].stop_reading()  # the polite close gets no reply

            harness.session.cancel()
            cancelled_at = harness.bank.facts().cancelled_at
            assert cancelled_at == time.now
            # Repetition changes neither the deadline nor the owner.
            time.advance(0.5)
            harness.session.cancel()
            time.advance(0.5)
            harness.session.cancel()
            assert harness.bank.facts().cancelled_at == cancelled_at
            finishing = asyncio.create_task(harness.session.finish())
            await asyncio.sleep(0)
            if point == "close":
                time.advance(1.0)  # two seconds in: the stuck close is dropped
            report = await finishing
            assert report.cleanup == "clean"
            assert report.failure is None
            if fake.peers and point != "close":
                await fake.peers[0].disconnected.wait()
            if gate is not None:
                gate.release.set()
            await harness.session.settle()
            assert harness.released()
            if fake.hold_handshake is not None:
                fake.hold_handshake.set()
            if point == "close":
                fake.peers[0].disconnected.set()
            names = [getattr(task.get_coro(), "__qualname__", "") for task in asyncio.all_tasks()]
            assert not [name for name in names if "LyriaSession" in name or "_join" in name]

    run(scenario())


class StuckClose:
    """A close that ignores cancellation until released, like a stuck native operation.

    Leaving the ``async with`` releases it, so a schedule that fails can't hang the run.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancellations = 0
        original = aiohttp.ClientSession.close

        async def stuck(session: aiohttp.ClientSession) -> None:
            self.entered.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
            await original(session)

        monkeypatch.setattr(aiohttp.ClientSession, "close", stuck)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release.set()


def test_a_missed_deadline_is_a_failed_verdict_that_settling_later_never_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            await peer.handshake()

        async with StuckClose(monkeypatch) as stuck, FakeLyria(script) as fake:
            harness = Harness(fake)
            time = harness.time
            harness.session.start()
            await until(lambda: bool(fake.peers))
            await fake.peers[0].played.wait()
            harness.session.cancel()
            first = time.now
            await stuck.entered.wait()

            finishing = asyncio.create_task(harness.session.finish())
            await asyncio.sleep(0)
            # Cancelling `finish` abandons nothing and moves nothing.
            finishing.cancel()
            await asyncio.wait({finishing})
            time.advance(1.0)
            harness.session.cancel()
            finishing = asyncio.create_task(harness.session.finish())
            await until(lambda: bool(time.delays()))
            time.advance(1.0)  # two seconds: the connection is told to drop
            await until(lambda: 1.0 in time.delays())
            assert stuck.cancellations == 0  # _join shields the actual close owner
            assert not finishing.done()
            time.advance(0.5)  # 2.5 seconds: still inside the original deadline
            for _ in range(20):
                await asyncio.sleep(0)
            assert not finishing.done()
            time.advance(0.5)  # exactly three seconds after the first cancellation
            report = await finishing
            assert time.now == first + 3.0
            assert report.cleanup == "expired"

            settling = asyncio.create_task(harness.session.settle())
            for _ in range(20):
                await asyncio.sleep(0)
            assert not settling.done()  # still owned
            stuck.release.set()
            await settling
            assert (await harness.session.finish()).cleanup == "expired"
            assert harness.released()

    run(scenario())


def test_an_owner_stopping_the_bank_first_keeps_that_earlier_clock() -> None:
    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            await peer.handshake()
            await peer.pre_roll()

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await harness.session.wait_prepared()
            harness.bank.cancel()
            first = harness.time.now
            harness.time.advance(4.0)
            harness.session.cancel()
            assert harness.bank.facts().cancelled_at == first
            report = await harness.finished()
            assert report.cleanup == "expired"  # it settled after the original deadline
            assert harness.released()

    run(scenario())


def test_cancelling_a_session_that_never_started_needs_no_cleanup() -> None:
    async def scenario() -> None:
        async with FakeLyria() as fake:
            harness = Harness(fake)
            harness.session.cancel()
            assert await harness.session.finish() == SessionReport(None, 0, None, "clean")
            assert harness.released()

    run(scenario())


def test_cancelling_before_the_pumps_first_step_settles_cleanly() -> None:
    async def scenario() -> None:
        async with FakeLyria() as fake:
            harness = Harness(fake)
            harness.session.start()
            harness.session.cancel()  # no yield: the coroutine's finally cannot run
            assert await harness.session.finish() == SessionReport(None, 0, None, "clean")
            await harness.session.settle()
            assert not fake.requests
            assert not harness.time.delays()
            assert harness.released()

    run(scenario())


def test_finishing_a_stream_before_its_pump_runs_has_no_failed_cleanup() -> None:
    async def scenario() -> None:
        async with FakeLyria() as fake:
            harness = StreamHarness(fake)
            harness.stream.start()
            await asyncio.sleep(0)  # supervisor creates the session; pump has not run
            report = await harness.stream.finish()
            assert report.sessions_started == 1
            assert report.failed_cleanups == 0
            assert report.retained_banks == 0
            assert harness.pool.leased == 0
            assert not fake.requests

    run(scenario())


# Streams: renewal, recovery and fades.


class StreamHarness:
    def __init__(self, fake: FakeLyria, *, other_banks: int = 0) -> None:
        self.time = ManualTime()
        self.allocations = [0]

        def allocate(size: int) -> bytearray:
            self.allocations[0] += 1
            return bytearray(size)

        self.pool = PcmBankPool(clock=self.time.clock, allocate=allocate)
        self.others = [self.pool.acquire() for _ in range(other_banks)]
        self.stream = GeneratedStream(
            REQUEST,
            api_key=KEY,
            endpoint=fake.url,
            pool=self.pool,
            clock=self.time.clock,
            sleep=self.time.sleep,
            renew_after=RENEW_SECONDS,
        )

    async def renewal_due(self) -> None:
        await until(lambda: RENEW_SECONDS in self.time.delays())
        self.time.advance(RENEW_SECONDS)

    def read_frames(self, count: int) -> list[bytes]:
        frames: list[bytes] = []
        for _ in range(count):
            frame = self.stream.read_frame()
            assert frame
            frames.append(frame)
        return frames


def serves(value: int, *, then_close: int | None = None) -> Script:
    async def script(peer: Peer) -> None:
        await peer.handshake()
        await peer.pre_roll(value)
        if then_close is not None:
            await peer.websocket.close(code=then_close)

    return script


def assert_faded(levels: list[int], old: int, new: int, frames: int) -> None:
    """Each frame's gain is exact to the rounding of the two scaled samples."""
    assert len(levels) == frames
    for position, level in enumerate(levels, start=1):
        gain = position / frames
        assert abs(level - (old * (1 - gain) + new * gain)) <= 2


def test_a_renewal_crossfades_on_the_second_bank_and_retires_the_first() -> None:
    async def scenario() -> None:
        async with FakeLyria(serves(1_000), serves(3_000)) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert not stream.ready_now()
            assert await stream.wait_ready()
            assert stream.ready_now()
            assert harness.pool.leased == 1
            assert {sample_of(f) for f in harness.read_frames(10)} == {1_000}

            await harness.renewal_due()
            await until(lambda: harness.pool.leased == 2)
            # The new audio pre-rolls while the old keeps playing at full level.
            assert {sample_of(f) for f in harness.read_frames(10)} == {1_000}
            await until(lambda: stream.crossfading)

            levels = [sample_of(f) for f in harness.read_frames(CROSSFADE_FRAMES)]
            assert_faded(levels, 1_000, 3_000, CROSSFADE_FRAMES)
            assert not stream.crossfading
            assert {sample_of(f) for f in harness.read_frames(10)} == {3_000}
            await fake.peers[0].disconnected.wait()
            await until(lambda: harness.pool.leased == 1)
            assert not fake.peers[1].disconnected.is_set()

            report = await stream.finish()
            assert report.crossfades == 1
            assert report.sessions_started == 2
            assert report.failed_cleanups == 0
            assert report.retained_banks == 0
            assert harness.pool.leased == 0

    run(scenario())


def test_a_server_close_renews_at_once_and_fades_over_what_is_left() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def later(peer: Peer) -> None:
            await peer.handshake()
            await release.wait()
            await peer.pre_roll(3_000)

        async with FakeLyria(serves(1_000, then_close=1000), later) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            await until(lambda: len(fake.peers) == 2)
            harness.read_frames(PRE_ROLL_FRAMES - 50)  # 50 frames of the old audio remain
            release.set()
            await until(lambda: stream.crossfading)
            levels = [sample_of(f) for f in harness.read_frames(50)]
            assert_faded(levels, 1_000, 3_000, 50)
            assert {sample_of(f) for f in harness.read_frames(10)} == {3_000}
            report = await stream.finish()
            assert report.crossfades == 1
            assert harness.pool.leased == 0

    run(scenario())


def test_audio_that_drains_before_the_replacement_is_ready_gets_no_fade() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def late(peer: Peer) -> None:
            await peer.handshake()
            await release.wait()
            await peer.pre_roll(3_000)

        async with FakeLyria(serves(1_000, then_close=1000), late) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            await until(lambda: len(fake.peers) == 2)
            harness.read_frames(PRE_ROLL_FRAMES)
            # Silence frames, not an end: a replacement is still coming.
            assert stream.read_frame() is None
            assert stream.read_frame() is None
            release.set()
            while (frame := stream.read_frame()) is None:
                await asyncio.sleep(0)
            assert frame
            assert sample_of(frame) == 3_000
            report = await stream.finish()
            assert report.crossfades == 0
            assert harness.pool.leased == 0

    run(scenario())


def test_recovery_attempts_are_finite_and_the_buffered_audio_still_plays() -> None:
    async def scenario() -> None:
        async with FakeLyria(
            serves(1_000, then_close=1000), Refuse(503), Refuse(503), Refuse(503)
        ) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            await advancing_until(harness.time, lambda: not fake.scripts)
            assert {sample_of(f) for f in harness.read_frames(PRE_ROLL_FRAMES)} == {1_000}
            while (frame := stream.read_frame()) is None:
                await asyncio.sleep(0)
            assert frame == b""
            report = await stream.finish()
            assert report.failure == "connect"
            assert report.sessions_started == 4
            assert harness.pool.leased == 0

    run(scenario())


def test_a_rejected_start_makes_one_attempt_and_nothing_becomes_ready() -> None:
    async def scenario() -> None:
        async with FakeLyria(Refuse(403), Refuse(403)) as fake:
            harness = StreamHarness(fake)
            harness.stream.start()
            assert not await harness.stream.wait_ready()
            assert not harness.stream.ready_now()
            assert harness.stream.read_frame() == b""
            report = await harness.stream.finish()
            assert report.failure == "rejected"
            assert report.sessions_started == 1
            assert len(fake.requests) == 1
            assert harness.pool.leased == 0

    run(scenario())


@pytest.mark.parametrize("status", [408, 429, 503])
def test_a_failing_start_uses_every_attempt_then_reports_why(status: int) -> None:
    async def scenario() -> None:
        async with FakeLyria(*(Refuse(status) for _ in range(4))) as fake:
            harness = StreamHarness(fake)
            harness.stream.start()
            ready = asyncio.create_task(harness.stream.wait_ready())
            await advancing_until(harness.time, ready.done)
            assert not await ready
            report = await harness.stream.finish()
            assert report.failure == "connect"
            assert len(fake.requests) == 3
            assert harness.pool.leased == 0

    run(scenario())


@pytest.mark.parametrize("status", [408, 429])
def test_a_transient_renewal_refusal_retries_while_old_audio_plays(status: int) -> None:
    async def scenario() -> None:
        async with FakeLyria(serves(1_000), Refuse(status), serves(3_000)) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            await harness.renewal_due()
            # Wait for the retry delay, without advancing the healthy session's clock.
            await until(
                lambda: (
                    len(fake.requests) == 2
                    and 1.0 in harness.time.delays()
                    and harness.pool.leased == 1
                )
            )
            assert sample_of(harness.read_frames(1)[0]) == 1_000
            harness.time.advance(1.0)
            await until(lambda: stream.crossfading)
            harness.read_frames(CROSSFADE_FRAMES)
            assert sample_of(harness.read_frames(1)[0]) == 3_000
            report = await stream.finish()
            assert report.sessions_started == 3
            assert report.failure is None
            assert report.failed_cleanups == 0
            assert harness.pool.leased == 0

    run(scenario())


def test_no_capacity_refuses_before_allocating_or_connecting() -> None:
    async def scenario() -> None:
        async with FakeLyria() as fake:
            harness = StreamHarness(fake, other_banks=2)
            assert harness.allocations[0] == 2
            harness.stream.start()
            ready = asyncio.create_task(harness.stream.wait_ready())
            await advancing_until(harness.time, ready.done)
            assert not await ready
            report = await harness.stream.finish()
            assert report.failure == "capacity"
            assert report.sessions_started == 0
            assert harness.allocations[0] == 2
            assert fake.requests == []
            assert harness.pool.leased == 2
            for bank in harness.others:
                assert bank.release()

    run(scenario())


def test_a_renewal_with_the_other_bank_taken_plays_out_then_follows_after_a_gap() -> None:
    async def scenario() -> None:
        async with FakeLyria(serves(1_000, then_close=1000), serves(3_000)) as fake:
            harness = StreamHarness(fake, other_banks=1)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            # All three attempts are refused (their waits total three seconds), and nothing
            # is charged past the two banks while the first session's audio plays out.
            deadline = harness.time.now + 4.0
            await advancing_until(harness.time, lambda: harness.time.now >= deadline)
            assert len(fake.peers) == 1
            assert harness.pool.leased == 2
            harness.read_frames(PRE_ROLL_FRAMES)
            while (frame := stream.read_frame()) is None:
                await asyncio.sleep(0)
            assert frame
            assert sample_of(frame) == 3_000
            assert len(fake.peers) == 2
            assert harness.pool.leased == 2
            report = await stream.finish()
            assert report.crossfades == 0
            assert harness.pool.leased == 1
            assert harness.others[0].release()

    run(scenario())


@pytest.mark.parametrize("failure", ["rejected", "filtered"])
@pytest.mark.parametrize("in_backoff", [False, True])
def test_a_permanent_refusal_from_the_audible_session_ends_recovery(
    failure: str, in_backoff: bool
) -> None:
    async def scenario() -> None:
        refuse = asyncio.Event()

        async def audible(peer: Peer) -> None:
            await peer.handshake()
            await peer.pre_roll(1_000)
            await refuse.wait()
            if failure == "rejected":
                await peer.websocket.close(code=1008)
            else:
                await peer.websocket.send_str('{"filteredPrompt":{}}')

        # A retry would succeed, so it would be observable both on the wire and in audio.
        scripts: list[Script | Refuse] = [audible]
        if in_backoff:
            scripts.append(Refuse(429))
        scripts.append(serves(3_000))
        async with FakeLyria(*scripts) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            if in_backoff:
                await harness.renewal_due()
                await until(lambda: 1.0 in harness.time.delays() and harness.pool.leased == 1)
            refuse.set()
            await fake.peers[0].disconnected.wait()
            if in_backoff:
                harness.time.advance(1.0)
            heard = [sample_of(f) for f in harness.read_frames(PRE_ROLL_FRAMES)]
            while (next_frame := stream.read_frame()) is None:
                await asyncio.sleep(0)
            report = await stream.finish()
            assert len(fake.requests) == (2 if in_backoff else 1)
            assert set(heard) == {1_000}
            assert next_frame == b""
            assert report.failure == failure
            assert report.crossfades == report.failed_cleanups == report.retained_banks == 0
            assert harness.pool.leased == 0

    run(scenario())


def test_a_filtered_renewal_keeps_the_healthy_session_playing() -> None:
    async def filtered(peer: Peer) -> None:
        await peer.handshake()
        await peer.websocket.send_str('{"filteredPrompt":{"filteredReason":"x"}}')

    async def scenario() -> None:
        async with FakeLyria(serves(1_000), filtered) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            await harness.renewal_due()
            await until(lambda: len(fake.peers) == 2)
            await fake.peers[1].disconnected.wait()
            await until(lambda: harness.pool.leased == 1)
            assert {sample_of(f) for f in harness.read_frames(20)} == {1_000}
            assert not fake.peers[0].disconnected.is_set()
            report = await stream.finish()
            assert report.failure == "filtered"
            assert report.sessions_started == 2
            assert harness.pool.leased == 0

    run(scenario())


@pytest.mark.parametrize("crossfading", [False, True])
def test_a_refusal_during_an_in_flight_replacement_survives_its_handoff(crossfading: bool) -> None:
    async def scenario() -> None:
        refuse = asyncio.Event()
        prepare = asyncio.Event()
        end = asyncio.Event()

        async def old(peer: Peer) -> None:
            await peer.handshake()
            await peer.pre_roll(1_000)
            await refuse.wait()
            await peer.websocket.close(code=1008)

        async def replacement(peer: Peer) -> None:
            await peer.handshake()
            await prepare.wait()
            await peer.pre_roll(3_000)
            await end.wait()
            await peer.websocket.close(code=1000)

        async with FakeLyria(old, replacement, serves(5_000)) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            await harness.renewal_due()
            await until(lambda: len(fake.peers) == 2)
            await fake.peers[1].played.wait()
            if crossfading:
                prepare.set()
                await until(lambda: stream.crossfading)
            refuse.set()
            await fake.peers[0].disconnected.wait()
            prepare.set()
            end.set()
            # A fade already in progress plays out. A still-preparing replacement
            # is discarded when it returns, leaving all the old frames to drain.
            frames = harness.read_frames(PRE_ROLL_FRAMES)
            if crossfading:
                assert_faded(
                    [sample_of(f) for f in frames[:CROSSFADE_FRAMES]],
                    1_000,
                    3_000,
                    CROSSFADE_FRAMES,
                )
                assert sample_of(frames[-1]) == 3_000
            else:
                assert {sample_of(f) for f in frames} == {1_000}
            while (last := stream.read_frame()) is None:
                await asyncio.sleep(0)
            report = await stream.finish()
            assert last == b""
            assert report.failure == "rejected"
            assert report.sessions_started == len(fake.requests) == 2
            assert report.failed_cleanups == report.retained_banks == harness.pool.leased == 0

    run(scenario())


def test_sessions_that_keep_dying_early_end_the_stream() -> None:
    async def scenario() -> None:
        scripts = [serves(value, then_close=1000) for value in (1_000, 2_000, 3_000, 4_000)]
        async with FakeLyria(*scripts) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            while (frame := stream.read_frame()) != b"":
                if frame is None:
                    await asyncio.sleep(0)
            report = await stream.finish()
            assert report.failure == "closed"
            assert report.sessions_started == 3
            assert len(fake.requests) == 3
            assert harness.pool.leased == 0

    run(scenario())


def test_fade_out_ramps_to_silence_then_ends() -> None:
    async def scenario() -> None:
        async with FakeLyria(serves(1_000)) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            stream.fade_out()
            stream.fade_out()  # a second request doesn't restart it
            levels = [sample_of(f) for f in harness.read_frames(FADE_OUT_FRAMES)]
            assert levels == sorted(levels, reverse=True)
            assert levels[0] == 990
            assert levels[-1] == 0
            assert stream.read_frame() == b""
            await stream.wait_silent()
            await stream.finish()
            assert harness.pool.leased == 0
            await fake.peers[0].disconnected.wait()

    run(scenario())


@pytest.mark.parametrize("point", ["first_prepare", "retry_wait", "renewal_wait", "drain_wait"])
def test_cancelling_a_stream_at_each_wait_returns_every_lease(point: str) -> None:
    async def scenario() -> None:
        async def holds(peer: Peer) -> None:
            await peer.handshake()

        scripts: dict[str, list[Script | Refuse]] = {
            "first_prepare": [holds],
            "retry_wait": [Refuse(503)],
            "renewal_wait": [serves(1_000)],
            "drain_wait": [serves(1_000, then_close=1000)],
        }
        async with FakeLyria(*scripts[point]) as fake:
            others = 1 if point == "drain_wait" else 0
            harness = StreamHarness(fake, other_banks=others)
            stream = harness.stream
            stream.start()
            match point:
                case "first_prepare":
                    await until(lambda: bool(fake.peers))
                    await fake.peers[0].played.wait()
                case "retry_wait":
                    await until(lambda: 1.0 in harness.time.delays())
                case "renewal_wait":
                    assert await stream.wait_ready()
                    await until(lambda: RENEW_SECONDS in harness.time.delays())
                case _:
                    assert await stream.wait_ready()
                    # Every attempt is refused for capacity, so it waits on its own bank.
                    deadline = harness.time.now + 4.0
                    await advancing_until(harness.time, lambda: harness.time.now >= deadline)
            stream.cancel()
            stream.cancel()
            report = await stream.finish()
            assert report.failed_cleanups == 0
            assert report.retained_banks == 0
            assert harness.pool.leased == others
            assert stream.read_frame() == b""
            for peer in fake.peers:
                await peer.disconnected.wait()
            for bank in harness.others:
                assert bank.release()

    run(scenario())


def test_cancelling_during_a_renewal_retires_both_sessions() -> None:
    async def scenario() -> None:
        async def holds(peer: Peer) -> None:
            await peer.handshake()

        async with FakeLyria(serves(1_000), holds) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            await harness.renewal_due()
            await until(lambda: len(fake.peers) == 2)
            stream.cancel()
            assert stream.read_frame() == b""
            report = await stream.finish()
            assert report.failed_cleanups == 0
            assert report.retained_banks == 0
            assert harness.pool.leased == 0
            for peer in fake.peers:
                await peer.disconnected.wait()
            assert await stream.finish() == report

    run(scenario())


def test_cancelling_during_a_crossfade_silences_and_retires_both() -> None:
    async def scenario() -> None:
        async with FakeLyria(serves(1_000), serves(3_000)) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            await harness.renewal_due()
            await until(lambda: stream.crossfading)
            harness.read_frames(5)
            stream.cancel()
            assert stream.read_frame() == b""
            report = await stream.finish()
            assert report.crossfades == 1
            assert harness.pool.leased == 0

    run(scenario())


def test_a_stream_keeps_a_stuck_session_charged_until_it_settles(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)

    async def scenario() -> None:
        async with StuckClose(monkeypatch) as stuck, FakeLyria(serves(1_000)) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            finishing = asyncio.create_task(stream.finish())
            await stuck.entered.wait()
            await until(lambda: 2.0 in harness.time.delays())
            # A repeat one second in must not move the deadline, which is two seconds away.
            harness.time.advance(1.0)
            stream.cancel()
            for _ in range(20):
                await asyncio.sleep(0)
            harness.time.advance(1.0)
            await until(lambda: 1.0 in harness.time.delays())
            assert stuck.cancellations == 0  # repeated cancellation abandons no close
            assert "missed its cleanup deadline" not in caplog.text
            harness.time.advance(1.0)
            await until(lambda: "missed its cleanup deadline" in caplog.text)
            for _ in range(20):
                await asyncio.sleep(0)
            assert not finishing.done()
            assert harness.pool.leased == 1  # the debt stays charged
            stream.cancel()  # a repeat after expiry frees nothing either
            assert harness.pool.leased == 1
            stuck.release.set()
            report = await finishing
            assert report.failed_cleanups == 1
            assert report.retained_banks == 0
            assert harness.pool.leased == 0

    run(scenario())


def test_a_bank_that_cannot_release_stays_as_debt_until_it_can() -> None:
    async def scenario() -> None:
        async with FakeLyria(serves(1_000)) as fake:
            harness = StreamHarness(fake)
            stream = harness.stream
            stream.start()
            assert await stream.wait_ready()
            # Something still borrows the bank's memory when the stream tries to release.
            bank = feed_bank(stream)
            borrowed = bank.reserve_write(FRAME_BYTES)
            report = await stream.finish()
            assert report.retained_banks == 1
            assert harness.pool.leased == 1
            assert not stream.release_debt()
            assert borrowed is not None
            bank.commit_write(0)
            assert stream.release_debt()
            assert harness.pool.leased == 0

    run(scenario())


def feed_bank(stream: GeneratedStream) -> PcmBank:
    """The one bank a single-session stream holds; a test peeks at its owner."""
    feeds = cast(set[Any], getattr(stream, "_feeds"))  # noqa: B009
    return cast(PcmBank, next(iter(feeds)).bank)


# Bounds on what is actually allocated.


def test_the_receive_socket_is_bounded_before_it_connects() -> None:
    sock = receive_bounded_socket((socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)))
    with sock:
        # Linux doubles the requested size for bookkeeping.
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) == 2 * RECEIVE_BUFFER_BYTES


def _accept_key(request: bytes) -> str:
    key = next(
        line.split(b":", 1)[1].strip()
        for line in request.split(b"\r\n")
        if line.lower().startswith(b"sec-websocket-key:")
    )
    digest = hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11", usedforsecurity=False)
    return base64.b64encode(digest.digest()).decode()


class RawSender:
    """A thread writing prebuilt frames, so its copies never enter the traced peak."""

    def __init__(self, frames: list[bytes]) -> None:
        self.frames = frames
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"ws://127.0.0.1:{self.listener.getsockname()[1]}/"
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        connection, _ = self.listener.accept()
        with connection:
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(4_096)
            connection.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
                + _accept_key(request).encode()
                + b"\r\n\r\n"
            )
            connection.recv(4_096)  # the setup message
            hello = b'{"setupComplete":{}}'
            connection.sendall(frame_header(len(hello)) + hello)
            try:
                for frame in self.frames:
                    connection.sendall(frame)
                while connection.recv(4_096):
                    pass
            except OSError:
                pass

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=5)


# The network slot, plus the plain-socket part of the read slot, plus decoded slices.
PLAIN_SOCKET_PEAK_LIMIT = 1_048_576 + 2 * 65_536 + 49_152 + 65_536
# Fragment assembly and a many-read fold each use this one synchronous reservation.
SHARED_DRAIN_BYTES = 524_288


@pytest.mark.parametrize("fragmented", [False, True])
@pytest.mark.parametrize("at_limit", [False, True])
def test_a_full_bank_holds_one_message_and_copies_stay_inside_the_budget(
    monkeypatch: pytest.MonkeyPatch,
    fragmented: bool,
    at_limit: bool,
) -> None:
    """Without the pause between messages, two more would assemble here and this fails."""
    monkeypatch.setattr("sobafm.lyria.STALL_SECONDS", 1e9)

    async def scenario() -> None:
        encoded = audio_message(11).encode()
        if at_limit:
            encoded += b" " * (MAX_MESSAGE_BYTES - 1 - len(encoded))
        frame = (
            frame_header(1, fin=False)
            + encoded[:1]
            + frame_header(len(encoded) - 1, opcode=0)
            + encoded[1:]
            if fragmented
            else frame_header(len(encoded)) + encoded
        )
        sender = RawSender([frame] * 14)
        time = ManualTime()
        pool = PcmBankPool(clock=time.clock)
        bank = pool.acquire()
        session = LyriaSession(
            REQUEST, bank, api_key=KEY, endpoint=sender.url, clock=time.clock, sleep=time.sleep
        )
        session.start()
        await session.wait_prepared()
        tracemalloc.start()
        try:
            baseline, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            await until(lambda: 0.0 < min(time.delays(), default=9.0) < 3.0)
            # Plenty of chances for aiohttp to assemble more while the bank is full.
            for _ in range(2_000):
                await asyncio.sleep(0)
            while session.pcm_bytes < 14 * MESSAGE_PCM:
                while bank.read_frame():
                    pass  # not kept: frames are the callback's copy, not this budget's
                time.advance(0.02)
                await asyncio.sleep(0)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            session.cancel()
            await session.finish()
            await session.settle()
            drain(bank)
            assert bank.release()
            sender.close()
            assert not sender.thread.is_alive()
        limit = PLAIN_SOCKET_PEAK_LIMIT + (SHARED_DRAIN_BYTES if fragmented else 0)
        assert peak - baseline <= limit

    run(scenario())


def test_one_frame_in_many_socket_reads_fits_the_shared_assembly_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        encoded = audio_message(11).encode()
        frame = frame_header(len(encoded)) + encoded
        begin = asyncio.Event()
        received = asyncio.Event()
        reads = 0
        received_bytes = 0
        original = ResponseHandler.data_received

        def observe(protocol: ResponseHandler, data: bytes) -> None:
            nonlocal reads, received_bytes
            original(protocol, data)
            if begin.is_set():
                reads += 1
                received_bytes += len(data)
                received.set()

        monkeypatch.setattr(ResponseHandler, "data_received", observe)

        async def script(peer: Peer) -> None:
            await peer.handshake()
            await begin.wait()
            # Force aiohttp to fold >2,048 pieces of one frame. Write only after
            # the client has processed every byte of the preceding piece.
            for offset in range(0, len(frame), 248):
                piece = frame[offset : offset + 248]
                peer.write_raw(piece)
                while received_bytes < offset + len(piece):
                    received.clear()
                    await received.wait()

        async with FakeLyria(script) as fake:
            harness = Harness(fake)
            harness.session.start()
            await until(lambda: bool(fake.peers))
            await fake.peers[0].played.wait()
            tracemalloc.start()
            try:
                baseline, _ = tracemalloc.get_traced_memory()
                tracemalloc.reset_peak()
                begin.set()
                await until(lambda: harness.session.pcm_bytes == MESSAGE_PCM)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
                harness.session.cancel()
                assert (await harness.finished()).cleanup == "clean"
                assert harness.released()
            assert reads > 2_048
            assert peak - baseline <= PLAIN_SOCKET_PEAK_LIMIT + SHARED_DRAIN_BYTES

    run(scenario())


@pytest.mark.parametrize(
    ("stop", "failure"), [("cancel", None), ("malformed", "malformed"), ("empty_close", "closed")]
)
def test_stopping_drops_the_socket_without_reading_more_audio(
    stop: str,
    failure: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel a full bank, or fail validation, while a real peer keeps sending."""
    monkeypatch.setattr("sobafm.lyria.STALL_SECONDS", 1e9)

    async def scenario() -> None:
        encoded = audio_message(11).encode()
        frame = frame_header(len(encoded)) + encoded
        frames = [frame] * 14
        if stop == "malformed":
            frames.insert(0, frame_header(1) + b"!")
        elif stop == "empty_close":
            frames.insert(0, frame_header(0, opcode=8))
        sender = RawSender(frames)
        time = ManualTime()
        pool = PcmBankPool(clock=time.clock)
        bank = pool.acquire()
        session = LyriaSession(
            REQUEST, bank, api_key=KEY, endpoint=sender.url, clock=time.clock, sleep=time.sleep
        )
        tracemalloc.start()
        try:
            baseline, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            session.start()
            if stop != "cancel":
                await session.wait_stopped()
            else:
                await until(lambda: 0.0 < min(time.delays(), default=9.0) < 3.0)
                session.cancel()
            # Settlement must not depend on finish(), a close reply, or advancing time.
            await asyncio.wait_for(session.settle(), timeout=2)
            _, peak = tracemalloc.get_traced_memory()
            assert peak - baseline <= PLAIN_SOCKET_PEAK_LIMIT
            assert (await session.finish()).cleanup == "clean"
            assert session.failure == failure
        finally:
            tracemalloc.stop()
            session.cancel()
            time.advance(2)
            await session.finish()
            await session.settle()
            assert bank.release()
            sender.close()
            assert not sender.thread.is_alive()

    run(scenario())


@pytest.fixture
def tls_pair(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """Server and client contexts from a one-day certificate made for this test."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("no openssl to make a loopback certificate")
    certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-days", "1", "-subj", "/CN=sobafm-test",
            "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(certificate),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate, key)
    client = ssl.create_default_context(cafile=certificate)
    return server, client


def test_over_tls_reading_is_paced_and_every_message_arrives(
    tls_pair: tuple[ssl.SSLContext, ssl.SSLContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sobafm.lyria.STALL_SECONDS", 1e9)
    limits: list[int] = []
    transport_type = cast(Any, sslproto)._SSLProtocolTransport
    original = transport_type.set_read_buffer_limits

    def recording(transport: object, high: int | None = None, low: int | None = None) -> None:
        if high is not None:
            limits.append(high)
        original(transport, high, low)

    monkeypatch.setattr(transport_type, "set_read_buffer_limits", recording)
    server, client = tls_pair

    async def scenario() -> None:
        total = 14

        async def script(peer: Peer) -> None:
            await peer.handshake()
            for index in range(total):
                await peer.websocket.send_str(audio_message(index + 1))
            await peer.websocket.close(code=1000)

        async with FakeLyria(script, ssl_context=server) as fake:
            time = ManualTime()
            pool = PcmBankPool(clock=time.clock)
            bank = pool.acquire()
            session = LyriaSession(
                REQUEST,
                bank,
                api_key=KEY,
                endpoint=fake.url,
                ssl_context=client,
                clock=time.clock,
                sleep=time.sleep,
            )
            session.start()
            await until(lambda: 0.0 < min(time.delays(), default=9.0) < 3.0)
            assert limits == [TLS_READ_HIGH_WATER_BYTES]
            samples: list[int] = []
            while not (session.stopped and bank.facts().audible_drain):
                for frame in drain(bank):
                    samples.append(sample_of(frame))
                time.advance(0.02)
                await asyncio.sleep(0)
            assert session.failure is None
            assert len(samples) == total * MESSAGE_PCM // FRAME_BYTES
            assert samples == sorted(samples)
            report = await session.finish()
            await session.settle()
            assert report.cleanup == "clean"
            assert bank.release()

    run(scenario())
