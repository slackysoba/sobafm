"""Opt-in, no-input FFmpeg startup check; never a PCM or Q3 qualification."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import resource
import select
import signal
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .probe import Identity, ProbeError, emit, observe, proc_fields, read_text, validate_identity

DATA_BYTES = 12_000_000
STACK_BYTES = 524_288
OUTPUT_BYTES = 16_384  # Each stream; encoded result still fits emit's 64 KiB limit.
STARTUP_SECONDS = 10.0
CLEANUP_SECONDS = 3.0
EXECUTABLE = "/usr/bin/ffmpeg"
ARGV = [EXECUTABLE, "-nostdin", "-version"]
CHILD_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/nonexistent"}
VERSION_PREFIX = b"ffmpeg version 7.1.5-0+deb13u1 "


def verify_rlimits(text: str) -> dict[str, int]:
    expected = {"Max data size": DATA_BYTES, "Max stack size": STACK_BYTES}
    found: dict[str, int] = {}
    for line in text.splitlines():
        for name, value in expected.items():
            if line.startswith(name):
                fields = line[len(name) :].split()
                if name in found or fields != [str(value), str(value), "bytes"]:
                    raise ProbeError("decoder soft/hard limits differ from the reviewed values")
                found[name] = value
    if found != expected:
        raise ProbeError("decoder resource-limit evidence is missing")
    return found


def verify_child(pid: int) -> dict[str, int]:
    root = Path(f"/proc/{pid}")
    status = proc_fields(read_text(root / "status"))
    group = read_text(root / "cgroup").strip()
    if group != "0::/" or status["NoNewPrivs"].strip() != "1" or os.getpgid(pid) != pid:
        raise ProbeError("decoder launch containment differs from the reviewed configuration")
    item = Identity(
        pid,
        int(status["PPid"]),
        int(status["Uid"].split()[1]),
        int(status["Gid"].split()[1]),
        "/",
        int(status["Seccomp"]),
        int(status["CapEff"], 16),
    )
    validate_identity(item, "/")
    return verify_rlimits(read_text(root / "limits"))


def group_members(pgid: int) -> list[int]:
    members: list[int] = []
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        try:
            # comm may contain spaces/parentheses; pgrp is field 5 after its final ')'.
            fields = read_text(path / "stat").rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid:
                members.append(int(path.name))
        except FileNotFoundError:
            continue
    return sorted(members)


@dataclass
class Child:
    pid: int
    exit_code: int | None = None

    def reap(self) -> None:
        if self.exit_code is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.exit_code = os.waitstatus_to_exitcode(status)


@dataclass(frozen=True)
class CleanupResult:
    deadline_met: bool
    retained_pids: list[int]


def cleanup(child: Child, deadline: float) -> CleanupResult:
    """One original cleanup deadline; an unsuccessful release stays debt."""
    child.reap()
    remaining = group_members(child.pid)
    if child.exit_code is None:
        with suppress(ProcessLookupError):
            os.kill(child.pid, signal.SIGTERM)
    if remaining:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
            os.killpg(child.pid, signal.SIGCONT)
    kill_at = min(deadline, time.monotonic() + 1.0)
    killed = False
    while time.monotonic() < deadline:
        child.reap()
        remaining = group_members(child.pid)
        observed_at = time.monotonic()
        if child.exit_code is not None and not remaining:
            return CleanupResult(observed_at <= deadline, [])
        if observed_at >= deadline:
            break
        if not killed and observed_at >= kill_at:
            if child.exit_code is None:
                with suppress(ProcessLookupError):
                    os.kill(child.pid, signal.SIGKILL)
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            killed = True
        time.sleep(0.01)  # Bounded release observation, not a race-inducing test schedule.
    child.reap()
    return CleanupResult(False, group_members(child.pid))


def child_exec(ready: int, gate: int, out: int, err: int, close_fds: list[int]) -> None:
    """Only the single-threaded fork child changes limits; no preexec callback."""
    try:
        os.setsid()
        null = os.open("/dev/null", os.O_RDONLY)
        os.dup2(null, 0)
        os.dup2(out, 1)
        os.dup2(err, 2)
        os.close(null)
        for fd in close_fds:
            if fd not in {ready, gate}:
                os.close(fd)
        resource.setrlimit(resource.RLIMIT_STACK, (STACK_BYTES, STACK_BYTES))
        resource.setrlimit(resource.RLIMIT_DATA, (DATA_BYTES, DATA_BYTES))
        # The parent reads this child's actual /proc limits while exec is gated.
        os.write(ready, b"R")
        if os.read(gate, 1) != b"x":
            os._exit(125)
        os.close(gate)
        os.execve(EXECUTABLE, ARGV, CHILD_ENV)
    except BaseException:
        # Setting a limit below the launcher's existing mappings does not reclaim
        # them. Failure here is setup/exec failure, not a decoder fit measurement.
        with suppress(OSError):
            os.write(ready, b"E")
        os._exit(125)


def capture(child: Child, streams: dict[int, bytearray], deadline: float) -> None:
    active = set(streams)
    while active or child.exit_code is None:
        child.reap()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError("startup deadline exceeded")
        readable, _, _ = select.select(list(active), [], [], min(remaining, 0.05))
        for fd in readable:
            part = os.read(fd, min(4096, OUTPUT_BYTES - len(streams[fd]) + 1))
            if not part:
                active.remove(fd)
                continue
            room = OUTPUT_BYTES - len(streams[fd])
            streams[fd].extend(part[:room])
            if len(part) > room:
                raise ProbeError("startup output exceeded its stream bound")


def run_startup(expected_digest: str) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ProbeError("an approved executable SHA256 is required")
    if len(list(Path("/proc/self/task").iterdir())) != 1:
        raise ProbeError("the fork launcher must have exactly one thread")
    with Path(EXECUTABLE).open("rb") as executable:
        digest = hashlib.file_digest(executable, "sha256").hexdigest()
    if digest != expected_digest:
        raise ProbeError("installed executable differs from the approved supply receipt")

    ready_r, ready_w = os.pipe()
    gate_r, gate_w = os.pipe()
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    fds = [ready_r, ready_w, gate_r, gate_w, out_r, out_w, err_r, err_w]
    child: Child | None = None
    stdout, stderr = bytearray(), bytearray()
    limits: dict[str, int] = {}
    failure: str | None = None
    released = False
    exec_failed = False
    debt = True
    cleanup_deadline_met = False
    retained: list[int] = []
    deadline = time.monotonic() + STARTUP_SECONDS
    try:
        pid = os.fork()
        if pid == 0:
            child_exec(ready_w, gate_r, out_w, err_w, fds)
            os._exit(125)
        child = Child(pid)
        for fd in (ready_w, gate_r, out_w, err_w):
            os.close(fd)
            fds.remove(fd)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([ready_r], [], [], remaining)[0]:
            raise ProbeError("limit-setup handshake timed out")
        if os.read(ready_r, 1) != b"R":
            raise ProbeError("limit setup failed before decoder exec")
        limits = verify_child(pid)
        emit({"event": "startup_exec_gate", "pid": pid, "rlimits": limits, "input": "none"})
        if os.write(gate_w, b"x") != 1:
            raise ProbeError("short exec-gate write")
        released = True
        os.close(gate_w)
        fds.remove(gate_w)
        capture(child, {out_r: stdout, err_r: stderr}, deadline)
        # close-on-exec pipe: an E distinguishes launcher/exec failure from FFmpeg.
        if not select.select([ready_r], [], [], 0)[0]:
            raise ProbeError("exec acknowledgement pipe remains owned")
        exec_failed = os.read(ready_r, 2) != b""
    except (OSError, ProbeError, ValueError) as error:
        failure = str(error)
    finally:
        if child is not None:
            cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
            try:
                release = cleanup(child, cleanup_deadline)
                retained = release.retained_pids
                cleanup_deadline_met = release.deadline_met
                debt = child.exit_code is None or bool(retained)
                if not cleanup_deadline_met:
                    failure = failure or "cleanup deadline exceeded; later release is not a pass"
            except (OSError, ValueError, ProbeError) as error:
                failure = failure or f"cleanup observation failed: {error}"
        for fd in fds:
            with suppress(OSError):
                os.close(fd)

    exit_code = child.exit_code if child is not None else None
    valid_version = stdout.startswith(VERSION_PREFIX) and b"configuration:" in stdout
    valid_version = valid_version and b"--enable-nonfree" not in stdout
    if debt or failure or not released or exec_failed:
        verdict = "incomplete"
    elif exit_code != 0:
        verdict = "startup_rejected"
    elif not valid_version:
        verdict = "incomplete"
        failure = "unexpected version/build output"
    else:
        verdict = "startup_only_pass"
    return {
        "event": "startup_result",
        "verdict": verdict,
        "failure": failure,
        "exec_gate_released": released,
        "exec_failed": exec_failed,
        "ffmpeg_sha256": digest,
        "rlimits": limits,
        "exit_code": exit_code,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "leader_reaped": child is not None and child.exit_code is not None,
        "retained_pids": retained,
        "cleanup_debt": debt,
        "cleanup_deadline_met": cleanup_deadline_met,
        "q3": "not_run: aggregate native PCM coverage unresolved",
        "q4": "not_run",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-owned-startup", action="store_true", required=True)
    parser.add_argument("--expected-ffmpeg-sha256", required=True)
    args = parser.parse_args()
    try:
        emit({"event": "startup_preflight", "observation": observe()})
        result = run_startup(str(args.expected_ffmpeg_sha256))
        emit(result)
        return 0 if result["verdict"] == "startup_only_pass" else 1
    except (OSError, ProbeError, ValueError) as error:
        emit({"event": "startup_incomplete", "error": str(error), "q3": "not_run"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
