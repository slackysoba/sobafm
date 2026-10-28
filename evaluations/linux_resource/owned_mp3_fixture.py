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
from pathlib import Path

from .decoder_startup import CHILD_ENV, Child, cleanup
from .probe import Identity, ProbeError, emit, proc_fields, read_text, validate_identity

SAMPLE_FRAMES = 5_760_000
RAW_BYTES = SAMPLE_FRAMES * 4
CHUNK_BYTES = 65_536
FIXTURE_BYTES = 2_500_000
STDERR_BYTES = 65_536
ENCODE_SECONDS = 60.0
CLEANUP_SECONDS = 3.0
# Public installed-binary digest, not a credential.
FFMPEG_SHA256 = (
    "e8a8d46f5225f3062cec7c07fb145d58ae73c603cb740dcd5bad34bfb54e455a"  # pragma: allowlist secret
)
FIXTURE = Path("/fixtures/owned-cbr-48000-stereo-120s.mp3")
ARGV = (
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
    "48000",
    "-ac",
    "2",
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
    "-b:a",
    "128000",
    "-flags:a",
    "+bitexact",
    "-write_xing",
    "1",
    "-id3v2_version",
    "0",
    "-f",
    "mp3",
    str(FIXTURE),
)


def fill_pcm(first_frame: int, buffer: bytearray) -> int:
    """Fill the caller-owned buffer; integer triangles require no random or floating state."""
    if not 0 <= first_frame <= SAMPLE_FRAMES or len(buffer) != CHUNK_BYTES:
        raise ValueError("invalid fixed generator position or buffer")
    frames = min(CHUNK_BYTES // 4, SAMPLE_FRAMES - first_frame)
    for offset in range(frames):
        frame = first_frame + offset
        left_phase, right_phase = frame % 200, frame % 300
        left = min(left_phase, 200 - left_phase) * 160 - 8000
        right = min(right_phase, 300 - right_phase) * 100 - 7500
        struct.pack_into("<hh", buffer, offset * 4, left, right)
    return frames * 4


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


def exec_encoder(stdin: int, stderr: int, ready: int, gate: int, fds: list[int]) -> None:
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
        os.execve(ARGV[0], ARGV, CHILD_ENV)
    except BaseException:
        with suppress(OSError):
            os.write(ready, b"E")
        os._exit(125)


def generate_fixture() -> dict[str, object]:
    if FIXTURE.exists() or not FIXTURE.parent.is_dir():
        raise ProbeError("one fresh fixture destination is required")
    if len(list(Path("/proc/self/task").iterdir())) != 1:
        raise ProbeError("generator must be single-threaded")
    with Path(ARGV[0]).open("rb") as executable:
        if hashlib.file_digest(executable, "sha256").hexdigest() != FFMPEG_SHA256:
            raise ProbeError("encoder differs from reviewed installed binding")
    in_r, in_w = os.pipe()
    err_r, err_w = os.pipe()
    ready_r, ready_w = os.pipe()
    gate_r, gate_w = os.pipe()
    fds = [in_r, in_w, err_r, err_w, ready_r, ready_w, gate_r, gate_w]
    child: Child | None = None
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
        pid = os.fork()
        if pid == 0:
            exec_encoder(in_r, err_w, ready_w, gate_r, fds)
            os._exit(125)
        child = Child(pid)
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
            if child.exit_code is not None and accepted != RAW_BYTES:
                raise ProbeError("encoder exited before accepting all owned PCM")
            if input_open and offset == count:
                count = fill_pcm(generated // 4, buffer)
                offset = 0
                if count:
                    raw_hash.update(view[:count])
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
                    written = os.write(in_w, view[offset:count])
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
        for fd in fds:
            with suppress(OSError):
                os.close(fd)
        if child is not None:
            try:
                result = cleanup(child, cleanup_deadline)
                retained, deadline_met = result.retained_pids, result.deadline_met
                debt = child.exit_code is None or bool(retained)
            except (OSError, ValueError, ProbeError) as error:
                failure = failure or f"cleanup observation failed: {error}"
    size = FIXTURE.stat().st_size if FIXTURE.exists() else 0
    fixture_hash: str | None = None
    passed = (
        failure is None
        and released
        and not exec_failed
        and child is not None
        and child.exit_code == 0
        and not debt
        and deadline_met
        and not stderr
        and accepted == generated == RAW_BYTES
        and 0 < size <= FIXTURE_BYTES
    )
    if passed:
        with FIXTURE.open("rb") as fixture:
            fixture_hash = hashlib.file_digest(fixture, "sha256").hexdigest()
    return {
        "event": "owned_fixture_result",
        "verdict": "fixture_generated" if passed else "incomplete",
        "failure": failure,
        "argv": list(ARGV),
        "encoder_sha256": FFMPEG_SHA256,
        "exec_gate_released": released,
        "exec_failed": exec_failed,
        "raw_bytes_generated": generated,
        "raw_bytes_accepted": accepted,
        "raw_sha256": raw_hash.hexdigest(),
        "expected_sample_frames": SAMPLE_FRAMES,
        "fixture_name": FIXTURE.name,
        "fixture_bytes": size,
        "fixture_sha256": fixture_hash,
        "exit_code": child.exit_code if child is not None else None,
        "leader_reaped": child is not None and child.exit_code is not None,
        "retained_pids": retained,
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
    parser.parse_args()
    try:
        result = generate_fixture()
        emit(result)
        return 0 if result["verdict"] == "fixture_generated" else 1
    except (OSError, ProbeError, ValueError) as error:
        emit({"event": "owned_fixture_incomplete", "error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
