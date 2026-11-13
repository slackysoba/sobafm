"""One-shot host supervisor. No Docker call occurs on import or in ordinary tests."""

from __future__ import annotations

import hashlib
import json
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import FrameType
from typing import Any, cast

# pragma: allowlist nextline secret - public candidate source commit
SOURCE = "fed87387b7ea8ab9eff2d516ad9d425d18b50f71"
IMAGE = "sha256:ae44556ff603f47e52d000cb7cc7f9b33ed289d0e3b176da582fbf16c99d35cc"
# pragma: allowlist nextline secret - published media SHA-256
FIXTURE_SHA256 = "ab0cf8ce5dbb75c072bda2cf9f8ae6cda261cdf7da505eee1e825011a00f5159"
# pragma: allowlist nextline secret - accepted catalog metadata digest
METADATA_DIGEST = "cb818dc6747032d514e1d73cfc7a9f537915c96f56f4bb76eb3e25393fc8e3ff"
LABEL = "org.sobafm.three-station"
WORK_SECONDS = 120
CLEANUP_SECONDS = 3
MAX_OUTPUT = 32_768
DIRECTORY = "evaluations/three_station"


class Refused(Exception):
    """Fixed safe reason codes, not subprocess output."""


def emit(event: str, **facts: object) -> None:
    print(json.dumps({"event": event, **facts}, allow_nan=False), flush=True)


def command(args: list[str], deadline: float, seconds: float = 5) -> bytes:
    remaining = min(seconds, deadline - time.monotonic())
    if remaining <= 0:
        raise Refused("deadline")
    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=remaining,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise Refused("command_timeout") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Refused("command_failed") from exc
    if len(result.stdout) > MAX_OUTPUT:
        raise Refused("output_limit")
    return result.stdout


def records(raw: bytes) -> list[dict[str, Any]]:
    """Accept bounded sanitized probe records only, including partial failure evidence."""
    if len(raw) > MAX_OUTPUT or len(raw.splitlines()) > 16:
        raise Refused("output_limit")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise Refused("duplicate_field")
        return result

    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            value = json.loads(line, object_pairs_hook=unique)
        except (ValueError, RecursionError) as exc:
            raise Refused("invalid_json") from exc
        if not isinstance(value, dict):
            raise Refused("record_type")
        row = cast(dict[str, Any], value)
        if len(row) > 16:
            raise Refused("record_fields")
        for key, item in row.items():
            if not re.fullmatch(r"[a-z_]{1,32}", key):
                raise Refused("field_name")
            if isinstance(item, str):
                if not re.fullmatch(r"[a-zA-Z0-9._+-]{1,128}", item):
                    raise Refused("unsafe_text")
            elif type(item) in (int, float):
                if not 0 <= item <= 2**63:
                    raise Refused("unsafe_number")
            elif type(item) is not bool and item is not None:
                raise Refused("unsafe_value")
        rows.append(row)
    return rows


def complete(rows: list[dict[str, Any]]) -> bool:
    if [r.get("event") for r in rows] != [
        "target",
        "load",
        "cleanup",
        "load",
        "cleanup",
        "load",
        "cleanup",
        "debt",
        "debt_released",
        "resources",
        "peer",
        "result",
    ]:
        return False
    for index, name, native, sessions in (
        (1, "catalog", 6, 0),
        (3, "generated", 0, 6),
        (5, "mixed", 4, 2),
    ):
        row, cleanup = rows[index : index + 2]
        if not (
            row.get("case") == name
            and row.get("peak_slots") == 6
            and row.get("native_slots") == native
            and row.get("frames") == 600
            and row.get("fourth_refused") is True
            and cleanup.get("case") == name
            and cleanup.get("clean") is True
            and cleanup.get("banks_left") == 0
            and cleanup.get("reservations_left") == 0
            and cleanup.get("sessions") == sessions
            and cleanup.get("failed_cleanups") == 0
            and type(cleanup.get("cleanup_ms")) in (int, float)
            and 0 <= cleanup["cleanup_ms"] <= 3000
            and cleanup.get("native_settled") == native
            and cleanup.get("crossfades") == sessions // 2
            and cleanup.get("stream_failure") is None
            and type(row.get("ready_ms")) in (int, float)
            and 0 <= row["ready_ms"] <= 15_000
        ):
            return False
    return (
        rows[0].get("group") == 268_435_456
        and rows[0].get("pcm") == 13_824_000
        and rows[0].get("copies") == 12_582_912
        and rows[7].get("expired") is True
        and rows[7].get("retained_banks") == 1
        and rows[7].get("fourth_refused") is True
        and rows[8].get("expired_still") is True
        and rows[8].get("banks_left") == 0
        and rows[8].get("reservations_left") == 0
        and rows[8].get("restart_admitted") is True
        and type(rows[9].get("memory_peak")) is int
        and 0 < rows[9]["memory_peak"] <= 268_435_456
        and type(rows[9].get("tasks_peak_sampled")) is int
        and 0 < rows[9]["tasks_peak_sampled"] <= 64
        and rows[10]
        == {"event": "peer", "requests": 8, "messages": 64, "rejected": 0, "pcm_bytes": 24_576_000}
        and rows[11] == {"event": "result", "passed": True}
    )


def create_args(name: str, probe: Path, fixture: Path) -> list[str]:
    return [
        "docker",
        "create",
        "--name",
        name,
        "--label",
        f"{LABEL}={name}",
        "--pull",
        "never",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--read-only",
        "--user",
        "1000:1000",
        "--init",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cgroupns",
        "private",
        "--memory",
        "268435456",
        "--memory-swap",
        "268435456",
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
        "max-size=32k",
        "--log-opt",
        "max-file=1",
        "--log-opt",
        "compress=false",
        "--mount",
        f"type=bind,src={probe},dst=/probe.py,readonly",
        "--mount",
        f"type=bind,src={fixture},dst=/fixture.mp3,readonly",
        "--entrypoint",
        "/app/.venv/bin/python",
        IMAGE,
        "-I",
        "-B",
        "/probe.py",
    ]


def admitted(fixture: Path) -> bool:
    """Check the existing local acceptance and exact media identity, without mutation."""
    root = fixture.parent.parent
    documents: list[dict[str, Any]] = []
    for name in ("reviews.json", "media.json"):
        with (root / name).open("rb") as source:
            raw = source.read(2_097_153)
        if len(raw) > 2_097_152:
            return False
        document = json.loads(raw)
        if not isinstance(document, dict):
            return False
        documents.append(cast(dict[str, Any], document))
    key = {"source": "fma", "recording_id": "9423"}

    def matching(document: dict[str, Any], field: str) -> list[dict[str, Any]]:
        values: object = document.get(field)
        if not isinstance(values, list):
            return []
        return [
            cast(dict[str, Any], r)
            for r in cast(list[object], values)
            if isinstance(r, dict) and cast(dict[str, Any], r).get("key") == key
        ]

    reviews = matching(documents[0], "reviews")
    media = matching(documents[1], "recordings")
    return (
        len(reviews) == len(media) == 1
        and reviews[0].get("metadata_digest") == METADATA_DIGEST
        and reviews[0].get("credits_complete") is True
        and reviews[0].get("access_ready") is True
        and reviews[0].get("synthetic") is False
        and media[0]
        == {
            "key": key,
            "file": "fma/9423.mp3",
            "size": 1_793_243,
            "sha256": FIXTURE_SHA256,
            "max_pcm_bytes": 21_699_840,
        }
    )


class Run:
    def __init__(self) -> None:
        self.name = "sobafm-three-station-" + uuid.uuid4().hex
        self.deadline = time.monotonic() + WORK_SECONDS
        self.create_attempted = False
        self.create_returned = False
        self.cleaning = False
        self.interrupted = False
        self.exit_status: int | None = None
        self.oom: bool | None = None
        self.valid = False
        self.cleanup_deadline: float | None = None

    def interrupt(self, signum: int, frame: FrameType | None) -> None:
        self.interrupted = True
        if not self.cleaning:
            raise Refused("interrupted")

    def call(self, *args: str, seconds: float = 5) -> bytes:
        return command(list(args), self.deadline, seconds)

    def work(self, harness: str, fixture: Path) -> None:
        root = Path(__file__).resolve().parents[2]
        for file in ("run.py", "probe.py", "README.md"):
            committed = self.call("git", "-C", str(root), "show", f"{harness}:{DIRECTORY}/{file}")
            if committed != (root / DIRECTORY / file).read_bytes():
                raise Refused("harness_mismatch")
        if (
            fixture.name != "9423.mp3"
            or fixture.is_symlink()
            or not fixture.is_file()
            or fixture.stat().st_size != 1_793_243
        ):
            raise Refused("fixture_size")
        if not admitted(fixture):
            raise Refused("fixture_admission")
        with fixture.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != FIXTURE_SHA256:
                raise Refused("fixture_digest")
        identity = (
            self.call(
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Os}}/{{.Architecture}} {{.Id}} "
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                IMAGE,
            )
            .decode()
            .strip()
        )
        if identity != f"linux/amd64 {IMAGE} {SOURCE}":
            raise Refused("image_binding")
        target = self.call("docker", "info", "--format", "{{.OSType}}/{{.Architecture}}")
        if target.strip() != b"linux/x86_64":
            raise Refused("native_target")
        emit(
            "binding",
            harness=harness,
            source=SOURCE,
            image=IMAGE,
            fixture_sha256=FIXTURE_SHA256,
            run=self.name,
        )
        args = create_args(self.name, root / DIRECTORY / "probe.py", fixture.resolve())
        emit("command", sha256=hashlib.sha256(json.dumps(args).encode()).hexdigest())
        self.create_attempted = True  # retain the unique name even if create times out
        identifier = self.call(*args).decode().strip()
        if not re.fullmatch(r"[a-f0-9]{64}", identifier):
            raise Refused("create_identity")
        self.create_returned = True
        self.call("docker", "start", self.name)
        status = self.call("docker", "wait", self.name, seconds=100).strip()
        if not re.fullmatch(rb"[0-9]{1,3}", status):
            raise Refused("exit_status")
        self.exit_status = int(status)

    def cleanup(self) -> bool:
        self.cleaning = True
        # One cleanup clock, including observation, evidence, stop, removal and absence.
        if self.cleanup_deadline is None:
            self.cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
        self.deadline = self.cleanup_deadline
        if not self.create_attempted:
            return True
        found = (
            self.call("docker", "ps", "-aq", "--no-trunc", "--filter", f"name=^/{self.name}$")
            .decode()
            .strip()
        )
        if not found:
            return False  # an interrupted create can still finish later; retain the handle
        if not re.fullmatch(r"[a-f0-9]{64}", found):
            raise Refused("cleanup_identity")
        state = (
            self.call(
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "' + LABEL + '"}} {{.State.Running}} {{.State.OOMKilled}}',
                found,
            )
            .decode()
            .strip()
            .split()
        )
        if len(state) != 3 or state[0] != self.name or state[1] not in ("true", "false"):
            raise Refused("cleanup_owner")
        self.oom = state[2] == "true" if state[2] in ("true", "false") else None
        if state[1] == "true":
            self.exit_status = None
            self.call("docker", "stop", "--time", "1", found, seconds=2)
        try:
            rows = records(self.call("docker", "logs", "--tail", "17", found))
            for row in rows:
                emit("probe", record=row)
            self.valid = complete(rows)
        except Refused:
            self.valid = False
        self.call("docker", "rm", found)
        absent = not self.call(
            "docker", "ps", "-aq", "--no-trunc", "--filter", f"name=^/{self.name}$"
        ).strip()
        return absent and self.create_returned and time.monotonic() <= self.cleanup_deadline


def main(argv: list[str]) -> int:
    if len(argv) != 2 or re.fullmatch(r"[a-f0-9]{40}", argv[0]) is None:
        emit("result", passed=False, reason="arguments")
        return 1
    run = Run()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, run.interrupt)
    reason: str | None = None
    clean = False
    try:
        run.work(argv[0], Path(argv[1]))
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, Refused) else type(exc).__name__
    finally:
        try:
            clean = run.cleanup()
        except BaseException:
            reason = reason or "cleanup_debt"
    passed = (
        reason is None
        and clean
        and run.valid
        and run.exit_status == 0
        and run.oom is False
        and not run.interrupted
    )
    emit(
        "result",
        passed=passed,
        reason=reason,
        cleanup_verified=clean,
        exit_status=run.exit_status,
        oom=run.oom,
        complete_evidence=run.valid,
        interrupted=run.interrupted,
        run=run.name,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
