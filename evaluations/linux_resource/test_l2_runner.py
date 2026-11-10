"""Bounded host-runner schedules. Every Docker command resolves to the local double."""

import json
import os
import select
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).parent.resolve()
IMAGE = "sha256:" + "0" * 64
SUMMARY = '{"L2":"pass","sessions":10,"renewals":5}'


def evidence() -> list[str]:
    manifest = {
        "case": "L2",
        "arch": "x86_64",
        "kernel": "test-kernel",
        "page_bytes": 4096,
        "memory_max": 90_177_536,
        "swap_max": 0,
        "pids_max": 64,
        "cpus": 2,
        "pcm": 4_608_000,
        "copies": 4_194_304,
    }
    rows = [json.dumps(manifest)]
    for pair in range(1, 6):
        rows.append(
            json.dumps(
                {
                    "pair": pair,
                    "failure": None,
                    "cancelled": False,
                    "sessions": 2,
                    "crossfades": 1,
                    "mixed_frames": 200,
                    "ready_ms": 50.0,
                    "buffered_bytes": [1_916_160, 1_920_000],
                    "traced_peaks": [50_000, 3_800_000, 6_400_000],
                    "traced_after_join": 40_000,
                    "cleanup_ms": [3.0, 2.0],
                    "failed_cleanups": 0,
                    "retained_banks": 0,
                    "leased_after_finish": 0,
                    "memory_peak": 70_000_000,
                    "pids_peak_sampled": 3,
                    "memory_events": {"max": 0, "oom": 0, "oom_kill": 0},
                    "pids_max_events": 0,
                }
            )
        )
    return [*rows, SUMMARY]


@dataclass
class Runner:
    cmd: list[str]
    repo: Path
    state: Path
    env: dict[str, str]

    def run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.cmd, cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=12
        )


@pytest.fixture
def runner(tmp_path: Path) -> Iterator[Runner]:
    repo = tmp_path / "repo"
    files = repo / "evaluations/linux_resource"
    files.mkdir(parents=True)
    for name in ("run-l2-buffering.sh", "l2_buffering.py", "l2_peer.py", "l2_run.py"):
        if (HERE / name).exists():
            shutil.copyfile(HERE / name, files / name)
    (repo / "uv.lock").write_text("offline fixture")
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    state = tmp_path / "state"
    state.mkdir()
    (state / "mode").write_text("normal")
    (state / "output").write_text("\n".join(evidence()) + "\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    shutil.copyfile(HERE / "l2_docker_double.sh", docker)
    docker.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # No service environment or user configuration needed by the double.
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "TMPDIR": str(scratch),
        "L2_DOUBLE": str(state),
    }
    cmd = ["bash", str(files / "run-l2-buffering.sh"), head, head, IMAGE, "test-kernel"]
    yield Runner(cmd, repo, state, env)


@pytest.mark.parametrize(
    "case",
    [
        "summary",
        "truncated",
        "failed_row",
        "kernel",
        "debt",
        "caps",
        "release",
        "order",
        "copy_peak",
    ],
)
def test_incomplete_or_inconsistent_evidence_is_rejected(runner: Runner, case: str) -> None:
    rows = evidence()
    match case:
        case "summary":
            rows = [rows[-1]]
        case "truncated":
            rows = [*rows[:2], rows[-1]]
        case "failed_row":
            rows[1] = rows[1].replace('"failure": null', '"failure": "copy_budget"')
        case "kernel":
            rows[0] = rows[0].replace("test-kernel", "other-kernel")
        case "debt":
            rows[1] = rows[1].replace('"retained_banks": 0', '"retained_banks": 1')
        case "caps":
            rows[0] = rows[0].replace("90177536", "99000000")
        case "release":
            rows[1] = rows[1].replace("3.0, 2.0", "3001.0, 2.0")
        case "order":
            rows[2] = rows[1]
        case "copy_peak":
            rows[1] = rows[1].replace("3800000", "5888169")
        case _:
            raise AssertionError(case)
    (runner.state / "output").write_text("\n".join(rows) + "\n")
    run = runner.run()
    assert run.returncode != 0
    assert json.loads(run.stdout.splitlines()[-1])["L2"] == "failed"


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "bad-rm",
        "ambiguous",
        "oom",
        "client-fails",
        "wait-fails",
        "wrong-image",
        "wrong-target",
    ],
)
def test_wrapper_verdict_after_cleanup(runner: Runner, mode: str) -> None:
    (runner.state / "mode").write_text(mode)
    run = runner.run()
    verdict = json.loads(run.stdout.splitlines()[-1])
    assert (run.returncode == 0) == (mode == "normal")
    assert verdict["L2"] == ("pass" if mode == "normal" else "failed")
    unconfirmed = mode in ("bad-rm", "ambiguous")
    assert verdict["cleanup_verified"] == (not unconfirmed)
    manifests = list(Path(runner.env["TMPDIR"]).glob("*/recovery.json"))
    assert len(manifests) == int(unconfirmed)
    if manifests:
        names = json.loads(manifests[0].read_text())
        assert (
            names["client"] == names["run"] + "-client" and names["peer"] == names["run"] + "-peer"
        )
        if mode == "bad-rm":
            assert (runner.state / ("2".zfill(64)) / "name").read_text().strip() == names["client"]


@pytest.mark.parametrize("mode", ["lost-peer", "lost-client", "wait"])
def test_interrupt_reconciles_names_and_wait_process(runner: Runner, mode: str) -> None:
    (runner.state / "mode").write_text(mode)
    marker = runner.state / "blocked"
    with subprocess.Popen(
        runner.cmd,
        cwd=runner.repo,
        env=runner.env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        wait_fd = None
        try:
            deadline = time.monotonic() + 6
            while not marker.exists() or not marker.read_text().strip().isdigit():
                assert time.monotonic() < deadline and process.poll() is None
                time.sleep(0.01)
            wait_fd = os.pidfd_open(int(marker.read_text()))
            if mode == "wait":
                process.terminate()  # even a signal delivered only to the owner interrupts wait
            else:
                os.killpg(process.pid, signal.SIGTERM)
            output, _ = process.communicate(timeout=3)
            assert process.returncode != 0
            assert not list(runner.state.glob("*/name"))
            assert not list(Path(runner.env["TMPDIR"]).iterdir())
            assert select.select([wait_fd], [], [], 0)[0]
            verdict = json.loads(output.splitlines()[-1])
            assert verdict["L2"] == "failed" and verdict["cleanup_verified"] is True
        finally:
            # Target only this exact double even when testing an escaped wait process.
            if wait_fd is not None:
                try:
                    signal.pidfd_send_signal(wait_fd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    os.close(wait_fd)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
