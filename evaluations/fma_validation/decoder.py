"""Fresh single-thread streaming runner; native execution is separately released."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import select
import signal
import struct
import time
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import cast

from evaluations.linux_resource.owned_mp3_cancel import CancelRun
from evaluations.linux_resource.owned_mp3_decode import (
    decoder_child,
    stopped_native_gate,
    verify_native_files,
)
from evaluations.linux_resource.owned_mp3_overlap import make_pipes
from evaluations.linux_resource.probe import (
    PCM_BYTES,
    ProbeError,
)

from .cases import CASE_IDS, NIGHT_OWL, CaseBinding, CaseId
from .diagnostics import DECODER, DIAGNOSTIC_BYTES, READ_BYTES, DiagnosticFacts, scan
from .limits import (
    CHUNK_BYTES,
    DECODE_SECONDS,
    PCM_LIMIT,
    PREPARE_SECONDS,
    READY_BYTES,
    REPORT_BYTES,
    CaseClock,
    encode_report,
)
from .observation import ObservationWorkspace, ResourceFacts
from .process import ProcessPin, inventory
from .pty_channel import open_diagnostics


def report_line(value: object) -> bytes:
    """Reserve newline space before JSON serialization, including escaped text."""
    return encode_report(value, REPORT_BYTES - 1) + b"\n"


def write_report(data: bytes) -> None:
    """Own one immutable record until every partial synchronous write settles."""
    offset = 0
    with memoryview(data) as view:
        while offset < len(view):
            with view[offset:] as remaining:
                try:
                    amount = os.write(1, remaining)
                except InterruptedError:
                    continue
                if not 0 < amount <= len(remaining):
                    raise ProbeError("decoder_report_write")
                offset += amount


class StreamRun(CancelRun):
    """Reuse actual close/signal debt handling, replacing fixture-only admission/I/O."""

    def __init__(
        self,
        init: ProcessPin,
        owner: ProcessPin,
        clock: CaseClock,
        cancel_fd: int,
        case: CaseBinding = NIGHT_OWL,
        workspace: ObservationWorkspace | None = None,
    ) -> None:
        super().__init__("feed")
        self.case = case
        self.init_pin, self.owner_pin, self.clock = init, owner, clock
        self.observation = ObservationWorkspace(clock) if workspace is None else workspace
        if self.observation.clock is not clock:
            raise ProbeError("observation_clock_owner")
        self.helper_pin = ProcessPin.capture(os.getpid(), self.observation)
        self.cancel_fd = cancel_fd
        self.fds.extend((0, cancel_fd))
        self.input_hash = hashlib.sha256()
        self.input_buffer = bytearray()
        self.input_offset = self.received = 0
        self.input_count: int = 0
        self.output_count: int = 0
        self.input_eof = self.input_complete = False
        self.ready_seconds: float | None = None
        self.decode_end = min(clock.end_monotonic, self.started + DECODE_SECONDS)
        self.phase_deadline = min(clock.end_monotonic, self.started + PREPARE_SECONDS)
        self.pty_slave_identity: tuple[int, int] | None = None
        self.resources_before: ResourceFacts | None = None
        self.final_resources_attempted = False
        # The inherited empty stderr becomes fixed backing only after fork.
        self.stderr_count = 0
        self.diagnostic_slab: bytearray | None = None
        self.diagnostic_facts: DiagnosticFacts | None = None
        self.diagnostic_attempted = self.diagnostic_active = False

    def members(self) -> set[int]:
        return inventory(
            self.init_pin,
            self.owner_pin,
            self.helper_pin,
            workspace=self.observation,
            cleanup=self.cancel.started is not None,
        )

    def process_start_identity(self, pid: int) -> tuple[str, int]:
        return self.observation.start_identity(pid, cleanup=self.cancel.started is not None)

    def process_group_members(self, pid: int) -> list[int]:
        return self.observation.group_members(pid, cleanup=self.cancel.started is not None)

    def request(self, reason: str) -> None:
        # The controller's bounded record carries the original cross-process
        # UTC obligation; a signal without that record is conservative failure.
        if reason == "external_signal":
            try:
                raw = os.read(self.cancel_fd, 9)
                if len(raw) != 8:
                    raise ProbeError("missing_original_cleanup_clock")
                self.clock.adopt_cleanup(struct.unpack("!d", raw)[0])
            except OSError, ProbeError, ValueError:
                self.clock.timing_unknown = True
        self.clock.cancel()
        assert self.clock.cleanup_monotonic is not None
        self.cancel.started = self.clock.cleanup_monotonic - 3
        self.cancel.deadline = self.clock.cleanup_monotonic
        self.cancel.requests += 1
        self.cancel.deadline_missed |= self.clock.missed or self.clock.timing_unknown

    def fail(self, error: BaseException | str) -> None:
        # Never export a native/parser exception body containing source text.
        self.failure = self.failure or "decoder_case_failed"
        if self.cancel.started is None:
            self.request("failure")

    def prepare(self) -> None:
        self.observation.single_thread(os.getpid())
        self.observation.environment(intake=False, phase_end=self.phase_deadline)
        if os.uname().machine != "x86_64" or os.sysconf("SC_PAGE_SIZE") != 4096 or self.members():
            raise ProbeError("fresh_pinned_single_thread_runner_required")
        verify_native_files(DECODER)
        self.resources_before = self.observation.resources(phase_end=self.phase_deadline)
        pipes = make_pipes(self.fds)
        self.pipes = pipes
        # Replace only stderr's channel. The slave must be a terminal to enable
        # pinned mpg123 filtering; disable kernel CRLF rewriting and echo.
        master, slave = open_diagnostics(self.fds)
        info = os.fstat(slave)
        self.pty_slave_identity = (info.st_dev, info.st_ino)
        self.close(pipes.error_read)
        self.close(pipes.error_write)
        pipes = replace(pipes, error_read=master, error_write=slave)
        self.pipes = pipes
        self.preparation_remaining()
        pid = os.fork()
        if pid == 0:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            decoder_child(
                pipes.input_read,
                pipes.output_write,
                slave,
                pipes.ready_write,
                pipes.gate_read,
                [fd for fd in self.fds if fd > 2],
                DECODER,
            )
            os._exit(125)
        self.child.pid = pid
        self.allocate_diagnostics()
        _, self.start_ticks = self.process_start_identity(pid)
        for fd in pipes.child_ends():
            self.close(fd)
        self.wait_ready(pipes.ready_read)
        if os.read(pipes.ready_read, 1) != b"R":
            raise ProbeError("native_setup_failed")
        self.observation.child_identity(pid, self.start_ticks, self.helper_pin.pid)
        self.observation.native_limits(pid, self.phase_deadline)
        if self.members() != {pid}:
            raise ProbeError("native_inventory_before_exec")
        if os.write(pipes.gate_write, b"x") != 1:
            raise ProbeError("exec_gate_write")
        self.exec_released = True
        self.close(pipes.gate_write)
        self.receive_diagnostic_banner()
        self.native_gate = stopped_native_gate(
            self.child,
            self.start_ticks,
            self.phase_deadline,
            frozenset({self.owner_pin.pid, pid}),
            DECODER,
            observation_gate=self.stopped_observation,
        )
        self.wait_ready(pipes.ready_read)
        if os.read(pipes.ready_read, 1) != b"":
            raise ProbeError("native_exec_acknowledgement")
        self.close(pipes.ready_read)
        self.members()
        stderr_info = Path(f"/proc/{pid}/fd/2").stat()
        if (stderr_info.st_dev, stderr_info.st_ino) != self.pty_slave_identity:
            raise ProbeError("native_diagnostic_descriptor_changed")
        self.observation.native_descriptors(pid)
        self.admit_stream()

    def stopped_observation(self) -> tuple[dict[str, int], dict[str, object]]:
        limits, mappings = self.observation.stopped_native(
            self.child.pid,
            self.start_ticks,
            DECODER.argv[0],
            self.init_pin,
            self.owner_pin,
            self.helper_pin,
            self.phase_deadline,
        )
        return limits.record(), mappings.record()

    def admit_stream(self) -> None:
        """Preflight the full admission record while the verified native is stopped."""
        assert self.pipes is not None
        data = report_line(
            {
                "event": "decoder_admitted",
                "case_binding": self.case.record(),
                "native_gate": self.native_gate,
                "init": self.init_pin.record(),
                "owner": self.owner_pin.record(),
                "runner": self.helper_pin.record(),
            }
        )
        self.preparation_remaining()
        self.buffers = [bytearray(PCM_BYTES), bytearray(2 * CHUNK_BYTES)]
        for buffer in self.buffers:
            for offset in range(0, len(buffer), 4096):
                buffer[offset] = 1
        self.views = [memoryview(buffer) for buffer in self.buffers]
        self.bank = self.views[0]
        self.output_view = self.views[1][:CHUNK_BYTES]
        self.views.append(self.output_view)
        for fd in (0, self.pipes.input_write, self.pipes.output_read, self.pipes.error_read):
            os.set_blocking(fd, False)
        self.preparation_remaining()
        os.kill(self.child.pid, signal.SIGCONT)
        self.input_released = True
        write_report(data)

    def read_pcm(self) -> None:
        assert self.pipes is not None and self.output_view is not None and self.bank is not None
        fd = self.pipes.output_read
        self.busy.add(fd)
        try:
            with self.output_view[: min(CHUNK_BYTES, PCM_LIMIT - self.output_count + 1)] as target:
                count = os.readv(fd, [target])
            if count == 0:
                self.output_eof = True
                return
            self.output_count += count
            if self.output_count > PCM_LIMIT:
                raise ProbeError("pcm_overrun")
            with self.output_view[:count] as chunk:
                self.pcm_hash.update(chunk)
                if self.ready_seconds is None and self.cancel.started is None:
                    if self.bank_filled + count > len(self.bank):
                        raise ProbeError("readiness_bank_overrun")
                    self.bank[self.bank_filled : self.bank_filled + count] = chunk
                    self.bank_filled += count
                    if self.bank_filled // 4 * 4 >= READY_BYTES:
                        self.ready_seconds = time.monotonic() - self.started
                        if time.monotonic() > self.phase_deadline:
                            raise ProbeError("readiness_late")
                elif self.cancel.started is None:
                    self.bank[:count] = chunk
                else:
                    self.drained += count
        except BlockingIOError, InterruptedError:
            return
        finally:
            self.busy.remove(fd)

    def allocate_diagnostics(self) -> None:
        try:
            if self.child.pid <= 0 or self.stderr or self.diagnostic_slab is not None:
                raise ProbeError("diagnostic_backing_owner")
            self.diagnostic_check(live=True)
            self.stderr = bytearray(DIAGNOSTIC_BYTES)
            self.diagnostic_check(live=True)
            self.diagnostic_slab = bytearray(READ_BYTES)
            self.diagnostic_check(live=True)
        except BaseException:
            self.clock.remaining(self.phase_deadline)
            self.fail("diagnostic_allocation_failed")
            raise

    def diagnostic_check(self, live: bool = False) -> None:
        self.clock.observe()
        if live or self.cancel.started is None:
            if (
                self.clock.remaining(
                    self.phase_deadline if not self.input_released else self.decode_end
                )
                <= 0
            ):
                raise ProbeError("diagnostic_deadline_or_cancel")
            if not self.input_released:
                self.preparation_remaining()

    def read_diagnostic_bytes(self, limit: int, *, preparing: bool = False) -> None:
        if self.pipes is None or self.diagnostic_slab is None:
            raise ProbeError("diagnostic_backing_missing")
        fd = self.pipes.error_read
        self.busy.add(fd)
        try:
            self.diagnostic_check(preparing)
            room = DIAGNOSTIC_BYTES - self.stderr_count
            request = min(READ_BYTES, limit, room + 1)
            try:
                with memoryview(self.diagnostic_slab)[:request] as target:
                    amount = os.readv(fd, [target])
            except BlockingIOError, InterruptedError:
                return
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                amount = 0
            if not 0 <= amount <= request:
                raise ProbeError("diagnostic_read_contract")
            kept = min(amount, room)
            with (
                memoryview(self.stderr) as backing,
                memoryview(self.diagnostic_slab)[:kept] as part,
            ):
                backing[self.stderr_count : self.stderr_count + kept] = part
            self.stderr_count += kept
            if amount > room:
                raise ProbeError("diagnostic_overflow")
            if amount == 0:
                # EOF/EIO describes the channel, never payload/process release.
                self.error_eof = True
            self.diagnostic_check(preparing)
        except BaseException:
            # An error after a held read may also cross the original phase
            # deadline. Adopt that earlier obligation before the caller closes.
            self.clock.remaining(
                self.phase_deadline if not self.input_released else self.decode_end
            )
            self.fail("diagnostic_capture_failed")
            raise
        finally:
            self.clock.observe()
            self.busy.remove(fd)

    def read_error(self) -> None:
        self.read_diagnostic_bytes(READ_BYTES)

    def receive_diagnostic_banner(self) -> None:
        try:
            assert self.pipes is not None
            banner = DECODER.banner
            while self.stderr_count < len(banner):
                self.diagnostic_check(live=True)
                self.child.reap()
                if self.child.exit_code is not None:
                    raise ProbeError("native_exit_before_banner")
                ready, _, _ = select.select(
                    [self.pipes.output_read, self.pipes.error_read],
                    [],
                    [],
                    min(0.05, self.preparation_remaining()),
                )
                if self.pipes.output_read in ready:
                    raise ProbeError("native_stdout_before_input_gate")
                if self.pipes.error_read in ready:
                    self.read_diagnostic_bytes(len(banner) - self.stderr_count, preparing=True)
                    if self.error_eof or any(
                        self.stderr[index] != banner[index] for index in range(self.stderr_count)
                    ):
                        raise ProbeError("native_banner_absent_or_changed")
            self.diagnostic_check(live=True)
        except BaseException:
            self.clock.remaining(self.phase_deadline)
            self.fail("diagnostic_banner_failed")
            raise

    def complete_diagnostics(self) -> None:
        if self.diagnostic_attempted:
            raise ProbeError("diagnostic_validation_already_attempted")
        self.diagnostic_attempted = self.diagnostic_active = True
        try:
            if not (
                self.input_complete
                and self.output_eof
                and self.error_eof
                and self.child.exit_code == 0
            ):
                raise ProbeError("diagnostic_completion_before_settlement")
            self.diagnostic_check(live=True)
            with memoryview(self.stderr)[: self.stderr_count] as data:
                facts = scan(data)
            self.diagnostic_check(live=True)
            self.diagnostic_facts = facts
        except BaseException:
            self.clock.remaining(
                self.phase_deadline if not self.input_released else self.decode_end
            )
            self.fail("diagnostic_validation_failed")
            raise
        finally:
            self.diagnostic_active = False

    def diagnostic_report(self, passed: bool) -> dict[str, object]:
        if passed:
            assert self.diagnostic_facts is not None
            return self.diagnostic_facts.record()
        if self.diagnostic_facts is not None:
            return {"bytes": self.diagnostic_facts.count, "sha256": self.diagnostic_facts.sha256}
        self.diagnostic_active = True
        try:
            with memoryview(self.stderr)[: self.stderr_count] as data:
                return {"bytes": self.stderr_count, "sha256": hashlib.sha256(data).hexdigest()}
        finally:
            self.clock.observe()
            self.diagnostic_active = False

    def can_feed(self) -> bool:
        return self.cancel.started is None and self.input_offset < len(self.input_buffer)

    def write_input(self) -> None:
        assert self.pipes is not None
        fd = self.pipes.input_write
        self.busy.add(fd)
        try:
            with memoryview(self.input_buffer)[self.input_offset :] as part:
                count = os.write(fd, part)
            if count <= 0:
                raise ProbeError("native_input_stalled")
            self.input_count += count
            self.input_offset += count
            if self.input_offset == len(self.input_buffer):
                self.input_buffer.clear()
                self.input_offset = 0
        except BlockingIOError, InterruptedError:
            return
        finally:
            self.busy.remove(fd)

    def check_trigger(self) -> None:
        if self.cancel.started is not None:
            return
        if self.clock.remaining(self.decode_end) <= 0:
            raise ProbeError("whole_or_decode_deadline")
        if self.ready_seconds is None and time.monotonic() > self.phase_deadline:
            raise ProbeError("readiness_deadline")
        if self.members() - {self.child.pid}:
            raise ProbeError("unexpected_descendant")
        if self.child.exit_code is not None and not self.input_complete:
            raise ProbeError("native_exit_before_exact_eof")

    def io_step(self, timeout: float = 0.01) -> None:
        if (
            self.input_released
            and self.cancel.started is None
            and not self.input_eof
            and not self.input_buffer
            and select.select([0], [], [], 0)[0]
        ):
            self.busy.add(0)
            try:
                data = os.read(0, min(CHUNK_BYTES, self.case.bytes - self.received + 1))
                self.received += len(data)
                if self.received > self.case.bytes:
                    raise ProbeError("compressed_input_overrun")
                self.input_hash.update(data)
                self.input_buffer.extend(data)
                if not data:
                    self.input_eof = True
                    if (
                        self.received != self.case.bytes
                        or self.input_hash.hexdigest() != self.case.sha256
                    ):
                        raise ProbeError("compressed_input_binding")
                    assert self.pipes is not None
                    self.close(self.pipes.input_write)
                    self.close(0)
                    self.input_complete = True
            finally:
                self.busy.remove(0)
        super().io_step(timeout)
        if (
            self.cancel.started is None
            and self.output_eof
            and self.error_eof
            and self.child.exit_code is not None
        ):
            if (
                self.child.exit_code != 0
                or not self.input_complete
                or self.output_count % 4
                or self.output_count < READY_BYTES
            ):
                raise ProbeError("incomplete_full_stream")
            self.complete_diagnostics()
            self.request("complete")

    def release_if_done(self) -> bool:
        if self.released:
            return True
        self.clock.observe()
        self.cancel.deadline_missed |= self.clock.missed
        if (
            self.cancel.started is None
            or self.busy
            or self.diagnostic_active
            or self.observation.debt
            or self.fds
            or (self.child.pid and self.child.exit_code is None)
        ):
            return False
        self.retained_pids = sorted(self.members())
        if self.retained_pids:
            return False
        for view in reversed(self.views):
            view.release()
        self.views.clear()
        self.input_view = self.output_view = self.bank = None
        self.buffers.clear()
        self.input_buffer.clear()
        self.pcm_digest = self.pcm_hash.hexdigest()
        if not self.final_resources_attempted:
            # One attempt, after child/I/O settlement and before release. A
            # held or uncertain close owns this workspace and container debt.
            self.final_resources_attempted = True
            try:
                after = self.observation.resources(cleanup=True)
                if self.resources_before is None:
                    raise ProbeError("memory_baseline_missing")
                self.memory_delta = after.delta(self.resources_before)
                self.swap = str(after.swap)
            except ProbeError:
                self.fail("final_resources_failed")
        if self.observation.debt:
            return False
        self.released_at = time.monotonic()
        self.clock.observe()
        self.released = True
        return True

    def finish(self) -> dict[str, object]:
        self.require_released()
        self.clock.observe()
        passed = (
            self.failure is None
            and self.diagnostic_facts is not None
            and not self.clock.missed
            and not self.clock.case_missed
            and not self.clock.timing_unknown
            and self.memory_delta.get("oom") == 0
            and self.memory_delta.get("oom_kill") == 0
            and self.swap == "0"
        )
        # A failure-prefix hash may block too. Observe its actual completion
        # before projecting cleanup timing; never export a pre-hash verdict.
        diagnostic = self.diagnostic_report(passed)
        return {
            "event": "full_stream_result",
            "schema": self.case.schema("decode"),
            "case_binding": self.case.record(),
            "runner": self.helper_pin.record(),
            "passed": passed,
            "failure": self.failure,
            "input_bytes": self.input_count,
            "input_sha256": self.input_hash.hexdigest(),
            "input_complete": self.input_complete,
            "stdout_eof": self.output_eof,
            "stderr_eof": self.error_eof,
            "exit_code": self.child.exit_code,
            "pcm_bytes": self.output_count,
            "pcm_sha256": self.pcm_digest,
            "measured_duration_seconds": self.output_count / 192_000,
            "web_duration_seconds": None if self.case.synthetic else 194,
            "ready_seconds": self.ready_seconds,
            "cleanup": self.clock.receipt(),
            "cleanup_debt": not self.released,
            "memory_event_delta": self.memory_delta,
            "swap_current": self.swap,
            "diagnostics": diagnostic,
            "catalog_admitted": False,
            "listening": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--whole-end", type=float, required=True)
    parser.add_argument("--expires", type=float, required=True)
    parser.add_argument("--cancel-fd", type=int, required=True)
    parser.add_argument("--admit-fd", type=int, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--case", choices=CASE_IDS, default="night-owl")
    args = parser.parse_args()
    if not args.execute:
        parser.error("explicit finite native evaluation required")
    case = CaseBinding(cast(CaseId, args.case))
    if os.read(args.admit_fd, 1) != b"x":
        return 1
    os.close(args.admit_fd)
    init = ProcessPin.from_record(json.loads(args.init))
    owner = ProcessPin.from_record(json.loads(args.owner))
    clock = CaseClock(args.whole_end, args.expires)
    run = StreamRun(init, owner, clock, args.cancel_fd, case)

    def interrupted(_signum: int, _frame: FrameType | None) -> None:
        run.failure = run.failure or "external_cancel"
        run.request("external_signal")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    try:
        run.prepare()
    except Exception as error:
        run.fail(error)
    while not run.released:
        for operation in (
            run.manage_cancel,
            run.io_step,
            run.reconcile_closes,
            run.release_if_done,
        ):
            try:
                operation()
            except Exception as error:
                run.fail(error)
                select.select([], [], [], 0.01)
    result = run.finish()
    write_report(report_line(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
