"""Synthetic report submission and journal ownership; no native/media activity."""

from __future__ import annotations

import errno
import gc
import json
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from evaluations.fma_validation import controller, limits, metadata, report_io
from evaluations.fma_validation.cases import NIGHT_OWL
from evaluations.fma_validation.controller import Journal
from evaluations.fma_validation.limits import CaseClock, encode_report
from evaluations.fma_validation.packet import Packet
from evaluations.fma_validation.report_io import RecordWrite
from evaluations.linux_resource.probe import ProbeError
from sobafm import json_records
from tests.test_fma_report_encoding import track_arenas


def test_partial_eintr_and_last_lf_use_one_immutable_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = encode_report({"v": "synthetic"}, newline=True)
    record = RecordWrite(data)
    output = bytearray()
    views: list[memoryview] = []
    calls = 0

    def write(fd: int, view: memoryview) -> int:
        nonlocal calls
        assert fd == 17 and view.obj is data and record.busy and record.submitted
        views.append(view)
        calls += 1
        if calls == 1:
            raise InterruptedError
        amount = 3 if calls == 2 else (len(view) - 1 if len(view) > 1 else 1)
        output.extend(view[:amount])
        return amount

    monkeypatch.setattr(report_io, "os", SimpleNamespace(write=write))
    record.write(17)
    assert bytes(output) == data and output[-1] == 10 and calls == 4
    assert record.data is data and record.offset == len(data) and record.complete
    assert not record.busy and not record.failed
    for view in views:
        with pytest.raises(ValueError, match="released memoryview"):
            _ = view.obj
    with pytest.raises(ProbeError, match="report_operation_retained"):
        record.write(17)


@pytest.mark.parametrize("after_prefix", [False, True])
@pytest.mark.parametrize("outcome", ["zero", "overrun", "error"])
def test_failed_write_retains_record_and_cannot_be_resubmitted(
    monkeypatch: pytest.MonkeyPatch, after_prefix: bool, outcome: str
) -> None:
    data = encode_report({"v": "synthetic"}, newline=True)
    record = RecordWrite(data)
    views: list[memoryview] = []
    calls = 0

    def write(_fd: int, view: memoryview) -> int:
        nonlocal calls
        assert view.obj is data
        views.append(view)
        calls += 1
        if after_prefix and calls == 1:
            return 3
        if outcome == "error":
            raise OSError(errno.EIO, "synthetic unknown submission outcome")
        return 0 if outcome == "zero" else len(view) + 1

    monkeypatch.setattr(report_io, "os", SimpleNamespace(write=write))
    with pytest.raises((ProbeError, OSError)):
        record.write(17)
    assert record.submitted and record.failed and not record.complete and not record.busy
    assert record.offset == (3 if after_prefix else 0) and record.data is data
    with pytest.raises(ProbeError, match="report_operation_retained"):
        record.write(17)
    assert calls == (2 if after_prefix else 1)
    for view in views:
        with pytest.raises(ValueError, match="released memoryview"):
            _ = view.obj


def test_held_write_retains_record_while_original_clock_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    clock = CaseClock(200, 300, lambda: now[0], lambda: now[0])
    record = RecordWrite(encode_report({"v": "synthetic"}, newline=True))
    entered, release = threading.Event(), threading.Event()
    held: list[memoryview] = []

    def write(_fd: int, view: memoryview) -> int:
        held.append(view)
        entered.set()
        assert release.wait(3)
        return len(view)

    monkeypatch.setattr(report_io, "os", SimpleNamespace(write=write))
    worker = threading.Thread(target=lambda: record.write(17))
    worker.start()
    try:
        assert entered.wait(3)
        clock.cancel()
        now[0] = 104
        clock.cancel()
        assert clock.cleanup_monotonic == 103 and clock.missed
        assert record.busy and held[0].obj is record.data and not record.complete
        with pytest.raises(ProbeError, match="report_operation_retained"):
            record.write(17)
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and record.complete and clock.missed


@pytest.fixture
def metadata_entry(monkeypatch: pytest.MonkeyPatch) -> list[RecordWrite]:
    records: list[RecordWrite] = []

    class ObservedWrite(RecordWrite):
        def __init__(self, data: bytes, reason: str = "report_write_failed") -> None:
            super().__init__(data, reason)
            records.append(self)

    def contain() -> None:
        pass

    def inspect(_fd: int, _size: int) -> dict[str, object]:
        return {"synthetic": "safe"}

    monkeypatch.setattr(metadata, "RecordWrite", ObservedWrite)
    monkeypatch.setattr(metadata, "contain", contain)
    monkeypatch.setattr(metadata, "inspect", inspect)
    monkeypatch.setattr(
        metadata.sys,
        "argv",
        ["metadata", "--owned-fd", "10", "--cancel-fd", "11", "--admit-fd", "12", "--execute"],
    )
    return records


def metadata_syscalls(
    monkeypatch: pytest.MonkeyPatch, write: Callable[[int, bytes | memoryview], int]
) -> None:
    def read(fd: int, count: int) -> bytes:
        assert fd == 12 and count == 1
        return b"x"

    def close(fd: int) -> None:
        assert fd == 12

    monkeypatch.setattr(
        metadata, "os", SimpleNamespace(read=read, close=close, getpid=lambda: 9, write=write)
    )
    monkeypatch.setattr(report_io, "os", SimpleNamespace(write=write))


@pytest.mark.parametrize("after_prefix", [False, True])
@pytest.mark.parametrize("outcome", ["zero", "uncertain"])
def test_metadata_never_appends_rejection_after_possible_success_submission(
    metadata_entry: list[RecordWrite],
    monkeypatch: pytest.MonkeyPatch,
    after_prefix: bool,
    outcome: str,
) -> None:
    output = bytearray()
    stderr = bytearray()
    calls = 0

    def write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        if fd == 2:
            stderr.extend(data)
            return len(data)
        assert fd == 1 and isinstance(data, memoryview)
        calls += 1
        if after_prefix and calls == 1:
            output.extend(data[:3])
            return 3
        if outcome == "uncertain":
            output.extend(data[:2])  # Actual submission may precede an error.
            raise OSError(errno.EIO, "synthetic uncertain write")
        return 0

    metadata_syscalls(monkeypatch, write)
    assert metadata.main() == 1
    assert len(metadata_entry) == 1 and calls == (2 if after_prefix else 1)
    record = metadata_entry[0]
    assert record.submitted and record.failed and record.offset == (3 if after_prefix else 0)
    assert record.data.startswith(output) and b"metadata-rejection" not in output
    assert stderr == b"metadata_inspection_failed\n"


@pytest.mark.parametrize(
    "reason", ["unsupported_id3_version", "private source body\n", "x" * 10000]
)
def test_metadata_rejection_is_bounded_and_uses_partial_eintr_writer(
    metadata_entry: list[RecordWrite], monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    output = bytearray()
    calls = 0

    def inspect(_fd: int, _size: int) -> dict[str, object]:
        raise ProbeError(reason)

    def write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        if fd == 2:
            return len(data)
        calls += 1
        if calls == 1:
            raise InterruptedError
        amount = min(7, len(data))
        output.extend(data[:amount])
        return amount

    monkeypatch.setattr(metadata, "inspect", inspect)
    metadata_syscalls(monkeypatch, write)
    assert metadata.main() == 1 and len(metadata_entry) == 1
    result = json.loads(output)
    assert result["schema"] == NIGHT_OWL.schema("metadata-rejection")
    assert result["reason"] == (
        reason if reason == "unsupported_id3_version" else "metadata_inspection_failed"
    )
    assert output.endswith(b"\n") and metadata_entry[0].complete


@pytest.mark.parametrize("failure", ["build", "first_write", "partial_write"])
def test_rejection_failure_never_attempts_another_terminal(
    metadata_entry: list[RecordWrite], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    calls = 0
    builds = 0
    actual_encode = encode_report

    def inspect(_fd: int, _size: int) -> dict[str, object]:
        raise ProbeError("unsupported_id3_version")

    def encode(value: object, limit: int = limits.REPORT_BYTES, *, newline: bool = False) -> bytes:
        nonlocal builds
        builds += 1
        if failure == "build":
            raise MemoryError("synthetic rejection allocation failure")
        return actual_encode(value, limit, newline=newline)

    def write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        if fd == 2:
            return len(data)
        calls += 1
        if failure == "partial_write" and calls == 1:
            return 3
        raise OSError(errno.EIO, "synthetic rejection write failure")

    monkeypatch.setattr(metadata, "inspect", inspect)
    monkeypatch.setattr(metadata, "encode_report", encode)
    metadata_syscalls(monkeypatch, write)
    assert metadata.main() == 1 and builds == 1
    assert calls == {"build": 0, "first_write": 1, "partial_write": 2}[failure]
    assert len(metadata_entry) == (0 if failure == "build" else 1)


def test_metadata_releases_rejected_arena_and_exception_context_before_fallback(
    metadata_entry: list[RecordWrite],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arenas = track_arenas(monkeypatch)
    original_walk = json_records.JsonBuffer.walk
    original_encode = encode_report
    failures: list[BaseException] = []
    build = 0

    def walk(self: json_records.JsonBuffer, item: object, depth: int) -> None:
        original_walk(self, item, depth)
        if build % 2 == 1 and depth == 0 and self.backing is not None:
            cause = RuntimeError("synthetic source context")
            cause.__context__ = cause
            failures.append(cause)
            try:
                raise cause
            except RuntimeError as caught:
                error = ProbeError("report_changed_during_encoding")
                failures.append(error)
                raise error from caught

    def encode(value: object, limit: int = limits.REPORT_BYTES, *, newline: bool = False) -> bytes:
        nonlocal build
        build += 1
        if build % 2 == 0:
            assert metadata.sys.exception() is None
            assert all(arena() is None for arena in arenas)
            assert all(
                error.__traceback__ is None and error.__context__ is None for error in failures
            )
        return original_encode(value, limit, newline=newline)

    def write(_fd: int, data: bytes | memoryview) -> int:
        return len(data)

    monkeypatch.setattr(json_records.JsonBuffer, "walk", walk)
    monkeypatch.setattr(metadata, "encode_report", encode)
    metadata_syscalls(monkeypatch, write)
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(10):
            assert metadata.main() == 1
            assert all(arena() is None for arena in arenas)
        assert build == 20 and len(metadata_entry) == 10
    finally:
        if enabled:
            gc.enable()


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    def text(key: str) -> str:
        return {"case_id": "synthetic", "image": "synthetic-image", "volume": "synthetic-volume"}[
            key
        ]

    packet = cast(
        Packet,
        SimpleNamespace(
            case=NIGHT_OWL,
            fingerprint="a" * 64,
            text=text,
            request=SimpleNamespace(expires_at="2099-01-01T00:00:00Z"),
            whole_end=200,
        ),
    )
    # Frozen case time: the initial write is waited for however slow fsync is.
    result = Journal(
        tmp_path / "synthetic-ledger", packet, CaseClock(200, 300, lambda: 100.0, lambda: 10.0)
    )
    try:
        yield result
    finally:
        if result.worker is not None:
            result.worker.join(3)
            assert not result.worker.is_alive()
        try:
            info = os.fstat(result.fd)
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            assert (info.st_dev, info.st_ino) == result.identity
            os.close(result.fd)


def test_journal_pwrite_tails_alias_record_and_retry_eintr(
    journal: Journal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = os.pwrite
    calls: list[tuple[int, int]] = []
    views: list[memoryview] = []

    def pwrite(fd: int, view: memoryview, offset: int) -> int:
        assert fd == journal.fd and view.obj is journal.record
        calls.append((offset, len(view)))
        views.append(view)
        if len(calls) == 1:
            raise InterruptedError
        amount = min(11, len(view))
        with view[:amount] as part:
            return actual(fd, part, offset)

    journal.data["phase"] = "synthetic-new-phase"
    expected = encode_report(journal.data)
    monkeypatch.setattr(controller.os, "pwrite", pwrite)
    journal.save()
    assert journal.path.read_bytes() == expected and not expected.endswith(b"\n")
    assert calls[0] == calls[1] and journal.record is None
    for view in views:
        with pytest.raises(ValueError, match="released memoryview"):
            _ = view.obj


@pytest.mark.parametrize("failure", ["zero", "uncertain", "truncate", "fsync"])
def test_journal_failure_bars_replacement_and_retains_record(
    journal: Journal,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    actual_write, actual_truncate, actual_sync = os.pwrite, os.ftruncate, os.fsync
    calls: list[str] = []

    def pwrite(fd: int, view: memoryview, offset: int) -> int:
        calls.append("write")
        assert view.obj is journal.record
        if failure == "zero":
            return 0
        if failure == "uncertain":
            raise OSError(errno.EIO, "synthetic uncertain journal write")
        return actual_write(fd, view, offset)

    def truncate(fd: int, size: int) -> None:
        calls.append("truncate")
        if failure == "truncate":
            raise OSError(errno.EIO, "synthetic truncate failure")
        actual_truncate(fd, size)

    def sync(fd: int) -> None:
        calls.append("fsync")
        if failure == "fsync":
            raise OSError(errno.EIO, "synthetic fsync failure")
        actual_sync(fd)

    monkeypatch.setattr(controller.os, "pwrite", pwrite)
    monkeypatch.setattr(controller.os, "ftruncate", truncate)
    monkeypatch.setattr(controller.os, "fsync", sync)
    with pytest.raises((OSError, ProbeError)):
        journal.save()
    record, error, previous_calls = journal.record, journal.error, list(calls)
    assert record is not None and error is not None
    with pytest.raises((OSError, ProbeError)):
        journal.save()
    assert calls == previous_calls and journal.record is record and journal.error is error


@pytest.mark.parametrize("cleanup", [False, True])
def test_journal_waits_for_slow_write_while_original_clock_has_time(
    journal: Journal,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: bool,
) -> None:
    now = [100.0]
    journal.clock = CaseClock(200, 300, lambda: now[0], lambda: now[0])
    if cleanup:
        journal.clock.cancel()  # Three cleanup seconds remain from now.
    release = threading.Event()
    actual = os.pwrite

    def pwrite(fd: int, view: memoryview, offset: int) -> int:
        now[0] += 0.3
        # Held past the former fixed 250 ms in real time, too.
        assert release.wait(3)
        return actual(fd, view, offset)

    timer = threading.Timer(0.3, release.set)
    journal.data["phase"] = "synthetic-slow-phase"
    expected = encode_report(journal.data)
    monkeypatch.setattr(controller.os, "pwrite", pwrite)
    timer.start()
    try:
        journal.save()
    finally:
        timer.cancel()
        release.set()
    assert journal.path.read_bytes() == expected
    assert journal.record is None and journal.error is None
    assert journal.worker is not None and not journal.worker.is_alive()
    assert (journal.clock.cleanup_start_utc is not None) is cleanup
    assert not journal.clock.missed and not journal.clock.case_missed


@pytest.mark.parametrize("deadline", ["case_end", "cancelled", "cleanup_end"])
def test_journal_write_outliving_original_deadline_retains_owner(
    journal: Journal,
    monkeypatch: pytest.MonkeyPatch,
    deadline: str,
) -> None:
    now = [100.0]
    clock = CaseClock(200, 300, lambda: now[0], lambda: now[0])
    journal.clock = clock
    if deadline == "cleanup_end":
        clock.cancel()
    entered, release = threading.Event(), threading.Event()
    actual = os.pwrite
    calls: list[int] = []

    def pwrite(fd: int, view: memoryview, offset: int) -> int:
        calls.append(offset)
        if deadline == "case_end":
            now[0] = 200.5
        elif deadline == "cancelled":
            clock.cancel()  # A case-phase save must not spend the cleanup window.
        else:
            now[0] = 103.5
        entered.set()
        assert release.wait(3)
        return actual(fd, view, offset)

    journal.data["phase"] = "synthetic-held-phase"
    expected = encode_report(journal.data)
    monkeypatch.setattr(controller.os, "pwrite", pwrite)
    try:
        with pytest.raises(ProbeError, match="journal_operation_retained"):
            journal.save()
        worker, record = journal.worker, journal.record
        assert entered.is_set() and worker is not None and worker.is_alive()
        assert record == expected and journal.error is None
        assert clock.cleanup_start_utc == (200 if deadline == "case_end" else 100)
        journal.data["phase"] = "synthetic-replacement"
        with pytest.raises(ProbeError, match="journal_operation_retained"):
            journal.save()  # A live worker still blocks replacement.
        assert journal.worker is worker and journal.record is record and calls == [0]
    finally:
        release.set()
        assert journal.worker is not None
        journal.worker.join(3)
        assert not journal.worker.is_alive()
    assert journal.path.read_bytes() == expected


@pytest.mark.parametrize("stage", ["write", "fsync"])
@pytest.mark.parametrize("uncertain_close", [False, True])
def test_journal_held_operation_close_and_original_cleanup_debt(
    journal: Journal,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    uncertain_close: bool,
) -> None:
    now = [100.0]
    clock = CaseClock(200, 300, lambda: now[0], lambda: now[0])
    clock.cancel()
    owner = object.__new__(controller.Controller)
    owner.journal, owner.clock = journal, clock
    journal.clock = clock
    entered, release, closing, closed = (threading.Event() for _ in range(4))
    actual_write, actual_sync, actual_close = os.pwrite, os.fsync, os.close
    errors: list[Exception] = []

    def hold() -> None:
        now[0] = 103.5  # The held operation outlives the original cleanup deadline.
        entered.set()
        assert release.wait(3)

    def pwrite(fd: int, view: memoryview, offset: int) -> int:
        assert view.obj is journal.record
        if stage == "write":
            hold()
        return actual_write(fd, view, offset)

    def sync(fd: int) -> None:
        if stage == "fsync":
            hold()
        actual_sync(fd)

    def close(fd: int) -> None:
        assert release.is_set() and journal.worker is not None and not journal.worker.is_alive()
        assert clock.cleanup_monotonic == 103 and clock.missed
        if uncertain_close:
            raise OSError(errno.EIO, "synthetic uncertain journal close")
        actual_close(fd)

    def close_journal() -> None:
        closing.set()
        try:
            journal.close()
        except Exception as error:
            errors.append(error)
        finally:
            closed.set()

    monkeypatch.setattr(controller.os, "pwrite", pwrite)
    monkeypatch.setattr(controller.os, "fsync", sync)
    monkeypatch.setattr(controller.os, "close", close)
    with pytest.raises(ProbeError, match="journal_operation_retained"):
        journal.save()
    closer = threading.Thread(target=close_journal)
    try:
        assert entered.is_set() and journal.record is not None
        record = journal.record
        assert not clock.missed
        owner.persist(cleanup=True)
        assert clock.missed  # The real owner records the retained operation.
        now[0] = 104
        clock.cancel()
        with pytest.raises(ProbeError, match="journal_operation_retained"):
            journal.save()
        closer.start()
        assert closing.wait(3) and not closed.is_set()
        assert journal.record is record and clock.cleanup_monotonic == 103 and clock.missed
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(3)
        monkeypatch.setattr(controller.os, "close", actual_close)
    assert closed.is_set() and not closer.is_alive()
    assert bool(errors) is uncertain_close and journal.record is record and clock.missed
