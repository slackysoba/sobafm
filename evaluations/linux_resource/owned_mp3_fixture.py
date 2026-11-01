"""Opt-in generation of one fixed, owned MP3 fixture; not a runtime-fit test."""

from __future__ import annotations

import argparse
import hashlib
import os
import resource
import select
import struct
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .decoder_startup import CHILD_ENV, Child, cleanup
from .mp3_vbr_contract import inspect_vbr_fixture
from .probe import Identity, ProbeError, emit, proc_fields, read_text, validate_identity

CHUNK_BYTES = 65_536
FIXTURE_BYTES = 2_500_000
STDERR_BYTES = 65_536
ENCODE_SECONDS = 60.0
CLEANUP_SECONDS = 3.0
# Public installed-binary digest, not a credential.
FFMPEG_SHA256 = (
    "e8a8d46f5225f3062cec7c07fb145d58ae73c603cb740dcd5bad34bfb54e455a"  # pragma: allowlist secret
)


@dataclass(frozen=True)
class FixtureRecipe:
    rate: int
    channels: Literal[1, 2]
    vbr: bool
    path: Path

    @property
    def sample_frames(self) -> int:
        return self.rate * 120

    @property
    def raw_bytes(self) -> int:
        return self.sample_frames * self.channels * 2


CBR = FixtureRecipe(48000, 2, False, Path("/fixtures/owned-cbr-48000-stereo-120s.mp3"))
VBR = FixtureRecipe(44100, 1, True, Path("/fixtures/owned-vbr-44100-mono-120s.mp3"))
RECIPES = {"cbr-48000-stereo": CBR, "vbr-44100-mono": VBR}
SAMPLE_FRAMES = CBR.sample_frames
RAW_BYTES = CBR.raw_bytes


def encoder_argv(recipe: FixtureRecipe) -> tuple[str, ...]:
    if recipe not in (CBR, VBR):
        raise ValueError("only the two reviewed owned fixture recipes are supported")
    return (
        "/usr/bin/ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-fflags",
        "+bitexact",
        "-f",
        "s16le",
        "-ar",
        str(recipe.rate),
        "-ac",
        str(recipe.channels),
        "-i",
        "pipe:0",
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-c:a",
        "libmp3lame",
        "-threads:a",
        "1",
        *(("-q:a", "4") if recipe.vbr else ("-b:a", "128000")),
        "-flags:a",
        "+bitexact",
        "-write_xing",
        "1",
        "-id3v2_version",
        "0",
        "-f",
        "mp3",
        str(recipe.path),
    )


def fill_pcm(first_frame: int, buffer: bytearray, recipe: FixtureRecipe = CBR) -> int:
    """Fill the caller-owned buffer; integer triangles require no random or floating state."""
    if recipe not in (CBR, VBR):
        raise ValueError("unreviewed fixture recipe")
    if not 0 <= first_frame <= recipe.sample_frames or len(buffer) != CHUNK_BYTES:
        raise ValueError("invalid fixed generator position or buffer")
    frame_bytes = recipe.channels * 2
    frames = min(CHUNK_BYTES // frame_bytes, recipe.sample_frames - first_frame)
    for offset in range(frames):
        frame = first_frame + offset
        if recipe == VBR:
            # Four fixed five-second tone segments exercise varying encoder demand.
            period = (100, 137, 200, 333)[frame // (recipe.rate * 5) % 4]
            phase = frame % period
            sample = min(phase, period - phase) * 32000 // period - 8000
            struct.pack_into("<h", buffer, offset * 2, sample)
        else:
            left_phase, right_phase = frame % 200, frame % 300
            left = min(left_phase, 200 - left_phase) * 160 - 8000
            right = min(right_phase, 300 - right_phase) * 100 - 7500
            struct.pack_into("<hh", buffer, offset * 4, left, right)
    return frames * frame_bytes


def verify_file_limits(text: str) -> dict[str, int]:
    wanted = {"Max file size": FIXTURE_BYTES, "Max core file size": 0}
    found: dict[str, int] = {}
    for line in text.splitlines():
        for name, value in wanted.items():
            if line.startswith(name):
                if name in found or line[len(name) :].split() != [str(value), str(value), "bytes"]:
                    raise ProbeError("encoder hard/soft file limits differ")
                found[name] = value
    if found != wanted:
        raise ProbeError("encoder file-limit evidence missing")
    return found


def verify_encoder(pid: int) -> dict[str, int]:
    root = Path(f"/proc/{pid}")
    status = proc_fields(read_text(root / "status"))
    if read_text(root / "cgroup").strip() != "0::/" or os.getpgid(pid) != pid:
        raise ProbeError("encoder group identity differs")
    if status["NoNewPrivs"].strip() != "1":
        raise ProbeError("encoder no-new-privileges missing")
    validate_identity(
        Identity(
            pid,
            int(status["PPid"]),
            int(status["Uid"].split()[1]),
            int(status["Gid"].split()[1]),
            "/",
            int(status["Seccomp"]),
            int(status["CapEff"], 16),
        ),
        "/",
    )
    for name, value in {
        "memory.max": "536870912",
        "memory.swap.max": "0",
        "pids.max": "64",
        "cpu.max": "200000 100000",
    }.items():
        if read_text(Path("/sys/fs/cgroup") / name).strip() != value:
            raise ProbeError("development group limits differ")
    return verify_file_limits(read_text(root / "limits"))


def exec_encoder(
    stdin: int, stderr: int, ready: int, gate: int, fds: list[int], argv: tuple[str, ...]
) -> None:
    try:
        os.setsid()
        null = os.open("/dev/null", os.O_WRONLY)
        os.dup2(stdin, 0)
        os.dup2(null, 1)
        os.dup2(stderr, 2)
        os.close(null)
        for fd in fds:
            if fd not in {ready, gate}:
                os.close(fd)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (FIXTURE_BYTES, FIXTURE_BYTES))
        os.write(ready, b"R")
        if os.read(gate, 1) != b"x":
            os._exit(125)
        os.close(gate)
        os.execve(argv[0], argv, CHILD_ENV)
    except BaseException:
        with suppress(OSError):
            os.write(ready, b"E")
        os._exit(125)


def generate_fixture(recipe: FixtureRecipe = CBR) -> dict[str, object]:
    argv = encoder_argv(recipe)
    fixture = recipe.path
    if fixture.exists() or not fixture.parent.is_dir():
        raise ProbeError("one fresh fixture destination is required")
    if len(list(Path("/proc/self/task").iterdir())) != 1:
        raise ProbeError("generator must be single-threaded")
    with Path(argv[0]).open("rb") as executable:
        if hashlib.file_digest(executable, "sha256").hexdigest() != FFMPEG_SHA256:
            raise ProbeError("encoder differs from reviewed installed binding")
    fds: list[int] = []
    child = Child(0)
    buffer: bytearray | None = None
    view: memoryview | None = None
    stderr = bytearray()
    raw_hash = hashlib.sha256()
    accepted = generated = 0
    failure: str | None = None
    released = exec_failed = False
    debt = True
    deadline_met = False
    retained: list[int] = []
    started = time.monotonic()
    deadline = started + ENCODE_SECONDS
    try:
        pipes: list[tuple[int, int]] = []
        for _ in range(4):
            pair = os.pipe()
            fds.extend(pair)
            pipes.append(pair)
        (in_r, in_w), (err_r, err_w), (ready_r, ready_w), (gate_r, gate_w) = pipes
        pid = os.fork()
        if pid == 0:
            exec_encoder(in_r, err_w, ready_w, gate_r, fds, argv)
            os._exit(125)
        child.pid = pid
        for fd in (in_r, err_w, ready_w, gate_r):
            os.close(fd)
            fds.remove(fd)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([ready_r], [], [], remaining)[0]:
            raise ProbeError("encoder setup deadline exceeded")
        if os.read(ready_r, 1) != b"R":
            raise ProbeError("encoder setup failed")
        limits = verify_encoder(pid)
        emit({"event": "fixture_exec_gate", "rlimits": limits, "input": "owned_integer_pcm"})
        if os.write(gate_w, b"x") != 1:
            raise ProbeError("short encoder exec-gate write")
        released = True
        os.close(gate_w)
        fds.remove(gate_w)
        os.set_blocking(in_w, False)
        os.set_blocking(err_r, False)
        buffer = bytearray(CHUNK_BYTES)
        view = memoryview(buffer)
        count = offset = 0
        input_open = diagnostics_open = True
        while input_open or diagnostics_open or child.exit_code is None:
            child.reap()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError("encoding deadline exceeded")
            if child.exit_code is not None and accepted != recipe.raw_bytes:
                raise ProbeError("encoder exited before accepting all owned PCM")
            if input_open and offset == count:
                count = fill_pcm(generated // (recipe.channels * 2), buffer, recipe)
                offset = 0
                if count:
                    with view[:count] as payload:
                        raw_hash.update(payload)
                    generated += count
                else:
                    os.close(in_w)
                    fds.remove(in_w)
                    input_open = False
            readable, writable, _ = select.select(
                [err_r] if diagnostics_open else [],
                [in_w] if input_open else [],
                [],
                min(remaining, 0.05),
            )
            if err_r in readable:
                part = os.read(err_r, min(4096, STDERR_BYTES - len(stderr) + 1))
                if not part:
                    diagnostics_open = False
                room = STDERR_BYTES - len(stderr)
                stderr.extend(part[:room])
                if len(part) > room:
                    raise ProbeError("encoder diagnostics exceeded bound")
            if in_w in writable:
                try:
                    with view[offset:count] as payload:
                        written = os.write(in_w, payload)
                except BlockingIOError:
                    continue
                if written <= 0:
                    raise ProbeError("encoder input write made no progress")
                offset += written
                accepted += written
        exec_failed = os.read(ready_r, 2) != b""
    except (OSError, ProbeError, ValueError) as error:
        failure = str(error)
    finally:
        cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
        # Pipe closure, signals and reaping share this original cleanup deadline.
        for fd in tuple(fds):
            try:
                os.close(fd)
                fds.remove(fd)
            except OSError as error:
                failure = failure or f"encoder descriptor release failed: {error}"
        debt = bool(fds)
        if child.pid > 0:
            try:
                result = cleanup(child, cleanup_deadline)
                retained, deadline_met = result.retained_pids, result.deadline_met
                debt = debt or child.exit_code is None or bool(retained)
            except (OSError, ValueError, ProbeError) as error:
                debt = True
                failure = failure or f"cleanup observation failed: {error}"
        else:
            deadline_met = True
        if view is not None:
            view.release()
            view = None
        buffer = None
        deadline_met = deadline_met and time.monotonic() <= cleanup_deadline and not debt
    size = fixture.stat().st_size if fixture.exists() else 0
    fixture_hash: str | None = None
    header_shape: dict[str, object] | None = None
    passed = (
        failure is None
        and released
        and not exec_failed
        and child.pid > 0
        and child.exit_code == 0
        and not debt
        and deadline_met
        and not stderr
        and accepted == generated == recipe.raw_bytes
        and 0 < size <= FIXTURE_BYTES
    )
    if passed:
        try:
            with fixture.open("rb") as stream:
                compressed = stream.read(FIXTURE_BYTES + 1)
            if len(compressed) != size:
                raise ProbeError("owned fixture changed after encoder completion")
            if recipe == VBR:
                header_shape = asdict(inspect_vbr_fixture(compressed))
            fixture_hash = hashlib.sha256(compressed).hexdigest()
        except (OSError, ProbeError, ValueError) as error:
            passed = False
            failure = str(error)
    return {
        "event": "owned_fixture_result",
        "verdict": "fixture_generated" if passed else "incomplete",
        "failure": failure,
        "argv": list(argv),
        "encoder_sha256": FFMPEG_SHA256,
        "exec_gate_released": released,
        "exec_failed": exec_failed,
        "raw_bytes_generated": generated,
        "raw_bytes_accepted": accepted,
        "raw_sha256": raw_hash.hexdigest(),
        "expected_sample_frames": recipe.sample_frames,
        "sample_rate": recipe.rate,
        "channels": recipe.channels,
        "encoding": "vbr_quality_4" if recipe.vbr else "cbr_128k",
        "header_shape": header_shape,
        "fixture_name": fixture.name,
        "fixture_bytes": size,
        "fixture_sha256": fixture_hash,
        "exit_code": child.exit_code,
        "leader_reaped": child.pid > 0 and child.exit_code is not None,
        "retained_pids": retained,
        "retained_fds": fds,
        "parent_payload_views_released": view is None and buffer is None,
        "cleanup_debt": debt,
        "cleanup_deadline_met": deadline_met,
        "stderr_bytes": len(stderr),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "stderr_preview": stderr[:4096].decode("utf-8", errors="replace"),
        "elapsed_seconds": time.monotonic() - started,
        "runtime_fit": "not_tested",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-owned-fixture-generation", action="store_true", required=True)
    parser.add_argument("--fixture", choices=tuple(RECIPES), default="cbr-48000-stereo")
    args = parser.parse_args()
    try:
        result = generate_fixture(RECIPES[args.fixture])
        emit(result)
        return 0 if result["verdict"] == "fixture_generated" else 1
    except (OSError, ProbeError, ValueError) as error:
        emit({"event": "owned_fixture_incomplete", "error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
