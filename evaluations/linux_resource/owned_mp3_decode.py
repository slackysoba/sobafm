"""Opt-in one-fixture decoder proof; no source media, playback or complete Q3 claim."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import resource
import select
import signal
import stat
import time
from contextlib import suppress
from pathlib import Path

from .decoder_startup import (
    ADDRESS_SPACE_BYTES,
    CHILD_ENV,
    DATA_BYTES,
    STACK_BYTES,
    Child,
    cleanup,
    group_members,
    verify_child,
)
from .probe import COPY_BYTES, PCM_BYTES, ProbeError, counters, emit, observe, read_text

FIXTURE = Path("/fixtures/owned-cbr-48000-stereo-120s.mp3")
FIXTURE_BYTES = 1_920_768
FIXTURE_SHA256 = (
    "1a431c03098d8be79a522a3b1f64a8182445a8bf6c009f54c1b3bb1f6dad044d"  # pragma: allowlist secret
)
PCM_OUTPUT_BYTES = 23_040_000
READY_BYTES = 1_152_000
BANK_BYTES = PCM_BYTES // 2
CHUNK_BYTES = 65_536
OUTPUT_COPY_OFFSET = 2_000_000
STDERR_BYTES = 65_536
PREPARE_SECONDS = 15.0
WORKLOAD_SECONDS = 150.0
CLEANUP_SECONDS = 3.0
ARGV = (
    "/usr/bin/mpg123",
    "--no-control",
    "--no-visual",
    "--buffer",
    "0",
    "--stdout",
    "--rate",
    "48000",
    "--stereo",
    "--encoding",
    "s16",
    "--resample",
    "ntom",
    "--no-resync",
    "-",
)
# Existing native-main output, before any stream input; exact 1.33.7 source is cited in the plan.
BANNER = (
    b"High Performance MPEG 1.0/2.0/2.5 Audio Player for Layers 1, 2 and 3\n"
    b"\tversion 1.33.7; written and copyright by Michael Hipp and others\n"
    b"\tfree software (LGPL) without any warranty but with best wishes\n"
)
# Public installed-file identities from the independently reviewed immutable image binding.
MAPPED_ELF = {
    "/usr/bin/mpg123": (
        # pragma: allowlist nextline secret
        "62ef82208722eda0fedd39de0ced0d8e3a8450d129760152ea16d075651f0a91"
    ),
    "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2": (
        # pragma: allowlist nextline secret
        "c8438e4fde1934e61c88311633f00949ff645d5c04cdb8671fa3d78164d2f307"
    ),
    "/usr/lib/x86_64-linux-gnu/libc.so.6": (
        # pragma: allowlist nextline secret
        "9792e3cbb541c8f44c7acf5f14f4022ea62998ecc787d326bed4d8b6547dfd92"
    ),
    "/usr/lib/x86_64-linux-gnu/libm.so.6": (
        # pragma: allowlist nextline secret
        "6d567d53e895273ca14a1f9dc164fc6c8d39aed2f60aa46a733c2784228915f3"
    ),
    "/usr/lib/x86_64-linux-gnu/libmpg123.so.0.49.4": (
        # pragma: allowlist nextline secret
        "aa51a519374ae04a75914f3d011db1b3c7a99d089443e6d25043055465c050c8"
    ),
    "/usr/lib/x86_64-linux-gnu/libout123.so.0.5.2": (
        # pragma: allowlist nextline secret
        "38af2e5aa2cf16f9230b1de9434dc92bc80b27bc0d2b22e5588435b2330a5a93"
    ),
    "/usr/lib/x86_64-linux-gnu/libsyn123.so.0.2.3": (
        # pragma: allowlist nextline secret
        "16b29c3d037522390e8d71c3b8904c90fb760509284d9279f3dd775b8a8afa55"
    ),
}
SPECIAL_MAPS = {"[vdso]", "[vvar]", "[vvar_vclock]", "[vsyscall]"}


def verify_core_limit(text: str) -> None:
    rows = [
        line[len("Max core file size") :].split()
        for line in text.splitlines()
        if line.startswith("Max core file size")
    ]
    if rows != [["0", "0", "bytes"]]:
        raise ProbeError("decoder hard/soft core limits differ")


def parse_maps(text: str) -> dict[str, object]:
    """Closed file/backing scope; observed lengths are evidence, not a sampled PCM bound."""
    records: list[dict[str, object]] = []
    files: set[str] = set()
    ordinary = special = 0
    previous_end = 0
    for line in text.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 5:
            raise ProbeError("incomplete native mapping record")
        address, perms, offset, device, inode = fields[:5]
        name = fields[5] if len(fields) == 6 else ""
        if (
            not re.fullmatch(r"[0-9a-f]+-[0-9a-f]+", address)
            or not re.fullmatch(r"[r-][w-][x-]p", perms)
            or not re.fullmatch(r"[0-9a-f]+", offset)
            or not re.fullmatch(r"[0-9a-f]+:[0-9a-f]+", device)
            or not inode.isdecimal()
        ):
            raise ProbeError("unexpected mapping syntax or shared backing")
        start, end = (int(value, 16) for value in address.split("-"))
        size = end - start
        if (
            size <= 0
            or start < previous_end
            or start % 4096
            or end % 4096
            or ("w" in perms and "x" in perms)
        ):
            raise ProbeError("unexpected mapping extent or writable executable mapping")
        previous_end = end
        if name in SPECIAL_MAPS:
            if "w" in perms or int(inode) != 0:
                raise ProbeError("kernel special mapping differs from non-PCM scope")
            special += size
        else:
            ordinary += size
            if name in MAPPED_ELF:
                if int(inode) == 0:
                    raise ProbeError("mapped ELF lacks a file identity")
                files.add(name)
            elif name in {"", "[heap]", "[stack]"}:
                if int(inode) != 0 or "x" in perms:
                    raise ProbeError("unexpected anonymous mapping identity or execution")
            else:
                raise ProbeError("native mapping lies outside the reviewed file/backing scope")
        records.append({"path": name or "anonymous", "permissions": perms, "bytes": size})
    if files != set(MAPPED_ELF) or not records or ordinary > ADDRESS_SPACE_BYTES:
        raise ProbeError("native mapping inventory or ordinary extent differs")
    return {
        "records": records,
        "ordinary_bytes_observed": ordinary,
        "special_bytes_observed": special,
        "maps_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "special_scope": "kernel code/time only; no numeric PCM allowance",
        "sampled_pcm_proof": False,
    }


def validate_diagnostics(data: bytes) -> None:
    if not data.startswith(BANNER):
        raise ProbeError("native banner differs")
    counts = {"directory": 0, "encoding": 0, "playing": 0, "format": 0, "complete": 0}
    for line in data[len(BANNER) :].decode("ascii").splitlines():
        if not line:
            continue
        if line.startswith("Directory: ") and len(line) <= 256:
            counts["directory"] += 1
        elif line == "Note: forcing output encoding s16":
            counts["encoding"] += 1
        elif line == "Playing MPEG stream 1 of 1: - ...":
            counts["playing"] += 1
        # This fixture's Info extension has method 0, which mpg123 labels VBR.
        # Its independently inspected frame headers remain constant 128 kbit/s.
        elif line == "MPEG 1.0 L III vbr 48000 j-s":
            counts["format"] += 1
        elif re.fullmatch(r"\[[0-9]+:[0-5][0-9]\] Decoding of - finished\.", line):
            counts["complete"] += 1
        else:
            raise ProbeError("unexpected native diagnostic; preserve without accepting completion")
    if counts["directory"] > 1 or any(
        counts[name] != 1 for name in ("encoding", "playing", "format", "complete")
    ):
        raise ProbeError("missing or repeated native format/completion evidence")


def read_start_identity(pid: int) -> tuple[str, int]:
    fields = read_text(Path(f"/proc/{pid}/stat")).rsplit(")", 1)[1].split()
    if len(fields) <= 19 or not fields[19].isdecimal():
        raise ProbeError("incomplete native start identity")
    return fields[0], int(fields[19])


def native_processes() -> set[int]:
    rows = read_text(Path("/sys/fs/cgroup/cgroup.procs")).splitlines()
    if any(not row.isdecimal() or int(row) <= 0 for row in rows):
        raise ProbeError("invalid whole-group process inventory")
    pids = {int(row) for row in rows}
    if len(pids) > 64:
        raise ProbeError("whole-group process inventory exceeds task bound")
    return pids - {1, os.getpid()}


def verify_native_files() -> None:
    for name, expected in MAPPED_ELF.items():
        path = Path(name)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ProbeError("native file ownership or mode differs")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ProbeError("native file differs from installed binding")


def decoder_child(
    stdin: int, stdout: int, stderr: int, ready: int, gate: int, fds: list[int]
) -> None:
    try:
        os.setsid()
        for source, target in ((stdin, 0), (stdout, 1), (stderr, 2)):
            os.dup2(source, target)
        for fd in fds:
            if fd not in {ready, gate}:
                os.close(fd)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_STACK, (STACK_BYTES, STACK_BYTES))
        resource.setrlimit(resource.RLIMIT_DATA, (DATA_BYTES, DATA_BYTES))
        resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
        os.write(ready, b"R")
        if os.read(gate, 1) != b"x":
            os._exit(125)
        os.close(gate)
        os.execve(ARGV[0], ARGV, CHILD_ENV)
    except BaseException:
        with suppress(OSError):
            os.write(ready, b"E")
        os._exit(125)


def receive_banner(child: Child, stdout: int, stderr: int, deadline: float) -> bytearray:
    data = bytearray()
    while len(data) < len(BANNER):
        child.reap()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or child.exit_code is not None:
            raise ProbeError("native-main boundary was not reached before preparation deadline")
        readable, _, _ = select.select([stdout, stderr], [], [], min(remaining, 0.05))
        if stdout in readable:
            raise ProbeError("native stdout activity or closure before the input gate")
        if stderr in readable:
            part = os.read(stderr, len(BANNER) - len(data))
            if not part:
                raise ProbeError("native stderr closed before the banner")
            data.extend(part)
            if not BANNER.startswith(data):
                raise ProbeError("unexpected output before native-main input boundary")
    return data


def stopped_native_gate(child: Child, start_ticks: int, deadline: float) -> dict[str, object]:
    os.kill(child.pid, signal.SIGSTOP)
    while time.monotonic() < deadline:
        pid, status = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED)
        if pid:
            if os.WIFSTOPPED(status):
                break
            child.exit_code = os.waitstatus_to_exitcode(status)
            raise ProbeError("decoder exited before stopped identity observation")
        time.sleep(0.01)  # Bounded stopped-state observation, not a test race schedule.
    else:
        raise ProbeError("native stopped-state deadline exceeded")
    if read_start_identity(child.pid) != ("T", start_ticks):
        raise ProbeError("native PID/start/stopped identity differs")
    root = Path(f"/proc/{child.pid}")
    if os.readlink(root / "exe") != ARGV[0] or len(list((root / "task").iterdir())) != 1:
        raise ProbeError("native executable or thread identity differs")
    if native_processes() != {child.pid} or group_members(child.pid) != [child.pid]:
        raise ProbeError("unexpected retained native address space")
    limits = verify_child(child.pid, True)
    verify_core_limit(read_text(root / "limits"))
    mappings = parse_maps(read_text(root / "maps"))
    if time.monotonic() >= deadline:
        raise ProbeError("native identity observation exceeded preparation deadline")
    return {
        "pid": child.pid,
        "start_ticks": start_ticks,
        "state": "stopped",
        "rlimits": limits | {"Max core file size": 0},
        "mappings": mappings,
        "native_address_spaces": 1,
        "input_bytes": 0,
    }


def load_fixture_snapshot(copy_view: memoryview) -> None:
    """Authenticate the complete owned compressed snapshot before admitting its first byte."""
    accepted = 0
    with FIXTURE.open("rb", buffering=0) as stream:
        while accepted < FIXTURE_BYTES:
            end = min(FIXTURE_BYTES, accepted + CHUNK_BYTES)
            with copy_view[accepted:end] as part:
                amount = stream.readinto(part)
            if amount is None or amount <= 0:
                raise ProbeError("owned fixture ended before its authenticated length")
            accepted += amount
        with copy_view[FIXTURE_BYTES : FIXTURE_BYTES + 1] as extra:
            if stream.readinto(extra) != 0:
                raise ProbeError("owned fixture exceeds its authenticated length")
    with copy_view[:FIXTURE_BYTES] as payload:
        if hashlib.sha256(payload).hexdigest() != FIXTURE_SHA256:
            raise ProbeError("owned compressed snapshot differs from generation receipt")


def decode_fixture() -> dict[str, object]:
    if (
        not 0 < FIXTURE_BYTES <= OUTPUT_COPY_OFFSET
        or OUTPUT_COPY_OFFSET + CHUNK_BYTES > COPY_BYTES
        or not 0 < READY_BYTES <= BANK_BYTES
        or 2 * BANK_BYTES != PCM_BYTES
    ):
        raise ProbeError("fixed payload partitions overlap or exceed their reservations")
    if (
        os.sysconf("SC_PAGE_SIZE") != 4096
        or len(list(Path("/proc/self/task").iterdir())) != 1
        or native_processes()
    ):
        raise ProbeError("fresh single-threaded native-amd64 proof process required")
    if read_text(Path("/proc/sys/kernel/core_pattern")).strip() != "core":
        raise ProbeError("core routing differs; no host setting will be changed")
    verify_native_files()
    before_events = counters(read_text(Path("/sys/fs/cgroup/memory.events")))
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    ready_r, ready_w = os.pipe()
    gate_r, gate_w = os.pipe()
    fds = [in_r, in_w, out_r, out_w, err_r, err_w, ready_r, ready_w, gate_r, gate_w]
    child: Child | None = None
    stderr = bytearray()
    buffers: list[bytearray] = []
    views: list[memoryview] = []
    pcm_hash = hashlib.sha256()
    pcm_digest = ""
    input_count = output_count = 0
    bank_filled = ready_buffered = 0
    ready_seconds: float | None = None
    failure: str | None = None
    phase = "exec_setup"
    exec_released = input_released = exec_failed = False
    debt = True
    cleanup_met = False
    retained: list[int] = []
    native_gate: dict[str, object] = {}
    events: dict[str, int] = {}
    swap = "unobserved"
    started = time.monotonic()
    prepare_deadline = started + PREPARE_SECONDS
    workload_deadline = started + WORKLOAD_SECONDS
    try:
        pid = os.fork()
        if pid == 0:
            decoder_child(in_r, out_w, err_w, ready_w, gate_r, fds)
            os._exit(125)
        child = Child(pid)
        for fd in (in_r, out_w, err_w, ready_w, gate_r):
            os.close(fd)
            fds.remove(fd)
        remaining = prepare_deadline - time.monotonic()
        if remaining <= 0 or not select.select([ready_r], [], [], remaining)[0]:
            raise ProbeError("decoder setup deadline exceeded")
        if os.read(ready_r, 1) != b"R":
            raise ProbeError("decoder limit setup failed")
        limits = verify_child(pid, True)
        verify_core_limit(read_text(Path(f"/proc/{pid}/limits")))
        _, start_ticks = read_start_identity(pid)
        emit(
            {
                "event": "decode_exec_gate",
                "rlimits": limits | {"Max core file size": 0},
                "input_bytes": 0,
                "parent_pcm_allocated": False,
            }
        )
        if os.write(gate_w, b"x") != 1:
            raise ProbeError("short decoder exec-gate write")
        exec_released = True
        os.close(gate_w)
        fds.remove(gate_w)
        phase = "native_main_gate"
        stderr = receive_banner(child, out_r, err_r, prepare_deadline)
        native_gate = stopped_native_gate(child, start_ticks, prepare_deadline)
        emit({"event": "decode_native_gate", "observation": native_gate})
        # No PCM existed at fork. Full pools are now physically allocated and remain prepaid.
        buffers = [bytearray(BANK_BYTES), bytearray(BANK_BYTES), bytearray(COPY_BYTES)]
        # Exercise every page of the full prepaid capacity before admitting input.
        for index in range(len(buffers)):
            for offset in range(0, len(buffers[index]), 4096):
                buffers[index][offset] = 1
        views = [memoryview(buffer) for buffer in buffers]
        bank_view, _, copy_view = views
        phase = "fixture_snapshot"
        load_fixture_snapshot(copy_view)
        input_view = copy_view[:FIXTURE_BYTES].toreadonly()
        output_view = copy_view[OUTPUT_COPY_OFFSET : OUTPUT_COPY_OFFSET + CHUNK_BYTES]
        views.extend([input_view, output_view])
        if time.monotonic() >= prepare_deadline:
            raise ProbeError("fixture preparation exceeded readiness deadline")
        if read_start_identity(pid) != ("T", start_ticks) or native_processes() != {pid}:
            raise ProbeError("stopped decoder ownership changed before input admission")
        emit(
            {
                "event": "decode_input_gate",
                "fixture_bytes": FIXTURE_BYTES,
                "fixture_sha256": FIXTURE_SHA256,
                "pcm_bank_capacity": PCM_BYTES,
                "copy_capacity": COPY_BYTES,
                "native_address_spaces": 1,
                "pcm_capacity_with_two_reserved_address_spaces": 2 * ADDRESS_SPACE_BYTES
                + PCM_BYTES
                + COPY_BYTES,
            }
        )
        for fd in (in_w, out_r, err_r):
            os.set_blocking(fd, False)
        os.kill(pid, signal.SIGCONT)
        input_released = True
        phase = "decode"
        input_open = output_open = error_open = True
        while input_open or output_open or error_open or child.exit_code is None:
            child.reap()
            now = time.monotonic()
            deadline = prepare_deadline if ready_seconds is None else workload_deadline
            remaining = deadline - now
            if remaining <= 0:
                raise ProbeError(
                    "readiness deadline exceeded"
                    if ready_seconds is None
                    else "workload deadline exceeded"
                )
            if native_processes() - {pid}:
                raise ProbeError("unexpected native address space; retain its cleanup debt")
            if child.exit_code is not None and input_count != FIXTURE_BYTES:
                raise ProbeError("decoder exited before the authenticated input completed")
            readable, writable, _ = select.select(
                ([out_r] if output_open else []) + ([err_r] if error_open else []),
                [in_w] if input_open else [],
                [],
                min(remaining, 0.05),
            )
            if err_r in readable:
                part = os.read(err_r, min(4096, STDERR_BYTES - len(stderr) + 1))
                if not part:
                    error_open = False
                room = STDERR_BYTES - len(stderr)
                stderr.extend(part[:room])
                if len(part) > room:
                    raise ProbeError("decoder diagnostics exceeded reserved capacity")
            if out_r in readable:
                limit = min(CHUNK_BYTES, PCM_OUTPUT_BYTES - output_count + 1)
                with output_view[:limit] as destination:
                    amount = os.readv(out_r, [destination])
                if amount == 0:
                    output_open = False
                else:
                    output_count += amount
                    if output_count > PCM_OUTPUT_BYTES:
                        raise ProbeError("decoded PCM exceeds the known fixture count")
                    with output_view[:amount] as payload:
                        pcm_hash.update(payload)
                        if ready_seconds is None:
                            if bank_filled + amount > BANK_BYTES:
                                raise ProbeError("readiness bank capacity exceeded")
                            with bank_view[bank_filled : bank_filled + amount] as bank_destination:
                                bank_destination[:] = payload
                            bank_filled += amount
                            if bank_filled // 4 * 4 >= READY_BYTES:
                                ready_seconds = time.monotonic() - started
                                ready_buffered = bank_filled // 4 * 4
                                if ready_seconds > PREPARE_SECONDS:
                                    raise ProbeError("readiness completed after its deadline")
                        else:
                            # A counted drain sink: preserve bounded ownership, never export PCM.
                            with bank_view[:amount] as bank_destination:
                                bank_destination[:] = payload
            if in_w in writable:
                end = min(FIXTURE_BYTES, input_count + CHUNK_BYTES)
                with input_view[input_count:end] as payload:
                    try:
                        amount = os.write(in_w, payload)
                    except BlockingIOError:
                        continue
                if amount <= 0:
                    raise ProbeError("decoder input made no progress")
                input_count += amount
                if input_count == FIXTURE_BYTES:
                    os.close(in_w)
                    fds.remove(in_w)
                    input_open = False
        if not select.select([ready_r], [], [], 0)[0]:
            raise ProbeError("decoder exec acknowledgement remains owned")
        exec_failed = os.read(ready_r, 2) != b""
        if child.exit_code != 0 or exec_failed:
            raise ProbeError("decoder or exec did not finish successfully")
        if output_count != PCM_OUTPUT_BYTES or output_count % 4:
            raise ProbeError("decoded PCM differs from the known complete fixture count")
        validate_diagnostics(bytes(stderr))
        if time.monotonic() > workload_deadline:
            raise ProbeError("completion evidence arrived after the workload deadline")
    except (OSError, ProbeError, ValueError) as error:
        failure = str(error)
    finally:
        cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
        for fd in fds:
            with suppress(OSError):
                os.close(fd)
        if child is not None:
            try:
                release = cleanup(child, cleanup_deadline)
                retained = sorted(set(release.retained_pids) | native_processes())
                debt = child.exit_code is None or bool(retained)
                cleanup_met = release.deadline_met and not debt
            except (OSError, ValueError, ProbeError) as error:
                failure = failure or f"cleanup observation failed: {error}"
        for view in reversed(views):
            view.release()
        views.clear()
        buffers.clear()
        pcm_digest = pcm_hash.hexdigest()
        del pcm_hash
        if time.monotonic() > cleanup_deadline:
            cleanup_met = False
        try:
            after = counters(read_text(Path("/sys/fs/cgroup/memory.events")))
            if set(after) != set(before_events) or any(
                after[key] < before_events[key] for key in after
            ):
                raise ProbeError("memory event counters changed identity or decreased")
            events = {key: after[key] - before_events[key] for key in after}
            swap = read_text(Path("/sys/fs/cgroup/memory.swap.current")).strip()
        except (OSError, ValueError, ProbeError) as error:
            failure = failure or f"final memory observation failed: {error}"
    passed = (
        failure is None
        and input_released
        and not exec_failed
        and not debt
        and cleanup_met
        and ready_seconds is not None
        and ready_seconds <= PREPARE_SECONDS
        and input_count == FIXTURE_BYTES
        and output_count == PCM_OUTPUT_BYTES
        and events.get("oom") == 0
        and events.get("oom_kill") == 0
        and swap == "0"
    )
    return {
        "event": "owned_decode_result",
        "verdict": "owned_fixture_decode_pass" if passed else "incomplete",
        "failure": failure,
        "phase": phase,
        "argv": list(ARGV),
        "decoder_sha256": MAPPED_ELF[ARGV[0]],
        "fixture_sha256": FIXTURE_SHA256,
        "fixture_bytes_accepted": input_count,
        "exec_gate_released": exec_released,
        "native_input_gate_released": input_released,
        "exec_failed": exec_failed,
        "native_gate": native_gate,
        "pcm_output_bytes": output_count,
        "pcm_output_sha256": pcm_digest,
        "ready_seconds": ready_seconds,
        "ready_buffered_bytes": ready_buffered,
        "pcm_bank_capacity": PCM_BYTES,
        "copy_capacity": COPY_BYTES,
        "exit_code": child.exit_code if child is not None else None,
        "leader_reaped": child is not None and child.exit_code is not None,
        "retained_pids": retained,
        "cleanup_debt": debt,
        "cleanup_deadline_met": cleanup_met,
        "parent_payload_views_released": not views and not buffers,
        "stderr_bytes": len(stderr),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "stderr_preview": stderr[:4096].decode("utf-8", errors="replace"),
        "memory_event_delta": events,
        "swap_current": swap,
        "elapsed_seconds": time.monotonic() - started,
        "q3": "partial one-fixture result only; other cases remain unqualified",
        "q4": "not_run",
        "native_arm64": "not_run",
        "listening": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-owned-mp3-decode", action="store_true", required=True)
    parser.parse_args()
    try:
        emit({"event": "owned_decode_preflight", "observation": observe()})
        result = decode_fixture()
        emit(result)
        return 0 if result["verdict"] == "owned_fixture_decode_pass" else 1
    except (OSError, ProbeError, ValueError) as error:
        emit({"event": "owned_decode_incomplete", "error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
