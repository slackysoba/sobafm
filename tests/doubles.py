"""Test doubles for Lyria RealTime sessions, Gemini, the voice player, the clock, and servers."""

import asyncio
import itertools
import json
import threading
import weakref
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal

import discord
from google.genai import errors, types

from sobafm.audio_budget import AudioBytes, AudioLease, AudioLedger
from sobafm.catalog import CatalogPage, CatalogQuery, CatalogTrack
from sobafm.lyria_engine import SessionPool
from sobafm.mixer import Mixer
from sobafm.pcm import BYTES_PER_SECOND, FRAME_SECONDS, MIME_TYPE
from sobafm.plan import MusicPlan
from sobafm.program import Mode, Outcome, Program
from sobafm.source import Source, State


class FakeSource:
    """A buffered producer with explicit verified completion and delayed cleanup controls."""

    _numbers = itertools.count(1)

    def __init__(self, program: Program) -> None:
        self.program = program
        self.mode = program.mode
        self.title = program.plan.title
        self.number = next(self._numbers)
        self.frames: deque[bytes] = deque()
        self.state = State.GENERATING
        self.completed = False
        self.failure: Outcome | None = None
        self.retired = False
        self.hold_cleanup = False
        self._ended = asyncio.Event()

    @property
    def buffered_seconds(self) -> float:
        return len(self.frames) * FRAME_SECONDS

    @property
    def ready(self) -> bool:
        return self.buffered_seconds >= 6 or (self.completed and bool(self.frames))

    @property
    def failed(self) -> bool:
        return self.failure is not None

    def start(self) -> None:
        pass

    def send_audio(self, seconds: float, *, sample: int = 0) -> None:
        frame = sample.to_bytes(2, "little", signed=True) * 1920
        self.frames.extend(frame for _ in range(round(seconds / FRAME_SECONDS)))

    def finish(self, failure: Outcome | None = None) -> None:
        self.completed = failure is None
        self.failure = failure
        self.release_cleanup()

    def retire(self) -> None:
        self.retired = True
        if not self.hold_cleanup:
            self.release_cleanup()

    def release_cleanup(self) -> None:
        self.state = State.ENDED
        self._ended.set()

    async def wait_ended(self) -> None:
        await self._ended.wait()

    async def regulate(self) -> None:
        pass


class FakeEngine:
    """Controllable engine; no provider policy is needed to test station coordination.

    Its synthetic complete-envelope leases are test assumptions, never actual
    Lyria/native qualification. The production Lyria engine remains unqualified.
    """

    def __init__(self, mode: Mode, audio: AudioLedger, pool: SessionPool | None = None) -> None:
        self.mode = mode
        self.audio = audio
        self.pool = pool
        self.sources: list[FakeSource] = []
        self.live_numbers: list[int] = []
        self._seen: weakref.WeakSet[FakeSource] = weakref.WeakSet()
        self._lease: AudioLease | None = None
        self.close_calls = 0

    def reserve(self, program: Program) -> bool:
        if self.pool is not None and self.mode is Mode.GENERATED and not self.pool.reserve(self):
            return False
        if self._lease is None:
            self._lease = self.audio.reserve(AudioBytes(pcm=12_000_000, other=512_000))
        return self._lease is not None

    def release_if_ended(self, retained: Sequence[Program]) -> None:
        if any(p.mode is self.mode for p in retained):
            return
        if all(s.state is State.ENDED for s in self.sources) and self.pool is not None:
            self.pool.release(self)
        if not self.sources and self._lease is not None:
            self._lease.release()
            self._lease = None

    def generate(self, program: Program) -> None:
        for _ in range(2 - sum(s.state is not State.ENDED for s in self.sources)):
            self.sources.append(FakeSource(program))

    def ended_failures(self, program: Program | None) -> list[FakeSource]:
        ended = [s for s in self.sources if s.state is State.ENDED and s not in self._seen]
        self._seen.update(ended)
        return [s for s in ended if s.failure is not None and s.program is program]

    @staticmethod
    def terminal(program: Program | None, connected: bool) -> Outcome | None:
        return None

    def count_failure(
        self, source: Source, program: Program | None, connected: bool
    ) -> Outcome | None:
        if (
            program is not None
            and isinstance(source, FakeSource)
            and source.program is program
            and not program.playing
        ):
            return source.failure
        return None

    def drop_ended(self, program: Program | None, mixer: Mixer) -> None:
        self.sources = [
            source
            for source in self.sources
            if source.state is not State.ENDED
            or mixer.uses(source)
            or (source.program is program and source.ready and source.failure is None)
        ]

    def retire(self, program: Program | None, mixer: Mixer) -> None:
        for source in self.sources:
            if source.program is not program and not mixer.uses(source):
                source.retire()

    def ready_source(self, program: Program, exclude: Source | None = None) -> FakeSource | None:
        ready = [s for s in self.sources if s.program is program and s.ready and s is not exclude]
        if self.mode is Mode.CATALOG:
            return next(iter(ready), None)
        return max(ready, key=lambda s: s.buffered_seconds, default=None)

    def handover_due(self, source: Source) -> bool:
        if self.mode is Mode.GENERATED:
            return source.buffered_seconds < 4
        return isinstance(source, FakeSource) and (
            source.completed or (source.failure is not None and not source.frames)
        )

    def going_live(self, source: Source) -> None:
        self.live_numbers.append(source.number)

    async def close(self) -> None:
        self.close_calls += 1
        for source in self.sources:
            source.retire()
        await asyncio.gather(*(s.wait_ended() for s in self.sources))
        self.sources.clear()
        if self.pool is not None:
            self.pool.release(self)
        if self._lease is not None:
            self._lease.release()
            self._lease = None


class FakeCatalogCycle:
    def __init__(self, results: list[tuple[CatalogTrack, ...] | CatalogPage | Exception]) -> None:
        self.results = results
        self.closed = False

    async def next_page(self) -> CatalogPage:
        if self.closed:
            raise RuntimeError("closed fixture cycle")
        result = self.results.pop(0) if self.results else ()
        if isinstance(result, Exception):
            raise result
        page = (
            result
            if isinstance(result, CatalogPage)
            else CatalogPage(tracks=result, received=len(result), exhausted=True)
        )
        self.closed = page.exhausted
        return page

    def close(self) -> None:
        self.closed = True


class FakeCatalog:
    """Scripted catalog discovery; records validated queries without provider I/O."""

    def __init__(self, *results: tuple[CatalogTrack, ...] | CatalogPage | Exception) -> None:
        self.results = list(results)
        self.queries: list[CatalogQuery] = []

    def open(self, query: CatalogQuery) -> FakeCatalogCycle:
        self.queries.append(query)
        return FakeCatalogCycle(self.results)

    async def search(self, query: CatalogQuery) -> tuple[CatalogTrack, ...]:
        self.queries.append(query)
        result = self.results.pop(0) if self.results else ()
        if isinstance(result, Exception):
            raise result
        return result.tracks if isinstance(result, CatalogPage) else result


class FakeSession:
    """Stands in for the Google Gen AI SDK's live music session."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prompts: list[types.WeightedPrompt] = []
        self.config: types.LiveMusicGenerationConfig | None = None
        self.closed = False
        self.ending = False  # once Lyria has ended the session, sends fail
        self.stall = False  # sends never finish, as on a socket whose close hangs
        self._messages: asyncio.Queue[types.LiveMusicServerMessage | BaseException] = (
            asyncio.Queue()
        )

    async def set_weighted_prompts(self, prompts: list[types.WeightedPrompt]) -> None:
        self.calls.append("prompts")
        self.prompts = prompts

    async def set_music_generation_config(self, config: types.LiveMusicGenerationConfig) -> None:
        self.calls.append("config")
        self.config = config

    async def play(self) -> None:
        await self._check_open()
        self.calls.append("play")

    async def pause(self) -> None:
        await self._check_open()
        self.calls.append("pause")

    async def _check_open(self) -> None:
        if self.ending or self.closed:
            raise ConnectionError("the session is closing")
        if self.stall:
            await asyncio.Event().wait()

    async def receive(self) -> AsyncGenerator[types.LiveMusicServerMessage]:
        while True:
            message = await self._messages.get()
            if isinstance(message, BaseException):
                # The SDK's session keeps no reference to the error it raises; one here would
                # make a cycle through the traceback that keeps a dropped deck (#89).
                error, message = message, None
                try:
                    raise error
                finally:
                    del error
            yield message

    def send_audio(
        self, seconds: float = 0.0, *, data: bytes | None = None, mime_type: str = MIME_TYPE
    ) -> None:
        if data is None:
            data = bytes(round(seconds * BYTES_PER_SECOND))
        chunk = types.AudioChunk(data=data, mime_type=mime_type)
        content = types.LiveMusicServerContent(audio_chunks=[chunk])
        self._messages.put_nowait(types.LiveMusicServerMessage(server_content=content))

    def refuse(self, reason: str = "We couldn't create what you asked for.") -> None:
        refusal = types.LiveMusicFilteredPrompt(text="prompt", filtered_reason=reason)
        self._messages.put_nowait(types.LiveMusicServerMessage(filtered_prompt=refusal))

    def close(
        self, code: int = 1011, reason: str = "The service is currently unavailable."
    ) -> None:
        """End the session the way the SDK reports a WebSocket close."""
        self.fail(errors.APIError(code, reason))

    def fail(self, error: BaseException) -> None:
        self.ending = True
        self._messages.put_nowait(error)


class FakeLyria:
    """Opens fake sessions, or fails to connect when `failure` is set."""

    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.failure: BaseException | None = None
        self.stall = False  # connecting never finishes
        self.stall_sends = False  # each session's play() and pause() never finish
        self.closing: Callable[[], Awaitable[None]] | None = None  # runs as a session closes

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[FakeSession]:
        if self.failure is not None:
            raise self.failure
        if self.stall:
            await asyncio.Event().wait()
        session = FakeSession()
        session.stall = self.stall_sends
        self.sessions.append(session)
        try:
            yield session
        finally:
            if self.closing is not None:
                await self.closing()
            session.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def settle() -> None:
    """Let pending tasks run until they block."""
    for _ in range(20):
        await asyncio.sleep(0)


async def serve(reply: bytes, seen: list[bytes]) -> tuple[asyncio.Server, int]:
    """Start a local server on a free port, returning it and the port.

    It records each request's head in `seen` and answers it with `reply`.
    """

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(reply)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


class FakePlayer:
    """Stands in for discord.py's voice client, with its checks on `play()`.

    As in discord.py, `is_playing()` stays true when the player thread ends on its own, and a
    stopped thread calls `after` only once it notices, which a voice reconnect can delay:
    `end_stopped_threads()` lets it.
    """

    def __init__(self) -> None:
        self.playing = False  # what is_playing() reports
        self.running = False  # whether the player thread runs, reading the source
        self.connected = True
        self.failure: Exception | None = None  # raised by play(), such as OpusNotLoaded
        self.plays = 0
        self.source: discord.AudioSource | None = None
        self._after: Callable[[Exception | None], Any] | None = None
        self._stopped: list[Callable[[Exception | None], Any]] = []  # threads still ending

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self.playing

    def play(
        self,
        source: discord.AudioSource,
        *,
        after: Callable[[Exception | None], Any] | None = None,
        signal_type: Literal["auto", "voice", "music"] = "auto",
    ) -> None:
        if self.failure is not None:
            raise self.failure
        if not self.connected:
            raise discord.ClientException("Not connected to voice.")
        if self.playing:
            raise discord.ClientException("Already playing audio.")
        self.playing = self.running = True
        self.plays += 1
        self.source = source
        self._after = after

    def stop(self) -> None:
        self.playing = self.running = False
        if self._after is not None:
            self._stopped.append(self._after)
            self._after = None

    def end_thread(self) -> None:
        """End the running player thread on its own, as when it gives up on a reconnect."""
        self.running = False
        if (after := self._after) is not None:
            self._after = None
            _call_from_thread(after)

    def end_stopped_threads(self) -> None:
        for after in self._stopped:
            _call_from_thread(after)
        self._stopped.clear()


def _call_from_thread(after: Callable[[Exception | None], Any]) -> None:
    """Call `after` from another thread, as discord.py's player thread does."""
    thread = threading.Thread(target=after, args=(None,))
    thread.start()
    thread.join()


def answer(
    text: str, finish: types.FinishReason = types.FinishReason.STOP
) -> types.GenerateContentResponse:
    """Gemini's response with `text` as its answer."""
    content = types.Content(role="model", parts=[types.Part(text=text)])
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=content, finish_reason=finish)]
    )


class FakeGemini:
    """Answers `models.generate_content` like the Google Gen AI SDK's async client.

    Without a `response`, it reads each request as new music described by the request itself.
    """

    def __init__(self, response: types.GenerateContentResponse | None = None) -> None:
        self.models = self
        self.response = response
        self.error: Exception | None = None
        self.held: asyncio.Event | None = None  # a call made while this holds an event waits for it
        self.calls: list[dict[str, Any]] = []

    async def generate_content(
        self, *, model: str, contents: str, config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        response, error, held = self.response, self.error, self.held  # as set when it's made
        if held is not None:
            await held.wait()
        if error is not None:
            raise error
        if response is not None:
            return response
        plan = MusicPlan.from_request(json.loads(contents)["request"])
        return answer(json.dumps({"kind": "new", "plan": plan.model_dump(mode="json")}))
