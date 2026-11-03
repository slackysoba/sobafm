"""Synthetic packet and persistent journal contracts; no Docker/source activity."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from evaluations.fma_validation.cases import BASE_SHA256, CaseBinding, OwnedQualificationRequest
from evaluations.fma_validation.controller import Journal, creation_args
from evaluations.fma_validation.limits import FILE_BYTES, FILE_SHA256, CaseClock, encode_report
from evaluations.fma_validation.metadata import PARSER_TAG_BYTES, BoundedFile, preflight
from evaluations.fma_validation.packet import SOURCE_FILES, Packet, load, load_qualification
from evaluations.linux_resource.probe import ProbeError
from sobafm.fma_intake import (
    CREDIT_SNAPSHOT,
    METADATA_DIGEST,
    SOURCE_URL,
    Acquisition,
    FileIdentity,
    IntakeRequest,
)

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 11, 2, 0, 1, tzinfo=UTC).timestamp()


@pytest.fixture
def packet_path(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic-packet.json"
    request = IntakeRequest(
        SOURCE_URL,
        METADATA_DIGEST,
        CREDIT_SNAPSHOT,
        Acquisition("2026-11-02T00:00:00Z", "conservative_lower_bound", "Synthetic evidence only."),
        "2026-11-02T00:10:00Z",
        ("Synthetic notice.",),
        FileIdentity(1, 2, FILE_BYTES, 0, 0, FILE_SHA256),
    )
    raw: dict[str, object] = {
        "schema": "sobafm-fma-case-1",
        "case_id": "sobafm-44-synthetic",
        "review": "https://github.com/slackysoba/sobafm/issues/44#synthetic-test-only",
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
        "source_hashes": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES
        },
        "image": "sha256:" + "3" * 64,
        "target": "linux/amd64",
        "init_sha256": "4" * 64,
        "python_sha256": "5" * 64,
        "python_path": "/usr/local/bin/python",
        "whole_end_utc": "2026-11-02T00:04:30Z",
        "request": request.manifest(),
        "volume": "sobafm-44-synthetic-volume",
        "volume_created": "synthetic",
        "volume_mountpoint_sha256": "6" * 64,
        "slot_device": 1,
        "slot_inode": 2,
        "source_path": str(tmp_path / "absent-synthetic-input"),
        "checkout_path": str(ROOT),
        "packet_path": str(path),
        "synthetic_preflight_sha256": "7" * 64,
    }
    path.write_bytes(encode_report(raw))
    return path


def test_closed_packet_does_not_open_original_and_binds_invocation(packet_path: Path) -> None:
    packet = load(packet_path, ROOT, now=NOW)
    assert not Path(packet.text("source_path")).exists()
    args = creation_args(packet)
    assert "--init" in args and "--network=none" in args and "--restart=no" in args
    assert "--rm" not in args and "--read-only" in args and "--log-driver=none" in args
    assert "--memory=90177536" in args and "--memory-swap=90177536" in args
    binds = [value for value in args if value.startswith("type=bind,")]
    assert len(binds) == 3 and all(value.endswith(",readonly") for value in binds)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("target", "linux/arm64"),
        ("image", "mutable:latest"),
        ("slot_inode", True),
        ("source_path", "/tmp/input,readonly=false"),
        ("whole_end_utc", "2026-11-02T00:04:31Z"),
        ("whole_end_utc", "2026-11-02T00:01:00Z"),
        ("unexpected", True),
    ],
)
def test_packet_rejects_changed_scope_before_resource_creation(
    packet_path: Path,
    key: str,
    value: object,
) -> None:
    raw = cast(dict[str, object], json.loads(packet_path.read_bytes()))
    raw[key] = value
    packet_path.write_bytes(encode_report(raw))
    with pytest.raises(ProbeError):
        load(packet_path, ROOT, now=NOW)


@pytest.mark.parametrize("name", [SOURCE_FILES[0], "evaluations/fma_validation/native_records.py"])
def test_runtime_source_change_and_missing_binding_fail(packet_path: Path, name: str) -> None:
    assert name in SOURCE_FILES
    raw = cast(dict[str, object], json.loads(packet_path.read_bytes()))
    hashes = cast(dict[str, object], raw["source_hashes"])
    hashes[name] = "0" * 64
    packet_path.write_bytes(encode_report(raw))
    with pytest.raises(ProbeError, match="reviewed_source_changed"):
        load(packet_path, ROOT, now=NOW)
    del hashes[name]
    packet_path.write_bytes(encode_report(raw))
    with pytest.raises(ProbeError, match="packet_fields"):
        load(packet_path, ROOT, now=NOW)


@pytest.mark.parametrize("qualification", [False, True])
@pytest.mark.parametrize("change", ["omitted", "digest", "module_bytes"])
@pytest.mark.parametrize(
    "name",
    [
        "evaluations/fma_validation/native_records.py",
        "evaluations/fma_validation/observation.py",
        "evaluations/fma_validation/diagnostics.py",
        "evaluations/fma_validation/decoder.py",
        "evaluations/fma_validation/limits.py",
        "evaluations/fma_validation/report_io.py",
        "evaluations/fma_validation/ingress.py",
        "src/sobafm/json_records.py",
    ],
)
def test_runtime_policy_binding_rejects_before_controller_resources(
    packet_path: Path,
    tmp_path: Path,
    qualification: bool,
    change: str,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from evaluations.fma_validation import controller
    from evaluations.fma_validation import packet as packet_module

    assert name in SOURCE_FILES
    if qualification:
        qualification_packet(packet_path)
    raw = cast(dict[str, object], json.loads(packet_path.read_bytes()))
    hashes = cast(dict[str, object], raw["source_hashes"])
    if change == "omitted":
        del hashes[name]
    elif change == "digest":
        hashes[name] = "0" * 64
    packet_path.write_bytes(encode_report(raw))
    actual_read = packet_module.read_bounded

    def read(path: Path, limit: int = 65_536) -> bytes:
        value = actual_read(path, limit)
        if change == "module_bytes" and path == ROOT / name:
            return value + b"\n# synthetic source mutation\n"
        return value

    selected_load = load_qualification if qualification else load

    def load_now(path: Path, root: Path, *, recovery: bool = False) -> Packet:
        return selected_load(path, root, now=NOW, recovery=recovery)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unbound policy reached journal/controller resource creation")

    journal = tmp_path / "never-created-journal"
    monkeypatch.setattr(packet_module, "read_bounded", read)
    monkeypatch.setattr(controller, "load_qualification" if qualification else "load", load_now)
    monkeypatch.setattr(controller, "Journal", forbidden)
    monkeypatch.setattr(controller, "Controller", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "controller",
            "--packet",
            str(packet_path),
            "--journal",
            str(journal),
            "--execute-reviewed-packet",
        ],
    )
    expected = "packet_fields" if change == "omitted" else "reviewed_source_changed"
    with pytest.raises(ProbeError, match=expected):
        controller.main(qualification=qualification)
    assert not journal.exists()
    assert not Path(cast(str, raw["source_path"])).exists()


def test_journal_is_durable_non_reusable_and_rejects_path_substitution(
    packet_path: Path,
    tmp_path: Path,
) -> None:
    import os

    packet: Packet = load(packet_path, ROOT, now=NOW)
    path = tmp_path / "ledger.json"
    journal = Journal(path, packet)
    try:
        assert json.loads(path.read_bytes())["cleanup_debt"] is True
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(FileExistsError):
            Journal(path, packet)
        path.rename(tmp_path / "retained-original-ledger")
        path.write_bytes(b"do not replace")
        with pytest.raises(ProbeError, match="journal_identity_debt"):
            journal.save()
        assert path.read_bytes() == b"do not replace"
    finally:
        os.close(journal.fd)


def test_whole_deadline_includes_final_management_not_just_native_work() -> None:
    wall, mono = [100.0], [10.0]
    clock = CaseClock(101, 200, lambda: wall[0], lambda: mono[0])
    clock.cancel()
    wall[0], mono[0] = 102, 12
    receipt = clock.receipt()
    assert receipt["cleanup_deadline_met"] is True
    assert receipt["case_deadline_met"] is False
    wall[0] = 99
    assert clock.receipt()["case_deadline_met"] is False


def test_parser_copy_reservation_rejected_before_mutagen(tmp_path: Path) -> None:
    import os

    size = PARSER_TAG_BYTES
    length = bytes((size >> 21 & 127, size >> 14 & 127, size >> 7 & 127, size & 127))
    path = tmp_path / "synthetic-large-tag"
    path.write_bytes(b"ID3\x03\x00\x00" + length + b"\x00" * size)
    fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(ProbeError, match="metadata_copy_reservation"):
            preflight(BoundedFile(fd, size + 10))
    finally:
        os.close(fd)


def qualification_packet(packet_path: Path, name: str = "metadata-rejection") -> Path:
    from evaluations.fma_validation.cases import CaseId

    raw = cast(dict[str, object], json.loads(packet_path.read_bytes()))
    case = CaseBinding(cast(CaseId, name))
    request = OwnedQualificationRequest(
        case,
        "2026-11-02T00:01:00Z",
        "2026-11-02T00:04:30Z",
        FileIdentity(1, 2, case.bytes, 0, 0, case.sha256),
    )
    raw.update(schema="sobafm-owned-qualification-case-1", request=request.manifest())
    packet_path.write_bytes(encode_report(raw))
    return packet_path


@pytest.mark.parametrize("name", ["metadata-rejection", "tagged-full", "first-write-cancel"])
def test_disjoint_qualification_request_and_entry_point(packet_path: Path, name: str) -> None:
    original = packet_path.read_bytes()
    qualification_packet(packet_path, name)
    packet = load_qualification(packet_path, ROOT, now=NOW)
    assert isinstance(packet.request, OwnedQualificationRequest)
    assert packet.case.name == name
    assert "evaluations.fma_validation.qualification_owner" in creation_args(packet)
    assert not Path(packet.text("source_path")).exists()
    with pytest.raises(ProbeError):
        load(packet_path, ROOT, now=NOW)
    packet_path.write_bytes(original)
    with pytest.raises(ProbeError):
        load_qualification(packet_path, ROOT, now=NOW)


@pytest.mark.parametrize("name", ["tagged-full", "first-write-cancel"])
@pytest.mark.parametrize("digest", [None, BASE_SHA256, FILE_SHA256])
def test_tagged_binding_rejects_unbound_or_substitute_input(
    packet_path: Path, name: str, digest: str | None
) -> None:
    qualification_packet(packet_path, name)
    raw = cast(dict[str, object], json.loads(packet_path.read_bytes()))
    request = cast(dict[str, object], raw["request"])
    cast(dict[str, object], request["case_binding"])["input_sha256"] = digest
    cast(dict[str, object], request["file"])["sha256"] = digest
    packet_path.write_bytes(encode_report(raw))
    with pytest.raises(ProbeError, match="validation_case_binding"):
        load_qualification(packet_path, ROOT, now=NOW)


@pytest.mark.parametrize("name", ["metadata-rejection", "tagged-full", "first-write-cancel"])
@pytest.mark.parametrize("mutation", ["purpose", "case", "hash", "field", "expiry"])
def test_qualification_cannot_substitute_identity_or_renew_expiry(
    packet_path: Path,
    name: str,
    mutation: str,
) -> None:
    qualification_packet(packet_path, name)
    raw = cast(dict[str, object], json.loads(packet_path.read_bytes()))
    request = cast(dict[str, object], raw["request"])
    binding = cast(dict[str, object], request["case_binding"])
    if mutation == "purpose":
        binding["purpose"] = "night-owl-validation"
    elif mutation == "case":
        binding["case"] = "custom"
    elif mutation == "hash":
        cast(dict[str, object], request["file"])["sha256"] = "0" * 64
    elif mutation == "field":
        request["decoder_command"] = "custom"
    else:
        request["expires_at"] = "2026-11-03T00:04:30Z"
    packet_path.write_bytes(encode_report(raw))
    with pytest.raises((ValueError, ProbeError)):
        load_qualification(packet_path, ROOT, now=NOW)


@pytest.mark.parametrize(
    "kind", ["nonzero", "partial", "cross_case", "absent", "bad_body", "bad_verdict"]
)
def test_outer_terminal_capture_survives_nonzero_or_late_drain(
    packet_path: Path,
    tmp_path: Path,
    kind: str,
) -> None:
    import sys
    import time
    from datetime import UTC, datetime

    from evaluations.fma_validation.consumer import ValidationConsumer
    from evaluations.fma_validation.controller import Command, Controller
    from evaluations.fma_validation.process import ProcessPin
    from sobafm.fma_intake import LocalIntake

    packet = load(packet_path, ROOT, now=NOW)
    journal = Journal(tmp_path / "outer-ledger", packet)
    controller = Controller(packet, journal)
    controller.clock = CaseClock(packet.whole_end, packet.expires, lambda: NOW + 5, lambda: 15.0)
    init = ProcessPin(1, 1, 0, "/init", 1, 1, 1000, "/")
    owner_pin = ProcessPin(2, 1, 1, "/python", 1, 2, 1000, "/")
    owner = LocalIntake(
        tmp_path,
        None,
        packet.request,
        utc_now=lambda: datetime.fromtimestamp(NOW, UTC),
        monotonic=lambda: 10.0,
    )
    consumer = ValidationConsumer(owner, init, owner_pin, controller.clock, packet.case)
    record: dict[str, object] = {
        "schema": packet.case.schema("owner"),
        "case_binding": packet.case.record(),
        "packet_sha256": packet.fingerprint,
        "passed": False,
        "failure": "validation_owner_failed",
        "init": init.record(),
        "owner": owner_pin.record(),
        "intake": {} if kind == "bad_body" else owner.report(),
        "reader": None,
        "validation": consumer.receipt(),
        "memory_event_delta": {},
        "cleanup": {
            "whole_end_utc": packet.whole_end,
            "expires_utc": packet.expires,
            "cleanup_start_utc": NOW,
            "cleanup_deadline_met": False,
            "cleanup_timing_unknown": False,
            "case_deadline_met": True,
        },
        "catalog_admitted": False,
        "container_exit_observed": False,
    }
    if kind == "cross_case":
        record["case_binding"] = CaseBinding("metadata-rejection").record()
    elif kind == "bad_verdict":
        record["passed"] = "unsupported"
    raw = b"" if kind == "absent" else encode_report(record) + (b"" if kind == "partial" else b"\n")
    command = Command(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import os; os.write(1," + repr(raw) + "); raise SystemExit(1)",
        ]
    )
    command.owner_output = True
    command.launch()
    try:
        command.worker.join(3)
        assert command.process is not None and command.process.wait(timeout=3) == 1
        end = time.monotonic() + 3
        while not command.settled() and time.monotonic() < end:
            command.pump()
            controller.capture_owner(command, final=command.settled())
        assert command.settled()
        if kind == "nonzero":
            assert controller.owner_report is not None
            assert controller.clock.cleanup_start_utc == NOW and controller.clock.missed
            assert not controller.clock.timing_unknown
        elif kind in ("bad_body", "bad_verdict"):
            assert controller.owner_report is None and controller.clock.timing_unknown
            assert controller.clock.cleanup_start_utc == NOW and controller.clock.missed
        else:
            assert controller.clock.timing_unknown and controller.clock.missed
            assert controller.owner_report is None
        controller.clock.cancel()
        assert controller.clock.missed and command.process.returncode == 1
    finally:
        command.stop()
        end = time.monotonic() + 3
        while not command.settled() and time.monotonic() < end:
            command.pump()
        assert command.settled()
        journal.close()
