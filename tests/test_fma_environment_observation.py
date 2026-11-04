"""Bounded synthetic environment/counter input and final-settlement schedules."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest

from evaluations.fma_validation import decoder, owner
from evaluations.fma_validation import observation as observation_module
from evaluations.fma_validation.cases import NIGHT_OWL
from evaluations.fma_validation.consumer import reports
from evaluations.fma_validation.diagnostics import DiagnosticFacts
from evaluations.fma_validation.limits import COPY_PARTITIONS, CaseClock, encode_report
from evaluations.fma_validation.observation import (
    INPUT_LIMIT,
    SELECTED_RESOURCE_BYTES,
    ObservationWorkspace,
    ProcessPin,
    ResourceFacts,
)
from evaluations.fma_validation.packet import Packet
from evaluations.linux_resource import owned_mp3_cancel as cancellation
from evaluations.linux_resource.probe import ProbeError
from tests.test_fma_failure_receipts import terminal
from tests.test_fma_native_observation import NativeStreams
from tests.test_fma_process_observation import Bridge, MonotonicClock

EVENTS = b"/sys/fs/cgroup/memory.events"
SWAP = b"/sys/fs/cgroup/memory.swap.current"
MOUNTS = b"/proc/self/mountinfo"
COUNTERS = b"low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n"
MOUNT_TABLE = (
    b"1 0 0:1 / /sys/fs/cgroup ro,nosuid - cgroup2 cgroup rw\n"
    b"2 0 0:2 /repo /source ro,relatime - ext4 /dev/sda rw\n"
    b"3 0 0:2 /file /input/original ro - ext4 /dev/sda rw\n"
    b"4 0 0:2 /packet /case/packet.json ro - ext4 /dev/sda rw\n"
    b"5 0 0:2 /volume /pending rw - ext4 /dev/sda rw\n"
)


@dataclass
class Environment:
    observation: ObservationWorkspace
    streams: NativeStreams
    now: list[float]


def environment(monkeypatch: pytest.MonkeyPatch, chunk: int = 2048) -> Environment:
    now = [100.0]
    clock = CaseClock(200, 300, wall=lambda: now[0], monotonic=lambda: now[0])
    streams = NativeStreams(chunk)
    streams.files.update(
        {
            MOUNTS: MOUNT_TABLE,
            EVENTS: COUNTERS,
            SWAP: b"0\n",
            b"/sys/fs/cgroup/memory.current": b"123456\n",
            b"/sys/fs/cgroup/memory.max": b"90177536\n",
            b"/sys/fs/cgroup/memory.swap.max": b"0\n",
            b"/sys/fs/cgroup/pids.max": b"64\n",
            b"/sys/fs/cgroup/cpu.max": b"200000 100000\n",
            b"/proc/sys/kernel/core_pattern": b"core\n",
        }
    )

    def unwritable(_path: bytes, _mode: int) -> bool:
        return False

    monkeypatch.setattr(observation_module.os, "getpid", lambda: 2)
    monkeypatch.setattr(observation_module.os, "access", unwritable)
    return Environment(ObservationWorkspace(clock, Bridge(), streams), streams, now)


@pytest.mark.parametrize("chunk", [1, 2, 7, 19, 32, 64, 511, 1024, 2048])
@pytest.mark.parametrize("intake", [False, True])
def test_environment_fragments_preserve_gates_and_discard_control_values(
    monkeypatch: pytest.MonkeyPatch, chunk: int, intake: bool
) -> None:
    env = environment(monkeypatch, chunk)
    env.observation.environment(intake, phase_end=150)
    assert env.streams.opened == env.streams.closed
    assert env.observation.committed.resources is None
    assert env.observation.pending_events == ()
    assert len(env.observation.arena) == 8192 and not env.observation.debt
    assert (b"/proc/sys/kernel/core_pattern" in env.streams.paths) is not intake


@pytest.mark.parametrize(
    "path,payload",
    [
        (b"/sys/fs/cgroup/memory.max", b"90177537\n"),
        (b"/sys/fs/cgroup/pids.max", b"65\n"),
        (b"/sys/fs/cgroup/memory.swap.max", b"1\n"),
        (b"/sys/fs/cgroup/memory.current", b"18446744073709551616\n"),
        (b"/sys/fs/cgroup/memory.current", b"9" * 21 + b"\n"),
        (b"/sys/fs/cgroup/memory.current", b"0"),
        (b"/sys/fs/cgroup/memory.current", b"0\n0\n"),
        (b"/sys/fs/cgroup/cpu.max", b"max 100000\n"),
        (b"/sys/fs/cgroup/cpu.max", b"0 0\n"),
        (b"/sys/fs/cgroup/cpu.max", b"18446744073709551614 9223372036854775808\n"),
        (b"/sys/fs/cgroup/cpu.max", b"200000 100000 extra\n"),
        (b"/sys/fs/cgroup/cpu.max", b"200000 100000"),
        (b"/proc/sys/kernel/core_pattern", b"core"),
        (b"/proc/sys/kernel/core_pattern", b"core \n"),
        (b"/proc/sys/kernel/core_pattern", b"|helper\n"),
        (b"/proc/2/cgroup", b"0::/other\n"),
    ],
)
def test_environment_invalid_records_do_not_commit(
    monkeypatch: pytest.MonkeyPatch, path: bytes, payload: bytes
) -> None:
    env = environment(monkeypatch)
    env.streams.files[path] = payload
    previous = env.observation.committed
    with pytest.raises(ProbeError):
        env.observation.environment(False)
    assert env.observation.committed is previous
    assert env.observation.clock.cleanup_start_utc == 100
    assert env.streams.closed == env.streams.opened


@pytest.mark.parametrize("name", [b"memory.max", b"memory.swap.max", b"pids.max", b"cpu.max"])
def test_each_limit_must_be_unwritable(monkeypatch: pytest.MonkeyPatch, name: bytes) -> None:
    env = environment(monkeypatch)

    def writable(path: bytes, _mode: int) -> bool:
        return path.endswith(name)

    monkeypatch.setattr(observation_module.os, "access", writable)
    with pytest.raises(ProbeError):
        env.observation.environment(True)


@pytest.mark.parametrize("extra", [0, 1])
def test_cpu_complete_record_boundary(monkeypatch: pytest.MonkeyPatch, extra: int) -> None:
    env = environment(monkeypatch, 7)
    prefix = b"18446744073709551614 9223372036854775807"
    env.streams.files[b"/sys/fs/cgroup/cpu.max"] = (
        prefix + b" " * (63 + extra - len(prefix)) + b"\n"
    )
    if extra:
        with pytest.raises(ProbeError):
            env.observation.environment(True)
    else:
        env.observation.environment(True)


def test_environment_expiry_after_permission_check_prevents_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = environment(monkeypatch)
    previous = env.observation.committed

    def permission(_path: bytes, _mode: int) -> bool:
        env.now[0] = 151
        return False

    monkeypatch.setattr(observation_module.os, "access", permission)
    with pytest.raises(ProbeError):
        env.observation.environment(True, phase_end=150)
    assert env.observation.committed is previous
    assert env.observation.clock.cleanup_start_utc == 150


@pytest.mark.parametrize(
    "table",
    [
        MOUNT_TABLE + MOUNT_TABLE.splitlines(keepends=True)[0],
        MOUNT_TABLE + MOUNT_TABLE.splitlines(keepends=True)[1].replace(b" ro,", b" rw,"),
        MOUNT_TABLE.replace(b"/source ro,", b"/source rw,"),
        MOUNT_TABLE.replace(b"/source ro,", b"/source ro,rw,"),
        MOUNT_TABLE.replace(b"/source ro,", b"/source ro,ro,"),
        MOUNT_TABLE.replace(b"/source ro,", b"/source readonly,"),
        MOUNT_TABLE.replace(b"/source ro,", b"/source ro,,"),
        MOUNT_TABLE.replace(b"/source", b"/sou\\162ce"),
        MOUNT_TABLE.replace(b"/source", b"//source"),
        MOUNT_TABLE.replace(b"/source", b"/source/.."),
        MOUNT_TABLE.replace(b"/source", b"/source/"),
        MOUNT_TABLE.replace(b"/pending rw", b"/pending ro"),
        MOUNT_TABLE + b"6 0 0:2 / /pending/hidden rw - ext4 /dev/sda rw\n",
        MOUNT_TABLE.replace(b"cgroup2", b"tmpfs"),
        MOUNT_TABLE.replace(b" - cgroup2", b" - - cgroup2"),
        MOUNT_TABLE.replace(b"cgroup rw\n", b"cgroup rw extra\n"),
        MOUNT_TABLE.replace(b"/case/packet.json", b"/case/other.json"),
        MOUNT_TABLE[:-1],
    ],
)
def test_mounts_reject_duplicate_conflicting_escaped_and_incomplete_destinations(
    monkeypatch: pytest.MonkeyPatch, table: bytes
) -> None:
    env = environment(monkeypatch, 7)
    env.streams.files[MOUNTS] = table
    with pytest.raises(ProbeError):
        env.observation.environment(True)
    assert env.streams.closed == env.streams.opened


def padding_mounts(size: int, maximum: int = 1025) -> bytes:
    rows = (size + maximum - 1) // maximum
    result: list[bytes] = []
    for index in range(rows):
        width = size // (rows - index)
        row = f"10 0 0:1 / /other{index} rw - tmpfs tmpfs rw\n".encode()
        assert len(row) <= width <= maximum
        result.append(row[:-1] + b" " * (width - len(row)) + b"\n")
        size -= width
    return b"".join(result)


@pytest.mark.parametrize("maximum", [1025, 2049])
def test_environment_shares_exact_combined_byte_budget(
    monkeypatch: pytest.MonkeyPatch, maximum: int
) -> None:
    env = environment(monkeypatch)
    env.observation.environment(False)
    extra = INPUT_LIMIT - env.observation.input_bytes
    env.streams.files[MOUNTS] += padding_mounts(extra, maximum)
    env.observation.environment(False)
    assert env.observation.input_bytes == INPUT_LIMIT
    env.streams.files[MOUNTS] = b" " + env.streams.files[MOUNTS]
    with pytest.raises(ProbeError):
        env.observation.environment(False)
    assert env.observation.input_bytes == INPUT_LIMIT + 1


def overlay_root_row(width: int) -> bytes:
    """A containerd-snapshotter root overlay row: 12 lowerdirs under snapshot paths."""
    snapshots = "/var/lib/desktop-containerd/daemon/io.containerd.snapshotter.v1.overlayfs"
    lower = ":".join(f"{snapshots}/snapshots/{index}/fs" for index in range(101, 113))
    prefix = f"6 1 0:3 / / ro,relatime - overlay overlay rw,lowerdir={lower},upperdir="
    suffix = f",workdir={snapshots}/snapshots/113/work"
    fill = width - len(prefix) - len(suffix)
    assert fill > 0
    return f"{prefix}{snapshots}/snapshots/113/{'f' * fill}{suffix}".encode()[:width]


@pytest.mark.parametrize("count", [256, 257])
def test_mount_row_boundary(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    env = environment(monkeypatch)
    env.streams.files[MOUNTS] += b"10 0 0:1 / /other rw - tmpfs tmpfs rw\n" * (count - 5)
    if count == 256:
        env.observation.environment(True)
    else:
        with pytest.raises(ProbeError):
            env.observation.environment(True)


@pytest.mark.parametrize("width", [1024, 2048, 2049])
def test_mount_line_boundary(monkeypatch: pytest.MonkeyPatch, width: int) -> None:
    env = environment(monkeypatch)
    row, rest = MOUNT_TABLE.split(b"\n", 1)
    env.streams.files[MOUNTS] = row + b" " * (width - len(row)) + b"\n" + rest
    if width <= 2048:
        env.observation.environment(True)
    else:
        with pytest.raises(ProbeError):
            env.observation.environment(True)
    assert env.streams.closed == env.streams.opened


@pytest.mark.parametrize("width", [1353, 2048, 2049])
@pytest.mark.parametrize("chunk", [7, 2048])
def test_overlay_root_mount_row_boundary(
    monkeypatch: pytest.MonkeyPatch, width: int, chunk: int
) -> None:
    env = environment(monkeypatch, chunk)
    row = overlay_root_row(width)
    assert len(row) == width and row.count(b"lowerdir=") == 1 and row.count(b"/fs:") == 11
    env.streams.files[MOUNTS] = row + b"\n" + MOUNT_TABLE
    if width <= 2048:
        env.observation.environment(True)
        assert len(env.observation.arena) == 8192 and not env.observation.debt
    else:
        with pytest.raises(ProbeError):
            env.observation.environment(True)
    assert env.streams.closed == env.streams.opened


@pytest.mark.parametrize(
    "payload",
    [
        b"oom 0\n",
        COUNTERS + b"oom 0\n",
        COUNTERS[:-1],
        COUNTERS + b"\n",
        COUNTERS + b"bad-key 0\n",
        COUNTERS + b"x" * 33 + b" 0\n",
        COUNTERS + b"extra 18446744073709551616\n",
        COUNTERS + b"extra 0 tail\n",
        COUNTERS + b"extra \xff\n",
        COUNTERS + b"extra -1\n",
        COUNTERS + b"".join(f"key{i} 0\n".encode() for i in range(28)),
    ],
)
def test_counter_rejection_preserves_old_committed_facts(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    env = environment(monkeypatch)
    old = env.observation.resources()
    env.streams.files[EVENTS] = payload
    with pytest.raises(ProbeError):
        env.observation.resources()
    assert env.observation.committed.resources is old


def maximum_events(value: int) -> bytes:
    keys = [b"oom", b"oom_kill", *(f"k{i:031}".encode() for i in range(30))]
    return b"".join(
        key + b" " * (126 - len(key) - len(str(value))) + str(value).encode() + b"\n"
        for key in keys
    )


def test_exact_counter_file_maximum_and_uint64(monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment(monkeypatch, 1)
    # Each 127-byte row plus newline; complete maximum before the EOF probe.
    payload = maximum_events((1 << 64) - 1).replace(b"\n", b" \n")
    assert len(payload) == 4096
    env.streams.files[EVENTS] = payload
    observed = env.observation.resources()
    assert len(observed.events) == 32 and observed.events[-1][1] == (1 << 64) - 1
    env.streams.files[EVENTS] += b"x"
    with pytest.raises(ProbeError):
        env.observation.resources()
    assert env.observation.committed.resources is observed


@pytest.mark.parametrize("payload", [b"0", b"0\n0\n", b"9" * 21 + b"\n", b"9" * 64, b"9" * 65])
def test_swap_complete_scalar_and_bounds(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    env = environment(monkeypatch)
    env.streams.files[SWAP] = payload
    with pytest.raises(ProbeError):
        env.observation.resources()


@pytest.mark.parametrize(
    "events",
    [
        (("oom", 0), ("other", 0)),
        (("oom", 0),),
        (("oom", 0), ("oom_kill", 1)),
        (("oom", (1 << 63) + 2), ("oom_kill", 2)),
    ],
)
def test_delta_key_set_monotonic_and_wire_bounds(events: tuple[tuple[str, int], ...]) -> None:
    with pytest.raises(ProbeError):
        ResourceFacts(events, 0).delta(ResourceFacts((("oom", 1), ("oom_kill", 2)), 0))


def test_delta_accepts_reordered_keys_and_exact_wire_ceiling() -> None:
    before = ResourceFacts((("oom", 0), ("oom_kill", 2)), 0)
    after = ResourceFacts((("oom_kill", 2), ("oom", 1 << 63)), 0)
    assert after.delta(before) == {"oom_kill": 0, "oom": 1 << 63}


@pytest.mark.parametrize("stage", ["read", "close"])
def test_resource_aliases_remain_owned_while_blocked(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    env = environment(monkeypatch)
    env.streams.files[EVENTS] = maximum_events(0)
    before = env.observation.resources()
    env.streams.files[EVENTS] = maximum_events(1)
    entered, release = threading.Event(), threading.Event()
    errors: list[Exception] = []

    def hold() -> None:
        if env.streams.path == SWAP:
            entered.set()
            assert release.wait(3)

    if stage == "read":
        env.streams.before_read = hold
    else:
        env.streams.before_close = hold

    def observe() -> None:
        try:
            env.observation.resources()
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=observe)
    worker.start()
    try:
        assert entered.wait(3)
        assert env.observation.debt and env.observation.fd == 20
        assert env.observation.committed.resources is before
        assert len(env.observation.pending_events) == 32
        if stage == "close":
            staged = env.observation.staged.resources
            assert staged is not None and staged.events[-1][1] == 1 and staged.swap == 0
        assert before.events[-1][1] == 0 and env.observation.pending_events[-1][1] == 1
        with pytest.raises(ProbeError, match="lease_retained"):
            env.observation.resources(cleanup=True)
        env.observation.clock.cancel()
        env.now[0] += 4
        env.observation.clock.cancel()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and len(errors) == 1
    assert env.observation.clock.cleanup_monotonic == 103 and env.observation.clock.missed
    assert env.observation.committed.resources is before and not env.observation.debt


def test_resource_overlap_stays_inside_original_partitions(monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment(monkeypatch)
    env.streams.files[EVENTS] = maximum_events(0)
    before = env.observation.resources()
    env.streams.files[EVENTS] = maximum_events(1)
    after = env.observation.resources()
    delta = after.delta(before)
    record = terminal(ProcessPin(2, 1, 1, "/python", 1, 2, 1000, "/"))
    record.update(memory_event_delta=delta, swap_current=str(after.swap))
    wire = encode_report(record, newline=True)
    received = reports(wire, "decoder")
    assert received[0]["memory_event_delta"] == delta
    assert before.events[-1][1] == 0 and after.events[-1][1] == 1
    assert 4 * 32 * (32 + 32) + 1024 <= SELECTED_RESOURCE_BYTES == 16_384
    assert env.observation.selected_resource_capacity == SELECTED_RESOURCE_BYTES
    assert 696_320 + SELECTED_RESOURCE_BYTES == 712_704 < COPY_PARTITIONS["decoder_runner"]


def test_resource_first_failure_starts_clock_before_held_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = environment(monkeypatch)
    before = env.observation.resources()
    env.streams.files[EVENTS] = COUNTERS[:-1]
    entered, release = threading.Event(), threading.Event()
    failures: list[Exception] = []

    def close() -> None:
        assert env.observation.clock.cleanup_start_utc == 100
        entered.set()
        assert release.wait(3)

    def observe() -> None:
        try:
            env.observation.resources()
        except Exception as error:
            failures.append(error)

    env.streams.before_close = close
    worker = threading.Thread(target=observe)
    worker.start()
    try:
        assert entered.wait(3)
        assert env.observation.debt and env.observation.committed.resources is before
        env.now[0] = 104
        env.observation.clock.cancel()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and len(failures) == 1
    assert env.observation.clock.cleanup_monotonic == 103 and env.observation.clock.missed
    assert env.observation.committed.resources is before


def settled_decoder(monkeypatch: pytest.MonkeyPatch, env: Environment) -> decoder.StreamRun:
    pin = ProcessPin(2, 30, 1, "/synthetic/python", 3, 4, 1000, "/")

    def capture(_cls: type[ProcessPin], _pid: int, _workspace: ObservationWorkspace) -> ProcessPin:
        return pin

    def members() -> set[int]:
        return set()

    monkeypatch.setattr(ProcessPin, "capture", classmethod(capture))
    monkeypatch.setattr(cancellation, "time", MonotonicClock(env.now))
    monkeypatch.setattr(decoder, "time", MonotonicClock(env.now))
    run = decoder.StreamRun(pin, pin, env.observation.clock, -1, workspace=env.observation)
    run.fds.clear()  # Synthetic descriptors were never opened.
    run.resources_before = env.observation.resources()
    # This fixture starts after successful diagnostic validation; resource
    # settlement tests neither reparse nor synthesize a native decode.
    run.diagnostic_facts = DiagnosticFacts("MPEG 1.0 L III vbr 44100 j-s", 0, "0" * 64, None)
    run.request("complete")
    monkeypatch.setattr(run, "members", members)
    return run


def test_decoder_success_preserves_wire_and_finish_does_not_resample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = environment(monkeypatch)
    run = settled_decoder(monkeypatch, env)
    assert run.release_if_done()
    result = run.finish()
    assert result["passed"] is True and result["cleanup_debt"] is False
    assert result["swap_current"] == "0"
    assert result["memory_event_delta"] == {"low": 0, "high": 0, "max": 0, "oom": 0, "oom_kill": 0}
    assert env.streams.opened == env.streams.closed == 4
    env.streams.files[EVENTS] = b"invalid changed later"
    assert run.finish() == result and env.streams.opened == 4


@pytest.mark.parametrize("stage", ["read", "close"])
def test_decoder_final_observation_precedes_release_and_preserves_original_clock(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    env = environment(monkeypatch)
    run = settled_decoder(monkeypatch, env)
    entered, release = threading.Event(), threading.Event()
    result: list[bool] = []

    def hold() -> None:
        if env.streams.path == SWAP:
            entered.set()
            assert release.wait(3)

    if stage == "read":
        env.streams.before_read = hold
    else:
        env.streams.before_close = hold
    worker = threading.Thread(target=lambda: result.append(run.release_if_done()))
    worker.start()
    try:
        assert entered.wait(3)
        assert not run.released and env.observation.debt and run.final_resources_attempted
        env.now[0] = 104
        run.request("repeat")
        assert run.cancel.deadline == 103
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and result == [True]
    assert run.finish()["passed"] is False and run.clock.missed
    opened = env.streams.opened
    assert run.release_if_done()
    run.finish()
    assert env.streams.opened == opened == 4


def test_decoder_uncertain_final_close_retains_debt_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = environment(monkeypatch)
    run = settled_decoder(monkeypatch, env)
    closes: list[bytes] = []

    def fail_close() -> None:
        closes.append(env.streams.path)
        if env.streams.path == SWAP:
            raise OSError("synthetic uncertain close")

    env.streams.before_close = fail_close
    assert not run.release_if_done()
    assert not run.released and run.failure and env.observation.debt
    assert env.observation.fd == 20 and env.observation.pending_events
    opened = env.streams.opened
    for _ in range(3):
        assert not run.release_if_done()
    with pytest.raises(ProbeError, match="lease remains owned"):
        run.finish()
    assert env.streams.opened == opened == 4 and closes == [EVENTS, SWAP]


@pytest.mark.parametrize("change", ["keys", "decrease", "wire_overflow", "swap"])
def test_decoder_final_resource_failure_never_becomes_success(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env = environment(monkeypatch)
    if change == "decrease":
        env.streams.files[EVENTS] = COUNTERS.replace(b"oom 0", b"oom 1")
    run = settled_decoder(monkeypatch, env)
    if change == "keys":
        env.streams.files[EVENTS] += b"new 0\n"
    elif change == "wire_overflow":
        env.streams.files[EVENTS] = COUNTERS.replace(b"oom 0", f"oom {(1 << 63) + 1}".encode())
    elif change == "swap":
        env.streams.files[SWAP] = b"1\n"
    else:
        env.streams.files[EVENTS] = COUNTERS
    assert run.release_if_done()
    assert run.finish()["passed"] is False
    assert env.streams.opened == 4


@pytest.mark.parametrize(
    "failure", ["uncertain_close", "late_close", "unknown_clock", "invalid_counter"]
)
def test_owner_final_observation_precedes_clean_decision(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    env = environment(monkeypatch)
    pin = ProcessPin(2, 30, 1, "/synthetic/python", 3, 4, 1000, "/")
    pending = threading.Event()
    pending.set()

    class Intake:
        def __init__(self) -> None:
            self.pending = pending
            self.joined = False

        def start(self, *, execute: bool) -> None:
            assert execute

        def join(self, _timeout: float) -> bool:
            self.joined = True
            return True

        def cancel(self, *_args: object, **_kwargs: object) -> None:
            pass

        def read_into(self, _consumer: object) -> object:
            def join(_timeout: float) -> bool:
                return True

            return SimpleNamespace(join=join, report=lambda: {"completed": True})

        def report(self) -> dict[str, object]:
            return {"released": True, "cleanup_deadline_met": True}

    intake = Intake()

    def preflight(
        _packet: Packet, _workspace: ObservationWorkspace
    ) -> tuple[ProcessPin, ProcessPin]:
        return pin, pin

    def clock(*_args: object) -> CaseClock:
        return env.observation.clock

    def workspace(_clock: CaseClock) -> ObservationWorkspace:
        return env.observation

    def create_intake(*_args: object) -> Intake:
        return intake

    def consumer(*_args: object, **_kwargs: object) -> object:
        def receipt() -> dict[str, object]:
            return {}

        return SimpleNamespace(finished=True, receipt=receipt)

    def inventory(*_args: object, **_kwargs: object) -> set[int]:
        return set()

    def signal(*_args: object) -> None:
        pass

    def before_read() -> None:
        if env.streams.opened > 2:
            assert intake.joined
            if failure == "invalid_counter" and env.streams.path == EVENTS:
                env.streams.current = b"oom 0\n"

    def before_close() -> None:
        if env.streams.opened == 4 and env.streams.path == SWAP:
            assert intake.joined
            if failure == "uncertain_close":
                raise OSError("synthetic uncertain close")
            if failure == "late_close":
                env.now[0] = 104
            if failure == "unknown_clock":
                env.observation.clock.timing_unknown = True

    env.streams.before_read, env.streams.before_close = before_read, before_close
    monkeypatch.setattr(owner, "preflight", preflight)
    monkeypatch.setattr(owner, "CaseClock", clock)
    monkeypatch.setattr(owner, "ObservationWorkspace", workspace)
    monkeypatch.setattr(owner, "LocalIntake", create_intake)
    monkeypatch.setattr(owner, "ValidationConsumer", consumer)
    monkeypatch.setattr(owner, "inventory", inventory)
    monkeypatch.setattr(owner.signal, "signal", signal)
    packet = cast(
        Packet,
        SimpleNamespace(
            whole_end=200, expires=300, request=None, case=NIGHT_OWL, fingerprint="a" * 64
        ),
    )
    result = owner.execute(packet)
    assert result["passed"] is False
    assert env.observation.clock.cleanup_monotonic == 103
    if failure == "uncertain_close":
        assert env.observation.debt and env.observation.fd == 20
    elif failure == "invalid_counter":
        assert result["memory_event_delta"] == {} and result["failure"]
    else:
        assert env.observation.clock.missed
