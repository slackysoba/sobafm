"""One explicitly authorized Lyria session; no provider activity by default."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import signal
import socket
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FrameType
from typing import Any

from sobafm.crash_dumps import disable_crash_dumps

# Apply these before importing native dependencies or reading the private key.
if __name__ == "__main__" and "--send-to-lyria" in sys.argv:
    try:
        disable_crash_dumps()
    except Exception:
        print('{"result":"failed","reason":"crash_controls"}')
        sys.exit(1)

from sobafm import lyria  # noqa: E402
from sobafm.lyria import (  # noqa: E402
    CLEANUP_SECONDS,
    ENDPOINT,
    MAX_MESSAGE_PCM_BYTES,
    LyriaSession,
    MusicRequest,
    Prompt,
)
from sobafm.pcm import BANK_BYTES, FRAME_BYTES, PcmBankPool  # noqa: E402

WORK_SECONDS = 25
HARD_SECONDS = 35
FRAMES = 200
MAX_PCM_BYTES = BANK_BYTES + FRAMES * FRAME_BYTES
KEY_FILE = Path("/run/lyria-key")
REQUEST = MusicRequest((Prompt("gentle instrumental jazz, piano and soft drums"),), bpm=90)


class Failed(Exception):
    """A fixed local reason, never provider text."""


@contextmanager
def one_socket() -> Generator[None]:
    """Cap the isolated evaluator before aiohttp's internal GET retry can connect.

    The existing public socket-factory boundary still creates the first socket
    with its production receive limits. Deny every subsequent allocation,
    including address fallback, and keep the guard through actual settlement.
    This process runs one probe; no application or other client runs alongside it.
    """
    original = lyria.receive_bounded_socket
    attempted = False

    def bounded(
        addr_info: tuple[
            int | socket.AddressFamily, int | socket.SocketKind, int, str, tuple[Any, ...]
        ],
    ) -> socket.socket:
        nonlocal attempted
        if attempted:
            raise OSError("evaluation connection limit")
        attempted = True
        return original(addr_info)

    lyria.receive_bounded_socket = bounded
    try:
        yield
    finally:
        lyria.receive_bounded_socket = original


@dataclass
class Result:
    result: str = "failed"
    reason: str | None = None
    first_pcm_ms: float | None = None
    ready_ms: float | None = None
    pressure_bytes: int = 0
    held_bytes: int = 0
    resumed: bool = False
    frames: int = 0
    pcm_bytes: int = 0
    cleanup: str | None = None
    release_ms: float | None = None
    leased: int = 1

    def fail(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason


async def probe(
    key: str,
    *,
    endpoint: str = ENDPOINT,
) -> Result:
    """The only network entry. CLI opt-in is enforced separately in main.

    Endpoint injection is for explicitly invoked offline regressions;
    the CLI never accepts an endpoint, prompt, duration, retry or quota override.
    """
    with one_socket():
        return await _probe(key, endpoint=endpoint)


async def _probe(key: str, *, endpoint: str) -> Result:
    result = Result()
    pool = PcmBankPool()
    bank = pool.acquire()
    session = LyriaSession(REQUEST, bank, api_key=key, endpoint=endpoint)
    start = time.monotonic()

    def check() -> None:
        result.pcm_bytes = session.pcm_bytes
        if result.pcm_bytes and result.first_pcm_ms is None:
            result.first_pcm_ms = round((time.monotonic() - start) * 1000, 2)
        if session.ready and result.ready_ms is None:
            result.ready_ms = round((time.monotonic() - start) * 1000, 2)
        if session.failure:
            raise Failed(session.failure)
        if session.stopped:
            raise Failed("early_close")
        if session.pcm_bytes > MAX_PCM_BYTES:
            raise Failed("pcm_limit")

    async def until(predicate: Callable[[], bool]) -> None:
        while not predicate():
            check()
            await asyncio.sleep(0.01)
        check()

    async def cleanup() -> None:
        session.cancel()  # retains the original stop/cancellation clock
        report = await session.finish()
        result.cleanup = report.cleanup
        if report.failure:
            result.fail(report.failure)
        if report.cleanup != "clean":
            result.fail("cleanup_expired")
        await session.settle()  # a late verdict never refunds this lease early
        while not bank.release():
            result.fail("retained_bank")
            await asyncio.sleep(0.01)
        cancelled = bank.facts().cancelled_at
        assert cancelled is not None
        elapsed = time.monotonic() - cancelled
        result.release_ms = round(1000 * elapsed, 2)
        if elapsed > CLEANUP_SECONDS:
            result.fail("late_release")
        result.leased = pool.leased
        result.pcm_bytes = report.pcm_bytes

    session.start()
    try:
        async with asyncio.timeout(WORK_SECONDS):
            # With no consumer, reaching this threshold blocks the next maximum message.
            await until(lambda: BANK_BYTES - bank.facts().buffered_bytes < MAX_MESSAGE_PCM_BYTES)
            result.pressure_bytes = bank.facts().buffered_bytes
            await asyncio.sleep(1)
            check()
            result.held_bytes = bank.facts().buffered_bytes
            if result.held_bytes != result.pressure_bytes:
                raise Failed("pressure_changed")
            before = session.pcm_bytes
            for _ in range(FRAMES):
                check()
                frame = bank.read_frame()
                if frame is None or len(frame) != FRAME_BYTES:
                    raise Failed("missing_frame")
                del frame  # no audio capture or accumulation
                result.frames += 1
                await asyncio.sleep(0.02)
            await until(lambda: session.pcm_bytes > before)
            result.resumed = True
    except Failed as exc:
        result.fail(str(exc))
    except TimeoutError:
        result.fail("work_timeout")
    except asyncio.CancelledError:
        result.fail("cancelled")
    except Exception:
        result.fail("internal")
    finally:
        owner = asyncio.create_task(cleanup())
        while not owner.done():
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError:
                result.fail("cancelled")
        owner.result()
    if result.reason is None:
        result.result = "pass"
    return result


def hard_stop(_signum: int, _frame: FrameType | None) -> None:
    os.write(1, b'{"result":"failed","reason":"hard_stop","settlement":"unconfirmed"}\n')
    os._exit(124)


async def live() -> Result:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    # Never let asyncio diagnostics serialize a provider exception or its headers.
    loop.set_exception_handler(lambda _loop, _context: None)
    with KEY_FILE.open("r", encoding="ascii") as handle:
        key = handle.read(513).strip()
    if not key or len(key) > 512 or any(c.isspace() for c in key):
        raise Failed("credential_file")
    return await probe(key)


def main(args: list[str]) -> int:
    logging.disable(logging.CRITICAL)
    if not args:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "sessions": 1,
                    "retries": 0,
                    "renewals": 0,
                    "work_seconds": WORK_SECONDS,
                    "hard_seconds": HARD_SECONDS,
                    "max_pcm_bytes": MAX_PCM_BYTES,
                }
            )
        )
        return 0
    if args != ["--send-to-lyria", "--confirm-free-tier"]:
        print('{"result":"failed","reason":"explicit_opt_in_required"}')
        return 2
    try:
        signal.signal(signal.SIGALRM, hard_stop)
        signal.alarm(HARD_SECONDS)
        if platform.system() != "Linux" or platform.machine() != "x86_64" or os.getuid() == 0:
            raise Failed("target")
        root = Path("/sys/fs/cgroup")
        for name, expected in (
            ("memory.max", "90177536"),
            ("memory.swap.max", "0"),
            ("pids.max", "64"),
        ):
            if (root / name).read_text().strip() != expected:
                raise Failed("limits")
        quota, period = map(int, (root / "cpu.max").read_text().split())
        if period <= 0 or quota != 2 * period:
            raise Failed("limits")
        result = asyncio.run(live())
        print(
            json.dumps(
                {
                    **asdict(result),
                    "arch": platform.machine(),
                    "kernel": platform.release(),
                    "page_bytes": os.sysconf("SC_PAGE_SIZE"),
                }
            ),
            flush=True,
        )
        signal.alarm(0)
        return 0 if result.result == "pass" else 1
    except Exception:
        # No traceback, exception text, provider URL, header, key or payload.
        print('{"result":"failed","reason":"preflight_or_internal"}', flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
