"""Short development smoke checks. Explicit invocation only, without Docker or cgroups."""

import asyncio
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

from evaluations.linux_resource import l2_buffering as l2


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


def test_tls_renewal_and_stop_on_measured_failure(tmp_path: Path) -> None:
    cert, key = tmp_path / "fixture.crt", tmp_path / "fixture.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-days", "1", "-subj", "/CN=loopback", "-addext", "subjectAltName=IP:127.0.0.1",
         "-out", str(cert), "-keyout", str(key)],
        capture_output=True, check=True, timeout=10,
    )  # fmt: skip

    async def smoke() -> None:
        peer = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(Path(__file__).with_name("l2_peer.py")),
            str(cert), str(key), "--port", "0", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        try:
            assert peer.stdout is not None
            ready, port = (await peer.stdout.readline()).split()
            assert ready == b"ready"
            tls = ssl.create_default_context(cafile=str(cert))
            good = l2.Pair(1)
            await l2.run_pair(good, tls, int(port), lambda: None, frame_interval=0)
            assert good.failure is None, good
            assert (good.sessions, good.crossfades, good.mixed_frames) == (2, 1, 200)
            assert len(good.cleanup_ms) == 2
            assert good.leased_after_finish == good.retained_banks == good.failed_cleanups == 0
            assert all(
                peak > count * l2.BANK_BYTES for count, peak in enumerate(good.traced_peaks, 1)
            )
            failed = l2.Pair(2)

            def trip() -> None:
                if failed.mixed_frames:
                    raise l2.Bound("copy_budget")

            await l2.run_pair(failed, tls, int(port), trip, frame_interval=0)
            assert failed.failure == "copy_budget"
            assert failed.mixed_frames == 1
            assert (
                failed.leased_after_finish == failed.retained_banks == failed.failed_cleanups == 0
            )
            assert len(failed.cleanup_ms) == 2
        finally:
            if peer.returncode is None:
                peer.terminate()
            await peer.wait()

    async def bounded() -> None:
        async with asyncio.timeout(55):
            await smoke()

    asyncio.run(bounded())
