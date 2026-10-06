# /// script
# requires-python = ">=3.14"
# dependencies = ["google-genai>=2.26", "websockets>=16.1.1"]
# ///
"""Measure Lyria RealTime session behavior for the playback engine decision (#11).

Each command opens one or more sessions, plays a fixed prompt with the default generation
config, and prints one JSON object per session: the SDK version, connection time, time to
first audio, generation rate, chunk sizes and MIME types, other server messages, and how
the session ended. No audio is saved. The API key is read from GEMINI_API_KEY and passed
to the SDK explicitly, so GOOGLE_API_KEY is ignored.

    uv run --env-file .env scripts/lyria_probe.py rate --sessions 2 --seconds 120
    uv run --env-file .env scripts/lyria_probe.py lifetime --max-minutes 15
    uv run --env-file .env scripts/lyria_probe.py pauses --pauses 30 120 300
    uv run --env-file .env scripts/lyria_probe.py concurrency --max-sessions 8 --spacing 45

Rates count whole 2-second chunks, so a rate over one minute is accurate to about 0.03.
The SDK drops message fields it does not know, so such messages are reported as empty.
"""

import argparse
import asyncio
import contextlib
import itertools
import json
import os
import sys
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import websockets.asyncio.client
from google import genai
from google.genai import errors, live_music, types

MODEL = "models/lyria-realtime-exp"
BYTES_PER_SECOND = 48_000 * 2 * 2  # 48 kHz, 16-bit, stereo
FRAME_BYTES = 3_840  # 20 ms
DEFAULT_PROMPT = "mellow lo-fi hip hop, soft piano, warm bass, gentle drums"
STALL_S = 30.0  # how long audio may lag real time before a session counts as stalled
CLOSED_BY_PROBE = "closed by probe"


@dataclass
class SessionStats:
    """Timing and outcome of one Lyria RealTime session."""

    label: str
    api_version: str
    continuous: bool = True  # False when the probe pauses playback, which voids the rates
    started_at: float = field(default_factory=time.monotonic)
    connected_at: float | None = None
    played_at: float | None = None
    first_audio_s: float | None = None
    ended_at: float | None = None
    end: str | None = None
    audio_bytes: int = 0
    chunk_sizes: list[int] = field(default_factory=list[int])
    arrivals: list[float] = field(default_factory=list[float])
    mime_types: set[str] = field(default_factory=set[str])
    filtered: list[str] = field(default_factory=list[str])
    other_messages: list[tuple[float, dict[str, Any]]] = field(
        default_factory=list[tuple[float, dict[str, Any]]]
    )
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def playing(self) -> bool:
        """Whether the session has produced audio and is still open."""
        return bool(self.chunk_sizes) and self.end is None

    def add_chunk(self, chunk: types.AudioChunk, now: float) -> None:
        data = chunk.data or b""
        if self.first_audio_s is None and self.played_at is not None:
            self.first_audio_s = now - self.played_at
        self.audio_bytes += len(data)
        self.chunk_sizes.append(len(data))
        self.arrivals.append(now)
        self.mime_types.add(chunk.mime_type or "none")

    def fail(self, error: Exception) -> None:
        """Record an error raised on the probe's side, unless the receiver saw the real end."""
        if self.end in (None, CLOSED_BY_PROBE):
            self.end = describe(error)

    def audio_seconds_between(self, start: float, end: float) -> float:
        received = sum(
            size
            for size, at in zip(self.chunk_sizes, self.arrivals, strict=True)
            if start <= at < end
        )
        return received / BYTES_PER_SECOND

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "label": self.label,
            "api_version": self.api_version,
            "connect_s": _elapsed(self.started_at, self.connected_at),
            "first_audio_s": _round(self.first_audio_s),
            "open_s": _elapsed(self.connected_at, self.ended_at),
            "end": self.end,
            "chunks": len(self.chunk_sizes),
            "audio_s": round(self.audio_bytes / BYTES_PER_SECOND, 1),
            "chunk_durations_s": sorted({round(n / BYTES_PER_SECOND, 3) for n in self.chunk_sizes}),
            "frame_aligned": (
                all(n % FRAME_BYTES == 0 for n in self.chunk_sizes) if self.chunk_sizes else None
            ),
            "mime_types": sorted(self.mime_types),
            "other_messages": len(self.other_messages),
        }
        if self.other_messages:
            result["first_other_messages"] = [
                {"after_s": _elapsed(self.connected_at, at), "message": message}
                for at, message in self.other_messages[:10]
            ]
        if self.continuous and len(self.arrivals) > 1:
            span = self.arrivals[-1] - self.arrivals[0]
            steady_audio = (self.audio_bytes - self.chunk_sizes[0]) / BYTES_PER_SECOND
            gaps = [b - a for a, b in itertools.pairwise(self.arrivals)]
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


def _elapsed(start: float | None, end: float | None) -> float | None:
    return None if start is None or end is None else round(end - start, 2)


def describe(error: BaseException) -> str:
    if isinstance(error, errors.APIError):
        # HTTP errors fill `message`; a WebSocket close puts its reason in the untyped `details`.
        details: object = getattr(error, "details", None)
        reason = error.message or error.status or details or "no reason given"
        return f"{type(error).__name__} code={error.code}: {reason}"[:300]
    return f"{type(error).__name__}: {error}"[:300]


async def receive(session: live_music.AsyncMusicSession, stats: SessionStats) -> None:
    """Record server messages until the session ends; the end reason goes into stats."""
    try:
        async for message in session.receive():
            now = time.monotonic()
            content = message.server_content
            chunks = (content.audio_chunks if content else None) or []
            for chunk in chunks:
                stats.add_chunk(chunk, now)
            if prompt := message.filtered_prompt:
                stats.filtered.append(f"{prompt.text!r}: {prompt.filtered_reason}")
            elif not chunks:
                fields = message.model_dump(mode="json", exclude_none=True)
                stats.other_messages.append((now, fields))
        stats.end = "stream ended"
    except asyncio.CancelledError:
        stats.end = CLOSED_BY_PROBE
        raise
    except Exception as error:  # noqa: BLE001 - every end reason is a measurement
        stats.end = describe(error)
    finally:
        stats.ended_at = time.monotonic()


@asynccontextmanager
async def playing_session(
    client: genai.Client, stats: SessionStats, prompt: str
) -> AsyncGenerator[tuple[live_music.AsyncMusicSession, asyncio.Task[None]]]:
    """Connect, send the prompt and the default config, start playback, and record messages."""
    async with client.aio.live.music.connect(model=MODEL) as session:
        stats.connected_at = time.monotonic()
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
        stats.fail(error)


async def wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    """Poll `condition` for up to `seconds`, and return its final value."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            while not condition():  # noqa: ASYNC110 - polls the probe's own counters
                await asyncio.sleep(0.05)
    return condition()


async def wait_for_audio(stats: SessionStats, receiver: asyncio.Task[None], seconds: float) -> bool:
    """Wait for `seconds` more audio; False if the session ends or stalls first."""
    target = stats.audio_bytes + seconds * BYTES_PER_SECOND
    await wait_until(lambda: stats.audio_bytes >= target or receiver.done(), seconds + STALL_S)
    return stats.audio_bytes >= target


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
    stats = SessionStats("pauses", args.api_version, continuous=False)
    results: list[dict[str, Any]] = []
    stats.extra["pauses"] = results
    try:
        async with playing_session(client, stats, args.prompt) as (session, receiver):
            played = await wait_for_audio(stats, receiver, args.play_seconds)
            for pause_s in args.pauses:
                if not played:
                    break
                await session.pause()
                chunks_before = len(stats.chunk_sizes)
                await asyncio.wait({receiver}, timeout=pause_s)
                result: dict[str, Any] = {
                    "pause_s": pause_s,
                    "chunks_during_pause": len(stats.chunk_sizes) - chunks_before,
                    "survived": not receiver.done(),
                }
                results.append(result)
                if receiver.done():
                    break
                resumed_at = time.monotonic()
                chunks_at_resume = len(stats.chunk_sizes)
                await session.play()
                await wait_until(
                    lambda n=chunks_at_resume: len(stats.chunk_sizes) > n or receiver.done(),
                    STALL_S,
                )
                if len(stats.chunk_sizes) > chunks_at_resume:
                    result["resume_latency_s"] = round(
                        stats.arrivals[chunks_at_resume] - resumed_at, 2
                    )
                played = await wait_for_audio(stats, receiver, args.play_seconds)
    except Exception as error:  # noqa: BLE001
        stats.fail(error)
    return [stats]


async def measure_concurrency(client: genai.Client, args: argparse.Namespace) -> list[SessionStats]:
    """Start sessions `spacing` seconds apart until one fails or `max_sessions` play together.

    A start fails when the new session has no audio within `audio_timeout`, or when an
    earlier session ends. All playing sessions are then held together for `hold` seconds.
    """
    stop = asyncio.Event()
    sessions: list[SessionStats] = []
    tasks: list[asyncio.Task[None]] = []

    async def hold(stats: SessionStats) -> None:
        try:
            async with playing_session(client, stats, args.prompt) as (_, receiver):
                stopper = asyncio.create_task(stop.wait())
                try:
                    await asyncio.wait({receiver, stopper}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    stopper.cancel()
        except Exception as error:  # noqa: BLE001
            stats.fail(error)

    for index in range(args.max_sessions):
        stats = SessionStats(f"concurrent-{index + 1}", args.api_version)
        sessions.append(stats)
        tasks.append(asyncio.create_task(hold(stats)))
        await wait_until(
            lambda s=stats: bool(s.chunk_sizes or s.filtered) or s.end is not None,
            args.audio_timeout,
        )
        if not all(s.playing for s in sessions):
            break
        if index + 1 < args.max_sessions:
            await asyncio.sleep(max(0.0, stats.started_at + args.spacing - time.monotonic()))
    hold_start = time.monotonic()
    await asyncio.sleep(args.hold)
    hold_end = time.monotonic()
    playing = [s for s in sessions if s.playing]
    stop.set()
    await asyncio.gather(*tasks)
    for stats in sessions:
        stats.extra["concurrent_peak"] = len(playing)
        if stats in playing:
            audio_s = stats.audio_seconds_between(hold_start, hold_end)
            stats.extra["rate_during_hold"] = round(audio_s / (hold_end - hold_start), 3)
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
    concurrency.add_argument("--max-sessions", type=int, default=8)
    concurrency.add_argument(
        "--spacing", type=float, default=45, help="seconds between session starts"
    )
    concurrency.add_argument(
        "--audio-timeout", type=float, default=STALL_S, help="seconds to wait for first audio"
    )
    concurrency.add_argument(
        "--hold", type=float, default=120, help="seconds to keep all sessions playing"
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY is not set; run with uv run --env-file .env")
    # As in SobaFM's decks, follow no redirect, which would take the key's header along (#85).
    websockets.asyncio.client.MAX_REDIRECTS = 1
    client = genai.Client(api_key=api_key, http_options={"api_version": args.api_version})
    measure = {
        "rate": measure_rate,
        "lifetime": measure_lifetime,
        "pauses": measure_pauses,
        "concurrency": measure_concurrency,
    }[args.command]
    for stats in await measure(client, args):
        record = {"command": args.command, "sdk": genai.__version__} | stats.summary()
        print(json.dumps(record), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
