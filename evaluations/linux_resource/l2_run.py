"""Host owner for one separately authorized L2 run. Uses only the Python standard library."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import FrameType
from typing import Any, TypeGuard, cast

DIR = "evaluations/linux_resource"
GROUP = 90_177_536
BANK = 2_304_000
PRIVATE = 1_572_864
SHARED = 1_048_576
RECORD = 16_709
LABEL = "org.sobafm.l2-run"
WORK_SECONDS = 120
CLEANUP_SECONDS = 30


class Refused(Exception):
    """Only fixed reason codes reach retained output."""


def emit(record: dict[str, Any]) -> None:
    print(json.dumps(record, allow_nan=False, separators=(",", ":")), flush=True)


def command(args: list[str], deadline: float, seconds: float = 5) -> bytes:
    remaining = min(deadline - time.monotonic(), seconds)
    if remaining <= 0:
        raise Refused("deadline")
    try:
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=remaining,
            check=True,
        ).stdout
    except subprocess.TimeoutExpired as exc:
        raise Refused("command_timeout") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Refused("command_failed") from exc


def integer(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def numbers(
    value: object, ceilings: list[int], *, milliseconds: bool = False
) -> TypeGuard[list[Any]]:
    if not isinstance(value, list) or len(cast(list[object], value)) != len(ceilings):
        return False
    return all(
        (type(n) in (int, float) and 0 <= n <= cap) if milliseconds else integer(n, 0, cap)
        for n, cap in zip(cast(list[Any], value), ceilings, strict=True)
    )


def passed_pair(row: dict[str, Any], index: int) -> bool:
    peaks = row.get("traced_peaks")
    buffers = row.get("buffered_bytes")
    events = row.get("memory_events")
    ceilings = [SHARED + owners * (BANK + PRIVATE - RECORD) for owners in range(3)]
    return (
        row.keys()
        == {
            "pair",
            "failure",
            "cancelled",
            "sessions",
            "crossfades",
            "mixed_frames",
            "ready_ms",
            "buffered_bytes",
            "traced_peaks",
            "traced_after_join",
            "cleanup_ms",
            "failed_cleanups",
            "retained_banks",
            "leased_after_finish",
            "memory_peak",
            "pids_peak_sampled",
            "memory_events",
            "pids_max_events",
        }
        and integer(row.get("pair"), index, index)
        and row["failure"] is None
        and row.get("cancelled") is False
        and integer(row.get("sessions"), 2, 2)
        and integer(row.get("crossfades"), 1, 1)
        and integer(row.get("mixed_frames"), 200, 200)
        and numbers([row.get("ready_ms")], [22_000], milliseconds=True)
        and numbers(buffers, [BANK, BANK])
        and buffers[0] >= 1_916_160
        and buffers[1] >= 1_920_000
        and numbers(peaks, ceilings)
        and peaks[1] >= BANK
        and peaks[2] >= 2 * BANK
        and integer(row.get("traced_after_join"), 0, peaks[0])
        and numbers(row.get("cleanup_ms"), [3000, 3000], milliseconds=True)
        and all(
            integer(row.get(key), 0, 0)
            for key in (
                "failed_cleanups",
                "retained_banks",
                "leased_after_finish",
                "pids_max_events",
            )
        )
        and integer(row.get("memory_peak"), 1, GROUP)
        and integer(row.get("pids_peak_sampled"), 1, 64)
        and isinstance(events, dict)
        and {"max", "oom", "oom_kill"} <= cast(dict[str, Any], events).keys()
        and all(integer(count, 0, 0) for count in cast(dict[str, Any], events).values())
    )


def evidence(output: bytes, kernel: str) -> bool:
    """Retain bounded numeric evidence and safe failure codes, including partial runs."""
    if len(output) > 16_384:
        return False
    lines = output.splitlines()
    if len(lines) > 8:
        return False

    def unique(fields: list[tuple[str, Any]]) -> dict[str, Any]:
        values = dict(fields)
        if len(fields) != len(values):
            raise ValueError("duplicate_field")
        return values

    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line, object_pairs_hook=unique)
        except ValueError, RecursionError:
            return False
        if not isinstance(row, dict):
            return False
        record = cast(dict[str, Any], row)
        # These two reviewed scripts emit numeric facts, enum codes and target identity.
        # Keep already parsed rows on truncation; never copy arbitrary log lines.
        if not safe_record(record):
            return False
        emit({"client_record": record})
        rows.append(record)
    expected = {
        "case": "L2",
        "arch": "x86_64",
        "kernel": kernel,
        "page_bytes": 4096,
        "memory_max": GROUP,
        "swap_max": 0,
        "pids_max": 64,
        "cpus": 2,
        "pcm": 2 * BANK,
        "copies": 2 * PRIVATE + SHARED,
    }
    return (
        len(rows) == 7
        and rows[0] == expected
        and all(type(rows[0][key]) is type(value) for key, value in expected.items())
        and all(passed_pair(rows[i], i) for i in range(1, 6))
        and rows[-1] == {"L2": "pass", "sessions": 10, "renewals": 5}
        and integer(rows[-1]["sessions"], 10, 10)
        and integer(rows[-1]["renewals"], 5, 5)
    )


def safe_record(record: dict[str, Any]) -> bool:
    numeric = {
        "page_bytes",
        "memory_max",
        "swap_max",
        "pids_max",
        "cpus",
        "pcm",
        "copies",
        "pair",
        "sessions",
        "crossfades",
        "mixed_frames",
        "ready_ms",
        "traced_after_join",
        "failed_cleanups",
        "retained_banks",
        "leased_after_finish",
        "memory_peak",
        "pids_peak_sampled",
        "pids_max_events",
        "leased_before_join",
        "renewals",
    }
    for key, value in record.items():
        if key in numeric:
            if value is not None and not (type(value) in (int, float) and 0 <= value <= 2**63):
                return False
        elif key in {"traced_peaks", "cleanup_ms", "buffered_bytes"}:
            if (
                not isinstance(value, list)
                or len(cast(list[Any], value)) > 3
                or any(
                    n is not None and not (type(n) in (int, float) and 0 <= n <= 2**63)
                    for n in cast(list[Any], value)
                )
            ):
                return False
        elif key == "memory_events":
            if not isinstance(value, dict) or not all(
                re.fullmatch(r"[a-z_]{1,32}", k) and integer(n, 0, 2**63)
                for k, n in cast(dict[str, Any], value).items()
            ):
                return False
        elif key == "cancelled":
            if type(value) is not bool:
                return False
        elif key in {"case", "arch", "kernel", "failure", "L2", "client", "reason"}:
            if value is not None and not (
                isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9._+-]{1,128}", value)
            ):
                return False
        else:
            return False
    return True


class Run:
    def __init__(self, harness: str, source: str, image: str, kernel: str) -> None:
        self.harness, self.source, self.image, self.kernel = harness, source, image, kernel
        self.deadline = time.monotonic() + WORK_SECONDS
        self.run_id = "sobafm-l2-" + uuid.uuid4().hex
        self.tmp = Path(tempfile.gettempdir()) / self.run_id
        self.peer, self.client = self.run_id + "-peer", self.run_id + "-client"
        self.recovery = self.tmp / "recovery.json"
        self.uncertain_creates: set[str] = set()
        self.status: int | None = None
        self.oom: bool | None = None
        self.valid = False
        self.cleaning = False
        self.interrupted = False
        self.owns_directory = False

    def interrupt(self, signum: int, frame: FrameType | None) -> None:
        first = not self.interrupted
        self.interrupted = True
        if first and not self.cleaning:
            raise Refused("interrupted")

    def docker(self, *args: str, seconds: float = 5) -> bytes:
        return command(["docker", *args], self.deadline, seconds)

    def start(self, name: str, args: list[str], entry: list[str]) -> None:
        common = [
            "--name",
            name,
            "--label",
            f"{LABEL}={self.run_id}",
            "--pull",
            "never",
            "--platform",
            "linux/amd64",
            "--user",
            "1000:1000",
            "--read-only",
            "--init",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cgroupns",
            "private",
            "--pids-limit",
            "64",
            "--cpus",
            "2",
            "--ulimit",
            "core=0:0",
            "--restart",
            "no",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=64k",
            "--log-opt",
            "max-file=1",
            "--log-opt",
            "compress=false",
            "--entrypoint",
            "/app/.venv/bin/python",
        ]
        for file in ("fixture.crt", *entry):
            common += ["--mount", f"type=bind,src={self.tmp / file},dst=/{file},readonly"]
        self.uncertain_creates.add(name)
        self.docker(
            "create",
            *common,
            *args,
            self.image,
            "-I",
            "/" + entry[0],
            *(["/fixture.crt", "/fixture.key"] if name == self.peer else []),
        )
        self.uncertain_creates.remove(name)
        self.docker("start", name)

    def work(self) -> None:
        def git(*args: str) -> bytes:
            return command(["git", *args], self.deadline)

        for file in ("run-l2-buffering.sh", "l2_run.py"):
            if git("show", f"{self.harness}:{DIR}/{file}") != (Path(DIR) / file).read_bytes():
                raise Refused("harness")
        if git("rev-parse", self.source + "^{commit}").decode().strip() != self.source:
            raise Refused("source")
        expected = f"linux/amd64 {self.image}".encode()
        if (
            self.docker(
                "image", "inspect", "--format", "{{.Os}}/{{.Architecture}} {{.Id}}", self.image
            ).strip()
            != expected
        ):
            raise Refused("image")
        if (
            self.docker(
                "info", "--format", "{{.OSType}}/{{.Architecture}} {{.KernelVersion}}"
            ).strip()
            != f"linux/x86_64 {self.kernel}".encode()
        ):
            raise Refused("target")
        manifest = {
            "case": "L2",
            "harness": self.harness,
            "source": self.source,
            "image": self.image,
            "lock_sha256": hashlib.sha256(git("show", self.source + ":uv.lock")).hexdigest(),
            "kernel": self.kernel,
            "run": self.run_id,
            "peer": self.peer,
            "client": self.client,
        }
        self.tmp.mkdir(mode=0o700)
        self.owns_directory = True
        self.recovery.write_text(json.dumps(manifest))
        emit(manifest)
        for file in ("l2_peer.py", "l2_buffering.py"):
            (self.tmp / file).write_bytes(git("show", f"{self.harness}:{DIR}/{file}"))
        command(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=loopback",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
                "-out",
                str(self.tmp / "fixture.crt"),
                "-keyout",
                str(self.tmp / "fixture.key"),
            ],
            self.deadline,
        )
        for file in ("l2_peer.py", "l2_buffering.py", "fixture.crt", "fixture.key"):
            (self.tmp / file).chmod(0o444)
        self.start(
            self.peer,
            ["--network", "none", "--memory", "134217728", "--memory-swap", "134217728"],
            ["l2_peer.py", "fixture.key"],
        )
        for _ in range(50):
            if self.docker("logs", "--tail", "1", self.peer).strip() == b"ready 8765":
                break
            time.sleep(0.1)
        else:
            raise Refused("peer_start")
        self.start(
            self.client,
            [
                "--network",
                "container:" + self.peer,
                "--memory",
                str(GROUP),
                "--memory-swap",
                str(GROUP),
            ],
            ["l2_buffering.py"],
        )
        status = self.docker("wait", self.client, seconds=90).strip()
        if not re.fullmatch(rb"[0-9]{1,3}", status):
            raise Refused("wait_status")
        self.status = int(status)

    def find(self, name: str) -> str:
        found = (
            self.docker(
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                f"name=^/{name}$",
                "--format",
                "{{.ID}}",
                seconds=2,
            )
            .decode()
            .strip()
        )
        if found and not re.fullmatch(r"[a-f0-9]{64}", found):
            raise Refused("lookup")
        return found

    def remove(self, name: str) -> bool:
        identifier = self.find(name)
        if not identifier:
            # The daemon may still be processing an interrupted create. Preserve its handle.
            return name not in self.uncertain_creates
        state = self.docker(
            "inspect",
            "--format",
            '{{index .Config.Labels "' + LABEL + '"}} {{.State.Running}}',
            identifier,
            seconds=2,
        ).split()
        if (
            len(state) != 2
            or state[0] != self.run_id.encode()
            or state[1] not in (b"true", b"false")
        ):
            return False
        if state[1] == b"true":
            if name == self.client:
                self.status = None  # stopping a running client cannot confirm a workload exit
            self.docker("stop", "--time", "1", identifier, seconds=3)
        if name == self.client:
            try:
                self.valid = evidence(
                    self.docker("logs", "--tail", "9", identifier, seconds=2), self.kernel
                )
                oom = self.docker(
                    "inspect", "--format", "{{.State.OOMKilled}}", identifier, seconds=2
                )
                if oom.strip() in (b"true", b"false"):
                    self.oom = oom.strip() == b"true"
            except Refused, ValueError, OSError:
                self.valid = False
        self.docker("rm", identifier, seconds=2)
        return not self.find(name)

    def cleanup(self) -> bool:
        self.cleaning = True
        if not self.owns_directory:
            return True
        self.deadline = time.monotonic() + CLEANUP_SECONDS
        removed = True
        for name in (self.client, self.peer):
            try:
                if not self.remove(name):
                    removed = False
            except Refused, ValueError, OSError:
                removed = False
        if removed:
            for file in ("fixture.key", "fixture.crt", "l2_peer.py", "l2_buffering.py"):
                (self.tmp / file).unlink(missing_ok=True)
            self.recovery.unlink(missing_ok=True)
            self.tmp.rmdir()
        return removed


def main() -> int:
    if len(sys.argv) != 5 or not all(
        re.fullmatch(pattern, value)
        for pattern, value in zip(
            (r"[0-9a-f]{40}", r"[0-9a-f]{40}", r"sha256:[0-9a-f]{64}", r"[a-zA-Z0-9._+-]{1,128}"),
            sys.argv[1:],
            strict=True,
        )
    ):
        emit({"L2": "failed", "reason": "identity", "cleanup_verified": True})
        return 1
    os.chdir(Path(__file__).resolve().parents[2])
    run = Run(*sys.argv[1:])
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, run.interrupt)
    reason = None
    clean = False
    try:
        run.work()
    except Refused as exc:
        reason = str(exc)
    except OSError, ValueError:
        reason = "setup_or_io"
    finally:
        try:
            clean = run.cleanup()
        except OSError:
            reason = reason or "fixture_cleanup"
    passed = (
        reason is None
        and not run.interrupted
        and clean
        and run.valid
        and run.status == 0
        and run.oom is False
    )
    emit(
        {
            "L2": "pass" if passed else "failed",
            "reason": reason,
            "client_exit": run.status,
            "oom_killed": run.oom,
            "complete_evidence": run.valid,
            "cleanup_verified": clean,
            "interrupted": run.interrupted,
            "run": run.run_id,
        }
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
