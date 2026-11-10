"""Short development smoke checks. Explicit invocation only, without Docker or cgroups."""

import asyncio
import contextlib
import runpy
import select
import ssl
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import aiohttp
import pytest

from evaluations.linux_resource import l2_buffering as l2
from evaluations.linux_resource import l2_peer
from evaluations.linux_resource.l2_run import passed_pair
from sobafm import crash_dumps


def test_peak_limit_includes_native_record_and_original_partitions() -> None:
    assert l2.GROUP + 2 * l2.BANK_BYTES + 2 * l2.PRODUCER_COPIES + l2.SHARED_COPIES == 98_979_840
    for count in (0, 1, 2):
        limit = count * (l2.BANK_BYTES + l2.PRODUCER_COPIES - l2.NATIVE_RECORD) + l2.SHARED_COPIES
        l2.check_peak(limit, count)
        with pytest.raises(l2.Bound, match="copy_budget"):
            l2.check_peak(limit + 1, count)


def test_cgroup_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(l2, "CGROUP", tmp_path)
    (tmp_path / "memory.peak").write_text(str(l2.GROUP))
    (tmp_path / "memory.events").write_text("max 0\noom 0\noom_kill 0\n")
    (tmp_path / "pids.current").write_text("2")
    (tmp_path / "pids.events").write_text("max 0\n")
    resources = l2.Resources()
    resources.check()
    assert resources.memory_peak == l2.GROUP
    (tmp_path / "memory.events").write_text("max 1\noom 0\noom_kill 0\n")
    with pytest.raises(l2.Bound, match="cgroup_pressure"):
        resources.check()
    assert resources.memory_events["max"] == 1


type Endpoint = tuple[ssl.SSLContext, int]


@pytest.fixture
def identity(tmp_path: Path) -> tuple[Path, Path]:
    cert, key = tmp_path / "fixture.crt", tmp_path / "fixture.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-days", "1", "-subj", "/CN=loopback", "-addext", "subjectAltName=IP:127.0.0.1",
         "-out", str(cert), "-keyout", str(key)],
        capture_output=True, check=True, timeout=10,
    )  # fmt: skip
    return cert, key


@pytest.fixture
def endpoint(identity: tuple[Path, Path]) -> Iterator[Endpoint]:
    cert, key = identity
    with subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).with_name("l2_peer.py")), str(cert), str(key),
         "--port", "0"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as peer:  # fmt: skip
        try:
            assert peer.stdout is not None and select.select([peer.stdout], [], [], 5)[0]
            ready, port = peer.stdout.readline().split()
            assert ready == b"ready"
            yield ssl.create_default_context(cafile=str(cert)), int(port)
        finally:
            peer.terminate()
            peer.wait(timeout=5)


def test_peer_normal_lifetime(identity: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    sleep = asyncio.sleep

    async def expire(delay: float) -> None:
        await sleep(0 if delay == 120 else delay)

    monkeypatch.setattr(asyncio, "sleep", expire)
    asyncio.run(asyncio.wait_for(l2_peer.serve(*identity, 0), 5))


def test_crash_control_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse() -> None:
        raise crash_dumps.CrashDumpError("private fixture detail")

    monkeypatch.setattr(crash_dumps, "disable_crash_dumps", refuse)
    with pytest.raises(SystemExit) as result:
        runpy.run_path(l2.__file__, run_name="__main__")
    assert result.value.code == 1
    assert capsys.readouterr().out.strip() == '{"client":"failed","reason":"crash_controls"}'


def test_tls_renewal_and_stop_on_measured_failure(endpoint: Endpoint) -> None:
    async def smoke() -> None:
        good = l2.Pair(1)
        await l2.run_pair(good, *endpoint, lambda: None, frame_interval=0.02)
        assert good.failure is None, good
        assert (good.sessions, good.crossfades, good.mixed_frames) == (2, 1, 200)
        assert len(good.cleanup_ms) == 2
        assert good.leased_after_finish == good.retained_banks == good.failed_cleanups == 0
        counters = l2.Resources(
            memory_peak=50_000_000,
            pids_peak_sampled=2,
            memory_events={"max": 0, "oom": 0, "oom_kill": 0},
        )
        assert passed_pair(asdict(good) | asdict(counters), 1)
        failed = l2.Pair(2)

        def trip() -> None:
            if failed.mixed_frames:
                raise l2.Bound("copy_budget")

        await l2.run_pair(failed, *endpoint, trip, frame_interval=0)
        assert failed.failure == "copy_budget"
        assert failed.mixed_frames == 1
        assert failed.leased_after_finish == failed.retained_banks == failed.failed_cleanups == 0
        assert len(failed.cleanup_ms) == 2

    asyncio.run(asyncio.wait_for(smoke(), 55))


def test_one_bank_peak_during_final_close(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    close = aiohttp.ClientSession.close
    closes = 0

    async def oversized_close(session: aiohttp.ClientSession) -> None:
        nonlocal closes
        await close(session)
        closes += 1
        if closes == 2:
            oversized = bytearray(3_500_000)
            del oversized

    monkeypatch.setattr(aiohttp.ClientSession, "close", oversized_close)
    result = l2.Pair(1)
    asyncio.run(
        asyncio.wait_for(l2.run_pair(result, *endpoint, lambda: None, frame_interval=0), 25)
    )
    assert result.failure == "copy_budget"
    assert result.leased_after_finish == 0


def test_cancelled_owner_joins_final_close(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    close = aiohttp.ClientSession.close

    async def schedule() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        closes = 0

        async def held(session: aiohttp.ClientSession) -> None:
            nonlocal closes
            closes += 1
            if closes == 2:
                entered.set()
                await release.wait()
            await close(session)

        monkeypatch.setattr(aiohttp.ClientSession, "close", held)
        result = l2.Pair(1)
        owner = asyncio.create_task(l2.run_pair(result, *endpoint, lambda: None, frame_interval=0))
        try:
            await entered.wait()
            for _ in range(2):
                owner.cancel()
                await asyncio.sleep(0)
                assert not owner.done()
        finally:
            release.set()
            with contextlib.suppress(asyncio.CancelledError):
                await owner
        assert owner.cancelled() and result.failure == "cancelled"
        assert result.sessions == 2 and result.leased_after_finish == 0
        assert len(result.cleanup_ms) == 2

    asyncio.run(asyncio.wait_for(schedule(), 25))


def test_failed_pair_retains_late_releases_and_final_counter(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    close = aiohttp.ClientSession.close
    monkeypatch.setattr(l2, "CGROUP", tmp_path)
    (tmp_path / "memory.peak").write_text("1000")
    (tmp_path / "memory.events").write_text("max 0")
    (tmp_path / "pids.current").write_text("2")
    (tmp_path / "pids.events").write_text("max 0")
    resources = l2.Resources()

    async def schedule() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        closes = 0

        async def held(session: aiohttp.ClientSession) -> None:
            nonlocal closes
            closes += 1
            if closes == 2:
                entered.set()
            await release.wait()
            await close(session)

        monkeypatch.setattr(aiohttp.ClientSession, "close", held)
        result = l2.Pair(1)

        def trip() -> None:
            resources.check()
            if result.mixed_frames:
                raise l2.Bound("copy_budget")

        owner = asyncio.create_task(l2.run_pair(result, *endpoint, trip, frame_interval=0))
        try:
            await entered.wait()
            await asyncio.sleep(l2.CLEANUP_SECONDS + 0.05)
            (tmp_path / "memory.peak").write_text("2000")
            (tmp_path / "memory.events").write_text("max 1")
        finally:
            release.set()
            await owner
        assert result.failure == "copy_budget"
        assert result.failed_cleanups == 2 and result.leased_after_finish == 0
        assert len(result.cleanup_ms) == 2
        assert all(ms is not None and ms > 3000 for ms in result.cleanup_ms)
        assert resources.memory_peak == 2000 and resources.memory_events["max"] == 1

    asyncio.run(asyncio.wait_for(schedule(), 25))
