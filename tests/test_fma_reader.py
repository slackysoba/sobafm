"""Owned synthetic seek/stream/downstream schedules; no actual recording/parser."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from sobafm import fma_intake as intake
from sobafm.catalog import Catalog
from tests.test_fma_intake import Case, interrupted_cli, manifest, start
from tests.test_fma_intake import case as case
from tests.test_fma_intake import catalog as catalog
from tests.test_fma_intake import owners as owners


class Consumer:
    def __init__(self) -> None:
        self.digest = hashlib.sha256()
        self.count = 0
        self.maximum_chunk = 0
        self.inspected = threading.Event()
        self.finished = threading.Event()
        self.cancelled = threading.Event()

    def inspect(self, fd: int) -> None:
        assert os.read(fd, 7) == b"synthet"
        os.lseek(fd, -3, os.SEEK_END)
        assert len(os.read(fd, 3)) == 3
        self.inspected.set()

    def write(self, chunk: bytes) -> None:
        self.maximum_chunk = max(self.maximum_chunk, len(chunk))
        self.count += len(chunk)
        self.digest.update(chunk)

    def finish(self) -> None:
        self.finished.set()

    def cancel(self) -> None:
        self.cancelled.set()

    def join(self, timeout: float) -> bool:
        return True


def test_lower_bound_record_round_trip_fingerprint_safe_receipt_and_cap(
    case: Case,
    catalog: Catalog,
) -> None:
    case.clock.utc = datetime(2026, 11, 2, 12, tzinfo=UTC)
    evidence = "Synthetic operator report: acquired earlier today; midnight is a lower bound."
    request = replace(
        case.request,
        acquisition=intake.Acquisition(
            "2026-11-02T00:00:00Z",
            "conservative_lower_bound",
            evidence,
        ),
        expires_at="2026-11-09T00:00:00Z",
    )
    parsed = intake.parse_request(manifest(request), catalog, case.clock.now())
    assert parsed == request
    assert parsed.fingerprint == request.fingerprint
    changed = replace(
        request, acquisition=replace(request.acquisition, operator_evidence="Other evidence.")
    )
    assert changed.fingerprint != request.fingerprint
    case.request = request
    receipt = case.owner().report()
    assert evidence not in json.dumps(receipt)
    safe = cast(dict[str, object], receipt["acquisition"])
    assert safe["basis"] == "conservative_lower_bound"
    assert safe["retention_anchor_utc"] == "2026-11-02T00:00:00Z"
    assert "acquired_at" not in receipt
    too_late = replace(request, expires_at="2026-11-09T00:00:00.000001Z")
    with pytest.raises(intake.IntakeError, match="invalid_retention_window"):
        intake.parse_request(manifest(too_late), catalog, case.clock.now())


@pytest.mark.parametrize(
    "field,value",
    [
        ("basis", "estimated"),
        ("operator_evidence", ""),
        ("operator_evidence", "x" * 1025),
        ("operator_evidence", "embedded\ncontrol"),
        ("schema", "sobafm-acquisition-2"),
        ("extra", "unknown"),
    ],
)
def test_acquisition_record_is_closed_and_bounded(
    case: Case,
    catalog: Catalog,
    field: str,
    value: object,
) -> None:
    data = case.request.manifest()
    cast(dict[str, object], data["acquisition"])[field] = value
    with pytest.raises(intake.IntakeError):
        intake.parse_request(json.dumps(data).encode(), catalog, case.clock.now())
    assert list(case.slot.iterdir()) == []


@pytest.mark.parametrize("change", ["anchor", "basis", "evidence"])
@pytest.mark.parametrize("target", ["request", "state"])
def test_changed_acquisition_cannot_recover_original_slot(
    case: Case,
    tmp_path: Path,
    owners: list[intake.LocalIntake],
    change: str,
    target: str,
) -> None:
    interrupted_cli(case, tmp_path)
    acquisition = case.request.acquisition
    if change == "anchor":
        original = datetime.fromisoformat(acquisition.retention_anchor_utc)
        acquisition = replace(
            acquisition,
            retention_anchor_utc=(original - timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
        )
    elif change == "basis":
        acquisition = replace(acquisition, basis="conservative_lower_bound")
    else:
        acquisition = replace(acquisition, operator_evidence="Changed operator evidence.")
    if target == "request":
        case.request = replace(case.request, acquisition=acquisition)
    else:
        path = case.slot / "state.json"
        state = cast(dict[str, object], json.loads(path.read_bytes()))
        state["acquisition"] = acquisition.record()
        path.write_text(json.dumps(state))
    owner = case.owner(recovery=True)
    owners.append(owner)
    owner.start(execute=True)
    assert owner.join(5)
    assert owner.report()["unresolved_previous_slot"] is True
    assert owner.report()["cleanup_deadline"] is None
    assert (case.slot / "payload").read_bytes() == case.data


def legacy_request(case: Case) -> intake.IntakeRequest:
    return replace(
        case.request,
        version=1,
        acquisition=replace(
            case.request.acquisition,
            basis="exact",
            operator_evidence=None,
        ),
    )


def test_v1_fingerprint_and_exact_time_remain_explicit(
    case: Case,
    catalog: Catalog,
    owners: list[intake.LocalIntake],
) -> None:
    case.request = legacy_request(case)
    old_fields = case.request.manifest()
    del old_fields["schema"]
    expected = hashlib.sha256(
        json.dumps(old_fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    parsed = intake.parse_request(manifest(case.request), catalog, case.clock.now())
    assert parsed.fingerprint == expected
    assert parsed.version == 1 and parsed.acquisition.basis == "exact"
    assert parsed.acquisition.operator_evidence is None
    owner = start(case, owners)
    assert owner.pending.wait(5)
    state = json.loads((case.slot / "state.json").read_bytes())
    assert state["schema"] == "sobafm-fma-owned-slot-1"
    assert "acquisition" not in state and "reader_state" not in state
    with pytest.raises(intake.IntakeError, match="reader_handoff_not_available"):
        owner.read_into(Consumer())


def test_v1_recovery_stays_cleanup_only(
    case: Case,
    tmp_path: Path,
    owners: list[intake.LocalIntake],
) -> None:
    case.request = legacy_request(case)
    interrupted_cli(case, tmp_path)
    owner = case.owner(recovery=True)
    owners.append(owner)
    owner.start(execute=True)
    assert owner.join(5)
    assert owner.report()["released"] is True
    assert not (case.slot / "payload").exists()
    assert case.source.read_bytes() == case.data


def test_seek_inspection_rewinds_same_inode_for_complete_bounded_stream(
    case: Case,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    consumer = Consumer()
    reader = owner.read_into(consumer)
    assert reader.join(5)
    receipt = reader.report()
    assert receipt["completed"] is True
    assert receipt["stream_verified"] is True
    assert receipt["request_sha256"] == case.request.fingerprint
    assert receipt["read_bytes"] == receipt["delivered_bytes"] == len(case.data)
    assert receipt["read_sha256"] == consumer.digest.hexdigest() == case.request.file.sha256
    assert consumer.inspected.is_set() and consumer.finished.is_set()
    assert consumer.maximum_chunk <= 1024
    assert receipt["descriptor_retained"] is False
    assert {p.name for p in case.slot.iterdir()} <= {
        "owner.lock",
        "state.json",
        "state.tmp",
        "payload",
    }
    with pytest.raises(intake.IntakeError, match="reader_handoff_not_available"):
        owner.read_into(Consumer())
    assert case.source.read_bytes() == case.data


def test_cancel_between_admission_and_start_never_consumes(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    entered, proceed = threading.Event(), threading.Event()
    save = owner._save  # pyright: ignore[reportPrivateUsage]

    def held_save(phase: str) -> None:
        if phase == "pending_review":
            entered.set()
            assert proceed.wait(5)
        save(phase)

    monkeypatch.setattr(owner, "_save", held_save)
    consumer = Consumer()
    reader = owner.read_into(consumer)
    try:
        assert entered.wait(5)
        owner.cancel()
        deadline = owner.cleanup_deadline
        case.clock.advance(4)
        owner.cancel()
        assert owner.cleanup_deadline == deadline
        assert not owner.join()
        assert not consumer.inspected.is_set() and consumer.count == 0
    finally:
        proceed.set()
    assert owner.join(5)
    assert reader.join()
    assert reader.report()["completed"] is False
    assert owner.report()["cleanup_deadline_met"] is False
    assert owner.report()["released"] is True


@pytest.mark.parametrize("stage", ["inspect", "read", "write", "finish", "join"])
def test_expiry_during_owned_operation_revokes_later_consumption(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
    stage: str,
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    consumer = Consumer()
    entered, proceed = threading.Event(), threading.Event()
    read = os.read
    inspect = consumer.inspect
    write = consumer.write
    finish = consumer.finish

    def held_inspect(fd: int) -> None:
        if stage == "inspect":
            entered.set()
            assert proceed.wait(5)
        inspect(fd)

    def held_read(fd: int, size: int) -> bytes:
        if stage == "read" and threading.current_thread().name == "fma-owned-read" and size == 1024:
            entered.set()
            assert proceed.wait(5)
        return read(fd, size)

    def held_write(chunk: bytes) -> None:
        entered.set()
        assert proceed.wait(5)
        write(chunk)

    def held_finish() -> None:
        entered.set()
        assert proceed.wait(5)
        finish()

    def held_join(_timeout: float) -> bool:
        entered.set()
        assert proceed.wait(5)
        return True

    monkeypatch.setattr(consumer, "inspect", held_inspect)
    monkeypatch.setattr(os, "read", held_read)
    if stage == "write":
        monkeypatch.setattr(consumer, "write", held_write)
    elif stage == "finish":
        monkeypatch.setattr(consumer, "finish", held_finish)
    elif stage == "join":
        monkeypatch.setattr(consumer, "join", held_join)
    reader = owner.read_into(consumer)
    try:
        assert entered.wait(5)
        case.clock.advance(3604)
        assert not owner.join()
        assert owner.report()["cleanup_deadline"] == 3703
        assert owner.report()["cleanup_debt"] is True
        assert reader.report()["descriptor_retained"] is True
        assert (case.slot / "payload").exists()
        assert consumer.cancelled.wait(5)
        owner.cancel()
        assert owner.cleanup_deadline == 3703
    finally:
        proceed.set()
    assert owner.join(5)
    assert consumer.count == (
        len(case.data) if stage in ("finish", "join") else 1024 if stage == "write" else 0
    )
    assert reader.report()["completed"] is False
    assert owner.report()["released"] is True
    assert owner.report()["cleanup_deadline_met"] is False
    assert case.source.read_bytes() == case.data


@pytest.mark.parametrize("truncate", [False, True])
def test_mutation_or_truncation_after_inspection_stops_stream(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
    truncate: bool,
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    consumer = Consumer()

    def mutate(_fd: int) -> None:
        with (case.slot / "payload").open("r+b") as stream:
            if truncate:
                stream.truncate(7)
            else:
                stream.write(b"changed")

    monkeypatch.setattr(consumer, "inspect", mutate)
    reader = owner.read_into(consumer)
    assert owner.join(5)
    assert consumer.count == 0
    assert reader.report()["failure"] == "reader_payload_changed"
    assert reader.report()["completed"] is False
    assert owner.report()["released"] is True
    assert case.source.read_bytes() == case.data


def test_uncertain_recovered_reader_blocks_cleanup(
    case: Case,
    tmp_path: Path,
    owners: list[intake.LocalIntake],
) -> None:
    interrupted_cli(case, tmp_path)
    path = case.slot / "state.json"
    state = cast(dict[str, object], json.loads(path.read_bytes()))
    state["reader_state"] = "owned"
    path.write_text(json.dumps(state))
    owner = case.owner(recovery=True)
    owners.append(owner)
    owner.start(execute=True)
    assert owner.join(5)
    assert owner.failure == "uncertain_reader_ownership_debt"
    assert owner.report()["cleanup_debt"] is True
    assert owner.report()["released"] is False
    assert (case.slot / "payload").read_bytes() == case.data
    assert case.source.read_bytes() == case.data


def test_actual_downstream_thread_and_pipe_remain_owned_until_join(
    case: Case,
    owners: list[intake.LocalIntake],
) -> None:
    proceed = threading.Event()

    class Downstream(Consumer):
        def __init__(self) -> None:
            super().__init__()
            self.thread: threading.Thread | None = None
            self.pipe: tuple[int, int] | None = None

        def write(self, chunk: bytes) -> None:
            if self.thread is None:
                self.pipe = os.pipe()
                read_fd, write_fd = self.pipe
                amount = os.write(write_fd, chunk)

                def work() -> None:
                    assert proceed.wait(5)
                    assert len(os.read(read_fd, amount)) == amount
                    os.close(read_fd)
                    os.close(write_fd)
                    self.pipe = None

                self.thread = threading.Thread(target=work)
                self.thread.start()
            super().write(chunk)

        def join(self, timeout: float) -> bool:
            if self.thread is not None:
                self.thread.join(timeout)
            return self.pipe is None and (self.thread is None or not self.thread.is_alive())

    owner = start(case, owners)
    assert owner.pending.wait(5)
    consumer = Downstream()
    reader = owner.read_into(consumer)
    try:
        assert consumer.finished.wait(5)
        assert not reader.join()
        owner.cancel()
        assert consumer.cancelled.wait(5)
        case.clock.advance(4)
        assert not owner.join()
        assert owner.cleanup_deadline == 103
        assert owner.report()["cleanup_debt"] is True
        assert reader.report()["descriptor_retained"] is True
        assert consumer.pipe is not None
        assert os.fstat(consumer.pipe[0]).st_nlink == 1
        assert (case.slot / "payload").stat().st_nlink == 1
        owner.cancel()
        assert owner.cleanup_deadline == 103
    finally:
        proceed.set()
    assert owner.join(5)
    assert consumer.pipe is None and consumer.join(0)
    assert reader.report()["settled"] is True
    assert owner.report()["released"] is True
    assert owner.report()["cleanup_deadline_met"] is False
    assert case.source.read_bytes() == case.data


def test_blocked_reader_close_is_not_actual_release(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    entered, proceed = threading.Event(), threading.Event()
    close = os.close

    def held_close(fd: int) -> None:
        if threading.current_thread().name == "fma-owned-read":
            entered.set()
            assert proceed.wait(5)
        close(fd)

    monkeypatch.setattr(os, "close", held_close)
    reader = owner.read_into(Consumer())
    try:
        assert entered.wait(5)
        owner.cancel()
        case.clock.advance(4)
        assert not owner.join()
        assert reader.report()["in_flight"] == "reader_close"
        assert reader.report()["descriptor_retained"] is True
        assert owner.report()["released"] is False
        assert (case.slot / "payload").exists()
    finally:
        proceed.set()
    assert owner.join(5)
    assert reader.report()["settled"] is True
    assert owner.report()["cleanup_deadline_met"] is False


def test_held_cancel_callback_retains_borrowed_descriptor_and_backing(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    consumer = Consumer()
    inspecting, inspected = threading.Event(), threading.Event()
    cancelling, cancelled = threading.Event(), threading.Event()

    def inspect(_fd: int) -> None:
        inspecting.set()
        assert inspected.wait(5)

    def cancel() -> None:
        cancelling.set()
        assert cancelled.wait(5)

    monkeypatch.setattr(consumer, "inspect", inspect)
    monkeypatch.setattr(consumer, "cancel", cancel)
    reader = owner.read_into(consumer)
    try:
        assert inspecting.wait(5)
        owner.cancel()
        assert cancelling.wait(5)
        inspected.set()
        case.clock.advance(4)
        assert not owner.join()
        assert reader.report()["cancel_alive"] is True
        assert reader.report()["descriptor_retained"] is True
        assert owner.report()["released"] is False
        assert (case.slot / "payload").exists()
    finally:
        inspected.set()
        cancelled.set()
    assert owner.join(5)
    assert owner.report()["cleanup_deadline_met"] is False
    assert consumer.count == 0


def test_replaced_borrowed_descriptor_is_not_closed_or_used(
    case: Case,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    unrelated = tmp_path / "unrelated-synthetic-file"
    unrelated.write_bytes(b"must be preserved")
    consumer = Consumer()
    substituted, restored, wrong_close = threading.Event(), threading.Event(), threading.Event()
    descriptors: dict[str, int] = {}
    close = os.close

    def inspect(fd: int) -> None:
        descriptors["borrowed"] = fd
        descriptors["saved"] = os.dup(fd)
        other = os.open(unrelated, os.O_RDONLY)
        os.dup2(other, fd)
        close(other)
        substituted.set()

    def guarded_close(fd: int) -> None:
        if threading.current_thread().name == "fma-owned-read" and fd == descriptors.get(
            "borrowed"
        ):
            if os.fstat(fd).st_ino == unrelated.stat().st_ino:
                wrong_close.set()
            assert restored.wait(5)
        close(fd)

    monkeypatch.setattr(consumer, "inspect", inspect)
    monkeypatch.setattr(os, "close", guarded_close)
    reader = owner.read_into(consumer)
    try:
        assert substituted.wait(5)
        assert owner.stop.wait(5)
        assert not owner.join(0.1)
        assert not wrong_close.is_set()
        assert os.fstat(descriptors["borrowed"]).st_ino == unrelated.stat().st_ino
        assert owner.report()["cleanup_debt"] is True
        assert consumer.count == 0
    finally:
        os.dup2(descriptors["saved"], descriptors["borrowed"])
        close(descriptors["saved"])
        restored.set()
    assert owner.join(5)
    assert reader.report()["completed"] is False
    assert unrelated.read_bytes() == b"must be preserved"
    assert case.source.read_bytes() == case.data


def test_fresh_local_helper_borrows_same_inode_and_settles_before_stream(
    case: Case,
    owners: list[intake.LocalIntake],
) -> None:
    class LocalHelper(Consumer):
        def __init__(self) -> None:
            super().__init__()
            self.child: subprocess.Popen[bytes] | None = None

        def inspect(self, fd: int) -> None:
            script = (
                "import os,sys; f=int(sys.argv[1]); s=os.fstat(f); "
                "os.lseek(f,-3,os.SEEK_END); b=os.read(f,3); "
                "print(str(s.st_ino)+':'+str(len(b)))"
            )
            self.child = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", script, str(fd)],
                pass_fds=(fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert self.child.stdout is not None
            with self.child.stdout:
                result = self.child.stdout.read(100)
            assert self.child.wait(timeout=5) == 0
            assert result.strip() == f"{os.fstat(fd).st_ino}:3".encode()
            self.inspected.set()

        def cancel(self) -> None:
            super().cancel()
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()

        def join(self, timeout: float) -> bool:
            if self.child is None:
                return True
            return (
                self.child.poll() is not None
                and self.child.stdout is not None
                and self.child.stdout.closed
            )

    owner = start(case, owners)
    assert owner.pending.wait(5)
    consumer = LocalHelper()
    reader = owner.read_into(consumer)
    assert reader.join(5)
    assert reader.report()["completed"] is True
    assert consumer.count == len(case.data)
    assert consumer.digest.hexdigest() == case.request.file.sha256
    assert consumer.join(0)


def test_named_payload_replacement_after_inspection_never_streams_or_deletes_it(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    consumer = Consumer()
    payload, saved = case.slot / "payload", case.slot / "moved-by-test"
    changed = threading.Event()

    def inspect(_fd: int) -> None:
        payload.rename(saved)
        payload.write_bytes(b"unrelated synthetic replacement")
        changed.set()

    monkeypatch.setattr(consumer, "inspect", inspect)
    reader = owner.read_into(consumer)
    try:
        assert changed.wait(5)
        assert owner.stop.wait(5)
        assert not owner.join(0.1)
        assert consumer.count == 0
        assert payload.read_bytes() == b"unrelated synthetic replacement"
        assert saved.read_bytes() == case.data
    finally:
        payload.unlink()
        saved.rename(payload)
        owner.wake.set()
    assert owner.join(5)
    assert reader.report()["completed"] is False
    assert case.source.read_bytes() == case.data


@pytest.mark.parametrize("corrupt", [False, True])
def test_reader_requires_complete_count_and_hash(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
    corrupt: bool,
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    consumer = Consumer()
    read = os.read
    injected = False

    def incomplete(fd: int, amount: int) -> bytes:
        nonlocal injected
        data = read(fd, amount)
        if threading.current_thread().name == "fma-owned-read" and amount == 1024 and not injected:
            injected = True
            return b"X" + data[1:] if corrupt else b""
        return data

    monkeypatch.setattr(os, "read", incomplete)
    reader = owner.read_into(consumer)
    assert owner.join(5)
    assert injected
    assert reader.report()["failure"] == "reader_integrity_failed"
    assert reader.report()["stream_verified"] is False
    assert not consumer.finished.is_set()
    assert owner.report()["released"] is True
    assert case.source.read_bytes() == case.data


def test_invalid_or_unpersisted_admission_never_launches_consumer_work(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    with pytest.raises(intake.IntakeError, match="invalid_reader_consumer"):
        owner.read_into(cast(intake.ReadConsumer, object()))
    save = owner._save  # pyright: ignore[reportPrivateUsage]

    def failed_save(phase: str) -> None:
        if phase == "pending_review":
            raise OSError("synthetic ownership persistence failure")
        save(phase)

    monkeypatch.setattr(owner, "_save", failed_save)
    consumer = Consumer()
    reader = owner.read_into(consumer)
    assert owner.join(5)
    assert reader.join()
    assert not consumer.inspected.is_set() and consumer.count == 0
    assert not consumer.cancelled.is_set()
    assert owner.report()["released"] is True
    assert case.source.read_bytes() == case.data


def test_reader_thread_must_actually_exit_after_its_io_settles(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    entered, proceed = threading.Event(), threading.Event()
    settle = intake.OwnedRead._settle  # pyright: ignore[reportPrivateUsage]

    def held_exit(reader: intake.OwnedRead) -> None:
        settle(reader)
        entered.set()
        assert proceed.wait(5)

    monkeypatch.setattr(intake.OwnedRead, "_settle", held_exit)
    owner = start(case, owners)
    assert owner.pending.wait(5)
    reader = owner.read_into(Consumer())
    try:
        assert entered.wait(5)
        assert reader.report()["descriptor_retained"] is False
        assert reader.report()["consumer_released"] is True
        assert reader.report()["settled"] is False
        owner.cancel()
        case.clock.advance(4)
        assert not owner.join()
        assert owner.report()["released"] is False
        assert owner.report()["cleanup_deadline_met"] is False
        assert (case.slot / "payload").exists()
    finally:
        proceed.set()
    assert owner.join(5)
    assert owner.report()["cleanup_deadline_met"] is False
    assert owner.report()["released_at"] == 104


def test_reader_thread_start_failure_never_launches_consumer_work(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    original = threading.Thread.start

    def fail(thread: threading.Thread) -> None:
        if thread.name == "fma-owned-read":
            raise RuntimeError("synthetic thread-start failure")
        original(thread)

    monkeypatch.setattr(threading.Thread, "start", fail)
    consumer = Consumer()
    reader = owner.read_into(consumer)
    assert owner.join(5)
    assert reader.join()
    assert not consumer.inspected.is_set() and consumer.count == 0
    assert owner.report()["released"] is True
    assert owner.failure == "reader_start_failed"


def test_cancel_thread_start_failure_retains_then_settles_same_owner(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    consumer = Consumer()
    entered, proceed = threading.Event(), threading.Event()
    original = threading.Thread.start
    failed = False

    def fail_once(thread: threading.Thread) -> None:
        nonlocal failed
        if thread.name == "fma-consumer-cancel" and not failed:
            failed = True
            raise RuntimeError("synthetic cancellation-thread start failure")
        original(thread)

    def inspect(_fd: int) -> None:
        entered.set()
        assert proceed.wait(5)

    monkeypatch.setattr(threading.Thread, "start", fail_once)
    monkeypatch.setattr(consumer, "inspect", inspect)
    reader = owner.read_into(consumer)
    try:
        assert entered.wait(5)
        owner.cancel()
        assert consumer.cancelled.wait(5)
        assert failed
        case.clock.advance(4)
        assert not owner.join()
        assert reader.report()["descriptor_retained"] is True
        assert owner.cleanup_deadline == 103
    finally:
        proceed.set()
    assert owner.join(5)
    assert owner.report()["released"] is True
    assert owner.report()["cleanup_deadline_met"] is False
