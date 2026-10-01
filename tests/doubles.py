"""Test doubles for Lyria RealTime sessions and the clock."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from google.genai import errors, types

from sobafm.pcm import BYTES_PER_SECOND


class FakeSession:
    """Stands in for the Google Gen AI SDK's live music session."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prompts: list[types.WeightedPrompt] = []
        self.config: types.LiveMusicGenerationConfig | None = None
        self.closed = False
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
        self.calls.append("play")

    async def pause(self) -> None:
        self.calls.append("pause")

    async def receive(self) -> AsyncGenerator[types.LiveMusicServerMessage]:
        while True:
            message = await self._messages.get()
            if isinstance(message, BaseException):
                raise message
            yield message

    def send_audio(self, seconds: float = 0.0, *, size: int | None = None) -> None:
        data = bytes(size if size is not None else round(seconds * BYTES_PER_SECOND))
        content = types.LiveMusicServerContent(audio_chunks=[types.AudioChunk(data=data)])
        self._messages.put_nowait(types.LiveMusicServerMessage(server_content=content))

    def refuse(self, reason: str = "We couldn't create what you asked for.") -> None:
        refusal = types.LiveMusicFilteredPrompt(text="prompt", filtered_reason=reason)
        self._messages.put_nowait(types.LiveMusicServerMessage(filtered_prompt=refusal))

    def close(
        self, code: int = 1011, reason: str = "The service is currently unavailable."
    ) -> None:
        """End the session the way the SDK reports a WebSocket close."""
        self._messages.put_nowait(errors.APIError(code, reason))


class FakeLyria:
    """Opens fake sessions, or fails to connect when `failure` is set."""

    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.failure: BaseException | None = None

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[FakeSession]:
        if self.failure is not None:
            raise self.failure
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
