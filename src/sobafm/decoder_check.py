"""Small owned-fixture load check, distinct from untrusted-track qualification.

No URL, credentials or catalog input reaches this check. It proves only that an
already verified package loads and converts these four owned silent fixtures.
It does not approve supply, containment, audio envelopes or catalog readiness.
"""

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from sobafm.catalog_audio import TrackError, TrackFailure
from sobafm.decoder_process import close_pipes, command, environment
from sobafm.native import VerifiedNativeRuntime, verify_approved_native_runtime

PCM_CAP = 256 * 1024
STDERR_CAP = 4096
CHECK_SECONDS = 15.0
CLEANUP_SECONDS = 3.0
TERMINATE_SECONDS = 1.0
KILL_SECONDS = 1.0
_owners: set[asyncio.Task[None]] = set()
log = logging.getLogger(__name__)


async def prepare_decoder(root: Path | None, registry: Path | None) -> VerifiedNativeRuntime | None:
    """Catalog-only startup check; no missing prerequisite breaks generated mode."""
    if root is None:
        return None
    try:
        if not root.is_absolute():
            raise TrackError(TrackFailure.INELIGIBLE)
        # Full package hashing and filesystem validation do not block the loop.
        runtime = await asyncio.to_thread(verify_approved_native_runtime, root, registry=registry)
        await check_decoder(runtime)
    except Exception as error:  # noqa: BLE001 - supply/OS failure is catalog-only, never raw text
        error.__traceback__ = error.__context__ = error.__cause__ = None
        log.warning(
            "Catalog decoder unavailable; check the reviewed native installation and trust registry"
        )
        return None
    log.info(
        "Catalog decoder capability checked; containment and audio qualification still required"
    )
    return runtime


def fixtures() -> dict[str, bytes]:
    """MPEG-1 Layer III,128kbit/s,40 frames with zero spectral coefficients."""
    return {
        f"{rate}-{channels}": (header + bytes(size - 4)) * 40
        for rate, size, prefix in ((44100, 417, b"\xff\xfb\x90"), (48000, 384, b"\xff\xfb\x94"))
        for channels, header in (("mono", prefix + b"\xc4"), ("stereo", prefix + b"\x00"))
    }


async def check_decoder(runtime: VerifiedNativeRuntime) -> dict[str, int]:
    """Run only owned fixed input; retain any unfinished cleanup owner on failure.

    Repeated caller cancellation never cancels the cleanup owner. Another check
    cannot start while a pending/failed owner still holds process or pipe debt.
    """
    if _owners:
        raise TrackError(TrackFailure.CAPACITY)
    result: dict[str, int] = {}
    for name, encoded in fixtures().items():
        decision = asyncio.get_running_loop().create_future()
        cleaning = asyncio.Event()
        # Python3.14 eager entry makes the owner claim spawn/cleanup responsibility
        # before a caller can cancel it at its first suspension.
        owner = asyncio.create_task(_probe(runtime, encoded, decision, cleaning), eager_start=True)
        _owners.add(owner)
        owner.add_done_callback(_owners.discard)
        cancelled = False
        while True:
            try:
                count = await asyncio.shield(decision)
                break
            except asyncio.CancelledError:
                # Forward at most one work cancellation, never a cancellation
                # into the same owner's already-entered cleanup/finally block.
                if not cancelled and not cleaning.is_set():
                    owner.cancel()
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        if count is None:
            raise TrackError(TrackFailure.DECODER)
        result[name] = count
    return result


async def _probe(
    runtime: VerifiedNativeRuntime,
    encoded: bytes,
    decision: asyncio.Future[int | None],
    cleaning: asyncio.Event,
) -> None:
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *command(runtime.executable),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment(),
            limit=16 * 1024,
            start_new_session=os.name != "nt",
        )
    )
    workers: list[asyncio.Task[Any]] = []
    count: int | None = None
    try:
        async with asyncio.timeout(CHECK_SECONDS):
            process = await asyncio.shield(spawn)
            workers = [
                asyncio.create_task(_feed(process, encoded)),
                asyncio.create_task(_read(process.stdout, PCM_CAP, silence=True)),
                asyncio.create_task(_read(process.stderr, STDERR_CAP, silence=False)),
                asyncio.create_task(process.wait()),
            ]
            values = await asyncio.gather(*workers)
            # 40*1152 samples, converted to48kHz stereo; allow only codec delay.
            expected = 40 * 1152 * 48000 / (44100 if len(encoded) == 417 * 40 else 48000) * 4
            if (
                values[3] != 0
                or values[2] != 0
                or not expected - 8192 <= values[1] <= expected + 8192
            ):
                raise TrackError(TrackFailure.DECODER)
            if values[1] % 4:
                raise TrackError(TrackFailure.OUTPUT)
            count = values[1]
    except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - fixed result only
        error.__traceback__ = error.__context__ = error.__cause__ = None
    finally:
        cleaning.set()
        await _cleanup(spawn, workers, decision, count)


async def _cleanup(
    spawn: asyncio.Task[asyncio.subprocess.Process],
    workers: list[asyncio.Task[Any]],
    decision: asyncio.Future[int | None],
    count: int | None,
) -> None:
    deadline = time.monotonic() + CLEANUP_SECONDS
    timer = asyncio.get_running_loop().call_later(
        CLEANUP_SECONDS, _deadline, decision, float("-inf")
    )
    for task in workers:
        task.cancel()
    while not spawn.done():
        _deadline(decision, deadline)
        await asyncio.sleep(0.01)
    if spawn.cancelled():
        _deadline(decision, float("-inf"))
        await asyncio.Future[None]()  # cancelled spawn cannot prove absence of a child
    try:
        process = spawn.result()
    except Exception as error:  # noqa: BLE001 - retrieve launch failure without text
        error.__traceback__ = error.__context__ = error.__cause__ = None
        if not decision.done():
            decision.set_result(None)
        timer.cancel()
        return
    # No wait-before-drain dependency. These are trusted fixed-input children;
    # production TrackDeck separately requires an enforced cgroup/Job before input.
    cleanup: list[Coroutine[Any, Any, object]] = [
        _read(process.stdout, PCM_CAP, silence=False),
        _read(process.stderr, STDERR_CAP, silence=False),
        _close_input(process),
        process.wait(),
        _join(workers),
    ]
    tasks = [asyncio.create_task(work) for work in cleanup]
    with contextlib.suppress(OSError):
        process.terminate()
    kill_at = min(deadline, time.monotonic() + TERMINATE_SECONDS)
    kill_deadline = min(deadline, kill_at + KILL_SECONDS)
    while True:
        _deadline(decision, deadline)
        if decision.done():
            count = None
        if time.monotonic() >= kill_at and process.returncode is None:
            with contextlib.suppress(OSError):
                process.kill()
        if time.monotonic() >= kill_deadline and process.returncode is None:
            _deadline(decision, float("-inf"))
        closed = False
        if process.returncode is not None and (count is None or all(task.done() for task in tasks)):
            try:
                closed = close_pipes(process)
            except Exception as error:  # noqa: BLE001 - retain unproved pipe debt
                error.__traceback__ = error.__context__ = error.__cause__ = None
                count = None
            if closed and count is None:
                # Failed/cancelled probes need actual handle release, not success
                # EOF. A broken reader must not prevent trying transport closure.
                for task in tasks:
                    if not task.done():
                        task.cancel()
        if all(task.done() for task in tasks) and process.returncode is not None:
            values = await asyncio.gather(*tasks, return_exceptions=True)
            failed = any(isinstance(value, BaseException) for value in values)
            for value in values:
                if isinstance(value, BaseException):
                    value.__traceback__ = value.__context__ = value.__cause__ = None
            if closed:
                timer.cancel()
                if not decision.done():
                    decision.set_result(None if failed else count)
                return
        await asyncio.sleep(0.01)


def _deadline(decision: asyncio.Future[int | None], deadline: float) -> None:
    if time.monotonic() >= deadline and not decision.done():
        decision.set_result(None)


async def _feed(process: asyncio.subprocess.Process, encoded: bytes) -> None:
    if process.stdin is None:
        raise TrackError(TrackFailure.DECODER)
    for offset in range(0, len(encoded), 16384):
        process.stdin.write(encoded[offset : offset + 16384])
        await process.stdin.drain()
    await _close_input(process)


async def _close_input(process: asyncio.subprocess.Process) -> None:
    if process.stdin is not None:
        process.stdin.close()
        await process.stdin.wait_closed()


async def _read(reader: asyncio.StreamReader | None, cap: int, *, silence: bool) -> int:
    if reader is None:
        raise TrackError(TrackFailure.DECODER)
    count = 0
    while chunk := await reader.read(3840):
        count += len(chunk)
        if count > cap or (silence and any(chunk)):
            raise TrackError(TrackFailure.OUTPUT)
        await asyncio.sleep(0)
    return count


async def _join(workers: list[asyncio.Task[Any]]) -> None:
    values = await asyncio.gather(*workers, return_exceptions=True)
    for value in values:
        if isinstance(value, BaseException):
            value.__traceback__ = value.__context__ = value.__cause__ = None
