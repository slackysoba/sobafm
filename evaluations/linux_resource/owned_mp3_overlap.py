"""Opt-in two-decoder overlap using only the retained, authenticated owned MP3."""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import select
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from .decoder_startup import ADDRESS_SPACE_BYTES, Child, CleanupResult, group_members, verify_child
from .owned_mp3_decode import (
    ARGV,
    BANK_BYTES,
    CHUNK_BYTES,
    CLEANUP_SECONDS,
    FIXTURE_BYTES,
    FIXTURE_SHA256,
    MAPPED_ELF,
    OUTPUT_COPY_OFFSET,
    PCM_OUTPUT_BYTES,
    PREPARE_SECONDS,
    READY_BYTES,
    STDERR_BYTES,
    WORKLOAD_SECONDS,
    decoder_child,
    load_fixture_snapshot,
    native_processes,
    read_start_identity,
    receive_banner,
    stopped_native_gate,
    validate_diagnostics,
    verify_core_limit,
    verify_native_files,
)
from .probe import COPY_BYTES, PCM_BYTES, ProbeError, counters, emit, observe, read_text

SECOND_NS = 1_000_000_000
OVERLAP_NS = 30 * SECOND_NS
PCM_RATE = 192_000
FRAME_BYTES = 3_840
OVERLAP_BYTES = 30 * PCM_RATE
EXPECTED_PCM_SHA256 = (
    "ce1390078e09a1b06bcb10d64515886ce4d865456346d7be1b70e455703062df"  # pragma: allowlist secret
)


@dataclass(frozen=True)
class Pipes:
    input_read: int
    input_write: int
    output_read: int
    output_write: int
    error_read: int
    error_write: int
    ready_read: int
    ready_write: int
    gate_read: int
    gate_write: int

    def child_ends(self) -> tuple[int, ...]:
        return (
            self.input_read,
            self.output_write,
            self.error_write,
            self.ready_write,
            self.gate_read,
        )


def make_pipes(fds: list[int]) -> Pipes:
    ends: list[int] = []
    for _ in range(5):
        pair = os.pipe()
        fds.extend(pair)
        ends.extend(pair)
    return Pipes(*ends)


def close_owned(fd: int, fds: list[int]) -> None:
    if fd not in fds:
        raise ProbeError("descriptor ownership was already released")
    try:
        os.close(fd)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
    fds.remove(fd)


@dataclass
class Bank:
    capacity: int = BANK_BYTES
    position: int = 0
    occupied: int = 0
    high_water: int = 0
    drained: int = 0

    @property
    def free(self) -> int:
        return self.capacity - self.occupied

    def put(self, destination: memoryview, payload: memoryview) -> None:
        amount = len(payload)
        if len(destination) != self.capacity or not 0 < amount <= self.free:
            raise ProbeError("PCM bank write exceeds its exclusive capacity")
        start = (self.position + self.occupied) % self.capacity
        first = min(amount, self.capacity - start)
        with destination[start : start + first] as target, payload[:first] as source:
            target[:] = source
        if first != amount:
            with destination[: amount - first] as target, payload[first:] as source:
                target[:] = source
        self.occupied += amount
        self.high_water = max(self.high_water, self.occupied)

    def drain(self, amount: int) -> None:
        if amount < 0 or amount % 4 or amount > self.occupied:
            raise ProbeError("PCM drain lacks owned complete frames")
        self.position = (self.position + amount) % self.capacity
        self.occupied -= amount
        self.drained += amount


@dataclass
class PacedOverlap:
    started_ns: int
    drained_per_bank: int = 0

    def drain_due(self, banks: list[Bank], now_ns: int) -> bool:
        if len(banks) != 2 or banks[0] is banks[1] or now_ns < self.started_ns:
            raise ProbeError("invalid independent-bank pacing state")
        elapsed = min(now_ns - self.started_ns, OVERLAP_NS)
        due = elapsed * PCM_RATE // SECOND_NS // FRAME_BYTES * FRAME_BYTES
        amount = due - self.drained_per_bank
        if amount < 0 or any(bank.occupied < amount for bank in banks):
            raise ProbeError("paced overlap underflow or clock reversal")
        for bank in banks:
            bank.drain(amount)
        self.drained_per_bank = due
        return elapsed == OVERLAP_NS and due == OVERLAP_BYTES


@dataclass
class Decoder:
    pipes: Pipes
    child: Child = field(default_factory=lambda: Child(0))
    started: float = 0.0
    start_ticks: int = 0
    stderr: bytearray = field(default_factory=bytearray)
    native_gate: dict[str, object] = field(default_factory=lambda: dict[str, object]())
    exec_released: bool = False
    input_released: bool = False
    input_open: bool = True
    output_open: bool = True
    error_open: bool = True
    input_count: int = 0
    output_count: int = 0
    pcm_sha256: str = field(default_factory=lambda: hashlib.sha256().hexdigest())
    ready_seconds: float | None = None
    ready_buffered: int = 0


def fork_pair(owners: list[Decoder], fds: list[int]) -> None:
    if len(owners) != 2 or any(owner.child.pid != 0 for owner in owners):
        raise ProbeError("exactly two fresh decoder owners required")
    # All twenty pipe ends exist before either fork, so each child closes cross-child ends.
    for owner in owners:
        owner.started = time.monotonic()
        pid = os.fork()
        if pid == 0:
            pipes = owner.pipes
            decoder_child(
                pipes.input_read,
                pipes.output_write,
                pipes.error_write,
                pipes.ready_write,
                pipes.gate_read,
                fds,
            )
            os._exit(125)
        owner.child.pid = pid
    for owner in owners:
        for fd in owner.pipes.child_ends():
            close_owned(fd, fds)


def admit_pair(owners: list[Decoder], fds: list[int]) -> None:
    expected = frozenset(owner.child.pid for owner in owners)
    if len(owners) != 2 or len(expected) != 2 or min(expected) <= 0:
        raise ProbeError("two distinct forked decoder owners required")
    for index, owner in enumerate(owners):
        remaining = owner.started + PREPARE_SECONDS - time.monotonic()
        ready = owner.pipes.ready_read
        if remaining <= 0 or not select.select([ready], [], [], remaining)[0]:
            raise ProbeError("paired decoder setup deadline exceeded")
        if os.read(ready, 1) != b"R":
            raise ProbeError("paired decoder limit setup failed")
        limits = verify_child(owner.child.pid, True)
        verify_core_limit(read_text(Path(f"/proc/{owner.child.pid}/limits")))
        _, owner.start_ticks = read_start_identity(owner.child.pid)
        emit(
            {
                "event": "overlap_exec_gate",
                "decoder": index,
                "rlimits": limits | {"Max core file size": 0},
                "input_bytes": 0,
                "parent_pcm_allocated": False,
            }
        )
    for owner in owners:
        if os.write(owner.pipes.gate_write, b"x") != 1:
            raise ProbeError("short paired exec-gate write")
        owner.exec_released = True
        close_owned(owner.pipes.gate_write, fds)
    for index, owner in enumerate(owners):
        deadline = owner.started + PREPARE_SECONDS
        owner.stderr = receive_banner(
            owner.child, owner.pipes.output_read, owner.pipes.error_read, deadline
        )
        owner.native_gate = stopped_native_gate(owner.child, owner.start_ticks, deadline, expected)
        emit({"event": "overlap_native_gate", "decoder": index, "observation": owner.native_gate})


def require_pair_alive(owners: list[Decoder], stopped: bool | None = None) -> None:
    expected = {owner.child.pid for owner in owners}
    if len(owners) != 2 or len(expected) != 2 or native_processes() != expected:
        raise ProbeError("paired whole-group ownership differs")
    for owner in owners:
        owner.child.reap()
        state, ticks = read_start_identity(owner.child.pid)
        if (
            owner.child.exit_code is not None
            or ticks != owner.start_ticks
            or state in {"Z", "X"}
            or (stopped is True and state != "T")
            or (stopped is False and state == "T")
            or group_members(owner.child.pid) != [owner.child.pid]
        ):
            raise ProbeError("paired live native identity changed")


def signal_owned(children: list[Child], sig: signal.Signals) -> None:
    for child in children:
        if child.exit_code is None:
            with suppress(ProcessLookupError):
                os.kill(child.pid, sig)
        with suppress(ProcessLookupError):
            os.killpg(child.pid, sig)


def cleanup_pair(children: list[Child], deadline: float) -> CleanupResult:
    """Both slots retain the same original deadline, including second-owner debt."""
    for child in children:
        child.reap()
    signal_owned(children, signal.SIGTERM)
    signal_owned(children, signal.SIGCONT)
    kill_at = min(deadline, time.monotonic() + 1.0)
    killed = False
    while time.monotonic() < deadline:
        for child in children:
            child.reap()
        retained = native_processes()
        for child in children:
            retained.update(group_members(child.pid))
        now = time.monotonic()
        if all(child.exit_code is not None for child in children) and not retained:
            return CleanupResult(now <= deadline, [])
        if now >= kill_at and not killed:
            signal_owned(children, signal.SIGKILL)
            killed = True
        select.select([], [], [], min(0.01, max(0.0, deadline - time.monotonic())))
    # A scheduler stall cannot omit the hard-stop attempt. Late release stays failure.
    if not killed:
        signal_owned(children, signal.SIGKILL)
    for child in children:
        child.reap()
    retained = native_processes()
    for child in children:
        retained.update(group_members(child.pid))
    return CleanupResult(False, sorted(retained))


def allocate_payloads(buffers: list[bytearray], views: list[memoryview]) -> None:
    for size in (BANK_BYTES, BANK_BYTES, COPY_BYTES):
        buffers.append(bytearray(size))
        for offset in range(0, size, 4096):
            buffers[-1][offset] = 1
        views.append(memoryview(buffers[-1]))


def pump_pair(
    owners: list[Decoder],
    fds: list[int],
    banks: list[Bank],
    views: list[memoryview],
    workload_deadline: float,
) -> dict[str, object]:
    bank_views = views[:2]
    input_view = views[3]
    output_views = views[4:6]
    hashes = [hashlib.sha256(), hashlib.sha256()]
    pace: PacedOverlap | None = None
    overlap_done = False
    overlap_seconds = 0.0
    expected = {owner.child.pid for owner in owners}
    for owner in owners:
        for fd in (owner.pipes.input_write, owner.pipes.output_read, owner.pipes.error_read):
            os.set_blocking(fd, False)
        os.kill(owner.child.pid, signal.SIGCONT)
        owner.input_released = True
    while any(
        owner.input_open or owner.output_open or owner.error_open or owner.child.exit_code is None
        for owner in owners
    ):
        for owner in owners:
            owner.child.reap()
        now = time.monotonic()
        deadlines = [workload_deadline] + [
            owner.started + PREPARE_SECONDS for owner in owners if owner.ready_seconds is None
        ]
        remaining = min(deadlines) - now
        if remaining <= 0:
            raise ProbeError("paired readiness or workload deadline exceeded")
        if native_processes() - expected:
            raise ProbeError("unadmitted process during overlap; retain its cleanup debt")
        if not overlap_done:
            require_pair_alive(owners, stopped=False if pace is not None else None)
            if pace is not None:
                observed_ns = time.monotonic_ns()
                overlap_done = pace.drain_due(banks, observed_ns)
                if overlap_done:
                    require_pair_alive(owners, stopped=False)
                    overlap_seconds = (observed_ns - pace.started_ns) / SECOND_NS
                    emit(
                        {
                            "event": "overlap_interval_complete",
                            "seconds": overlap_seconds,
                            "drained_bytes_per_decoder": pace.drained_per_bank,
                            "live_decoders": 2,
                        }
                    )
        else:
            for bank in banks:
                bank.drain(bank.occupied // 4 * 4)
        reads: list[int] = []
        writes: list[int] = []
        for owner, bank in zip(owners, banks, strict=True):
            if owner.child.exit_code is not None and owner.input_count != FIXTURE_BYTES:
                raise ProbeError("decoder exited with incomplete owned input")
            if owner.output_open and bank.free:
                reads.append(owner.pipes.output_read)
            if owner.error_open:
                reads.append(owner.pipes.error_read)
            if owner.input_open:
                writes.append(owner.pipes.input_write)
        readable, writable, _ = select.select(reads, writes, [], min(remaining, 0.02))
        for index, (owner, bank) in enumerate(zip(owners, banks, strict=True)):
            pipes = owner.pipes
            if pipes.error_read in readable:
                room = STDERR_BYTES - len(owner.stderr)
                part = os.read(pipes.error_read, min(4096, room + 1))
                owner.stderr.extend(part[:room])
                if len(part) > room:
                    raise ProbeError("paired decoder diagnostic capacity exceeded")
                if not part:
                    owner.error_open = False
            if pipes.output_read in readable:
                limit = min(CHUNK_BYTES, bank.free, PCM_OUTPUT_BYTES - owner.output_count + 1)
                with output_views[index][:limit] as destination:
                    amount = os.readv(pipes.output_read, [destination])
                if not amount:
                    owner.output_open = False
                else:
                    owner.output_count += amount
                    if owner.output_count > PCM_OUTPUT_BYTES:
                        raise ProbeError("paired PCM exceeds the known fixture count")
                    with output_views[index][:amount] as payload:
                        hashes[index].update(payload)
                        owner.pcm_sha256 = hashes[index].hexdigest()
                        bank.put(bank_views[index], payload)
                    if owner.ready_seconds is None and bank.occupied // 4 * 4 >= READY_BYTES:
                        owner.ready_seconds = time.monotonic() - owner.started
                        owner.ready_buffered = bank.occupied // 4 * 4
                        if owner.ready_seconds > PREPARE_SECONDS:
                            raise ProbeError("paired readiness arrived after the original deadline")
            if pipes.input_write in writable:
                end = min(FIXTURE_BYTES, owner.input_count + CHUNK_BYTES)
                with input_view[owner.input_count : end] as payload:
                    try:
                        amount = os.write(pipes.input_write, payload)
                    except BlockingIOError:
                        continue
                if amount <= 0:
                    raise ProbeError("paired input made no progress")
                owner.input_count += amount
                if owner.input_count == FIXTURE_BYTES:
                    close_owned(pipes.input_write, fds)
                    owner.input_open = False
        if pace is None and all(owner.ready_seconds is not None for owner in owners):
            require_pair_alive(owners, stopped=False)
            pace = PacedOverlap(time.monotonic_ns())
            emit(
                {
                    "event": "overlap_interval_start",
                    "ready_buffered_bytes": [bank.occupied // 4 * 4 for bank in banks],
                    "live_decoders": 2,
                    "parent_pcm_bank_capacity": PCM_BYTES,
                }
            )
    if pace is None or not overlap_done or pace.drained_per_bank != OVERLAP_BYTES:
        raise ProbeError("full simultaneous overlap interval was not established")
    digests = [item.hexdigest() for item in hashes]
    for owner, bank, digest in zip(owners, banks, digests, strict=True):
        bank.drain(bank.occupied // 4 * 4)
        if not select.select([owner.pipes.ready_read], [], [], 0)[0]:
            raise ProbeError("paired exec acknowledgement remains owned")
        if os.read(owner.pipes.ready_read, 2) != b"" or owner.child.exit_code != 0:
            raise ProbeError("paired decoder or exec did not finish successfully")
        if (
            owner.input_count != FIXTURE_BYTES
            or owner.output_count != PCM_OUTPUT_BYTES
            or bank.occupied != 0
            or bank.drained != PCM_OUTPUT_BYTES
            or digest != EXPECTED_PCM_SHA256
        ):
            raise ProbeError("paired known completion or independent drain accounting differs")
        validate_diagnostics(bytes(owner.stderr))
    if time.monotonic() > workload_deadline:
        raise ProbeError("paired completion evidence exceeded workload deadline")
    return {
        "seconds": overlap_seconds,
        "drained_bytes_per_decoder": pace.drained_per_bank,
        "pcm_sha256": digests,
        "completed_decoders": 2,
    }


def run_overlap() -> dict[str, object]:
    if (
        not 0 < FIXTURE_BYTES <= OUTPUT_COPY_OFFSET
        or OUTPUT_COPY_OFFSET + 2 * CHUNK_BYTES > COPY_BYTES
        or not 0 < READY_BYTES <= BANK_BYTES
        or 2 * BANK_BYTES != PCM_BYTES
        or BANK_BYTES % 4
    ):
        raise ProbeError("paired payload partitions exceed the existing reservations")
    if (
        os.sysconf("SC_PAGE_SIZE") != 4096
        or len(list(Path("/proc/self/task").iterdir())) != 1
        or native_processes()
        or read_text(Path("/proc/sys/kernel/core_pattern")).strip() != "core"
    ):
        raise ProbeError("fresh single-threaded proof process and reviewed core routing required")
    verify_native_files()
    before = counters(read_text(Path("/sys/fs/cgroup/memory.events")))
    fds: list[int] = []
    owners: list[Decoder] = []
    buffers: list[bytearray] = []
    views: list[memoryview] = []
    banks = [Bank(BANK_BYTES), Bank(BANK_BYTES)]
    failure: str | None = None
    phase = "paired_exec_setup"
    started = time.monotonic()
    overlap: dict[str, object] = {}
    retained: list[int] = []
    cleanup_met = False
    debt = True
    events: dict[str, int] = {}
    swap = "unobserved"
    try:
        owners = [Decoder(make_pipes(fds)), Decoder(make_pipes(fds))]
        fork_pair(owners, fds)
        admit_pair(owners, fds)
        require_pair_alive(owners, stopped=True)
        phase = "paired_fixture_snapshot"
        allocate_payloads(buffers, views)
        copy_view = views[2]
        load_fixture_snapshot(copy_view)
        views.append(copy_view[:FIXTURE_BYTES].toreadonly())
        for index in range(2):
            start = OUTPUT_COPY_OFFSET + index * CHUNK_BYTES
            views.append(copy_view[start : start + CHUNK_BYTES])
        if any(time.monotonic() >= owner.started + PREPARE_SECONDS for owner in owners):
            raise ProbeError("paired input snapshot exceeded original readiness deadline")
        require_pair_alive(owners, stopped=True)
        emit(
            {
                "event": "overlap_input_gate",
                "fixture_bytes": FIXTURE_BYTES,
                "fixture_sha256": FIXTURE_SHA256,
                "shared_readonly_input_snapshot": True,
                "native_address_spaces": 2,
                "pcm_bank_capacity": PCM_BYTES,
                "copy_capacity": COPY_BYTES,
                "pcm_capacity_with_two_address_spaces": 2 * ADDRESS_SPACE_BYTES
                + PCM_BYTES
                + COPY_BYTES,
                "output_copy_offsets": [OUTPUT_COPY_OFFSET, OUTPUT_COPY_OFFSET + CHUNK_BYTES],
            }
        )
        phase = "paced_overlap"
        overlap = pump_pair(owners, fds, banks, views, started + WORKLOAD_SECONDS)
    except (OSError, ProbeError, ValueError, MemoryError) as error:
        failure = f"{type(error).__name__}: {error}"
    finally:
        deadline = time.monotonic() + CLEANUP_SECONDS
        for fd in tuple(fds):
            try:
                close_owned(fd, fds)
            except OSError as error:
                failure = failure or f"descriptor release failed: {error}"
        children = [owner.child for owner in owners if owner.child.pid > 0]
        try:
            released = cleanup_pair(children, deadline)
            retained = released.retained_pids
            debt = bool(retained) or any(child.exit_code is None for child in children) or bool(fds)
            cleanup_met = released.deadline_met and not debt
        except (OSError, ProbeError, ValueError) as error:
            failure = failure or f"paired cleanup observation failed: {error}"
        for view in reversed(views):
            view.release()
        views.clear()
        buffers.clear()
        if time.monotonic() > deadline:
            cleanup_met = False
        try:
            after = counters(read_text(Path("/sys/fs/cgroup/memory.events")))
            if set(before) != set(after) or any(after[key] < before[key] for key in before):
                raise ProbeError("paired memory counters changed identity or decreased")
            events = {key: after[key] - before[key] for key in before}
            swap = read_text(Path("/sys/fs/cgroup/memory.swap.current")).strip()
        except (OSError, ProbeError, ValueError) as error:
            failure = failure or f"paired final memory observation failed: {error}"
    passed = (
        failure is None
        and len(owners) == 2
        and overlap.get("completed_decoders") == 2
        and all(owner.input_released and owner.child.exit_code == 0 for owner in owners)
        and cleanup_met
        and not debt
        and events.get("oom") == 0
        and events.get("oom_kill") == 0
        and swap == "0"
    )
    return {
        "event": "owned_overlap_result",
        "verdict": "owned_fixture_overlap_pass" if passed else "incomplete",
        "failure": failure,
        "phase": phase,
        "argv": list(ARGV),
        "decoder_sha256": MAPPED_ELF[ARGV[0]],
        "fixture_sha256": FIXTURE_SHA256,
        "overlap": overlap,
        "decoders": [
            {
                "native_gate": owner.native_gate,
                "exec_gate_released": owner.exec_released,
                "input_gate_released": owner.input_released,
                "input_bytes": owner.input_count,
                "output_bytes": owner.output_count,
                "pcm_output_sha256": owner.pcm_sha256,
                "ready_seconds": owner.ready_seconds,
                "ready_buffered_bytes": owner.ready_buffered,
                "bank_high_water": bank.high_water,
                "bank_drained_bytes": bank.drained,
                "bank_retained_bytes_at_cleanup": bank.occupied,
                "exit_code": owner.child.exit_code,
                "leader_reaped": owner.child.pid > 0 and owner.child.exit_code is not None,
                "stderr_bytes": len(owner.stderr),
                "stderr_sha256": hashlib.sha256(owner.stderr).hexdigest(),
                "stderr_preview": owner.stderr[:4096].decode("utf-8", errors="replace"),
            }
            for owner, bank in zip(owners, banks, strict=False)
        ],
        "pcm_bank_capacity": PCM_BYTES,
        "copy_capacity": COPY_BYTES,
        "cleanup_debt": debt,
        "cleanup_deadline_met": cleanup_met,
        "retained_pids": retained,
        "retained_fds": fds,
        "parent_payload_views_released": not views and not buffers,
        "memory_event_delta": events,
        "swap_current": swap,
        "elapsed_seconds": time.monotonic() - started,
        "q3": "partial retained-fixture overlap only; other formats/rates/channels unqualified",
        "q4": "not_run",
        "native_arm64": "not_run",
        "listening": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-owned-mp3-overlap", action="store_true", required=True)
    parser.parse_args()
    try:
        emit({"event": "owned_overlap_preflight", "observation": observe()})
        result = run_overlap()
        emit(result)
        return 0 if result["verdict"] == "owned_fixture_overlap_pass" else 1
    except (OSError, ProbeError, ValueError) as error:
        emit({"event": "owned_overlap_incomplete", "error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
