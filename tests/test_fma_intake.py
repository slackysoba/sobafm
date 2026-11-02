"""Synthetic, bounded real-filesystem schedules; no source or decoder activity."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from sobafm import fma_intake as intake
from sobafm.catalog import Catalog, parse_catalog

CATALOG_PATH = Path(__file__).parents[1] / "evaluations/catalog/fma-night-owl-catalog.json"


def test_enclosing_case_can_only_tighten_original_cleanup_clock(case: Case) -> None:
    owner = case.owner()
    original = case.clock.utc - timedelta(seconds=1)
    owner.cancel(started_at=original)
    assert owner.cleanup_deadline == case.clock.mono + 2
    case.clock.advance(0.5)
    owner.cancel(started_at=case.clock.utc)
    assert owner.cleanup_deadline == case.clock.mono + 1.5
    owner.cancel(started_at=original - timedelta(seconds=1))
    assert owner.cleanup_deadline == case.clock.mono + 0.5


def test_invalid_enclosing_clock_does_not_change_intake_state(case: Case) -> None:
    owner = case.owner()
    for value in (case.clock.utc + timedelta(seconds=1), case.clock.utc.replace(tzinfo=None)):
        with pytest.raises(intake.IntakeError, match="invalid_original_cleanup_time"):
            owner.cancel(started_at=value)
        assert not owner.stop.is_set() and owner.cleanup_deadline is None
    recovered = case.owner(recovery=True)
    recovered.cancel(started_at=case.clock.utc - timedelta(seconds=1))
    assert recovered.cleanup_deadline_unknown and recovered.cleanup_deadline is None
    assert recovered.cleanup_missed


@dataclass
class Clock:
    utc: datetime
    mono: float = 100.0

    def now(self) -> datetime:
        return self.utc

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.utc += timedelta(seconds=seconds)
        self.mono += seconds


@dataclass
class Case:
    source: Path
    slot: Path
    request: intake.IntakeRequest
    clock: Clock
    data: bytes

    def owner(self, *, recovery: bool = False) -> intake.LocalIntake:
        return intake.LocalIntake(
            self.slot,
            None if recovery else self.source,
            self.request,
            recovery=recovery,
            utc_now=self.clock.now,
            monotonic=self.clock.monotonic,
        )


@pytest.fixture
def catalog() -> Catalog:
    return parse_catalog(CATALOG_PATH.read_text())


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Catalog) -> Case:
    monkeypatch.setattr(intake, "CHUNK_BYTES", 1024)
    source = tmp_path / "operator-original.unknown"
    data = b"synthetic-not-a-recording-" * 200
    source.write_bytes(data)
    info = source.stat()
    clock = Clock(datetime.now(UTC))
    identity = intake.FileIdentity(
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        hashlib.sha256(data).hexdigest(),
    )
    request = intake.IntakeRequest(
        intake.SOURCE_URL,
        intake.METADATA_DIGEST,
        intake.CREDIT_SNAPSHOT,
        intake.Acquisition(
            clock.utc.isoformat().replace("+00:00", "Z"), "exact", "Synthetic acquisition evidence."
        ),
        (clock.utc + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        ("Synthetic download-time notice for offline regression only.",),
        identity,
    )
    request = intake.parse_request(manifest(request), catalog, clock.now())
    slot = tmp_path / "pending"
    slot.mkdir(mode=0o700)
    return Case(source, slot, request, clock, data)


def manifest(request: intake.IntakeRequest) -> bytes:
    return json.dumps(request.manifest()).encode()


@pytest.fixture
def owners() -> Iterator[list[intake.LocalIntake]]:
    active: list[intake.LocalIntake] = []
    yield active
    for owner in active:
        owner.cancel()
        assert owner.join(5), owner.report()


def start(case: Case, owners: list[intake.LocalIntake]) -> intake.LocalIntake:
    owner = case.owner()
    owners.append(owner)
    owner.start(execute=True)
    return owner


def read_barrier(
    monkeypatch: pytest.MonkeyPatch,
    owner: intake.LocalIntake,
    *,
    read_number: int = 2,
) -> tuple[threading.Event, threading.Event]:
    entered, release = threading.Event(), threading.Event()
    original = os.read
    count = 0

    def read(fd: int, size: int) -> bytes:
        nonlocal count
        if fd == owner.fds.get("source") and owner.io == "source_read":
            count += 1
            if count == read_number:
                entered.set()
                assert release.wait(5), "test did not release its held read"
        return original(fd, size)

    monkeypatch.setattr(os, "read", read)
    return entered, release


def test_pending_copy_has_no_admission_and_preserves_original(
    case: Case,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    assert (case.slot / "payload").read_bytes() == case.data
    receipt = owner.report()
    assert receipt["pending_valid"] is True
    assert receipt["copied_sha256"] == case.request.file.sha256
    assert receipt["admission_reviews_created"] == 0
    assert receipt["recording_correspondence"] == "unverified"
    assert receipt["worker_alive"] is True
    assert str(case.source) not in json.dumps(receipt)
    assert case.request.download_notices[0] not in json.dumps(receipt)
    assert len(json.dumps(receipt)) < intake.MAX_MANIFEST_BYTES
    owner.cancel()
    assert owner.join(5)
    assert owner.report()["released"] is True
    assert owner.report()["cleanup_deadline_met"] is True
    assert not (case.slot / "payload").exists()
    assert case.source.read_bytes() == case.data
    observed_release = owner.report()["released_at"]
    case.clock.advance(4)
    assert owner.report()["released_at"] == observed_release
    assert owner.report()["cleanup_deadline_met"] is True


@pytest.mark.parametrize("version", [1, 2])
def test_fma_ledger_and_request_keep_original_serialized_namespace(
    case: Case,
    owners: list[intake.LocalIntake],
    version: int,
) -> None:
    from typing import Literal

    case.request = replace(
        case.request,
        version=cast(Literal[1, 2], version),
        acquisition=replace(
            case.request.acquisition,
            operator_evidence=None if version == 1 else "Synthetic evidence.",
        ),
    )
    owner = start(case, owners)
    assert owner.pending.wait(5)
    raw = (case.slot / "state.json").read_bytes()
    state = cast(dict[str, object], json.loads(raw))
    time_key = "acquired_at" if version == 1 else "acquisition"
    assert set(state) == {
        "schema",
        "owner_id",
        "request_sha256",
        time_key,
        "expires_at",
        "phase",
        "directory_identity",
        "lock_identity",
        "payload_identity",
        "copied_bytes",
        "copied_sha256",
        "cleanup_started_utc",
        "cleanup_missed",
    } | ({"reader_state"} if version == 2 else set())
    assert state["schema"] == f"sobafm-fma-owned-slot-{version}"
    assert state[time_key] == case.request.time_fields()[time_key]
    assert state["expires_at"] == case.request.expires_at
    assert (
        raw == json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    )
    manifest_fields = case.request.manifest()
    assert manifest_fields["schema"] == f"sobafm-fma-intake-{version}"
    if version == 1:
        del manifest_fields["schema"]
    original_fingerprint = hashlib.sha256(
        json.dumps(
            manifest_fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()
    assert state["request_sha256"] == original_fingerprint == case.request.fingerprint


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_url", "https://freemusicarchive.org/another-recording/"),
        ("metadata_digest", "0" * 64),
        ("credit_snapshot", "unknown"),
        ("acquisition", "2026-11-01T01:00:00-08:00"),
        ("expires_at", "not a time"),
        ("download_notices", []),
        ("download_notices", [" notice"]),
        ("download_notices", ["control\nnotice"]),
    ],
)
def test_request_rejection_precedes_destination_mutation(
    case: Case,
    catalog: Catalog,
    field: str,
    value: object,
) -> None:
    data = case.request.manifest()
    data[field] = value
    with pytest.raises(intake.IntakeError):
        intake.parse_request(json.dumps(data).encode(), catalog, case.clock.now())
    assert list(case.slot.iterdir()) == []


@pytest.mark.parametrize("kind", ["empty", "oversize", "future", "eight_days", "expired", "hash"])
def test_invalid_window_size_or_hash_never_creates_destination(
    case: Case,
    owners: list[intake.LocalIntake],
    kind: str,
) -> None:
    request = case.request
    if kind in ("empty", "oversize"):
        request = replace(
            request,
            file=replace(request.file, bytes=0 if kind == "empty" else intake.MAX_BYTES + 1),
        )
    elif kind == "hash":
        request = replace(request, file=replace(request.file, sha256="0" * 64))
    elif kind == "future":
        request = replace(
            request,
            acquisition=replace(request.acquisition, retention_anchor_utc=request.expires_at),
        )
    elif kind == "eight_days":
        request = replace(
            request,
            expires_at=(case.clock.utc + timedelta(days=8)).isoformat().replace("+00:00", "Z"),
        )
    else:
        case.clock.advance(3600)
    case.request = request
    if kind == "hash":
        owner = start(case, owners)
        assert owner.join(5)
        assert owner.failure == "source_integrity_failed"
    else:
        with pytest.raises(intake.IntakeError):
            case.owner().start(execute=True)
    assert list(case.slot.iterdir()) == []
    assert case.source.read_bytes() == case.data


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "parent_link", "alias", "fifo"])
def test_links_special_files_and_original_directory_are_rejected(
    case: Case,
    tmp_path: Path,
    owners: list[intake.LocalIntake],
    kind: str,
) -> None:
    original = case.source
    if kind == "symlink":
        case.source = tmp_path / "link"
        case.source.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, tmp_path / "other-link")
    elif kind == "parent_link":
        link = tmp_path / "parent-link"
        link.symlink_to(tmp_path, target_is_directory=True)
        case.source = link / original.name
    elif kind == "alias":
        case.slot = tmp_path
    else:
        case.source = tmp_path / "pipe"
        os.mkfifo(case.source)
    owner = start(case, owners)
    assert owner.join(5)
    assert owner.failure is not None
    assert not (case.slot / "owner.lock").exists()
    assert original.read_bytes() == case.data


@pytest.mark.parametrize("substitute", [False, True])
def test_mutation_or_substitution_after_partial_copy_is_rejected(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
    substitute: bool,
) -> None:
    owner = case.owner()
    owners.append(owner)
    entered, release = read_barrier(monkeypatch, owner)
    owner.start(execute=True)
    saved = case.source.with_name("saved-original")
    try:
        assert entered.wait(5)
        assert owner.copied_bytes == 1024
        if substitute:
            case.source.rename(saved)
            case.source.write_bytes(b"replacement")
        else:
            with case.source.open("r+b") as stream:
                stream.write(b"changed")
    finally:
        release.set()
    assert owner.join(5)
    assert owner.failure in ("source_changed", "source_substituted")
    assert not owner.pending.is_set()
    assert not (case.slot / "payload").exists()
    assert owner.report()["released"] is True
    assert (saved if substitute else case.source).exists()
    if substitute:
        assert saved.read_bytes() == case.data
        assert case.source.read_bytes() == b"replacement"


def test_repeated_cancel_retains_blocked_reader_and_original_deadline(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = case.owner()
    owners.append(owner)
    entered, release = read_barrier(monkeypatch, owner)
    owner.start(execute=True)
    try:
        assert entered.wait(5)
        owner.cancel()
        deadline = owner.cleanup_deadline
        case.clock.advance(4)
        owner.cancel()
        assert not owner.join()
        receipt = owner.report()
        assert receipt["cleanup_deadline"] == deadline == 103
        assert receipt["original_io_in_flight"] == "source_read"
        assert receipt["payload_retained"] is True
        assert receipt["cleanup_debt"] is True
        assert receipt["released"] is False
        assert receipt["worker_alive"] is True
        assert (case.slot / "payload").stat().st_size == 1024
    finally:
        release.set()
    assert owner.join(5)
    assert owner.copied_bytes == 1024
    assert owner.report()["cleanup_deadline_met"] is False
    assert owner.report()["released"] is True
    assert case.source.read_bytes() == case.data


def test_midcopy_expiry_and_wall_clock_rollback_do_not_publish_pending(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = case.owner()
    owners.append(owner)
    entered, release = read_barrier(monkeypatch, owner)
    owner.start(execute=True)
    try:
        assert entered.wait(5)
        case.clock.mono += 3604
        case.clock.utc -= timedelta(days=1)
        assert not owner.join()
        assert owner.report()["cleanup_deadline"] == 3703
        assert owner.report()["cleanup_debt"] is True
    finally:
        release.set()
    assert owner.join(5)
    assert owner.failure == "expired"
    assert not owner.pending.is_set()
    assert owner.report()["cleanup_deadline_met"] is False
    assert case.source.read_bytes() == case.data


def test_partial_write_cancel_counts_actual_bytes(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = case.owner()
    owners.append(owner)
    original = os.write

    def write(fd: int, data: bytes | memoryview[int]) -> int:
        if fd == owner.fds.get("payload"):
            amount = original(fd, data[:17])
            owner.cancel()
            return amount
        return original(fd, data)

    monkeypatch.setattr(os, "write", write)
    owner.start(execute=True)
    assert owner.join(5)
    assert owner.copied_bytes == 17
    assert not owner.pending.is_set()
    assert owner.report()["released"] is True
    assert case.source.read_bytes() == case.data


def test_failed_deletion_retains_debt_blocks_reuse_and_settles_late(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    attempted, allowed = threading.Event(), threading.Event()
    original = os.unlink

    def unlink(path: str, *, dir_fd: int | None = None) -> None:
        if path == "payload" and not allowed.is_set():
            attempted.set()
            raise PermissionError("synthetic deletion failure")
        original(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    try:
        owner.cancel()
        assert attempted.wait(5)
        case.clock.advance(4)
        assert not owner.join()
        deadline = owner.cleanup_deadline
        assert owner.report()["cleanup_debt"] is True
        assert owner.report()["payload_retained"] is True
        contender = start(case, owners)
        assert contender.join(5)
        assert contender.failure is not None
        assert (case.slot / "payload").read_bytes() == case.data
        owner.cancel()
        assert owner.cleanup_deadline == deadline
    finally:
        allowed.set()
        owner.wake.set()
    assert owner.join(5)
    assert owner.report()["released"] is True
    assert owner.report()["cleanup_deadline_met"] is False
    assert case.source.read_bytes() == case.data


def test_destination_replacement_is_never_deleted(
    case: Case,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    payload, saved = case.slot / "payload", case.slot / "temporarily-moved-owned-file"
    payload.rename(saved)
    payload.write_bytes(b"unowned replacement")
    try:
        owner.cancel()
        assert not owner.join(0.1)
        assert owner.report()["cleanup_debt"] is True
        assert payload.read_bytes() == b"unowned replacement"
        assert saved.read_bytes() == case.data
    finally:
        # The test owns the substitution; restore the exact original for settlement.
        payload.unlink()
        saved.rename(payload)
        owner.wake.set()
    assert owner.join(5)
    assert case.source.read_bytes() == case.data


def interrupted_cli(case: Case, tmp_path: Path) -> dict[str, object]:
    path = tmp_path / "manifest.json"
    path.write_bytes(manifest(case.request))
    with subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sobafm.fma_intake",
            "--execute-reviewed-local-intake",
            "--source",
            str(case.source),
            "--directory",
            str(case.slot),
            "--manifest",
            str(path),
            "--catalog",
            str(CATALOG_PATH),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        try:
            assert child.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                assert selector.select(10), "synthetic intake did not report pending"
                line = child.stdout.readline(intake.MAX_MANIFEST_BYTES + 1)
            receipt = cast(dict[str, object], json.loads(line))
            assert receipt["pending_valid"] is True
            return receipt
        finally:
            child.kill()
            child.wait(timeout=5)


def test_process_interruption_recovery_never_recopies_or_renews_expiry(
    case: Case,
    tmp_path: Path,
    owners: list[intake.LocalIntake],
) -> None:
    receipt = interrupted_cli(case, tmp_path)
    assert (case.slot / "payload").read_bytes() == case.data
    case.clock.advance(3610)
    owner = case.owner(recovery=True)
    owners.append(owner)
    owner.start(execute=True)
    assert owner.join(5)
    recovered = owner.report()
    assert recovered["owner_id"] == receipt["owner_id"]
    assert recovered["expires_at"] == receipt["expires_at"]
    assert recovered["acquisition"] == receipt["acquisition"]
    assert recovered["cleanup_deadline"] is None
    assert recovered["cleanup_deadline_known"] is False
    assert recovered["cleanup_deadline_met"] is False
    assert recovered["released"] is True
    assert not owner.pending.is_set()
    assert not (case.slot / "payload").exists()
    assert case.source.read_bytes() == case.data


@pytest.mark.parametrize(
    "change", ["directory", "lock", "missing_state", "time", "unbound", "original_identity"]
)
def test_recovery_inconsistent_ownership_never_authorizes_deletion(
    case: Case,
    tmp_path: Path,
    owners: list[intake.LocalIntake],
    change: str,
) -> None:
    interrupted_cli(case, tmp_path)
    state_path = case.slot / "state.json"
    state = cast(dict[str, object], json.loads(state_path.read_bytes()))
    if change == "directory":
        state["directory_identity"] = [0, 1]
    elif change == "lock":
        state["lock_identity"] = [0, 1]
    elif change == "time":
        state["cleanup_started_utc"] = "invalid"
    elif change == "unbound":
        state["payload_identity"] = None
    elif change == "original_identity":
        state["payload_identity"] = [case.request.file.device, case.request.file.inode]
    if change == "missing_state":
        state_path.unlink()
    else:
        state_path.write_text(json.dumps(state))
    owner = case.owner(recovery=True)
    owners.append(owner)
    owner.start(execute=True)
    assert owner.join(5)
    assert owner.report()["cleanup_debt"] is True
    assert owner.report()["released"] is False
    assert owner.report()["unresolved_previous_slot"] is True
    assert owner.report()["cleanup_deadline"] is None
    assert owner.report()["cleanup_deadline_known"] is False
    assert owner.report()["cleanup_deadline_met"] is False
    assert owner.cleanup_started_utc is None
    assert owner.cleanup_missed is True
    case.clock.advance(4)
    owner.cancel()
    assert owner.report()["cleanup_deadline"] is None
    assert (case.slot / "payload").read_bytes() == case.data
    assert case.source.read_bytes() == case.data


def test_explicit_opt_in_precedes_input_reads(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="2"):
        intake.main(["--directory", str(tmp_path), "--manifest", "missing", "--catalog", "missing"])
    assert list(tmp_path.iterdir()) == []


def test_failed_state_sync_retains_temporary_owner_until_settlement(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = case.owner()
    owners.append(owner)
    original = os.fsync
    failed = False

    def fsync(fd: int) -> None:
        nonlocal failed
        if fd == owner.fds.get("state_write") and not failed:
            failed = True
            raise OSError("synthetic sync failure")
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    owner.start(execute=True)
    assert owner.join(5)
    assert failed
    assert owner.failure == "local_io_failed"
    assert owner.report()["released"] is True
    assert not (case.slot / "state.tmp").exists()
    assert not (case.slot / "payload").exists()
    assert case.source.read_bytes() == case.data


def test_failed_close_retains_original_descriptor_and_deadline(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    original = os.close
    attempted, allowed = threading.Event(), threading.Event()
    payload_fd = owner.fds["payload"]

    def close(fd: int) -> None:
        if fd == payload_fd and not allowed.is_set():
            attempted.set()
            raise OSError("synthetic still-open descriptor")
        original(fd)

    monkeypatch.setattr(os, "close", close)
    try:
        owner.cancel()
        assert attempted.wait(5)
        case.clock.advance(4)
        assert not owner.join()
        assert owner.report()["cleanup_debt"] is True
        assert "payload" in cast(list[str], owner.report()["retained_handles"])
        assert os.fstat(payload_fd).st_nlink == 0
        assert not (case.slot / "payload").exists()
        assert owner.report()["released"] is False
    finally:
        allowed.set()
        owner.wake.set()
    assert owner.join(5)
    assert owner.report()["cleanup_deadline_met"] is False
    assert owner.report()["released"] is True


def test_pending_expiry_releases_copy_without_renewal(
    case: Case,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    case.clock.advance(3600)
    assert owner.join(5)
    assert owner.failure == "expired"
    assert owner.report()["cleanup_deadline"] == 3703
    assert owner.report()["pending_valid"] is False
    assert owner.report()["released"] is True
    assert case.source.read_bytes() == case.data


def test_expiry_while_saving_pending_never_publishes_pending_receipt(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = case.owner()
    owners.append(owner)
    original = os.fsync

    def fsync(fd: int) -> None:
        original(fd)
        if owner.phase == "pending_review":
            case.clock.advance(3604)

    monkeypatch.setattr(os, "fsync", fsync)
    owner.start(execute=True)
    assert owner.join(5)
    assert not owner.pending.is_set()
    assert owner.report()["cleanup_deadline_met"] is False
    assert owner.report()["released"] is True


def test_truncated_read_cannot_become_pending(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = case.owner()
    owners.append(owner)
    original = os.read

    def read(fd: int, size: int) -> bytes:
        if fd == owner.fds.get("source") and owner.io == "source_read" and owner.copied_bytes:
            return b""
        return original(fd, size)

    monkeypatch.setattr(os, "read", read)
    owner.start(execute=True)
    assert owner.join(5)
    assert owner.failure == "copy_integrity_failed"
    assert not owner.pending.is_set()
    assert owner.report()["released"] is True
    assert case.source.read_bytes() == case.data


def test_closed_schema_rejects_duplicate_keys_and_oversize_manifest(
    case: Case,
    catalog: Catalog,
) -> None:
    duplicate = manifest(case.request).replace(b"{", b'{"schema":"duplicate",', 1)
    for payload in (duplicate, b" " * (intake.MAX_MANIFEST_BYTES + 1)):
        with pytest.raises(intake.IntakeError):
            intake.parse_request(payload, catalog, case.clock.now())
    assert list(case.slot.iterdir()) == []


def test_stat_failure_after_creation_does_not_lose_payload_ownership(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
) -> None:
    owner = case.owner()
    owners.append(owner)
    original = os.fstat
    failed = False

    def fstat(fd: int) -> os.stat_result:
        nonlocal failed
        if fd == owner.fds.get("payload") and not failed:
            failed = True
            raise OSError("synthetic initial payload stat failure")
        return original(fd)

    monkeypatch.setattr(os, "fstat", fstat)
    owner.start(execute=True)
    assert owner.join(5)
    assert failed
    assert owner.report()["released"] is True
    assert not (case.slot / "payload").exists()
    assert case.source.read_bytes() == case.data


def test_recovery_preserves_recorded_first_cleanup_start(
    case: Case,
    tmp_path: Path,
    owners: list[intake.LocalIntake],
) -> None:
    interrupted_cli(case, tmp_path)
    state_path = case.slot / "state.json"
    state = cast(dict[str, object], json.loads(state_path.read_bytes()))
    state["cleanup_started_utc"] = (
        (case.clock.utc - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    )
    state_path.write_text(json.dumps(state))
    case.clock.advance(20)
    owner = case.owner(recovery=True)
    owners.append(owner)
    owner.start(execute=True)
    assert owner.join(5)
    assert owner.cleanup_started_utc == state["cleanup_started_utc"]
    assert owner.report()["cleanup_deadline"] == 98
    assert owner.report()["cleanup_deadline_known"] is True
    assert owner.report()["cleanup_deadline_met"] is False
    assert owner.report()["released"] is True


def test_released_slot_cannot_be_reused_by_retry(
    case: Case,
    owners: list[intake.LocalIntake],
) -> None:
    owner = start(case, owners)
    assert owner.pending.wait(5)
    owner.cancel()
    assert owner.join(5)
    state = (case.slot / "state.json").read_bytes()
    retry = start(case, owners)
    assert retry.join(5)
    assert retry.failure == "slot_not_empty"
    assert (case.slot / "state.json").read_bytes() == state
    assert not (case.slot / "payload").exists()
    assert case.source.read_bytes() == case.data


@pytest.mark.parametrize("observe_late_worker", [True, False])
def test_cleanup_waits_for_actual_worker_exit_after_io_settles(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    owners: list[intake.LocalIntake],
    observe_late_worker: bool,
) -> None:
    owner = case.owner()
    owners.append(owner)
    io_settled, allow_exit = threading.Event(), threading.Event()
    release = owner._release  # pyright: ignore[reportPrivateUsage]

    def hold_after_io() -> None:
        release()
        io_settled.set()
        assert allow_exit.wait(5), "test did not release its post-I/O worker barrier"

    monkeypatch.setattr(owner, "_release", hold_after_io)
    owner.start(execute=True)
    try:
        assert owner.pending.wait(5)
        owner.cancel()
        assert io_settled.wait(5)
        assert owner.fds == {}
        assert not (case.slot / "payload").exists()
        assert owner.cleanup_deadline == 103
        case.clock.advance(4)
        if observe_late_worker:
            assert not owner.join()
            interim = owner.report()
            assert interim["worker_alive"] is True
            assert interim["cleanup_debt"] is True
            assert interim["released"] is False
            assert interim["released_at"] is None
            assert interim["cleanup_deadline_met"] is False
            owner.cancel()
            assert owner.cleanup_deadline == 103
    finally:
        allow_exit.set()
    assert owner.join(5)
    final = owner.report()
    assert final["worker_alive"] is False
    assert final["released"] is True
    assert final["released_at"] == 104
    assert final["cleanup_deadline"] == 103
    assert final["cleanup_deadline_met"] is False
    case.clock.advance(4)
    assert owner.report()["cleanup_deadline_met"] is False
    assert owner.report()["released_at"] == 104
    assert case.source.read_bytes() == case.data
