"""Finite installed-image probe. Importing this module performs no workload."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import platform
import select
import signal
import sys
import threading
import time
from collections.abc import Buffer, Callable
from dataclasses import dataclass, field
from pathlib import Path

from sobafm.crash_dumps import disable_crash_dumps

if __name__ == "__main__":
    try:
        disable_crash_dumps()
    except Exception:
        print('{"event":"failure","reason":"crash_controls"}', flush=True)
        sys.exit(1)

from aiohttp import web  # noqa: E402

from sobafm.decode import DecodeInput, DecodeProducer, DecodeReport, RawInput  # noqa: E402
from sobafm.lyria import (  # noqa: E402
    CROSSFADE_FRAMES,
    PLAY_MESSAGE,
    RENEW_AFTER_SECONDS,
    SETUP_MESSAGE,
    GeneratedStream,
    MusicRequest,
    Prompt,
)
from sobafm.pcm import (  # noqa: E402
    BANK_BYTES,
    BYTES_PER_SECOND,
    FRAME_BYTES,
    PcmBank,
    PcmBankPool,
    PcmCapacityError,
    StationReservation,
)

GROUP = 268_435_456
FIXTURE_BYTES = 1_793_243
# Existing admitted FMA 9423; exact media/review identity under ADR 0007 and #122.
# pragma: allowlist nextline secret - published media SHA-256
FIXTURE_SHA256 = "ab0cf8ce5dbb75c072bda2cf9f8ae6cda261cdf7da505eee1e825011a00f5159"
PCM_LIMIT = 21_699_840
MAX_SESSIONS = 8
MESSAGES_PER_SESSION = 8
MESSAGE_PCM = 2 * BYTES_PER_SECOND
WORK_SECONDS = 90
CGROUP = Path("/sys/fs/cgroup")
# Keep failed cases' owners reachable until the outer supervisor settles the container.
RETAINED: list[object] = []


class Failed(Exception):
    """Fixed reason codes only; never include input or exception text."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise Failed(reason)


def emit(event: str, **facts: object) -> None:
    print(json.dumps({"event": event, **facts}, allow_nan=False), flush=True)


def fixture(path: Path) -> DecodeInput:
    require(path.is_file() and path.stat().st_size == FIXTURE_BYTES, "fixture_size")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    require(digest == FIXTURE_SHA256, "fixture_digest")
    return DecodeInput(path, FIXTURE_BYTES, FIXTURE_SHA256, PCM_LIMIT)


def refused(pool: PcmBankPool) -> bool:
    """Probe admission before constructing any additional producer or transport."""
    try:
        pool.reserve(4)
    except PcmCapacityError:
        return True
    return False


@dataclass
class Resources:
    memory_peak: int = 0
    tasks_peak_sampled: int = 0
    samples: int = 0

    def sample(self) -> None:
        self.memory_peak = int((CGROUP / "memory.peak").read_text())
        tasks = int((CGROUP / "pids.current").read_text())
        self.tasks_peak_sampled = max(self.tasks_peak_sampled, tasks)
        self.samples += 1
        events = dict(line.split() for line in (CGROUP / "memory.events").read_text().splitlines())
        pids = dict(line.split() for line in (CGROUP / "pids.events").read_text().splitlines())
        require(self.memory_peak <= GROUP and tasks <= 64, "resource_limit")
        require(all(int(v) == 0 for v in events.values()), "memory_pressure")
        require(int(pids["max"]) == 0, "task_pressure")
        require(int((CGROUP / "memory.swap.current").read_text()) == 0, "swap")

    def receipt(self) -> None:
        self.sample()
        cpu = dict(line.split() for line in (CGROUP / "cpu.stat").read_text().splitlines())
        emit(
            "resources",
            memory_peak=self.memory_peak,
            tasks_peak_sampled=self.tasks_peak_sampled,
            samples=self.samples,
            cpu_usage_usec=int(cpu["usage_usec"]),
            cpu_throttled_usec=int(cpu["throttled_usec"]),
            tasks_final=int((CGROUP / "pids.current").read_text()),
        )


async def until(predicate: Callable[[], bool], seconds: float = 15) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


@dataclass
class Peer:
    """Owned synthetic samples, same shape as L2; no provider endpoint or key."""

    counts: dict[str, int] = field(default_factory=dict[str, int])
    messages: int = 0
    requests: int = 0
    rejected: int = 0

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        self.requests += 1
        route = request.match_info["slot"]
        number = self.counts.get(route, 0) + 1
        if route not in ("1", "2", "3", "mixed") or number > 2 or self.requests > MAX_SESSIONS:
            self.rejected += 1
            raise web.HTTPForbidden()
        self.counts[route] = number
        ws = web.WebSocketResponse(max_msg_size=4096, compress=False)
        await ws.prepare(request)
        music = MusicRequest((Prompt("fixture"),), bpm=90)
        try:
            async with asyncio.timeout(20):
                require(await ws.receive_json() == SETUP_MESSAGE, "setup")
                await ws.send_json({"setupComplete": {}})
                for expected in (music.prompts_message(), music.config_message(), PLAY_MESSAGE):
                    require(await ws.receive_json() == expected, "control")
                pcm = (1000 * number).to_bytes(2, "little") * (MESSAGE_PCM // 2)
                message = json.dumps(
                    {
                        "serverContent": {
                            "audioChunks": [
                                {
                                    "data": base64.b64encode(pcm).decode("ascii"),
                                    "mimeType": "audio/l16;rate=48000;channels=2",
                                }
                            ]
                        }
                    }
                )
                for _ in range(MESSAGES_PER_SESSION):
                    await ws.send_str(message)
                    self.messages += 1
                await ws.receive()
        except ConnectionError, TimeoutError:
            pass
        return ws


@dataclass
class Native:
    bank: PcmBank
    producer: DecodeProducer
    finisher: asyncio.Task[DecodeReport]
    cleanup_ms: float | None = None


def live_decoder(owner: Native) -> bool:
    pidfd = owner.producer._pidfd  # pyright: ignore[reportPrivateUsage]
    if pidfd is None:
        return False
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return not poller.poll(0)


def native(source: DecodeInput, reservation: StationReservation) -> Native:
    bank = reservation.acquire()
    producer = DecodeProducer(source, bank=bank)
    producer.start()
    return Native(bank, producer, asyncio.create_task(asyncio.to_thread(producer.finish)))


async def retire(owner: Native) -> None:
    owner.producer.cancel()
    first = owner.producer.report().cleanup_deadline
    owner.producer.cancel()
    require(owner.producer.report().cleanup_deadline == first, "renewed_deadline")
    report = await owner.finisher
    require(report.cleanup == "clean" and report.failure is None, "decode_cleanup")
    sent = owner.producer._signalled  # pyright: ignore[reportPrivateUsage]
    require(
        report.returncode in (0, -signal.SIGTERM)
        or (sent == signal.SIGKILL and report.returncode == -signal.SIGKILL),
        "decode_exit",
    )
    require(
        report.cleanup_started_at is not None and first == report.cleanup_started_at + 3,
        "cleanup_clock",
    )
    require(owner.producer.release(), "decode_debt")
    require(owner.bank.release(), "bank_debt")
    require(first is not None and time.monotonic() <= first, "late_bank_release")
    assert report.cleanup_started_at is not None
    owner.cleanup_ms = round((time.monotonic() - report.cleanup_started_at) * 1000, 2)


async def load_case(name: str, source: DecodeInput, port: int) -> None:
    pool = PcmBankPool()
    stations = [pool.reserve(guild) for guild in range(1, 4)]
    require(refused(pool) and pool.leased == 0, "fourth_station")
    native_stations = 3 if name == "catalog" else 2 if name == "mixed" else 0
    natives: list[Native] = []
    streams: list[GeneratedStream] = []
    renewals: list[asyncio.Queue[None]] = []
    RETAINED.extend((pool, natives, streams))
    started = time.monotonic()
    error: BaseException | None = None
    try:
        for station in stations[:native_stations]:
            for _ in range(2):
                natives.append(native(source, station))
        for index in range(native_stations, 3):
            renewal = asyncio.Queue[None](maxsize=1)
            renewals.append(renewal)

            async def sleep(delay: float, event: asyncio.Queue[None] = renewal) -> None:
                if delay == RENEW_AFTER_SECONDS:
                    await event.get()  # expedite exactly one renewal, never the cleanup clock
                else:
                    await asyncio.sleep(delay)

            route = "mixed" if name == "mixed" else str(index + 1)
            stream = GeneratedStream(
                MusicRequest((Prompt("fixture"),), bpm=90),
                api_key="loopback-placeholder",  # pragma: allowlist secret
                endpoint=f"ws://127.0.0.1:{port}/{route}",
                pool=stations[index],
                sleep=sleep,
            )
            streams.append(stream)
            stream.start()
        await until(
            lambda: (
                all(n.bank.facts().ready for n in natives) and all(s.ready_now() for s in streams)
            )
        )
        ready_ms = round((time.monotonic() - started) * 1000, 2)
        for event in renewals:
            event.put_nowait(None)
        await until(
            lambda: (
                all(s.crossfading for s in streams)
                and all(n.bank.facts().buffered_bytes == BANK_BYTES for n in natives)
            )
        )
        require(pool.leased == 6 and all(s.leased == 2 for s in stations), "six_slots")
        require(all(live_decoder(n) for n in natives), "native_overlap")
        require(refused(pool), "fourth_loaded")
        # No pool can borrow a different guild's replacement slot.
        for station in stations:
            try:
                station.acquire()
            except PcmCapacityError:
                continue
            raise Failed("borrowed_slot")
        frames = 0
        nonzero = 0
        for index in range(CROSSFADE_FRAMES):
            for pair in range(native_stations):
                # Successive explicit replacement; no catalog mixing or overlap.
                owner = natives[2 * pair + int(index >= CROSSFADE_FRAMES // 2)]
                frame = owner.bank.read_frame()
                require(frame is not None and len(frame) == FRAME_BYTES, "catalog_frame")
                nonzero += bool(frame and any(frame))
                frames += 1
            for stream in streams:
                frame = stream.read_frame()
                require(frame is not None and len(frame) == FRAME_BYTES, "generated_frame")
                assert frame is not None
                value = int.from_bytes(frame[:2], "little", signed=True)
                expected = 1000 + 1000 * (index + 1) / CROSSFADE_FRAMES
                require(expected - 2 <= value <= expected, "crossfade_sample")
                frames += 1
            await asyncio.sleep(0.02)
        for stream in streams:
            frame = stream.read_frame()
            require(
                frame is not None and int.from_bytes(frame[:2], "little", signed=True) == 2000,
                "renewal_handoff",
            )
        # The supervisor accounts the handoff and retires the outgoing feed on its
        # next loop turn. Observe that actual refund before stopping the new feed.
        await until(lambda: all(s.leased == 1 for s in stations[native_stations:]), 3)
        emit(
            "load",
            case=name,
            ready_ms=ready_ms,
            peak_slots=6,
            native_slots=len(natives),
            frames=frames,
            native_nonzero_frames=nonzero,
            fourth_refused=True,
        )
    except BaseException as exc:
        error = exc
        emit(
            "failure", case=name, reason=str(exc) if isinstance(exc, Failed) else type(exc).__name__
        )
    finally:
        # Start every cancellation before joining; preserve every owner on a failure.
        cleanup_start = time.monotonic()
        for owner in natives:
            owner.producer.cancel()
        for stream in streams:
            stream.cancel()
        native_results = await asyncio.gather(*(retire(n) for n in natives), return_exceptions=True)
        reports = await asyncio.gather(*(s.finish() for s in streams))
        cleanup_ms = round((time.monotonic() - cleanup_start) * 1000, 2)
        clean = all(not isinstance(r, BaseException) for r in native_results)
        clean &= all(
            r.failure is None
            and r.failed_cleanups == 0
            and r.retained_banks == 0
            and r.sessions_started == 2
            and r.crossfades == 1
            for r in reports
        )
        clean &= cleanup_ms <= 3000
        released = [pool.release(guild) for guild in range(1, 4)]
        emit(
            "cleanup",
            case=name,
            clean=clean,
            banks_left=pool.leased,
            reservations_left=len(pool.owners),
            sessions=sum(r.sessions_started for r in reports),
            failed_cleanups=sum(r.failed_cleanups for r in reports),
            crossfades=sum(r.crossfades for r in reports),
            stream_failure=next((r.failure for r in reports if r.failure is not None), None),
            cleanup_ms=cleanup_ms,
            native_settled=sum(n.producer.report().settled for n in natives),
            expired_native=sum(n.producer.report().cleanup == "expired" for n in natives),
            native_failure=next(
                (
                    n.producer.report().failure
                    for n in natives
                    if n.producer.report().failure is not None
                ),
                None,
            ),
            max_native_cleanup_ms=max((n.cleanup_ms or 0 for n in natives), default=0),
        )
        require(clean and all(released) and not pool.owners, "load_cleanup")
    if error is not None:
        raise error
    RETAINED.clear()


class HeldInput:
    """Real fixture I/O; one deliberately held close is the only injected fault."""

    def __init__(self, path: Path) -> None:
        self.raw: RawInput = open(path, "rb", buffering=0)  # noqa: SIM115
        self.closing = threading.Event()
        self.allow = threading.Event()

    def readinto(self, buffer: Buffer, /) -> int | None:
        return self.raw.readinto(buffer)

    def close(self) -> None:
        self.closing.set()
        self.allow.wait()  # the outer container owner bounds recovery, not a new deadline
        self.raw.close()


async def debt_case(source: DecodeInput) -> None:
    pool = PcmBankPool()
    stations = [pool.reserve(guild) for guild in range(1, 4)]
    bank = stations[0].acquire()
    held = HeldInput(source.path)
    producer = DecodeProducer(source, bank=bank, open_input=lambda _: held)
    RETAINED.extend((pool, held, producer, bank))
    producer.start()
    finish = asyncio.create_task(asyncio.to_thread(producer.finish))
    original: float | None = None
    try:
        await until(lambda: bank.facts().ready)
        producer.cancel()
        original = producer.report().cleanup_deadline
        await until(held.closing.is_set, 2)
        producer.cancel()
        require(producer.report().cleanup_deadline == original, "renewed_deadline")
        report = await finish
        require(
            report.cleanup_started_at is not None and original == report.cleanup_started_at + 3,
            "cleanup_clock",
        )
        require(report.cleanup == "expired" and not report.settled, "missing_expiry")
        require(not producer.release() and not pool.release(1) and pool.leased == 1, "lost_debt")
        require(refused(pool) and pool.reserve(1) is stations[0], "debt_admission")
        emit(
            "debt",
            expired=True,
            retained_banks=pool.leased,
            fourth_refused=True,
            original_deadline=original,
        )
    finally:
        producer.cancel()
        held.allow.set()
        await finish
        await until(producer.release, 3)
        require(bank.release(), "late_bank_debt")
    require(
        producer.report().cleanup == "expired" and producer.report().cleanup_deadline == original,
        "rewritten_failure",
    )
    require(all([pool.release(guild) for guild in range(1, 4)]), "late_reservation_debt")
    pool.reserve(4)
    require(pool.release(4), "restart_debt")
    emit(
        "debt_released",
        expired_still=True,
        banks_left=pool.leased,
        reservations_left=len(pool.owners),
        restart_admitted=True,
    )
    RETAINED.clear()


async def target() -> None:
    from sobafm.__main__ import check_limits

    require(platform.machine() == "x86_64" and os.getuid() == 1000, "target")
    check_limits()
    quota, period = map(int, (CGROUP / "cpu.max").read_text().split())
    require(period > 0 and quota == 2 * period, "cpu_limit")
    emit(
        "target",
        kernel=platform.release(),
        page_bytes=os.sysconf("SC_PAGE_SIZE"),
        group=GROUP,
        pcm=6 * BANK_BYTES,
        copies=12_582_912,
    )
    source = fixture(Path("/fixture.mp3"))
    resources = Resources()
    peer = Peer()
    app = web.Application()
    app.router.add_get("/{slot}", peer.handle)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8765).start()

    async def monitor() -> None:
        while True:
            resources.sample()
            await asyncio.sleep(0.02)

    monitor_task = asyncio.create_task(monitor())
    try:
        async with asyncio.timeout(WORK_SECONDS):
            for case in ("catalog", "generated", "mixed"):
                await load_case(case, source, 8765)
            await debt_case(source)
        require(
            peer.requests == MAX_SESSIONS
            and peer.rejected == 0
            and peer.messages == MAX_SESSIONS * MESSAGES_PER_SESSION,
            "peer_counts",
        )
        require(not monitor_task.done(), "resource_monitor")
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        await runner.cleanup()
        resources.receipt()
        emit(
            "peer",
            requests=peer.requests,
            messages=peer.messages,
            rejected=peer.rejected,
            pcm_bytes=peer.messages * MESSAGE_PCM,
        )
    emit("result", passed=True)


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(target())
    except BaseException as exc:
        emit("failure", reason=str(exc) if isinstance(exc, Failed) else type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
