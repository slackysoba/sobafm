"""Opt-in direct-decoder Q4 preparation; native execution requires a separate release."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import os
import re
import select
import signal
import stat
import termios
import time
from array import array
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Literal

from .decoder_startup import Child, group_members, verify_child
from .owned_mp3_decode import (
    ARGV,
    BANK_BYTES,
    BANNER,
    CHUNK_BYTES,
    CLEANUP_SECONDS,
    FIXTURE_BYTES,
    FIXTURE_SHA256,
    MAPPED_ELF,
    OUTPUT_COPY_OFFSET,
    PCM_OUTPUT_BYTES,
    PREPARE_SECONDS,
    STDERR_BYTES,
    cbr_fixture,
    decoder_child,
    load_fixture_snapshot,
    native_processes,
    parse_maps,
    read_start_identity,
    validate_fixture_partitions,
    verify_core_limit,
    verify_native_files,
)
from .owned_mp3_overlap import Pipes, make_pipes
from .probe import COPY_BYTES, PCM_BYTES, ProbeError, counters, emit, observe, read_text

Case = Literal["feed", "stdout_backpressure"]
PREFIX_BYTES = 65_536
RETENTION_SECONDS = 45.0


@dataclass
class Cancellation:
    started: float | None = None
    deadline: float | None = None
    requests: int = 0
    second_request: float | None = None
    deadline_missed: bool = False

    def request(self, now: float) -> None:
        if self.started is None:
            self.started, self.deadline = now, now + CLEANUP_SECONDS
        elif self.second_request is None:
            self.second_request = now
        self.requests = min(self.requests + 1, 2**31 - 1)

    def observe(self, now: float) -> None:
        if self.deadline is not None and now > self.deadline:
            self.deadline_missed = True


def unread_bytes(fd: int) -> int:
    count = array("i", [0])
    fcntl.ioctl(fd, termios.FIONREAD, count, True)
    if count[0] < 0:
        raise ProbeError("negative pipe occupancy")
    return count[0]


def blocked_stdout_write(text: str) -> bool:
    """Only the admitted amd64 write(1, ...) syscall qualifies this observation."""
    fields = text.split()
    if fields == ["running"] or fields and fields[0] == "-1":
        return False
    if len(fields) != 9:
        raise ProbeError("unrecognized native syscall observation")
    try:
        numbers = [int(field, 0) for field in fields]
    except ValueError as error:
        raise ProbeError("invalid native syscall values") from error
    return numbers[0] == 1 and numbers[1] == 1 and numbers[3] > 0


def backpressure(pid: int, start_ticks: int, fd: int) -> dict[str, object] | None:
    capacity = fcntl.fcntl(fd, fcntl.F_GETPIPE_SZ)
    available = unread_bytes(fd)
    if not 0 < available <= capacity:
        if available == 0 and capacity > 0:
            return None
        raise ProbeError("pipe occupancy exceeds its actual capacity")
    if available != capacity:
        return None
    own = os.fstat(fd)
    native = os.stat(Path(f"/proc/{pid}/fd/1"))
    if not stat.S_ISFIFO(own.st_mode) or (own.st_dev, own.st_ino) != (native.st_dev, native.st_ino):
        raise ProbeError("native stdout no longer names the owned pipe")
    syscall = read_text(Path(f"/proc/{pid}/syscall"))
    if not blocked_stdout_write(syscall):
        return None
    state, actual_start = read_start_identity(pid)
    if actual_start != start_ticks:
        raise ProbeError("native identity changed during backpressure observation")
    if state != "S" or unread_bytes(fd) != capacity:
        return None
    return {
        "pipe_capacity": capacity,
        "unread_bytes": available,
        "syscall": "amd64_write",
        "stdout_fd": 1,
        "state": state,
    }


def validate_cancel_diagnostics(data: bytes) -> None:
    if not data.startswith(BANNER):
        raise ProbeError("cancellation native banner differs")
    required = {"encoding": 0, "playing": 0, "format": 0, "term": 0, "complete": 0}
    directory = 0
    for line in data[len(BANNER) :].decode("ascii").splitlines():
        if not line:
            continue
        if line == "Note: forcing output encoding s16":
            required["encoding"] += 1
        elif line == "Playing MPEG stream 1 of 1: - ...":
            required["playing"] += 1
        elif line == "MPEG 1.0 L III vbr 48000 j-s":
            required["format"] += 1
        elif line == "mpg123: death by SIGTERM":
            required["term"] += 1
        elif re.fullmatch(r"\[[0-9]+:[0-5][0-9]\] Decoding of - finished\.", line):
            required["complete"] += 1
        elif line == "Directory: /":
            directory += 1
        else:
            raise ProbeError("unexpected cancellation diagnostic")
    if directory > 1 or any(value != 1 for value in required.values()):
        raise ProbeError("missing or repeated cancellation diagnostic")


class CancelRun:
    """One fixed producer, its original synchronous I/O, and its retained payload lease."""

    def __init__(self, case: Case) -> None:
        self.case = case
        self.started = time.monotonic()
        self.phase_deadline = self.started + PREPARE_SECONDS
        self.cancel = Cancellation()
        self.child = Child(0)
        self.start_ticks = 0
        self.pipes: Pipes | None = None
        self.fds: list[int] = []
        self.busy: set[int] = set()
        self.uncertain_closes: set[int] = set()
        self.buffers: list[bytearray] = []
        self.views: list[memoryview] = []
        self.input_view: memoryview | None = None
        self.output_view: memoryview | None = None
        self.bank: memoryview | None = None
        self.stderr = bytearray()
        self.pcm_hash = hashlib.sha256()
        self.pcm_digest = ""
        self.input_count = self.output_count = self.drained = self.bank_filled = 0
        self.output_eof = self.error_eof = False
        self.exec_released = self.input_released = False
        self.term_sent: float | None = None
        self.kill_sent: float | None = None
        self.native_gate: dict[str, object] = {}
        self.trigger: dict[str, object] = {}
        self.failure: str | None = None
        self.released = False
        self.released_at: float | None = None
        self.retained_pids: list[int] = []
        self.events: list[dict[str, object]] = []
        self.memory_before: dict[str, int] = {}
        self.memory_delta: dict[str, int] = {}
        self.swap = "unobserved"

    def request(self, reason: str) -> None:
        now = time.monotonic()
        self.cancel.request(now)
        if self.cancel.requests <= 2:
            self.events.append(
                {
                    "event": "cancellation_request",
                    "reason": reason,
                    "at": now,
                    "deadline": self.cancel.deadline,
                }
            )

    def fail(self, error: BaseException | str) -> None:
        self.failure = self.failure or (str(error) or type(error).__name__)[:1024]
        if self.cancel.started is None:
            self.request("failure")

    def require_released(self) -> None:
        if not self.released:
            raise ProbeError("original producer lease remains owned; admission refused")

    def close(self, fd: int) -> None:
        if fd not in self.fds or fd in self.busy or fd in self.uncertain_closes:
            return
        try:
            os.close(fd)
            self.fds.remove(fd)
        except OSError as error:
            # Do not retry close blindly: Linux may have already released this descriptor.
            self.uncertain_closes.add(fd)
            self.fail(f"descriptor {fd} close not confirmed: {error}")

    def reconcile_closes(self) -> None:
        for fd in tuple(self.uncertain_closes):
            try:
                os.fstat(fd)
            except OSError as error:
                if error.errno == errno.EBADF:
                    self.fds.remove(fd)
                    self.uncertain_closes.remove(fd)
                else:
                    raise

    def preparation_remaining(self) -> float:
        remaining = self.phase_deadline - time.monotonic()
        if remaining <= 0 or self.cancel.started is not None or self.failure is not None:
            raise ProbeError("decoder preparation expired or was cancelled")
        return remaining

    def wait_ready(self, fd: int) -> None:
        while not select.select([fd], [], [], min(0.01, self.preparation_remaining()))[0]:
            self.child.reap()
            if self.child.exit_code is not None:
                raise ProbeError("decoder exited during setup")
        self.preparation_remaining()

    def admit_native(self, pipes: Pipes) -> dict[str, object]:
        # Keep partial diagnostics and cancellation checks in this original owner.
        while len(self.stderr) < len(BANNER):
            self.child.reap()
            if self.child.exit_code is not None:
                raise ProbeError("decoder exited before its native banner")
            ready, _, _ = select.select(
                [pipes.output_read, pipes.error_read],
                [],
                [],
                min(0.01, self.preparation_remaining()),
            )
            if pipes.output_read in ready:
                raise ProbeError("stdout activity before media admission")
            if pipes.error_read in ready:
                part = os.read(pipes.error_read, len(BANNER) - len(self.stderr))
                self.stderr.extend(part)
                if not part or not BANNER.startswith(self.stderr):
                    raise ProbeError("native banner absent or different")
        pid = self.child.pid
        self.preparation_remaining()
        os.kill(pid, signal.SIGSTOP)
        while True:
            self.preparation_remaining()
            waited, status = os.waitpid(pid, os.WNOHANG | os.WUNTRACED)
            if waited:
                if os.WIFSTOPPED(status):
                    break
                self.child.exit_code = os.waitstatus_to_exitcode(status)
                raise ProbeError("decoder exited before stopped identity observation")
            select.select([], [], [], 0.01)
        root = Path(f"/proc/{pid}")
        if (
            read_start_identity(pid) != ("T", self.start_ticks)
            or os.readlink(root / "exe") != ARGV[0]
            or len(list((root / "task").iterdir())) != 1
            or native_processes() != {pid}
            or group_members(pid) != [pid]
        ):
            raise ProbeError("stopped decoder identity or inventory differs")
        limits = verify_child(pid, True)
        verify_core_limit(read_text(root / "limits"))
        mappings = parse_maps(read_text(root / "maps"))
        self.preparation_remaining()
        return {
            "pid": pid,
            "start_ticks": self.start_ticks,
            "state": "stopped",
            "rlimits": limits | {"Max core file size": 0},
            "mappings": mappings,
            "native_address_spaces": 1,
            "input_bytes": 0,
        }

    def prepare(self) -> None:
        validate_fixture_partitions(cbr_fixture())
        if (
            os.uname().machine != "x86_64"
            or os.sysconf("SC_PAGE_SIZE") != 4096
            or len(list(Path("/proc/self/task").iterdir())) != 1
            or native_processes()
        ):
            raise ProbeError("fresh single-threaded native amd64 process required")
        if read_text(Path("/proc/sys/kernel/core_pattern")).strip() != "core":
            raise ProbeError("core routing differs; do not change host settings")
        verify_native_files()
        self.memory_before = counters(read_text(Path("/sys/fs/cgroup/memory.events")))
        if self.cancel.started is not None:
            raise ProbeError("cancelled before decoder launch")
        self.pipes = pipes = make_pipes(self.fds)
        pid = os.fork()
        if pid == 0:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            decoder_child(
                pipes.input_read,
                pipes.output_write,
                pipes.error_write,
                pipes.ready_write,
                pipes.gate_read,
                self.fds,
            )
            os._exit(125)
        self.child.pid = pid
        _, self.start_ticks = read_start_identity(pid)
        for fd in pipes.child_ends():
            self.close(fd)
        self.wait_ready(pipes.ready_read)
        if os.read(pipes.ready_read, 1) != b"R":
            raise ProbeError("decoder limit setup failed")
        limits = verify_child(pid, True)
        verify_core_limit(read_text(Path(f"/proc/{pid}/limits")))
        emit({"event": "cancel_exec_gate", "rlimits": limits, "parent_pcm_allocated": False})
        if self.cancel.started is not None or self.failure is not None:
            raise ProbeError("cancelled during decoder setup")
        if os.write(pipes.gate_write, b"x") != 1:
            raise ProbeError("short exec-gate write")
        self.exec_released = True
        self.close(pipes.gate_write)
        self.native_gate = self.admit_native(pipes)
        # Test access before media: no permission workaround is permitted for this observation.
        if self.case == "stdout_backpressure":
            blocked_stdout_write(read_text(Path(f"/proc/{pid}/syscall")))
        if not select.select([pipes.ready_read], [], [], 0)[0] or os.read(pipes.ready_read, 2):
            raise ProbeError("exec acknowledgement remains owned or failed")
        self.close(pipes.ready_read)
        self.buffers = [bytearray(BANK_BYTES), bytearray(BANK_BYTES), bytearray(COPY_BYTES)]
        for buffer in self.buffers:
            for position in range(0, len(buffer), 4096):
                buffer[position] = 1
        self.views = [memoryview(buffer) for buffer in self.buffers]
        self.bank, _, copy = self.views
        load_fixture_snapshot(copy)
        with copy[:FIXTURE_BYTES] as snapshot:
            self.input_view = snapshot.toreadonly()
        self.output_view = copy[OUTPUT_COPY_OFFSET : OUTPUT_COPY_OFFSET + CHUNK_BYTES]
        self.views.extend((self.input_view, self.output_view))
        if (
            time.monotonic() >= self.phase_deadline
            or self.cancel.started is not None
            or self.failure is not None
            or native_processes() != {pid}
            or read_start_identity(pid) != ("T", self.start_ticks)
        ):
            raise ProbeError("stopped input admission changed or expired")
        for fd in (pipes.input_write, pipes.output_read, pipes.error_read):
            os.set_blocking(fd, False)
        emit(
            {
                "event": "cancel_input_gate",
                "native_gate": self.native_gate,
                "fixture_sha256": FIXTURE_SHA256,
                "fixture_bytes": FIXTURE_BYTES,
                "prefix_bound": PREFIX_BYTES,
                "pcm_capacity": PCM_BYTES,
                "copy_capacity": COPY_BYTES,
            }
        )
        os.kill(pid, signal.SIGCONT)
        self.input_released = True

    def read_pcm(self) -> None:
        if self.pipes is None or self.output_view is None or self.bank is None:
            raise ProbeError("PCM reader lacks its original reservation")
        fd = self.pipes.output_read
        self.busy.add(fd)
        try:
            limit = min(CHUNK_BYTES, PCM_OUTPUT_BYTES - self.output_count + 1)
            with self.output_view[:limit] as destination:
                amount = os.readv(fd, [destination])
            if amount == 0:
                self.output_eof = True
                return
            self.output_count += amount
            if self.output_count > PCM_OUTPUT_BYTES:
                raise ProbeError("cancellation drain exceeds complete fixture bound")
            with self.output_view[:amount] as payload:
                self.pcm_hash.update(payload)
                if self.cancel.started is None:
                    if self.bank_filled + amount > len(self.bank):
                        raise ProbeError("prefix bank exceeds its reservation")
                    with self.bank[self.bank_filled : self.bank_filled + amount] as destination:
                        destination[:] = payload
                    self.bank_filled += amount
                else:
                    self.drained += amount
        except BlockingIOError, InterruptedError:
            return
        finally:
            self.busy.remove(fd)

    def read_error(self) -> None:
        if self.pipes is None:
            raise ProbeError("diagnostic reader missing")
        fd = self.pipes.error_read
        self.busy.add(fd)
        try:
            remaining = STDERR_BYTES - len(self.stderr)
            data = os.read(fd, min(4096, remaining + 1))
            if not data:
                self.error_eof = True
            self.stderr.extend(data[:remaining])
            if len(data) > remaining:
                raise ProbeError("bounded cancellation diagnostics overflowed")
        except BlockingIOError, InterruptedError:
            return
        finally:
            self.busy.remove(fd)

    def write_input(self) -> None:
        if self.pipes is None or self.input_view is None:
            raise ProbeError("input writer lacks its authenticated view")
        fd = self.pipes.input_write
        self.busy.add(fd)
        try:
            with self.input_view[self.input_count : PREFIX_BYTES] as payload:
                amount = os.write(fd, payload)
            if amount <= 0:
                raise ProbeError("prefix write made no progress")
            self.input_count += amount
        except BlockingIOError, InterruptedError:
            return
        finally:
            self.busy.remove(fd)

    def process_start_identity(self, pid: int) -> tuple[str, int]:
        """Overridable observation only; signal/reap ownership remains here."""
        return read_start_identity(pid)

    def process_group_members(self, pid: int) -> list[int]:
        return group_members(pid)

    def signal_group(self, sig: signal.Signals) -> None:
        pid = self.child.pid
        own_group = True
        if self.child.exit_code is None:
            _, actual_start = self.process_start_identity(pid)
            # An unreaped direct child cannot have its PID reused. Capture a missing
            # identity only on the pre-exec setup-failure path, before media exists.
            if self.start_ticks == 0 and not self.exec_released:
                self.start_ticks = actual_start
            if actual_start != self.start_ticks:
                raise ProbeError("owned process identity changed before signal")
            own_group = os.getpgid(pid) == pid
            if not own_group and self.exec_released:
                self.fail("admitted decoder process group changed")
        members = self.process_group_members(pid) if own_group else [pid]
        if members:
            with suppress(ProcessLookupError):
                if own_group:
                    os.killpg(pid, sig)
                    if sig == signal.SIGTERM:
                        os.killpg(pid, signal.SIGCONT)
                else:
                    os.kill(pid, sig)
                    if sig == signal.SIGTERM:
                        os.kill(pid, signal.SIGCONT)
        self.events.append(
            {"event": "signal", "signal": sig.name, "members": members, "at": time.monotonic()}
        )

    def manage_cancel(self) -> None:
        if self.cancel.started is None:
            return
        self.cancel.observe(time.monotonic())
        streams: set[int] = (
            set() if self.pipes is None else {self.pipes.output_read, self.pipes.error_read}
        )
        try:
            if self.child.pid > 0:
                self.child.reap()
                if self.term_sent is None:
                    # Queue cancellation before EOF can let an unfinished prefix exit normally.
                    self.signal_group(signal.SIGTERM)
                    self.term_sent = time.monotonic()
                    self.request("repeat_after_term_dispatch")
                if self.kill_sent is None and time.monotonic() >= self.cancel.started + 1.0:
                    if self.process_group_members(self.child.pid) or self.child.exit_code is None:
                        self.signal_group(signal.SIGKILL)
                    self.kill_sent = time.monotonic()
        finally:
            for fd in tuple(self.fds):
                if fd not in streams or (
                    self.pipes is not None
                    and fd == self.pipes.output_read
                    and self.output_view is None
                ):
                    self.close(fd)

    def check_trigger(self) -> None:
        if self.cancel.started is not None or self.pipes is None:
            return
        if self.child.exit_code is not None or self.output_eof or self.error_eof:
            raise ProbeError("decoder ended before the cancellation trigger")
        if time.monotonic() >= self.phase_deadline:
            raise ProbeError("controlled cancellation phase was not established in time")
        if not 0 < self.input_count <= PREFIX_BYTES < FIXTURE_BYTES or self.output_count < 4:
            return
        state, actual_start = read_start_identity(self.child.pid)
        if (
            not self.input_released
            or self.pipes.input_write not in self.fds
            or native_processes() != {self.child.pid}
            or (actual_start != self.start_ticks or state not in {"R", "S", "D"})
        ):
            raise ProbeError("decoder feed identity or ownership changed before trigger")
        evidence: dict[str, object] = {}
        if self.case == "stdout_backpressure":
            observed = backpressure(self.child.pid, self.start_ticks, self.pipes.output_read)
            if observed is None:
                return
            evidence = observed
        self.trigger = {
            "case": self.case,
            "input_bytes": self.input_count,
            "output_bytes": self.output_count,
            "observation": evidence,
        }
        self.request("controlled_trigger")
        emit({"event": "cancellation_trigger", **self.trigger})

    def can_feed(self) -> bool:
        return (
            self.cancel.started is None and self.input_count < PREFIX_BYTES and self.input_released
        )

    def reap_child(self) -> None:
        self.child.reap()

    def io_step(self, timeout: float = 0.01) -> None:
        if self.pipes is None:
            select.select([], [], [], timeout)
            return
        pipes = self.pipes
        readable: list[int] = []
        if pipes.error_read in self.fds and pipes.error_read not in self.uncertain_closes:
            readable.append(pipes.error_read)
        paused = (
            self.case == "stdout_backpressure"
            and self.output_count >= 4
            and self.cancel.started is None
        )
        if (
            not paused
            and pipes.output_read in self.fds
            and pipes.output_read not in self.uncertain_closes
            and self.output_view is not None
        ):
            readable.append(pipes.output_read)
        writable = [pipes.input_write] if self.can_feed() else []
        ready, writes, _ = select.select(readable, writable, [], timeout)
        for fd in ready:
            try:
                if fd == pipes.output_read:
                    self.read_pcm()
                    if self.output_eof:
                        self.close(fd)
                else:
                    self.read_error()
                    if self.error_eof:
                        self.close(fd)
            except (OSError, ValueError, ProbeError) as error:
                self.fail(error)
                self.close(fd)
        if self.child.pid > 0:
            self.reap_child()
        self.check_trigger()
        if writes and self.can_feed():
            try:
                self.write_input()
            except (OSError, ValueError, ProbeError) as error:
                self.fail(error)

    def release_snapshot(self) -> dict[str, object]:
        observation: dict[str, object] = {
            "cleanup_debt": not self.released,
            "retained_fds": list(self.fds),
            "original_io_in_flight": sorted(self.busy),
            "payload_lease_owned": bool(self.views or self.buffers),
            "leader_pid": self.child.pid,
            "leader_reaped": self.child.exit_code is not None,
            "cancel_started": self.cancel.started,
            "cancel_deadline": self.cancel.deadline,
            "released_at": self.released_at,
        }
        try:
            observation["retained_pids"] = sorted(native_processes())
            observation["inventory_status"] = "observed"
        except (OSError, ValueError, ProbeError) as error:
            observation["inventory_status"] = "unavailable"
            observation["inventory_error"] = str(error)[:1024]
        return observation

    def report_cleanup(self, event: str) -> None:
        # A failed evidence sink cannot retire this owner while cleanup still owns I/O.
        try:
            emit({"event": event, **self.release_snapshot()})
        except Exception as error:
            self.fail(error)

    def release_if_done(self) -> bool:
        if self.released:
            return True
        if self.cancel.started is None or self.busy or self.fds:
            return False
        if self.child.pid > 0 and self.child.exit_code is None:
            return False
        self.retained_pids = sorted(native_processes())
        if self.retained_pids:
            return False
        for view in reversed(self.views):
            view.release()
        self.views.clear()
        self.input_view = self.output_view = self.bank = None
        self.buffers.clear()
        self.pcm_digest = self.pcm_hash.hexdigest()
        self.pcm_hash = hashlib.sha256()
        self.released_at = time.monotonic()
        self.cancel.observe(self.released_at)
        self.released = True
        return True

    def finish(self) -> dict[str, object]:
        self.require_released()
        try:
            after = counters(read_text(Path("/sys/fs/cgroup/memory.events")))
            if set(after) != set(self.memory_before) or any(
                after[key] < self.memory_before[key] for key in after
            ):
                raise ProbeError("memory counters changed identity or decreased")
            self.memory_delta = {key: after[key] - self.memory_before[key] for key in after}
            self.swap = read_text(Path("/sys/fs/cgroup/memory.swap.current")).strip()
            if self.failure is None:
                validate_cancel_diagnostics(bytes(self.stderr))
        except (OSError, ValueError, ProbeError) as error:
            self.fail(error)
        passed = (
            self.failure is None
            and bool(self.trigger)
            and self.input_released
            and self.cancel.requests >= 2
            and not self.cancel.deadline_missed
            and self.term_sent is not None
            and self.output_eof
            and self.error_eof
            and self.child.exit_code is not None
            and self.swap == "0"
            and self.memory_delta.get("oom") == 0
            and self.memory_delta.get("oom_kill") == 0
        )
        return {
            "event": "owned_cancel_result",
            "case": self.case,
            "verdict": "direct_decoder_cancel_pass" if passed else "incomplete",
            "failure": self.failure,
            "trigger": self.trigger,
            "events": self.events,
            "argv": list(ARGV),
            "fixture_sha256": FIXTURE_SHA256,
            "decoder_sha256": MAPPED_ELF[ARGV[0]],
            "native_gate": self.native_gate,
            "input_bytes": self.input_count,
            "output_bytes": self.output_count,
            "drained_after_cancel_bytes": self.drained,
            "bank_high_water": self.bank_filled,
            "partial_frame_bytes_discarded": self.output_count % 4,
            "prefix_pcm_sha256": self.pcm_digest,
            "pcm_capacity": PCM_BYTES,
            "copy_capacity": COPY_BYTES,
            "cancel_started": self.cancel.started,
            "cancel_deadline": self.cancel.deadline,
            "cancel_requests": self.cancel.requests,
            "second_cancel_at": self.cancel.second_request,
            "released_at": self.released_at,
            "cleanup_deadline_met": not self.cancel.deadline_missed,
            "cleanup_debt": not self.released,
            "retained_fds": list(self.fds),
            "retained_pids": self.retained_pids,
            "original_io_in_flight": sorted(self.busy),
            "parent_payload_views_released": not self.views and not self.buffers,
            "exit_code": self.child.exit_code,
            "leader_reaped": self.child.exit_code is not None,
            "stderr_bytes": len(self.stderr),
            "stderr_sha256": hashlib.sha256(self.stderr).hexdigest(),
            "stderr_preview": self.stderr[:4096].decode("ascii", errors="replace"),
            "memory_event_delta": self.memory_delta,
            "swap_current": self.swap,
            "q4": "only this direct-decoder case; other cases remain unqualified",
            "complete_decode": False,
            "native_arm64": "not_run",
            "listening": False,
        }


def run_owner(owner: CancelRun) -> dict[str, object]:
    def interrupted(signum: int, frame: FrameType | None) -> None:
        if not owner.trigger:
            owner.failure = owner.failure or "external cancellation preceded the fixed trigger"
        owner.request("external_signal")

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        try:
            owner.prepare()
        except Exception as error:
            owner.fail(error)
        deadline_reported = backstop_reported = False
        while not owner.released:
            for step in (
                owner.manage_cancel,
                owner.io_step,
                owner.reconcile_closes,
                owner.release_if_done,
            ):
                try:
                    step()
                except Exception as error:
                    owner.fail(error)
                    select.select([], [], [], 0.01)
            if owner.cancel.deadline_missed and not deadline_reported:
                deadline_reported = True
                owner.report_cleanup("original_cleanup_deadline_failed")
            if time.monotonic() - owner.started >= RETENTION_SECONDS and not backstop_reported:
                owner.fail("native retention backstop exceeded; original owner remains accountable")
                backstop_reported = True
                owner.report_cleanup("native_retention_backstop_failed")
        return owner.finish()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_case(case: Case) -> dict[str, object]:
    return run_owner(CancelRun(case))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-reviewed-q4-cancellation", action="store_true", required=True)
    parser.add_argument("--case", choices=("feed", "stdout_backpressure"), required=True)
    args = parser.parse_args()
    emit({"event": "owned_cancel_preflight", "observation": observe()})
    case: Case = "feed" if args.case == "feed" else "stdout_backpressure"
    result = run_case(case)
    emit(result)
    return 0 if result["verdict"] == "direct_decoder_cancel_pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
