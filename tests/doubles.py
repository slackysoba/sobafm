"""Test doubles for Lyria RealTime sessions, the voice player, and the clock."""

import asyncio
import threading
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

import discord
from google.genai import errors, types

from sobafm.pcm import BYTES_PER_SECOND, MIME_TYPE


class FakeSession:
    """Stands in for the Google Gen AI SDK's live music session."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prompts: list[types.WeightedPrompt] = []
        self.config: types.LiveMusicGenerationConfig | None = None
        self.closed = False
        self.ending = False  # once Lyria has ended the session, sends fail
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
        self._check_open()
        self.calls.append("play")

    async def pause(self) -> None:
        self._check_open()
        self.calls.append("pause")

    def _check_open(self) -> None:
        if self.ending or self.closed:
            raise ConnectionError("the session is closing")

    async def receive(self) -> AsyncGenerator[types.LiveMusicServerMessage]:
        while True:
            message = await self._messages.get()
            if isinstance(message, BaseException):
                raise message
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

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[FakeSession]:
        if self.failure is not None:
            raise self.failure
        if self.stall:
            await asyncio.Event().wait()
        session = FakeSession()
        self.sessions.append(session)
        try:
            yield session
        finally:
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
