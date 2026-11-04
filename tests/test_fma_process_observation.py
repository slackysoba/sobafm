"""Synthetic byte streams and lifecycle schedules; no native/namespace probes."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn, Self

import pytest

from evaluations.fma_validation import observation as observation_module
from evaluations.fma_validation.decoder import StreamRun
from evaluations.fma_validation.limits import CaseClock
from evaluations.fma_validation.observation import (
    INPUT_LIMIT,
    DirectoryFacts,
    ExecutableFacts,
    LinuxObservationBridge,
    ObservationWorkspace,
    ProcessPin,
)
from evaluations.linux_resource import owned_mp3_cancel as cancellation
from evaluations.linux_resource.owned_mp3_cancel import CancelRun
from evaluations.linux_resource.probe import ProbeError


def stat(
    pid: int = 2,
    start: int = 30,
    comm: bytes = b"space ) (\nname",
    group: int = 2,
    parent: int = 1,
    state: bytes = b"S",
) -> bytes:
    fields = [0] * 49
    fields[0:4] = [parent, group, group, 0]
    fields[16], fields[18] = 1, start
    return (
        str(pid).encode()
        + b" ("
        + comm
        + b") "
        + state
        + b" "
        + b" ".join(str(value).encode() for value in fields)
        + b"\n"
    )


STATUS = (
    b"Name:\tsynthetic\nPPid:\t1\nUid:\t1000 1000 1000 1000\nGid:\t1000 1000 1000 1000\n"
    b"NoNewPrivs:\t1\nSeccomp:\t2\nCapEff:\t0000000000000000\nThreads:\t1\n"
)


class Streams:
    def __init__(self, chunk: int = 2048) -> None:
        self.files = {
            b"/proc/2/stat": stat(),
            b"/proc/2/status": STATUS,
            b"/proc/2/cgroup": b"0::/\n",
            b"/sys/fs/cgroup/cgroup.procs": b"1\n2\n",
        }
        self.chunk = chunk
        self.offset = 0
        self.current = b""
        self.opened = self.closed = 0
        self.live = False
        self.before_close: Callable[[], None] | None = None
        self.on_open: Callable[[bytes], None] | None = None
        self.read_sizes: list[int] = []

    def open(self, path: bytes) -> int:
        assert not self.live
        if self.on_open is not None:
            self.on_open(path)
        self.current = self.files[path]
        self.offset = 0
        self.opened += 1
        self.live = True
        return 20

    def read(self, fd: int, target: memoryview) -> int:
        assert fd == 20 and self.live
        self.read_sizes.append(len(target))
        size = min(self.chunk, len(target), len(self.current) - self.offset)
        target[:size] = self.current[self.offset : self.offset + size]
        self.offset += size
        return size

    def close(self, fd: int) -> None:
        assert fd == 20 and self.live
        if self.before_close is not None:
            self.before_close()
        self.closed += 1
        self.live = False


class Bridge:
    def __init__(self) -> None:
        self.exe = ExecutableFacts("/synthetic/python", 3, 4)
        self.calls = 0
        self.entries = DirectoryFacts(5, (2,))

    @property
    def debt(self) -> bool:
        return False

    def executable(self, pid: int, charge: Callable[[int], None]) -> ExecutableFacts:
        assert pid > 0
        self.calls += 1
        charge(len(self.exe.text))
        return self.exe

    def directory(
        self, pid: int | None, name: str, limit: int, charge: Callable[[int], None]
    ) -> DirectoryFacts:
        charge(sum(len(str(value)) for value in self.entries.identifiers))
        return self.entries


class SyntheticObservation(ObservationWorkspace):
    def scan_pids(self) -> None:
        self._run(self._scan_pids)

    def _scan_pids(self) -> None:
        self._pids()

    def cumulative(self, extra: bool = False) -> None:
        def action() -> None:
            for _ in range(32):
                assert self._record(b"/proc/2/stat", 2048) == 2048
            if extra:
                self._record(b"/proc/2/cgroup", 16)

        self._run(action)


@dataclass
class MonotonicClock:
    now: list[float]

    def monotonic(self) -> float:
        return self.now[0]


def workspace(
    chunk: int = 2048, *, uptime: float = 100.0
) -> tuple[SyntheticObservation, Streams, Bridge, list[float]]:
    now = [uptime]
    clock = CaseClock(uptime + 100, uptime + 200, wall=lambda: now[0], monotonic=lambda: now[0])
    streams, bridge = Streams(chunk), Bridge()
    return SyntheticObservation(clock, bridge, streams), streams, bridge, now


@pytest.mark.parametrize("chunk", range(1, len(stat()) + 1))
def test_whole_stat_handles_every_split_and_embedded_newline(chunk: int) -> None:
    observation, streams, _, _ = workspace(chunk)
    assert observation.start_identity(2) == ("S", 30)
    assert streams.opened == streams.closed == 1
    assert not observation.debt and len(observation.arena) == 8192


def test_capture_preserves_wire_keys_and_immutable_facts() -> None:
    observation, streams, _, _ = workspace(1)
    pin = ProcessPin.capture(2, observation)
    assert pin == ProcessPin(2, 30, 1, "/synthetic/python", 3, 4, 1000, "/")
    assert ProcessPin.from_record(pin.record()) == pin
    assert observation.committed.pins[0] is pin
    streams.files[b"/proc/2/stat"] = stat(start=31)
    with pytest.raises(ProbeError):
        pin.verify(observation)
    assert observation.committed.pins == (pin,)
    assert pin.start == 30 and observation.clock.cleanup_start_utc == 100


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", True),
        ("pid", 0),
        ("pid", 1 << 31),
        ("start", 1 << 64),
        ("parent", -1),
        ("executable_inode", 0),
        ("uid", 0),
        ("group", "/other"),
        ("executable", "/" + "a" * 64),
        ("executable", "/bad\x00"),
    ],
)
def test_pin_wire_rejects_before_construction(field: str, value: object) -> None:
    row = ProcessPin(2, 30, 1, "/synthetic/python", 3, 4, 1000, "/").record()
    row[field] = value
    with pytest.raises(ProbeError):
        ProcessPin.from_record(row)


@pytest.mark.parametrize(
    "payload",
    [
        STATUS + b"Uid:\t1000 1000 1000 1000\n",
        STATUS.replace(b"Threads:\t1\n", b""),
        STATUS.replace(b"PPid:\t1", b"PPid:\t999999999999999999999999"),
        STATUS.replace(b"Uid:\t1000", b"Uid:\t0"),
        STATUS + b"Bad:\xff\n",
        STATUS + b"Bad:" + b"x" * 509 + b"\n",
        STATUS[:-1],
        STATUS + b"Bad:\r\n",
    ],
)
def test_status_schema_rejection_keeps_previous_commit(payload: bytes) -> None:
    observation, streams, _, _ = workspace(1)
    pin = observation.capture(2)
    streams.files[b"/proc/2/status"] = payload
    with pytest.raises(ProbeError):
        observation.capture(2)
    assert observation.committed.pins == (pin,)
    assert streams.opened == streams.closed and not observation.debt


def test_status_exact_byte_limit_and_extra_probe() -> None:
    def padded(size: int) -> bytes:
        data = STATUS
        while size - len(data) > 513:
            data += b"Padding:" + b"x" * 504 + b"\n"
        return data + b"Padding:" + b"x" * (size - len(data) - 9) + b"\n"

    observation, streams, _, _ = workspace()
    streams.files[b"/proc/2/status"] = padded(16384)
    observation.capture(2)
    assert 1 in streams.read_sizes  # A complete maximum requires a separate EOF probe.
    streams.files[b"/proc/2/status"] += b"x"
    with pytest.raises(ProbeError):
        observation.capture(2)
    assert streams.offset == 16385


@pytest.mark.parametrize(
    "payload",
    [b"1\n2\n2\n", b"0\n", b"2147483648\n", b"2", b"2\n\n", b"1\n" * 65, b"99999999999\n"],
)
def test_inventory_rows_fail_closed(payload: bytes) -> None:
    observation, streams, _, _ = workspace(1)
    streams.files[b"/sys/fs/cgroup/cgroup.procs"] = payload

    with pytest.raises(ProbeError):
        observation.scan_pids()
    assert not observation.debt


def test_cumulative_limit_counts_brackets_and_rejects_one_extra_byte() -> None:
    observation, streams, _, _ = workspace()
    streams.files[b"/proc/2/stat"] = b"x" * 2048

    observation.cumulative()
    assert observation.input_bytes == INPUT_LIMIT
    with pytest.raises(ProbeError):
        observation.cumulative(extra=True)
    assert observation.input_bytes == INPUT_LIMIT + 1
    assert streams.offset == 1


@pytest.mark.parametrize("replace_executable", [False, True])
def test_capture_brackets_identity_replacement(replace_executable: bool) -> None:
    observation, streams, bridge, _ = workspace()
    opens = 0

    def replace(path: bytes) -> None:
        nonlocal opens
        if path == b"/proc/2/stat":
            opens += 1
            if opens == 2:
                if replace_executable:
                    bridge.exe = ExecutableFacts("/replacement", 3, 4)
                else:
                    streams.files[path] = stat(start=31)

    streams.on_open = replace
    with pytest.raises(ProbeError):
        observation.capture(2)
    assert not observation.committed.pins and streams.opened == streams.closed


def test_unavailable_bridge_fails_before_any_file_or_bridge_call() -> None:
    observation, streams, bridge, _ = workspace()
    observation.bridge = None
    with pytest.raises(ProbeError):
        observation.capture(2)
    assert streams.opened == bridge.calls == 0


def test_first_error_precedes_held_close_and_retains_original_clock() -> None:
    observation, streams, _, now = workspace(1)
    pin = observation.capture(2)
    streams.files[b"/proc/2/status"] = STATUS + b"Uid:\t1000 1000 1000 1000\n"
    entered, release = threading.Event(), threading.Event()
    errors: list[str] = []
    arena = observation.arena

    def close() -> None:
        if streams.current == streams.files[b"/proc/2/status"]:
            entered.set()
            assert release.wait(3)
            raise OSError("uncertain synthetic close")

    def capture() -> None:
        try:
            observation.capture(2)
        except ProbeError as error:
            errors.append(str(error))

    streams.before_close = close
    worker = threading.Thread(target=capture)
    worker.start()
    try:
        assert entered.wait(3)
        assert observation.clock.cleanup_start_utc == 100
        assert observation.debt and observation.fd == 20
        now[0] = 101
        observation.clock.cancel()
        with pytest.raises(ProbeError, match="lease_retained"):
            observation.start_identity(2, cleanup=True)
        assert observation.clock.cleanup_monotonic == 103
        assert observation.committed.pins == (pin,) and observation.arena is arena
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and errors == ["process_observation_failed"]
    assert observation.uncertain_close and observation.fd == 20 and observation.debt
    with pytest.raises(ProbeError, match="debt_retained"):
        observation.group_members(2, cleanup=True)
    assert observation.clock.cleanup_start_utc == 100 and observation.arena is arena


@dataclass
class Entry:
    name: bytes


class Entries:
    def __init__(self, names: list[bytes]) -> None:
        self.names = names
        self.index = 0
        self.closed = False
        self.before_close: Callable[[], None] | None = None

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> Entry:
        if self.index == len(self.names):
            raise StopIteration
        result = Entry(self.names[self.index])
        self.index += 1
        return result

    def close(self) -> None:
        if self.before_close is not None:
            self.before_close()
        self.closed = True


@pytest.mark.parametrize(
    "names,expected_reads",
    [
        ([str(index).encode() for index in range(1, 66)], 65),
        ([b"control"] * 257, 257),
        ([b"2", b"02"], 2),
        ([b"2147483648"], 1),
    ],
)
def test_local_directory_bridge_rejects_before_extra_selection(
    names: list[bytes],
    expected_reads: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, streams, _, _ = workspace()
    bridge = LinuxObservationBridge(observation.clock)
    observation.bridge = bridge
    entries = Entries(names)

    def scan(_path: bytes) -> Entries:
        return entries

    monkeypatch.setattr(observation_module.os, "scandir", scan)
    with pytest.raises(ProbeError):
        observation.group_members(2)
    assert entries.index == expected_reads and entries.closed
    assert not observation.debt and streams.opened == 0
    assert len(bridge.pending) <= 64


def test_local_readlink_rejects_unselected_payload_before_decode_or_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, _, _, _ = workspace()
    observation.bridge = LinuxObservationBridge(observation.clock)

    class Oversized(bytes):
        def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
            pytest.fail("oversized executable decoded")

    def readlink(_path: bytes) -> bytes:
        return Oversized(b"/" + b"x" * 64)

    monkeypatch.setattr(observation_module.os, "readlink", readlink)
    with pytest.raises(ProbeError):
        observation.capture(2)
    assert not observation.committed.pins


def test_uncertain_iterator_close_retains_selected_facts_and_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, _, _, now = workspace()
    observation.group_members(2)
    old = observation.committed
    bridge = LinuxObservationBridge(observation.clock)
    observation.bridge = bridge
    entries = Entries([b"2", b"0"])
    entered, release = threading.Event(), threading.Event()
    errors: list[ProbeError] = []

    def scan(_path: bytes) -> Entries:
        return entries

    def close() -> None:
        entered.set()
        assert release.wait(3)
        raise OSError("synthetic uncertain iterator close")

    def scan_group() -> None:
        try:
            observation.group_members(2)
        except ProbeError as error:
            errors.append(error)

    entries.before_close = close
    monkeypatch.setattr(observation_module.os, "scandir", scan)
    worker = threading.Thread(target=scan_group)
    worker.start()
    try:
        assert entered.wait(3)
        assert observation.clock.cleanup_start_utc == 100
        assert bridge.pending == (2,) and observation.debt
        now[0] = 102
        observation.clock.cancel()
        with pytest.raises(ProbeError, match="lease_retained"):
            observation.start_identity(2, cleanup=True)
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and len(errors) == 1
    assert errors[0].__context__ is None and errors[0].__cause__ is None
    assert observation.committed is old and bridge.iterator is entries
    assert observation.debt and bridge.uncertain_close and not entries.closed
    assert observation.clock.cleanup_monotonic == 103


def test_cancel_during_successful_close_prevents_commit_without_renewal() -> None:
    observation, streams, _, now = workspace()
    old = observation.capture(2)
    streams.files[b"/proc/2/stat"] = stat(start=31)

    def cancel() -> None:
        observation.clock.cancel()
        now[0] += 1
        observation.clock.cancel()

    streams.before_close = cancel
    with pytest.raises(ProbeError):
        observation.capture(2)
    assert observation.committed.pins == (old,) and not observation.debt
    assert observation.clock.cleanup_start_utc == 100


def test_namespace_unknown_is_not_empty_and_group_observations_are_bounded() -> None:
    observation, streams, bridge, _ = workspace(1)
    assert observation.group_members(2) == [2]
    bridge.entries = DirectoryFacts(6, (2, 3))
    with pytest.raises(ProbeError):
        observation.group_members(2)
    assert observation.committed.members == (2,)
    assert streams.opened == streams.closed


@pytest.mark.parametrize("uptime", [0.25, 500.0])
def test_inherited_signal_and_kill_check_use_both_observation_hooks(
    uptime: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [uptime]
    # Replace only this evaluation module's binding, not the shared time module.
    monkeypatch.setattr(cancellation, "time", MonotonicClock(now))
    owner = CancelRun("feed")
    owner.child.pid = 999
    owner.child.exit_code = 0
    owner.cancel.request(now[0])
    observed: list[int] = []

    def members(pid: int) -> list[int]:
        observed.append(pid)
        return []

    def forbidden(*_args: object) -> list[int]:
        pytest.fail("inherited generic inventory was reached")

    monkeypatch.setattr(owner, "process_group_members", members)
    monkeypatch.setattr("evaluations.linux_resource.owned_mp3_cancel.group_members", forbidden)
    owner.signal_group(signal.SIGTERM)
    owner.manage_cancel()
    assert observed == [999, 999] and owner.kill_sent is None
    now[0] = uptime + 0.875
    owner.manage_cancel()
    assert observed == [999, 999] and owner.kill_sent is None
    now[0] = uptime + 1
    owner.manage_cancel()
    assert observed == [999, 999, 999]
    assert owner.kill_sent == uptime + 1
    now[0] = uptime + 2
    owner.request("repeat")
    owner.manage_cancel()
    assert observed == [999, 999, 999]
    assert owner.cancel.started == uptime and owner.cancel.deadline == uptime + 3


@pytest.mark.parametrize("uptime", [0.25, 500.0])
def test_stream_run_routes_live_signal_and_release_through_retained_workspace(
    uptime: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation, streams, _, now = workspace(uptime=uptime)
    # The inherited cancellation schedule and CaseClock share this same source.
    monkeypatch.setattr(cancellation, "time", MonotonicClock(now))
    pin = ProcessPin(3, 30, 2, "/synthetic/python", 3, 4, 1000, "/")

    def capture(_cls: type[ProcessPin], _pid: int, actual: ObservationWorkspace) -> ProcessPin:
        assert actual is observation
        return pin

    def forbidden(*_args: object) -> list[int]:
        pytest.fail("FMA inherited generic proc path reached")

    def pgid(pid: int) -> int:
        return pid

    signals: list[signal.Signals] = []

    def kill(_pid: int, sig: signal.Signals) -> None:
        signals.append(sig)

    monkeypatch.setattr(ProcessPin, "capture", classmethod(capture))
    monkeypatch.setattr(
        "evaluations.linux_resource.owned_mp3_cancel.read_start_identity", forbidden
    )
    monkeypatch.setattr("evaluations.linux_resource.owned_mp3_cancel.group_members", forbidden)
    monkeypatch.setattr("evaluations.linux_resource.owned_mp3_cancel.os.getpgid", pgid)
    monkeypatch.setattr("evaluations.linux_resource.owned_mp3_cancel.os.killpg", kill)
    run = StreamRun(pin, pin, observation.clock, -1, workspace=observation)
    run.fds.clear()  # Synthetic descriptors were never opened or borrowed here.
    run.child.pid = 2
    run.start_ticks = 30
    run.exec_released = True
    run.signal_group(signal.SIGTERM)
    assert signals == [signal.SIGTERM, signal.SIGCONT]
    assert streams.opened == streams.closed == 3  # identity + two namespace brackets
    run.request("synthetic_cancel")
    run.child.exit_code = 0
    run.manage_cancel()
    assert signals == [signal.SIGTERM, signal.SIGCONT] * 2 and run.kill_sent is None
    now[0] = uptime + 0.875
    run.manage_cancel()
    assert signals == [signal.SIGTERM, signal.SIGCONT] * 2 and run.kill_sent is None
    now[0] = uptime + 1
    run.manage_cancel()
    assert signals[-1] == signal.SIGKILL
    assert len(signals) == 5 and run.kill_sent == uptime + 1
    now[0] = uptime + 2
    run.request("repeat")
    run.manage_cancel()
    assert len(signals) == 5
    assert observation.clock.cleanup_start_utc == uptime and run.cancel.deadline == uptime + 3
    assert observation.clock.cleanup_monotonic == uptime + 3
    observation.fd = 20
    observation.uncertain_close = True
    assert not run.release_if_done()
    assert not run.released and observation.debt


@pytest.mark.parametrize("fault", [None, "start", "state", "parent", "exe", "extra_group"])
def test_stopped_gate_keeps_exact_roles_security_and_namespace_members(fault: str | None) -> None:
    observation, streams, _, _ = workspace(1)

    class NativeBridge(Bridge):
        def executable(self, pid: int, charge: Callable[[int], None]) -> ExecutableFacts:
            text = "/usr/bin/mpg123" if pid == 4 else "/synthetic/python"
            if fault == "exe" and pid == 4:
                text = "/replacement"
            charge(len(text))
            return ExecutableFacts(text, 3, 4)

        def directory(
            self,
            pid: int | None,
            name: str,
            limit: int,
            charge: Callable[[int], None],
        ) -> DirectoryFacts:
            ids = (1, 2, 3, 4, 5) if fault == "extra_group" else (1, 2, 3, 4)
            if name == "task":
                assert pid is not None
                ids = (pid,)
            charge(len(ids))
            return DirectoryFacts(len(ids), ids)

    observation.bridge = NativeBridge()
    for pid in range(1, 6):
        parent = pid - 1
        if fault == "parent" and pid == 4:
            parent = 2
        streams.files[f"/proc/{pid}/stat".encode()] = stat(
            pid,
            start=31 if fault == "start" and pid == 4 else 30,
            parent=parent,
            group=4 if pid == 5 else pid,
            state=b"T" if pid == 4 and fault != "state" else b"S",
        )
        streams.files[f"/proc/{pid}/status".encode()] = STATUS.replace(
            b"PPid:\t1",
            f"PPid:\t{parent}".encode(),
        )
        streams.files[f"/proc/{pid}/cgroup".encode()] = b"0::/\n"
    streams.files[b"/sys/fs/cgroup/cgroup.procs"] = b"1\n2\n3\n4\n"
    roles = tuple(
        ProcessPin(pid, 30, pid - 1, "/synthetic/python", 3, 4, 1000, "/") for pid in (1, 2, 3)
    )
    if fault is None:
        observation.stopped_identity(4, 30, "/usr/bin/mpg123", *roles)
        assert len(observation.committed.pins) == 4
        assert observation.committed.pins[-1].executable == "/usr/bin/mpg123"
    else:
        with pytest.raises(ProbeError):
            observation.stopped_identity(4, 30, "/usr/bin/mpg123", *roles)
        assert not observation.committed.pins
    assert streams.opened == streams.closed and not observation.debt


def test_helper_launch_environment_names_the_image_source_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The image imports sobafm from /source rather than an installed package (#91)."""
    from evaluations.fma_validation import process as process_module
    from evaluations.fma_validation.process import OwnedHelper

    launched: list[dict[str, object]] = []

    def fake_popen(*_args: object, **kwargs: object) -> NoReturn:
        launched.append(kwargs)
        raise OSError("synthetic launch refusal")

    monkeypatch.setattr(process_module.subprocess, "Popen", fake_popen)
    helper = OwnedHelper(CaseClock(time.time() + 60, time.time() + 60), "metadata")
    with pytest.raises(OSError, match="synthetic launch refusal"):
        helper.launch(["python", "-m", "helper"])
    try:
        assert len(launched) == 1
        assert launched[0]["env"] == {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "HOME": "/nonexistent",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "/source/src:/source",
        }
        assert helper.process is None and not helper.launching
    finally:
        assert helper.join(1)
        helper.ingress.retire()
