# /// script
# requires-python = ">=3.14"
# dependencies = ["google-genai>=2.26"]
# ///
"""Measure Lyria RealTime session behavior for the playback engine decision (#11).

Each command opens one or more sessions, plays a fixed prompt, and prints one JSON object
per session with connection time, time to first audio, generation rate, chunk sizes, and
how the session ended. No audio is saved. The API key is read from GEMINI_API_KEY.

    uv run --env-file .env scripts/lyria_probe.py rate --sessions 2 --seconds 120
    uv run --env-file .env scripts/lyria_probe.py lifetime --max-minutes 15
    uv run --env-file .env scripts/lyria_probe.py pauses --pauses 30 120 300
    uv run --env-file .env scripts/lyria_probe.py concurrency --max-sessions 5
"""

import argparse
import asyncio
import json
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from google import genai
from google.genai import errors, live_music, types

MODEL = "models/lyria-realtime-exp"
BYTES_PER_SECOND = 48_000 * 2 * 2  # 48 kHz, 16-bit, stereo
FRAME_BYTES = 3_840  # 20 ms
DEFAULT_PROMPT = "mellow lo-fi hip hop, soft piano, warm bass, gentle drums"


@dataclass
class SessionStats:
    """Timing and outcome of one Lyria RealTime session."""

    label: str
    api_version: str
    opened_at: float = field(default_factory=time.monotonic)
    connect_s: float | None = None
    played_at: float | None = None
    first_audio_s: float | None = None
    audio_bytes: int = 0
    chunk_sizes: list[int] = field(default_factory=list[int])
    arrivals: list[float] = field(default_factory=list[float])
    filtered: list[str] = field(default_factory=list[str])
    ended_after_s: float | None = None
    end: str | None = None
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    def add_chunk(self, data: bytes, now: float) -> None:
        if self.first_audio_s is None and self.played_at is not None:
            self.first_audio_s = now - self.played_at
        self.audio_bytes += len(data)
        self.chunk_sizes.append(len(data))
        self.arrivals.append(now)

    def audio_seconds_between(self, start: float, end: float) -> float:
        received = sum(
            size
            for size, at in zip(self.chunk_sizes, self.arrivals, strict=True)
            if start <= at < end
        )
        return received / BYTES_PER_SECOND

    def summary(self) -> dict[str, Any]:
        audio_s = self.audio_bytes / BYTES_PER_SECOND
        result: dict[str, Any] = {
            "label": self.label,
            "api_version": self.api_version,
            "connect_s": _round(self.connect_s),
            "first_audio_s": _round(self.first_audio_s),
            "chunks": len(self.chunk_sizes),
            "audio_s": round(audio_s, 1),
            "chunk_durations_s": sorted({round(n / BYTES_PER_SECOND, 3) for n in self.chunk_sizes}),
            "frame_aligned": all(n % FRAME_BYTES == 0 for n in self.chunk_sizes),
            "ended_after_s": _round(self.ended_after_s),
            "end": self.end,
        }
        if len(self.arrivals) > 1:
            span = self.arrivals[-1] - self.arrivals[0]
            steady_audio = (self.audio_bytes - self.chunk_sizes[0]) / BYTES_PER_SECOND
            gaps = [b - a for a, b in zip(self.arrivals, self.arrivals[1:], strict=False)]
            result["rate"] = round(steady_audio / span, 3)
            result["chunk_interval_s"] = {
                "mean": round(sum(gaps) / len(gaps), 2),
                "min": round(min(gaps), 2),
                "max": round(max(gaps), 2),
            }
            result["rate_by_minute"] = self._rate_by_minute()
        if self.filtered:
            result["filtered"] = self.filtered
        return result | self.extra

    def _rate_by_minute(self) -> list[float]:
        start = self.arrivals[0]
        minutes = int((self.arrivals[-1] - start) // 60)
        return [
            round(self.audio_seconds_between(start + 60 * m, start + 60 * (m + 1)) / 60, 2)
            for m in range(minutes)
        ]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def describe(error: BaseException) -> str:
    if isinstance(error, errors.APIError):
        return f"{type(error).__name__} code={error.code}: {error.message or error.status}"
    return f"{type(error).__name__}: {error}"[:300]


async def receive(session: live_music.AsyncMusicSession, stats: SessionStats) -> None:
    """Record audio chunks until the session ends; the end reason goes into stats."""
    try:
        async for message in session.receive():
            now = time.monotonic()
            content = message.server_content
            for chunk in (content.audio_chunks or []) if content else []:
                stats.add_chunk(chunk.data or b"", now)
            if message.filtered_prompt:
                prompt = message.filtered_prompt
                stats.filtered.append(f"{prompt.text!r}: {prompt.filtered_reason}")
        stats.end = "stream ended"
    except asyncio.CancelledError:
        stats.end = "closed by probe"
        raise
    except Exception as error:  # noqa: BLE001 - every end reason is a measurement
        stats.end = describe(error)
    finally:
        stats.ended_after_s = time.monotonic() - stats.opened_at


@asynccontextmanager
async def playing_session(
    client: genai.Client, stats: SessionStats, prompt: str
) -> AsyncGenerator[tuple[live_music.AsyncMusicSession, asyncio.Task[None]]]:
    """Connect, send the prompt and full config, start playback, and record audio."""
    async with client.aio.live.music.connect(model=MODEL) as session:
        stats.connect_s = time.monotonic() - stats.opened_at
        receiver = asyncio.create_task(receive(session, stats))
        try:
            await session.set_weighted_prompts(
                prompts=[types.WeightedPrompt(text=prompt, weight=1.0)]
            )
            await session.set_music_generation_config(config=types.LiveMusicGenerationConfig())
            stats.played_at = time.monotonic()
            await session.play()
            yield session, receiver
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


async def run_session(
    client: genai.Client, stats: SessionStats, prompt: str, seconds: float
) -> None:
    """Play for `seconds`, or until the server ends the session."""
    try:
        async with playing_session(client, stats, prompt) as (_, receiver):
            await asyncio.wait({receiver}, timeout=seconds)
    except Exception as error:  # noqa: BLE001 - connection failures are measurements too
        stats.end = stats.end or describe(error)


async def wait_until(condition: Callable[[], bool], receiver: asyncio.Task[None]) -> None:
    """Return once `condition` holds or the session has ended."""
    while not (condition() or receiver.done()):  # noqa: ASYNC110 - polls the probe's own counters
        await asyncio.sleep(0.05)


async def wait_for_audio(stats: SessionStats, receiver: asyncio.Task[None], seconds: float) -> None:
    """Wait until `seconds` more audio has arrived or the session ends."""
    target = stats.audio_bytes + seconds * BYTES_PER_SECOND
    await wait_until(lambda: stats.audio_bytes >= target, receiver)


async def measure_rate(client: genai.Client, args: argparse.Namespace) -> list[SessionStats]:
    sessions = [
        SessionStats(f"rate-{i + 1}of{args.sessions}", args.api_version)
        for i in range(args.sessions)
    ]
    await asyncio.gather(*(run_session(client, s, args.prompt, args.seconds) for s in sessions))
    return sessions


async def measure_lifetime(client: genai.Client, args: argparse.Namespace) -> list[SessionStats]:
    stats = SessionStats("lifetime", args.api_version)
    await run_session(client, stats, args.prompt, args.max_minutes * 60)
    return [stats]


async def measure_pauses(client: genai.Client, args: argparse.Namespace) -> list[SessionStats]:
    stats = SessionStats("pauses", args.api_version)
    results: list[dict[str, Any]] = []
    stats.extra["pauses"] = results
    try:
        async with playing_session(client, stats, args.prompt) as (session, receiver):
            await wait_for_audio(stats, receiver, args.play_seconds)
            for pause_s in args.pauses:
                if receiver.done():
                    break
                await session.pause()
                chunks_before = len(stats.chunk_sizes)
                await asyncio.wait({receiver}, timeout=pause_s)
                result: dict[str, Any] = {
                    "pause_s": pause_s,
                    "chunks_during_pause": len(stats.chunk_sizes) - chunks_before,
                    "survived": not receiver.done(),
                }
                if not receiver.done():
                    resumed_at = time.monotonic()
                    chunks_at_resume = len(stats.chunk_sizes)
                    await session.play()
                    await wait_until(
                        lambda n=chunks_at_resume: len(stats.chunk_sizes) > n, receiver
                    )
                    if len(stats.chunk_sizes) > chunks_at_resume:
                        result["resume_latency_s"] = round(
                            stats.arrivals[chunks_at_resume] - resumed_at, 2
                        )
                    await wait_for_audio(stats, receiver, args.play_seconds)
                results.append(result)
    except Exception as error:  # noqa: BLE001
        stats.end = stats.end or describe(error)
    return [stats]


async def measure_concurrency(client: genai.Client, args: argparse.Namespace) -> list[SessionStats]:
    """Open sessions one at a time until one fails or `max_sessions` are playing."""
    stop = asyncio.Event()
    sessions: list[SessionStats] = []
    tasks: list[asyncio.Task[None]] = []

    async def hold(stats: SessionStats) -> None:
        try:
            async with playing_session(client, stats, args.prompt) as (_, receiver):
                stopper = asyncio.create_task(stop.wait())
                await asyncio.wait({receiver, stopper}, return_when=asyncio.FIRST_COMPLETED)
                stopper.cancel()
        except Exception as error:  # noqa: BLE001
            stats.end = stats.end or describe(error)

    for index in range(args.max_sessions):
        stats = SessionStats(f"concurrent-{index + 1}", args.api_version)
        sessions.append(stats)
        tasks.append(asyncio.create_task(hold(stats)))
        await asyncio.sleep(args.settle)
        if not stats.chunk_sizes:
            break
    await asyncio.sleep(args.hold)
    stop.set()
    await asyncio.gather(*tasks)
    for stats in sessions:
        stats.extra["concurrent_peak"] = len(sessions)
    return sessions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--api-version", default="v1beta", choices=["v1alpha", "v1beta"])
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    commands = parser.add_subparsers(dest="command", required=True)

    rate = commands.add_parser("rate", help="generation rate with one or more concurrent sessions")
    rate.add_argument("--sessions", type=int, default=1)
    rate.add_argument("--seconds", type=float, default=60)

    lifetime = commands.add_parser("lifetime", help="how long one session lasts while generating")
    lifetime.add_argument("--max-minutes", type=float, default=15)

    pauses = commands.add_parser(
        "pauses", help="whether sessions survive pauses, and resume latency"
    )
    pauses.add_argument("--pauses", type=float, nargs="+", default=[30, 120, 300])
    pauses.add_argument("--play-seconds", type=float, default=20)

    concurrency = commands.add_parser("concurrency", help="how many sessions can play at once")
    concurrency.add_argument("--max-sessions", type=int, default=5)
    concurrency.add_argument(
        "--settle", type=float, default=10, help="seconds to wait for audio before the next session"
    )
    concurrency.add_argument(
        "--hold", type=float, default=30, help="seconds to keep all sessions playing"
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    client = genai.Client(http_options={"api_version": args.api_version})
    measure = {
        "rate": measure_rate,
        "lifetime": measure_lifetime,
        "pauses": measure_pauses,
        "concurrency": measure_concurrency,
    }[args.command]
    for stats in await measure(client, args):
        print(json.dumps({"command": args.command} | stats.summary()), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
