"""Synthetic packet and persistent journal contracts; no Docker/source activity."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from evaluations.fma_validation.controller import Journal, creation_args
from evaluations.fma_validation.limits import FILE_BYTES, FILE_SHA256, CaseClock, encode_report
from evaluations.fma_validation.metadata import PARSER_TAG_BYTES, BoundedFile, preflight
from evaluations.fma_validation.packet import SOURCE_FILES, Packet, load
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


def test_runtime_source_change_and_missing_binding_fail(packet_path: Path) -> None:
    raw = cast(dict[str, object], json.loads(packet_path.read_bytes()))
    hashes = cast(dict[str, object], raw["source_hashes"])
    hashes[SOURCE_FILES[0]] = "0" * 64
    packet_path.write_bytes(encode_report(raw))
    with pytest.raises(ProbeError, match="reviewed_source_changed"):
        load(packet_path, ROOT, now=NOW)
    del hashes[SOURCE_FILES[0]]
    packet_path.write_bytes(encode_report(raw))
    with pytest.raises(ProbeError, match="packet_fields"):
        load(packet_path, ROOT, now=NOW)


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
