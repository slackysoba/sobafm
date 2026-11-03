"""Synthetic complete native evidence and deterministic retained-owner schedules."""

from __future__ import annotations

import hashlib
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import pytest

from evaluations.fma_validation import decoder
from evaluations.fma_validation.consumer import reports
from evaluations.fma_validation.limits import COPY_PARTITIONS, CaseClock, json_size
from evaluations.fma_validation.native_records import (
    ADDRESS_SPACE_BYTES,
    ELF_PATHS,
    LIMIT_VALUES,
    MAP_ROWS,
    NATIVE_RECORD_BYTES,
    SPECIAL_MAPS,
    NativeLimits,
    NativeMaps,
)
from evaluations.fma_validation.observation import (
    INPUT_LIMIT,
    SELECTED_PIN_BYTES,
    DirectoryFacts,
    ExecutableFacts,
    ObservationWorkspace,
    ProcessPin,
)
from evaluations.linux_resource import owned_mp3_cancel as cancellation
from evaluations.linux_resource import owned_mp3_decode as native
from evaluations.linux_resource.decoder_startup import Child
from evaluations.linux_resource.owned_mp3_overlap import Pipes
from evaluations.linux_resource.probe import ProbeError
from tests.test_fma_process_observation import STATUS, Bridge, MonotonicClock, Streams, stat

LIMITS = b"Limit Soft Limit Hard Limit Units\n" + b"".join(
    f"{name} {value} {value} bytes\n".encode() for name, value in LIMIT_VALUES
)


def maps(count: int = 14, width: int | None = None) -> bytes:
    paths = [*ELF_PATHS, "[heap]", "[stack]", "", *sorted(SPECIAL_MAPS)]
    assert count >= len(paths)
    paths.extend([""] * (count - len(paths)))
    rows: list[bytes] = []
    for index, path in enumerate(paths):
        start = 4096 * (index + 1)
        prefix = f"{start:x}-{start + 4096:x} r--p 0 00:00 {int(path in ELF_PATHS)}"
        row = (prefix + (" " + path if path else "")).encode()
        if width is not None:
            row = prefix.encode() + b" " * (width - len(prefix) - len(path)) + path.encode()
            assert len(row) == width
        rows.append(row + b"\n")
    return b"".join(rows)


class NativeStreams(Streams):
    def __init__(self, chunk: int) -> None:
        super().__init__(chunk)
        self.path = b""
        self.paths: list[bytes] = []
        self.before_read: Callable[[], None] | None = None

    def open(self, path: bytes) -> int:
        self.path = path
        self.paths.append(path)
        return super().open(path)

    def read(self, fd: int, target: memoryview) -> int:
        if self.before_read is not None:
            self.before_read()
        return super().read(fd, target)


class NativeBridge(Bridge):
    def __init__(self) -> None:
        super().__init__()
        self.changed_inode = False
        self.extra_pid = False
        self.extra_thread = False

    def executable(self, pid: int, charge: Callable[[int], None]) -> ExecutableFacts:
        text = "/usr/bin/mpg123" if pid == 4 else "/synthetic/python"
        charge(len(text))
        return ExecutableFacts(text, 3, 5 if self.changed_inode and pid == 4 else 4)

    def directory(
        self, pid: int | None, name: str, limit: int, charge: Callable[[int], None]
    ) -> DirectoryFacts:
        ids = (1, 2, 3, 4, 5) if self.extra_pid else (1, 2, 3, 4)
        if name == "task":
            assert pid is not None
            ids = (pid, 5) if self.extra_thread and pid == 4 else (pid,)
        charge(len(ids))
        return DirectoryFacts(len(ids), ids)


@dataclass
class Harness:
    observation: ObservationWorkspace
    streams: NativeStreams
    bridge: NativeBridge
    now: list[float]
    roles: tuple[ProcessPin, ProcessPin, ProcessPin]

    def admit(self, deadline: float | None = None) -> tuple[NativeLimits, NativeMaps]:
        original = self.observation.clock.end_monotonic - 50 if deadline is None else deadline
        return self.observation.stopped_native(4, 30, "/usr/bin/mpg123", *self.roles, original)


def harness(chunk: int = 2048, *, uptime: float = 100.0) -> Harness:
    now = [uptime]
    clock = CaseClock(uptime + 100, uptime + 200, wall=lambda: now[0], monotonic=lambda: now[0])
    streams, bridge = NativeStreams(chunk), NativeBridge()
    for pid in range(1, 6):
        streams.files[f"/proc/{pid}/stat".encode()] = stat(
            pid, parent=pid - 1, group=4 if pid == 5 else pid, state=b"T" if pid == 4 else b"S"
        )
        streams.files[f"/proc/{pid}/status".encode()] = STATUS.replace(
            b"PPid:\t1", f"PPid:\t{pid - 1}".encode()
        )
        streams.files[f"/proc/{pid}/cgroup".encode()] = b"0::/\n"
    streams.files[b"/sys/fs/cgroup/cgroup.procs"] = b"1\n2\n3\n4\n"
    streams.files[b"/proc/4/limits"] = LIMITS
    streams.files[b"/proc/4/maps"] = maps()
    roles = tuple(
        ProcessPin(pid, 30, pid - 1, "/synthetic/python", 3, 4, 1000, "/") for pid in (1, 2, 3)
    )
    return Harness(
        ObservationWorkspace(clock, bridge, streams),
        streams,
        bridge,
        now,
        cast(tuple[ProcessPin, ProcessPin, ProcessPin], roles),
    )


@pytest.mark.parametrize("chunk", range(1, 100))
def test_complete_maps_limits_and_wire_survive_split_reads(chunk: int) -> None:
    h = harness(chunk)
    limits, mappings = h.admit()
    assert limits.record() == dict(LIMIT_VALUES)
    assert mappings.record() == native.parse_maps(h.streams.files[b"/proc/4/maps"].decode())
    assert len(mappings.records) == 14
    assert h.streams.paths.count(b"/proc/4/limits") == 1
    assert h.streams.paths.count(b"/proc/4/maps") == 1
    assert len(h.observation.committed.pins) == 4
    assert h.streams.opened == h.streams.closed and not h.observation.debt


@pytest.mark.parametrize("field", [name.encode() for name, _ in LIMIT_VALUES])
@pytest.mark.parametrize(
    "fault",
    ["duplicate", "missing", "unit", "soft", "hard", "overflow", "wide", "leading_zero", "suffix"],
)
def test_each_exact_limit_is_required_once(field: bytes, fault: str) -> None:
    h = harness(1)
    row = next(line for line in LIMITS.splitlines(keepends=True) if line.startswith(field))
    tail = row[len(field) :].split()
    altered = {
        "duplicate": row + row,
        "missing": b"",
        "unit": row.replace(b"bytes", b"kbytes"),
        "soft": field + b" 1 " + tail[1] + b" bytes\n",
        "hard": field + b" " + tail[0] + b" 1 bytes\n",
        "overflow": field + b" 18446744073709551616 0 bytes\n",
        "wide": field + b" " + b"9" * 21 + b" 0 bytes\n",
        "leading_zero": field + b" 0" + tail[0] + b" " + tail[1] + b" bytes\n",
        "suffix": row.rstrip(b"\n") + b" extra\n",
    }[fault]
    h.streams.files[b"/proc/4/limits"] = LIMITS.replace(row, altered)
    with pytest.raises(ProbeError):
        h.observation.native_limits(4, 150)
    assert h.observation.committed.limits is None and h.observation.clock.cleanup_start_utc == 100
    assert h.streams.opened == h.streams.closed


@pytest.mark.parametrize("file", ["limits", "maps"])
@pytest.mark.parametrize("fault", ["unterminated", "nonascii", "nul", "empty_row", "width", "rows"])
def test_complete_rows_have_finite_encoding_width_and_count(file: str, fault: str) -> None:
    h = harness(7)
    key = f"/proc/4/{file}".encode()
    source = h.streams.files[key]
    width = 256 if file == "limits" else 512
    source = {
        "unterminated": source[:-1],
        "nonascii": b"\xff" + source,
        "nul": b"\x00" + source,
        "empty_row": b"\n" + source,
        "width": b" " * (width + 1) + b"\n" + source,
        "rows": (b"unknown\n" * 65 + source) if file == "limits" else maps(65),
    }[fault]
    h.streams.files[key] = source
    with pytest.raises(ProbeError):
        h.admit()
    assert h.observation.committed.maps is None and not h.observation.debt


@pytest.mark.parametrize(
    "old,new",
    [
        (b"1000-2000", b"1001-2000"),
        (b"1000-2000", b"2000-1000"),
        (b"1000-2000", b"1000-1000"),
        (b"2000-3000", b"1000-3000"),
        (b"r--p", b"rwxp"),
        (b"r--p", b"r--s"),
        (b"0 00:00 1", b"FFFFFFFFFFFFFFFF 00:00 1"),
        (b"0 00:00 1", b"10000000000000000 00:00 1"),
        (b"0 00:00 1", b"0 100000000:00 1"),
        (b"0 00:00 1", b"0 00:100000000 1"),
        (b"0 00:00 1", b"0 00:00 18446744073709551616"),
        (b"0 00:00 1", b"0 00:00 -1"),
        (b"0 00:00 1", b"0 00:00 0"),
        (b"/usr/bin/mpg123", b"/unreviewed/file"),
        (b"/usr/bin/mpg123", b"/usr/bin/mpg123 (deleted)"),
        (b"[heap]", b"[anon:unreviewed]"),
    ],
)
def test_map_scope_numeric_ranges_and_order_reject_changed_rows(old: bytes, new: bytes) -> None:
    h = harness(1)
    h.streams.files[b"/proc/4/maps"] = maps().replace(old, new, 1)
    with pytest.raises(ProbeError):
        h.admit()
    assert h.observation.committed.maps is None


@pytest.mark.parametrize("path", ["[heap]", "[stack]", "", *sorted(SPECIAL_MAPS)])
@pytest.mark.parametrize("fault", ["inode", "permission"])
def test_anonymous_and_kernel_scope_is_preserved(path: str, fault: str) -> None:
    h = harness()
    rows = maps().splitlines(keepends=True)
    index = [*ELF_PATHS, "[heap]", "[stack]", "", *sorted(SPECIAL_MAPS)].index(path)
    if fault == "inode":
        rows[index] = rows[index].replace(b"00:00 0", b"00:00 1")
    else:
        replacement = b"rw-p" if path in SPECIAL_MAPS else b"r-xp"
        rows[index] = rows[index].replace(b"r--p", replacement)
    h.streams.files[b"/proc/4/maps"] = b"".join(rows)
    with pytest.raises(ProbeError):
        h.admit()


@pytest.mark.parametrize("missing", ELF_PATHS)
def test_every_reviewed_elf_is_required(missing: str) -> None:
    h = harness()
    h.streams.files[b"/proc/4/maps"] = b"".join(
        row
        for row in maps().splitlines(keepends=True)
        if not row.rstrip().endswith(missing.encode())
    )
    with pytest.raises(ProbeError):
        h.admit()


def test_all_64_rows_and_exact_original_hash_are_retained() -> None:
    h = harness(511)
    raw = maps(64, 512)
    h.streams.files[b"/proc/4/maps"] = raw
    _, first = h.admit()
    assert len(first.records) == 64 and first.sha256 == hashlib.sha256(raw).hexdigest()
    assert first.record() == native.parse_maps(raw.decode())
    h.streams.files[b"/proc/4/maps"] = raw.replace(b" ", b"\t", 1)
    _, second = h.admit()
    assert second.records == first.records and second.sha256 != first.sha256
    assert h.observation.committed.maps is second
    assert first.sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "change", ["inode", "start", "state", "security", "thread", "inventory", "group"]
)
def test_final_identity_bracket_rejects_changes_after_map_eof(change: str) -> None:
    h = harness()

    def changed() -> None:
        if h.streams.path != b"/proc/4/maps":
            return
        if change == "inode":
            h.bridge.changed_inode = True
        elif change == "thread":
            h.bridge.extra_thread = True
        elif change == "group":
            h.bridge.extra_pid = True
        elif change == "inventory":
            h.streams.files[b"/sys/fs/cgroup/cgroup.procs"] += b"5\n"
        elif change == "security":
            h.streams.files[b"/proc/4/status"] = h.streams.files[b"/proc/4/status"].replace(
                b"Seccomp:\t2", b"Seccomp:\t0"
            )
        else:
            h.streams.files[b"/proc/4/stat"] = stat(
                4,
                parent=3,
                group=4,
                start=31 if change == "start" else 30,
                state=b"S" if change == "state" else b"T",
            )

    h.streams.before_close = changed
    with pytest.raises(ProbeError):
        h.admit()
    assert h.observation.committed.maps is None and h.observation.clock.cleanup_start_utc == 100


@pytest.mark.parametrize("stage", ["read", "close"])
def test_preparation_deadline_is_checked_after_held_operation(stage: str) -> None:
    h = harness()

    def advance() -> None:
        if h.streams.path == b"/proc/4/maps":
            h.now[0] = 150

    if stage == "read":
        h.streams.before_read = advance
    else:
        h.streams.before_close = advance
    with pytest.raises(ProbeError):
        h.admit()
    assert h.observation.committed.maps is None
    assert h.observation.clock.cleanup_start_utc == 150
    assert h.observation.clock.cleanup_monotonic == 153


@pytest.mark.parametrize("uncertain", [False, True])
def test_old_staged_selected_maps_and_fd_remain_owned_through_held_close(uncertain: bool) -> None:
    h = harness()
    _, old = h.admit()
    old_projection = old.record()
    h.streams.files[b"/proc/4/maps"] = maps(64, 512)
    entered, release = threading.Event(), threading.Event()
    errors: list[str] = []

    def held() -> None:
        if h.streams.path == b"/proc/4/maps":
            entered.set()
            assert release.wait(3)
            if uncertain:
                raise OSError("synthetic uncertain close")

    def observe() -> None:
        try:
            h.admit()
        except ProbeError as error:
            errors.append(str(error))

    h.streams.before_close = held
    worker = threading.Thread(target=observe)
    worker.start()
    try:
        assert entered.wait(2)
        assert h.observation.committed.maps is old and old.record() == old_projection
        staged = h.observation.staged.maps
        assert staged is not None and len(staged.records) == 64
        assert h.observation.pending_maps is staged.records and h.observation.fd == 20
        assert h.observation.debt
        h.observation.clock.cancel()
        h.now[0] = 102
        h.observation.clock.cancel()
        with pytest.raises(ProbeError, match="lease_retained"):
            h.admit()
        assert h.observation.clock.cleanup_start_utc == 100
        assert h.observation.clock.cleanup_monotonic == 103
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and errors == ["process_observation_failed"]
    assert h.observation.committed.maps is old
    assert h.observation.debt is uncertain
    assert (h.observation.staged.maps is staged) is uncertain
    if uncertain:
        assert h.observation.fd == 20 and h.observation.uncertain_close
        with pytest.raises(ProbeError, match="debt_retained"):
            h.admit()


def test_stopped_gate_uses_complete_callback_without_legacy_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness()
    calls: list[tuple[int, int]] = []

    def stop(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("complete callback fell through to an unbounded legacy read")

    def observe() -> tuple[dict[str, int], dict[str, object]]:
        limits, mappings = h.admit()
        return limits.record(), mappings.record()

    def waited(_pid: int, _flags: int) -> tuple[int, int]:
        return 4, (signal.SIGSTOP << 8) | 0x7F

    monkeypatch.setattr(native, "time", MonotonicClock(h.now))
    monkeypatch.setattr(native.os, "kill", stop)
    monkeypatch.setattr(native.os, "waitpid", waited)
    monkeypatch.setattr(native, "read_text", forbidden)
    monkeypatch.setattr(native, "parse_maps", forbidden)
    monkeypatch.setattr(native, "verify_child", forbidden)
    result = native.stopped_native_gate(Child(4), 30, 150, observation_gate=observe)
    assert calls == [(4, signal.SIGSTOP)]
    assert result == {
        "pid": 4,
        "start_ticks": 30,
        "state": "stopped",
        "rlimits": dict(LIMIT_VALUES),
        "mappings": cast(NativeMaps, h.observation.committed.maps).record(),
        "native_address_spaces": 1,
        "input_bytes": 0,
    }


def padded_limits(size: int = 16_384) -> bytes:
    rows = LIMITS.splitlines()
    rows.extend([b"Unknown"] * (64 - len(rows)))
    rows = [row.ljust(256) for row in rows]
    remove = 64 * 257 - size
    rows[-1] = rows[-1][:-remove] if remove else rows[-1]
    result = b"\n".join(rows) + b"\n"
    assert len(result) == size
    return result


@pytest.mark.parametrize("extra", [0, 1])
def test_limit_file_exact_byte_ceiling_and_extra_byte_probe(extra: int) -> None:
    h = harness()
    h.streams.files[b"/proc/4/limits"] = padded_limits(16_384 + extra)
    if extra:
        with pytest.raises(ProbeError):
            h.observation.native_limits(4, 150)
        assert h.observation.committed.limits is None
    else:
        assert h.observation.native_limits(4, 150).record() == dict(LIMIT_VALUES)
    assert h.observation.input_bytes == 16_384 + extra
    assert h.streams.opened == h.streams.closed


def status_padding(size: int) -> bytes:
    rows: list[bytes] = []
    while size:
        count = min(513, size)
        if 0 < size - count < 3:
            count -= 3
        assert count >= 3
        rows.append(b"X:" + b"a" * (count - 3) + b"\n")
        size -= count
    return b"".join(rows)


def test_combined_budget_includes_both_identity_brackets_and_bridge_bytes() -> None:
    h = harness()
    h.streams.files[b"/proc/4/limits"] = padded_limits()
    h.streams.files[b"/proc/4/maps"] = maps(64, 512)
    h.admit()
    gap = INPUT_LIMIT - h.observation.input_bytes
    if gap % 2:
        h.streams.files[b"/proc/4/limits"] = padded_limits(16_383)
        gap += 1
    # This status is read once in each bracket, so both halves must be charged.
    h.streams.files[b"/proc/1/status"] += status_padding(gap // 2)
    _, accepted = h.admit()
    assert h.observation.input_bytes == INPUT_LIMIT
    # One extra returned executable byte in each bracket cannot reset the budget.
    original = h.bridge.executable

    def extra(pid: int, charge: Callable[[int], None]) -> ExecutableFacts:
        value = original(pid, charge)
        if pid == 1:
            charge(1)
        return value

    h.bridge.executable = extra
    with pytest.raises(ProbeError):
        h.admit()
    assert h.observation.committed.maps is accepted
    assert h.observation.input_bytes > INPUT_LIMIT
    assert h.streams.opened == h.streams.closed


@pytest.mark.parametrize("extra_page", [0, 1])
def test_ordinary_extent_cap_keeps_special_totals_separate(extra_page: int) -> None:
    h = harness()
    rows = maps().splitlines()
    limit = ADDRESS_SPACE_BYTES // 4096 * 4096
    sizes = [limit - 9 * 4096 + extra_page * 4096, *([4096] * 13)]
    address = 4096
    result: list[bytes] = []
    for row, size in zip(rows, sizes, strict=True):
        result.append(f"{address:x}-{address + size:x}".encode() + b" " + row.split(b" ", 1)[1])
        address += size
    h.streams.files[b"/proc/4/maps"] = b"\n".join(result) + b"\n"
    if extra_page:
        with pytest.raises(ProbeError):
            h.admit()
    else:
        _, accepted = h.admit()
        assert accepted.ordinary == limit and accepted.special == 4 * 4096
        assert accepted.record()["sampled_pcm_proof"] is False


def test_row_65_failure_starts_original_clock_before_uncertain_close() -> None:
    h = harness()
    _, old = h.admit()
    h.streams.files[b"/proc/4/maps"] = maps(65)

    def failed_close() -> None:
        if h.streams.path != b"/proc/4/maps":
            return
        assert h.observation.clock.cleanup_start_utc == 100
        assert len(h.observation.pending_maps) == 64
        assert h.observation.staged.maps is None
        h.now[0] = 102
        raise OSError("uncertain close")

    h.streams.before_close = failed_close
    with pytest.raises(ProbeError):
        h.admit()
    assert len(h.observation.pending_maps) == 64 and h.observation.committed.maps is old
    assert h.observation.clock.cleanup_monotonic == 103 and h.observation.debt


def test_cancellation_cannot_replace_a_held_native_read() -> None:
    h = harness()
    entered, release = threading.Event(), threading.Event()
    errors: list[str] = []

    def held() -> None:
        if h.streams.path == b"/proc/4/maps":
            entered.set()
            assert release.wait(3)

    def observe() -> None:
        try:
            h.admit()
        except ProbeError as error:
            errors.append(str(error))

    h.streams.before_read = held
    worker = threading.Thread(target=observe)
    worker.start()
    try:
        assert entered.wait(2)
        h.observation.clock.cancel()
        h.now[0] = 104
        h.observation.clock.cancel()
        assert h.observation.fd == 20 and h.observation.debt
        with pytest.raises(ProbeError, match="lease_retained"):
            h.observation.native_limits(4, 150)
        assert h.observation.clock.cleanup_monotonic == 103 and h.observation.clock.missed
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and errors == ["process_observation_failed"]
    assert not h.observation.debt and h.observation.committed.maps is None


@pytest.mark.parametrize("fault", ["capacity", "cancel"])
@pytest.mark.parametrize("uptime", [0.25, 500.0])
def test_stream_report_failure_retains_complete_selected_maps_before_any_resume(
    fault: str, uptime: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness(uptime=uptime)
    h.streams.files[b"/proc/4/maps"] = maps(64, 512)

    def capture(_cls: type[ProcessPin], _pid: int, _workspace: ObservationWorkspace) -> ProcessPin:
        return h.roles[2]

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("failed report allocated banks, resumed, or wrote")

    monkeypatch.setattr(ProcessPin, "capture", classmethod(capture))
    # Use the real owner cancellation route and one clock on both sides of its
    # inherited admission check, so host uptime cannot mask a missing request.
    monkeypatch.setattr(cancellation, "time", MonotonicClock(h.now))
    run = decoder.StreamRun(
        h.roles[0], h.roles[1], h.observation.clock, -1, workspace=h.observation
    )
    run.child.pid, run.start_ticks = 4, 30
    limits, mappings = run.stopped_observation()
    run.native_gate = {"rlimits": limits, "mappings": mappings}
    run.pipes = Pipes(10, 11, 12, 13, 14, 15, 16, 17, 18, 19)
    selected = h.observation.committed.maps
    assert selected is not None and len(selected.records) == 64
    actual = decoder.report_line

    def cancel(value: object) -> bytes:
        data = actual(value)
        run.request("synthetic_cancel")
        return data

    if fault == "capacity":
        run.native_gate["oversize"] = "a" * 65_536
    else:
        monkeypatch.setattr(decoder, "report_line", cancel)
    monkeypatch.setattr(decoder, "bytearray", forbidden, raising=False)
    monkeypatch.setattr(decoder.os, "kill", forbidden)
    monkeypatch.setattr(decoder, "write_report", forbidden)
    assert run.preparation_remaining() == 15 and run.cancel.started is None
    expected = (
        "report_capacity_exceeded"
        if fault == "capacity"
        else "decoder preparation expired or was cancelled"
    )
    with pytest.raises(ProbeError, match=f"^{expected}$") as error:
        run.admit_stream()
    assert h.now[0] == uptime and run.phase_deadline - h.now[0] == 15
    if fault == "capacity":
        assert run.cancel.started is None and h.observation.clock.cleanup_start_utc is None
    else:
        assert run.cancel.started == uptime and h.observation.clock.cleanup_start_utc == uptime
    run.fail(error.value)
    assert not run.buffers and not run.input_released
    assert h.observation.committed.maps is selected and run.native_gate["mappings"] is mappings
    assert mappings == selected.record() and h.observation.clock.cleanup_monotonic == uptime + 3
    h.now[0] = uptime + 2
    run.request("repeat")
    assert run.cancel.started == uptime and run.cancel.deadline == uptime + 3


def test_selected_payload_projections_and_receiver_overlap_fit_named_reservations() -> None:
    h = harness()
    h.streams.files[b"/proc/4/maps"] = maps(MAP_ROWS, 512)
    limits, selected = h.admit()
    value = {"rlimits": limits.record(), "mappings": selected.record()}
    assert json_size(value) < 8192
    # Conservative selected payload, charging 32 bytes per bounded integer and
    # every row's canonical text/keys even when immutable backing aliases.
    assert max(len(path) for path in ELF_PATHS) <= 48
    one_version = MAP_ROWS * (48 + 4 + 32 + len("pathpermissionsbytes")) + 1024
    assert one_version <= 8192 and 4 * one_version <= NATIVE_RECORD_BYTES
    projected = cast(list[dict[str, object]], cast(dict[str, object], value["mappings"])["records"])
    assert all(
        row["path"] is original.path
        and row["permissions"] is original.permissions
        and row["bytes"] is original.size
        for row, original in zip(projected, selected.records, strict=True)
    )
    encoded = decoder.report_line(value)
    received = reports(encoded)
    assert received == [value] and len(encoded) < 8192
    # A subsequent parse owns new values while the prior record remains retained.
    h.streams.files[b"/proc/4/maps"] = maps(MAP_ROWS, 512).replace(b" ", b"\t", 1)
    new_limits, new_selected = h.admit()
    new_value = {"rlimits": new_limits.record(), "mappings": new_selected.record()}
    new_received = reports(decoder.report_line(new_value))
    assert new_received == [new_value] and new_received != received
    assert received == [value] and selected.sha256 != new_selected.sha256
    # Known simultaneous payload components, not full runner/encoder/PTY closure.
    known_runner = (
        NATIVE_RECORD_BYTES + SELECTED_PIN_BYTES + 2 * 65_536 + 2 * 65_536 + 3 * 65_536 + 3 * 65_536
    )
    assert known_runner == 696_320 < COPY_PARTITIONS["decoder_runner"] == 786_432
    assert COPY_PARTITIONS["intake_owner"] > 7 * 65_536 + NATIVE_RECORD_BYTES == 491_520
