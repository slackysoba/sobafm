"""Compact intake records retain wire compatibility and real persistence owners."""

from __future__ import annotations

import errno
import gc
import hashlib
import os
import threading
from dataclasses import replace
from typing import Literal, cast

import pytest

from sobafm import fma_intake as intake
from sobafm import json_records as records
from sobafm.catalog import Catalog
from tests.test_fma_intake import Case, manifest
from tests.test_fma_intake import case as case
from tests.test_fma_intake import catalog as catalog
from tests.test_fma_intake import owners as owners
from tests.test_fma_report_encoding import track_arenas
from tests.test_json_records import oracle

# Public SHA-256 vectors generated from the fixed synthetic requests below.
V1_GOLDEN = (
    "1bcaf1dd2f9ab1a4c986e848214023111ad95abe558f726a2c3f562c3ad84559"  # pragma: allowlist secret
)
V2_GOLDEN = (
    "6edac59d04e673f9ce29dd729ae418819f7adf8f5d4151483f6edd793385f9b6"  # pragma: allowlist secret
)


@pytest.mark.parametrize(
    ("version", "size", "expected"),
    [
        (1, 563, V1_GOLDEN),
        (2, 709, V2_GOLDEN),
    ],
)
def test_fixed_v1_v2_fingerprint_vectors(version: Literal[1, 2], size: int, expected: str) -> None:
    request = intake.IntakeRequest(
        intake.SOURCE_URL,
        intake.METADATA_DIGEST,
        intake.CREDIT_SNAPSHOT,
        intake.Acquisition(
            "2026-11-02T00:00:00Z", "exact", None if version == 1 else "Synthetic evidence."
        ),
        "2026-11-09T00:00:00Z",
        ("Synthetic notice.",),
        intake.FileIdentity(1, 2, 3, 4, 5, "a" * 64),
        version,
    )
    fields = request.manifest()
    if version == 1:
        del fields["schema"]
    wire = oracle(fields)
    assert len(wire) == size and hashlib.sha256(wire).hexdigest() == expected
    assert records.encode_record(fields, size) == wire
    assert request.fingerprint == expected


def test_large_identity_and_direct_fingerprint_have_no_report_scalar_or_size_cap(
    case: Case,
    catalog: Catalog,
) -> None:
    request = replace(case.request, file=replace(case.request.file, device=1 << 256))
    request.validate(case.clock.now())
    parsed = intake.parse_request(manifest(request), catalog, case.clock.now())
    assert parsed.file.device == 1 << 256
    assert parsed.fingerprint == hashlib.sha256(oracle(parsed.manifest())).hexdigest()
    # An unvalidated direct constructor had no fingerprint-output cap; retain it.
    direct = replace(request, download_notices=("x" * 20000,))
    assert len(oracle(direct.manifest())) > intake.MAX_MANIFEST_BYTES
    assert direct.fingerprint == hashlib.sha256(oracle(direct.manifest())).hexdigest()


def test_exact_notice_and_acquisition_gates_and_hashes(case: Case, catalog: Catalog) -> None:
    evidence = "\u00e9" * 682 + "aa"
    acquisition = replace(case.request.acquisition, operator_evidence=evidence)
    assert len(oracle(evidence)) == 4096
    acquisition.validate()
    assert (
        acquisition.record(safe=True)["operator_evidence_sha256"]
        == hashlib.sha256(oracle(evidence)).hexdigest()
    )
    with pytest.raises(intake.IntakeError, match="invalid_acquisition_evidence"):
        replace(acquisition, operator_evidence=evidence + "a").validate()
    notices = ("\u00e9" * 681 + "aaaaaa",)
    request = replace(case.request, download_notices=notices)
    assert len(oracle(notices)) == 4096
    request.validate(case.clock.now())
    assert request.summary_fields()["notices_sha256"] == hashlib.sha256(oracle(notices)).hexdigest()
    assert intake.parse_request(manifest(request), catalog, case.clock.now()) == request
    over = replace(request, download_notices=(notices[0] + "a",))
    with pytest.raises(intake.IntakeError, match="invalid_notices"):
        over.validate(case.clock.now())
    with pytest.raises(intake.IntakeError, match="notices_oversize"):
        intake.parse_request(manifest(over), catalog, case.clock.now())


def test_report_exact_gate_keeps_nonfinite_and_large_builtin_values(case: Case) -> None:
    owner = case.owner()
    owner.copied_bytes = 1 << 160
    owner.cleanup_deadline = float("inf")
    base = owner.report()
    assert records.encode_record(base, intake.MAX_MANIFEST_BYTES) == oracle(base)
    owner.owner_id += "x" * (intake.MAX_MANIFEST_BYTES - len(oracle(base)))
    assert len(oracle(owner.report())) == intake.MAX_MANIFEST_BYTES
    owner.owner_id += "x"
    with pytest.raises(intake.IntakeError, match="report_oversize"):
        owner.report()


def test_state_exact_gate_and_overflow_before_allocation_or_temp_mutation(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact = {"v": "x" * (intake.MAX_MANIFEST_BYTES - 8)}
    assert len(intake._state_record(exact)) == intake.MAX_MANIFEST_BYTES  # pyright: ignore[reportPrivateUsage]
    owner = case.owner()
    owner.fd_identities.update(directory=(1, 2), lock=(3, 4))
    owner.temp_identity = (5, 6)
    temp = case.slot / "state.tmp"
    temp.write_bytes(b"previous owned evidence")
    actual_allocate = bytearray

    def allocate(size: int) -> bytearray:
        assert size == records.HASH_BYTES, "rejected state reached document allocation"
        return actual_allocate(size)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("oversize state reached temp removal or open")

    monkeypatch.setattr(records, "bytearray", allocate, raising=False)
    monkeypatch.setattr(owner, "_remove_temp", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    with pytest.raises(intake.IntakeError, match="state_oversize") as retained:
        owner._save("x" * intake.MAX_MANIFEST_BYTES)  # pyright: ignore[reportPrivateUsage]
    assert retained.value.__context__ is None and retained.value.__cause__ is None
    assert owner.temp_identity == (5, 6) and temp.read_bytes() == b"previous owned evidence"


@pytest.mark.parametrize("adapter", ["_fits_record", "_record_hash", "_state_record"])
def test_neutral_failures_translate_without_completed_exception_context(adapter: str) -> None:
    with pytest.raises(intake.IntakeError, match="invalid_json_record") as retained:
        if adapter == "_fits_record":
            intake._fits_record(object(), 4096)  # pyright: ignore[reportPrivateUsage]
        elif adapter == "_record_hash":
            intake._record_hash(object())  # pyright: ignore[reportPrivateUsage]
        else:
            intake._state_record(object())  # pyright: ignore[reportPrivateUsage]
    assert retained.value.__context__ is None and retained.value.__cause__ is None


def test_state_partial_and_interrupted_writes_keep_one_immutable_alias(
    case: Case,
    owners: list[intake.LocalIntake],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = case.owner()
    owners.append(owner)
    original = os.write
    views: list[memoryview] = []
    records_seen: list[bytes] = []
    calls = 0

    def write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        if fd == owner.fds.get("state_write"):
            assert isinstance(data, memoryview)
            for previous in views:
                with pytest.raises(ValueError, match="released memoryview"):
                    _ = previous.obj
            views.append(data)
            if calls == 0:
                records_seen.append(cast(bytes, data.obj))
            if calls < 3:
                assert data.obj is records_seen[0]
                calls += 1
                if calls == 1:
                    raise InterruptedError(errno.EINTR, "synthetic interruption")
                with data[:7] as partial:
                    return original(fd, partial)
            calls += 1
        return original(fd, data)

    monkeypatch.setattr(os, "write", write)
    owner.start(execute=True)
    assert owner.pending.wait(5)
    owner.cancel()
    assert owner.join(5) and owner.report()["released"] is True and calls > 3
    for view in views:
        with pytest.raises(ValueError, match="released memoryview"):
            _ = view.obj


@pytest.mark.parametrize("bad", [0, -1, True, 100000, 0.5, None, "1"])
def test_invalid_state_write_return_runs_existing_cleanup(
    case: Case,
    owners: list[intake.LocalIntake],
    monkeypatch: pytest.MonkeyPatch,
    bad: object,
) -> None:
    owner = case.owner()
    owners.append(owner)
    original = os.write
    failed = False

    def write(fd: int, data: bytes | memoryview) -> int:
        nonlocal failed
        if fd == owner.fds.get("state_write") and not failed:
            failed = True
            return cast(int, bad)
        return original(fd, data)

    monkeypatch.setattr(os, "write", write)
    owner.start(execute=True)
    assert owner.join(5)
    assert failed and owner.failure == "state_write_failed" and owner.report()["released"] is True
    assert not (case.slot / "state.tmp").exists() and not (case.slot / "payload").exists()


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("stage", ["write", "fsync", "close", "replace", "parent_fsync"])
def test_held_persistence_retains_record_owner_and_original_clock(
    case: Case,
    owners: list[intake.LocalIntake],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    fail: bool,
) -> None:
    owner = case.owner()
    owners.append(owner)
    entered, release = threading.Event(), threading.Event()
    live: dict[int, int] = {}
    actual_record = intake._state_record  # pyright: ignore[reportPrivateUsage]
    original_write, original_sync, original_close, original_replace = (
        os.write,
        os.fsync,
        os.close,
        os.replace,
    )
    held = False
    retained: list[OSError] = []
    views: list[memoryview] = []

    class Record(bytes):
        def __del__(self) -> None:
            live.pop(id(self))

    def record(value: object) -> bytes:
        assert not live, "a completed persistence record survived into its replacement"
        result = Record(actual_record(value))
        live[id(result)] = len(result)
        return result

    def hold(point: str) -> None:
        nonlocal held
        if not held and point == stage:
            held = True
            entered.set()
            assert release.wait(5)
            if fail:
                error = OSError(errno.EIO, "synthetic persistence failure")
                retained.append(error)
                raise error

    def write(fd: int, data: bytes | memoryview) -> int:
        if fd == owner.fds.get("state_write"):
            assert isinstance(data, memoryview) and id(data.obj) in live
            views.append(data)
            hold("write")
        return original_write(fd, data)

    def sync(fd: int) -> None:
        if fd == owner.fds.get("state_write"):
            hold("fsync")
        elif fd == owner.fds.get("directory"):
            hold("parent_fsync")
        original_sync(fd)

    def close(fd: int) -> None:
        if fd == owner.fds.get("state_write"):
            hold("close")
        original_close(fd)

    def replace_file(
        src: str, dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
    ) -> None:
        if src == "state.tmp":
            hold("replace")
        original_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(intake, "_state_record", record)
    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(os, "replace", replace_file)
    try:
        owner.start(execute=True)
        assert entered.wait(5) and len(live) == 1
        owner.cancel()
        deadline = owner.cleanup_deadline
        case.clock.advance(4)
        owner.cancel()
        assert not owner.join() and owner.cleanup_deadline == deadline
        report = owner.report()
        assert report["cleanup_debt"] is True and report["worker_alive"] is True
        assert report["original_io_in_flight"] == "state_persistence" and len(live) == 1
        assert "directory" in owner.fds and "lock" in owner.fds
        if stage in ("write", "fsync", "close"):
            assert "state_write" in owner.fds and owner.temp_identity is not None
        elif stage == "replace":
            assert "state_write" not in owner.fds and owner.temp_identity is not None
        else:
            assert owner.temp_identity is None and owner.state_identity is not None
    finally:
        release.set()
    assert owner.join(5) and not live
    assert owner.report()["released"] is True and owner.report()["cleanup_deadline_met"] is False
    assert owner.cleanup_deadline == deadline
    assert len(retained) == int(fail)
    for error in retained:
        frame = error.__traceback__
        while frame is not None:
            if frame.tb_frame.f_code.co_name == "_save":
                assert frame.tb_frame.f_locals["data"] == b""
            frame = frame.tb_next
    for view in views:
        with pytest.raises(ValueError, match="released memoryview"):
            _ = view.obj


def test_failed_encoder_releases_arena_and_cleanup_uses_intake_error_boundary(
    case: Case,
    owners: list[intake.LocalIntake],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = case.owner()
    owners.append(owner)
    arenas = track_arenas(monkeypatch)
    original = records.JsonBuffer.walk
    failed = False
    retained: list[Exception] = []

    def walk(self: records.JsonBuffer, value: object, depth: int) -> None:
        nonlocal failed
        original(self, value, depth)
        if depth == 0 and self.backing is not None and self.hasher is None and not failed:
            failed = True
            error = records.RecordError("synthetic_build_failure")
            retained.append(error)
            raise error

    monkeypatch.setattr(records.JsonBuffer, "walk", walk)
    enabled = gc.isenabled()
    gc.disable()
    try:
        owner.start(execute=True)
        assert owner.join(5)
        assert failed and owner.failure == "invalid_json_record"
        assert retained[0].__traceback__ is not None and all(arena() is None for arena in arenas)
        assert owner.report()["released"] is True
    finally:
        if enabled:
            gc.enable()
