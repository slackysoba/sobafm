"""L2 client. Only the target entry point runs the five-pair experiment."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import ssl
import sys
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sobafm.crash_dumps import CrashDumpError, disable_crash_dumps

if __name__ == "__main__":
    try:
        disable_crash_dumps()
    except CrashDumpError:
        print('{"client":"failed","reason":"crash_controls"}', flush=True)
        sys.exit(1)

from sobafm.lyria import (  # noqa: E402
    CLEANUP_SECONDS,
    CROSSFADE_FRAMES,
    RENEW_AFTER_SECONDS,
    GeneratedStream,
    MusicRequest,
    Prompt,
)
from sobafm.pcm import BANK_BYTES, BYTES_PER_SECOND, FRAME_BYTES, PcmBank, PcmBankPool  # noqa: E402

GROUP = 90_177_536
PRODUCER_COPIES = 1_048_576 + 524_288
SHARED_COPIES = 2 * 524_288
NATIVE_RECORD = 16_709  # ADR 0008 partial TLS record, outside Python allocation tracking
CGROUP = Path("/sys/fs/cgroup")


class Bound(Exception):
    """Only fixed, payload-free reason codes may appear in the report."""


@dataclass
class Pair:
    pair: int
    failure: str | None = None
    cancelled: bool = False
    sessions: int = 0
    crossfades: int = 0
    mixed_frames: int = 0
    ready_ms: float | None = None
    buffered_bytes: list[int] = field(default_factory=list[int])
    # Raw traced peaks include both PCM and Python object overhead.
    traced_peaks: list[int] = field(default_factory=lambda: [0, 0, 0])
    traced_after_join: int | None = None
    cleanup_ms: list[float | None] = field(default_factory=list[float | None])
    failed_cleanups: int | None = None
    retained_banks: int | None = None
    leased_after_finish: int | None = None

    def fail(self, reason: str) -> None:
        if self.failure is None:
            self.failure = reason


def check_peak(raw: int, banks: int) -> None:
    # Charge the native allowance in addition to *all* traced allocation, then
    # compare to the PCM, private producer slots and shared scratch/drain.
    if raw + banks * NATIVE_RECORD > banks * (BANK_BYTES + PRODUCER_COPIES) + SHARED_COPIES:
        raise Bound("copy_budget")


class ObservedPool(PcmBankPool):
    """Actual allocator and release. Keep handles for public facts after refund."""

    def __init__(self, result: Pair) -> None:
        super().__init__()
        self.banks: list[PcmBank] = []
        self.result = result

    def measure(self) -> None:
        count = self.leased
        _, peak = tracemalloc.get_traced_memory()
        self.result.traced_peaks[count] = max(self.result.traced_peaks[count], peak)
        try:
            check_peak(peak, count)
        except Bound as exc:
            self.result.fail(str(exc))

    def acquire(self) -> PcmBank:
        self.measure()
        if len(self.banks) == 2:
            self.result.fail("extra_attempt")
        if self.result.failure:
            raise Bound(self.result.failure)
        tracemalloc.reset_peak()  # the next synchronous allocation belongs to the new lease
        bank = super().acquire()
        self.banks.append(bank)
        self.result.cleanup_ms.append(None)
        return bank

    def _refund(self) -> None:
        # PcmBank calls this synchronously AFTER it clears its backing and releases
        # its lock. Seal the prior owner interval, then start the smaller one.
        self.measure()
        super()._refund()
        tracemalloc.reset_peak()
        now = time.monotonic()
        for index, bank in enumerate(self.banks):
            if self.result.cleanup_ms[index] is not None:
                continue
            facts = bank.facts()
            if facts.released and facts.cancelled_at is not None:
                elapsed = now - facts.cancelled_at
                self.result.cleanup_ms[index] = round(1000 * elapsed, 2)
                if elapsed > CLEANUP_SECONDS:
                    self.result.fail("late_release")
        self.measure()


async def run_pair(
    result: Pair,
    tls: ssl.SSLContext,
    port: int,
    resources: Callable[[], None],
    *,
    frame_interval: float,
) -> None:
    pool = ObservedPool(result)
    renew = asyncio.Queue[None](maxsize=1)

    async def sleep(delay: float) -> None:
        if delay == RENEW_AFTER_SECONDS:
            await renew.get()  # only expedite the first renewal, keep every other clock real
        else:
            await asyncio.sleep(delay)

    stream = GeneratedStream(
        MusicRequest((Prompt("fixture"),), bpm=90),
        api_key="loopback-placeholder",  # pragma: allowlist secret - fixed synthetic header
        endpoint=f"wss://127.0.0.1:{port}/{result.pair}",
        ssl_context=tls,
        pool=pool,
        sleep=sleep,
    )

    def measure() -> None:
        try:
            resources()
        except Bound as exc:
            result.fail(str(exc))
        except OSError, ValueError, KeyError:
            result.fail("measurement")
        pool.measure()
        for bank in pool.banks:
            facts = bank.facts()
            if (
                not facts.released
                and facts.cancelled_at is not None
                and time.monotonic() - facts.cancelled_at > CLEANUP_SECONDS
            ):
                result.fail("late_release")

    def check() -> None:
        measure()
        if result.failure:
            raise Bound(result.failure)

    async def until(predicate: Callable[[], bool]) -> None:
        while not predicate():
            check()
            await asyncio.sleep(0.01)
        check()

    def pressured(index: int) -> bool:
        # After five 2s messages, the 12s bank cannot admit a maximum message.
        return (
            len(pool.banks) > index
            and pool.banks[index].facts().buffered_bytes >= 10 * BYTES_PER_SECOND
        )

    def sample() -> int:
        frame = stream.read_frame()
        if frame is None or len(frame) != FRAME_BYTES:
            raise Bound("missing_frame")
        return int.from_bytes(frame[:2], "little", signed=True)

    start = time.monotonic()
    own_trace = not tracemalloc.is_tracing()
    if own_trace:
        tracemalloc.start()
    tracemalloc.reset_peak()  # keep live allocations from earlier pairs in the target's trace
    stream.start()
    try:
        async with asyncio.timeout(22):
            await until(stream.ready_now)
            result.ready_ms = round(1000 * (time.monotonic() - start), 2)
            await until(lambda: pressured(0))
            if sample() != 1000:
                raise Bound("outgoing_frame")
            renew.put_nowait(None)
            await until(lambda: stream.crossfading and pressured(1))
            result.buffered_bytes = [bank.facts().buffered_bytes for bank in pool.banks]
            if pool.leased != 2:
                raise Bound("missing_overlap")
            for index in range(CROSSFADE_FRAMES):
                if not stream.crossfading:
                    raise Bound("short_crossfade")
                value = sample()
                expected = 1000 + 1000 * (index + 1) / CROSSFADE_FRAMES
                if not expected - 2 <= value <= expected:  # each scaled sample rounds down
                    raise Bound("missing_mix")
                result.mixed_frames += 1
                if index % 10 == 0:
                    check()
                await asyncio.sleep(frame_interval)
            if stream.crossfading or sample() != 2000:
                raise Bound("missing_handoff")
            await until(lambda: pool.banks[0].facts().released)
    except (Bound, TimeoutError) as exc:
        result.fail(str(exc) if isinstance(exc, Bound) else "pair_timeout")
    except asyncio.CancelledError:
        result.cancelled = True
        result.fail("cancelled")
    finally:
        # finish joins the original retirements, even after a failed verdict.
        # Never infer release from elapsed time. The host deadline bounds a hang.
        try:
            finish = asyncio.create_task(stream.finish())
            announced = False
            while not finish.done():
                if result.failure and not announced:
                    print(
                        json.dumps(
                            {
                                "pair": result.pair,
                                "failure": result.failure,
                                "leased_before_join": pool.leased,
                            }
                        ),
                        flush=True,
                    )
                    announced = True
                try:
                    await asyncio.wait({finish}, timeout=0.05)
                except asyncio.CancelledError:
                    # The wait was interrupted. finish still owns these same retirements.
                    result.cancelled = True
                    result.fail("cancelled")
                measure()
            report = finish.result()
            result.sessions = report.sessions_started
            result.crossfades = report.crossfades
            result.failed_cleanups = report.failed_cleanups
            result.retained_banks = report.retained_banks
            if report.failure or report.failed_cleanups or report.retained_banks or pool.leased:
                result.fail("stream_or_cleanup_failure")
            if None in result.cleanup_ms:
                result.fail("unreleased_bank")
            if (report.sessions_started, report.crossfades) != (2, 1):
                result.fail("session_count")
        finally:
            result.leased_after_finish = pool.leased
            result.traced_after_join = tracemalloc.get_traced_memory()[0]
            measure()
            if own_trace:
                tracemalloc.stop()
    if result.cancelled:
        raise asyncio.CancelledError


@dataclass
class Resources:
    memory_peak: int = 0
    pids_peak_sampled: int = 0
    memory_events: dict[str, int] = field(default_factory=dict[str, int])
    pids_max_events: int = 0

    def check(self) -> None:
        self.memory_peak = int((CGROUP / "memory.peak").read_text())
        pids = int((CGROUP / "pids.current").read_text())
        self.pids_peak_sampled = max(self.pids_peak_sampled, pids)
        self.memory_events = {
            key: int(value)
            for key, value in (
                line.split() for line in (CGROUP / "memory.events").read_text().splitlines()
            )
        }
        self.pids_max_events = int(
            dict(line.split() for line in (CGROUP / "pids.events").read_text().splitlines())["max"]
        )
        if (
            self.memory_peak > GROUP
            or pids > 64
            or any(self.memory_events.values())
            or self.pids_max_events
        ):
            raise Bound("cgroup_pressure")


async def target() -> int:
    # Import the same application dependency set and use its actual startup cap gate.
    from sobafm.__main__ import check_limits

    if platform.machine() != "x86_64" or os.getuid() == 0:
        raise Bound("target")
    check_limits()
    quota, period = map(int, (CGROUP / "cpu.max").read_text().split())
    if period <= 0 or quota != 2 * period:
        raise Bound("cpu_limit")
    resources = Resources()
    print(
        json.dumps(
            {
                "case": "L2",
                "arch": platform.machine(),
                "kernel": platform.release(),
                "page_bytes": os.sysconf("SC_PAGE_SIZE"),
                "memory_max": GROUP,
                "swap_max": 0,
                "pids_max": 64,
                "cpus": 2,
                "pcm": 2 * BANK_BYTES,
                "copies": 2 * PRODUCER_COPIES + SHARED_COPIES,
            }
        ),
        flush=True,
    )
    tls = ssl.create_default_context(cafile="/fixture.crt")
    tracemalloc.start()  # one target process and one trace across all ten sessions
    for index in range(1, 6):
        result = Pair(index)
        try:
            resources.check()
            await run_pair(result, tls, 8765, resources.check, frame_interval=0.02)
        except Bound as exc:
            result.fail(str(exc))
        except Exception as exc:
            result.fail(type(exc).__name__)
        finally:
            print(json.dumps({**asdict(result), **asdict(resources)}), flush=True)
        if result.failure:
            return 1
    print('{"L2":"pass","sessions":10,"renewals":5}', flush=True)
    return 0


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        sys.exit(asyncio.run(target()))
    except Exception as error:
        # Do not render exception text, requests, paths or stack traces.
        print(json.dumps({"L2": "failed", "reason": type(error).__name__}), flush=True)
        sys.exit(1)
