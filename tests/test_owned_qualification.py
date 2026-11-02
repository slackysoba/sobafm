"""The frozen synthetic case traverses actual ownership and metadata Python IPC."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from evaluations.fma_validation import consumer as consumer_module
from evaluations.fma_validation.cases import (
    BASE_BYTES,
    BASE_SHA256,
    PCM_SHA256,
    REJECTION_SHA256,
    TAGGED_SHA256,
    CaseBinding,
    OwnedQualificationRequest,
    rejection_fixture,
    tagged_prefix,
    tagged_suffix,
)
from evaluations.fma_validation.consumer import ValidationConsumer, expected_outcome
from evaluations.fma_validation.limits import CaseClock, encode_report
from evaluations.fma_validation.metadata import BoundedFile, inspect, preflight
from evaluations.fma_validation.process import OwnedHelper, ProcessPin
from evaluations.linux_resource.probe import ProbeError
from sobafm.fma_intake import FileIdentity, LocalIntake

ROOT = Path(__file__).parents[1]


def request(path: Path) -> OwnedQualificationRequest:
    now = datetime.now(UTC)
    info = path.stat()
    return OwnedQualificationRequest(
        CaseBinding("metadata-rejection"),
        now.isoformat().replace("+00:00", "Z"),
        (now + timedelta(seconds=210)).isoformat().replace("+00:00", "Z"),
        FileIdentity(
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            REJECTION_SHA256,
        ),
    )


def test_recipe_manifest_binds_components_without_deriving_owned_media(tmp_path: Path) -> None:
    manifest = cast(
        dict[str, object],
        json.loads((ROOT / "evaluations/fma_validation/qualification-recipes.json").read_bytes()),
    )
    base = cast(dict[str, object], manifest["base"])
    assert base["bytes"] == BASE_BYTES and base["sha256"] == BASE_SHA256
    tagged = cast(dict[str, object], manifest["tagged-full"])
    assert tagged["expected_pcm_sha256"] == PCM_SHA256
    for name in ("tagged-full", "first-write-cancel"):
        recipe = cast(dict[str, object], manifest[name])
        binding = CaseBinding.from_record({**CaseBinding("tagged-full").record(), "case": name})
        assert recipe["sha256"] == binding.sha256 == TAGGED_SHA256
        assert recipe["bytes"] == binding.bytes == 524_288 + BASE_BYTES + 128
    for name, data in (("prefix", tagged_prefix()), ("suffix", tagged_suffix())):
        assert len(data) == tagged[name + "_bytes"]
        assert hashlib.sha256(data).hexdigest() == tagged[name + "_sha256"]
    assert len(rejection_fixture()) == 4113
    assert hashlib.sha256(rejection_fixture()).hexdigest() == REJECTION_SHA256
    # Framing/notice recipe tests use only generated non-audio bytes.
    path = tmp_path / "synthetic-tags-only"
    path.write_bytes(tagged_prefix() + tagged_suffix())
    fd = os.open(path, os.O_RDONLY)
    try:
        result = inspect(fd, 524_416)
        assert result["credits_complete"] is False and result["unknown_frames"]
    finally:
        os.close(fd)


def test_rejection_recipe_fails_before_library_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evaluations.fma_validation import metadata

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("pre-parser rejection must not enter Mutagen")

    monkeypatch.setattr(metadata.importlib, "import_module", forbidden)
    path = tmp_path / "synthetic-rejection"
    path.write_bytes(rejection_fixture())
    fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(ProbeError, match="unsupported_id3_text_encoding"):
            preflight(BoundedFile(fd, 4113))
    finally:
        os.close(fd)


def test_synthetic_case_uses_actual_local_intake_and_metadata_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def capture(pid: int) -> ProcessPin:
        return ProcessPin(pid, 1, os.getpid(), sys.executable, 1, 1, os.geteuid(), "/")

    def verify(_pin: ProcessPin) -> None:
        return

    def inventory(*_pins: ProcessPin) -> set[int]:
        return set()

    monkeypatch.setattr(ProcessPin, "capture", capture)
    monkeypatch.setattr(ProcessPin, "verify", verify)
    monkeypatch.setattr(consumer_module, "inventory", inventory)
    source = tmp_path / "synthetic-rejection"
    source.write_bytes(rejection_fixture())
    slot = tmp_path / "slot"
    slot.mkdir(mode=0o700)
    req = request(source)
    owner = LocalIntake(slot, source, req)
    clock = CaseClock(time.time() + 30, datetime.fromisoformat(req.expires_at).timestamp())
    pin = capture(os.getpid())
    consumer = ValidationConsumer(owner, pin, pin, clock, req.case)
    try:
        owner.start(execute=True)
        assert owner.pending.wait(3), owner.report()
        ledger = json.loads((slot / "state.json").read_bytes())
        assert ledger["schema"] == "sobafm-owned-qualification-owned-slot-1"
        assert ledger["case_binding"] == req.case.record()
        assert "acquisition" not in ledger and "source_url" not in ledger
        reader = owner.read_into(consumer)
        end = time.monotonic() + 5
        while not reader.join(0.01) and time.monotonic() < end:
            pass
        assert reader.join(0) and not reader.report()["completed"]
        owner.cancel()
        assert owner.join(3), owner.report()
        result = consumer.receipt()
        assert expected_outcome(result, req.case) and not consumer.finished, encode_report(
            result
        ).decode()
        assert consumer.decode is None and len(consumer.history) == 1
        assert consumer.history[0]["role"] == "metadata" and consumer.history[0]["exit_code"] == 1
        assert result["case_binding"] == req.case.record()
        assert owner.report()["released"] is True and owner.report()["cleanup_deadline_met"] is True
        assert not owner.report()["cleanup_debt"] and source.read_bytes() == rejection_fixture()
    finally:
        owner.cancel()
        assert owner.join(3), owner.report()


def test_binding_cannot_mutate_or_cross_purpose(tmp_path: Path) -> None:
    path = tmp_path / "synthetic"
    path.write_bytes(rejection_fixture())
    req = request(path)
    with pytest.raises(FrozenInstanceError):
        req.case.name = "night-owl"  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ProbeError):
        CaseBinding.from_record({**req.case.record(), "input_sha256": "0" * 64})
    with pytest.raises(ValueError):
        replace(req, file=replace(req.file, sha256="0" * 64)).validate(datetime.now(UTC))
    owner = LocalIntake(tmp_path, path, req)
    pin = ProcessPin(1, 1, 1, "/python", 1, 1, 1000, "/")
    with pytest.raises(ProbeError, match="consumer_request_case_binding"):
        ValidationConsumer(owner, pin, pin, CaseClock(time.time() + 30, time.time() + 60))


def test_first_write_checkpoint_is_fixed_and_cannot_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercise the post-admission callback only, without starting intake or native
    # work. These generated bytes are not the retained tagged MPEG derivative.
    path = tmp_path / "synthetic"
    path.write_bytes(rejection_fixture())
    req = request(path)
    pin = ProcessPin(1, 1, 1, "/python", 1, 1, 1000, "/")
    clock = CaseClock(time.time() + 30, time.time() + 60)
    case = CaseBinding("first-write-cancel")
    pending = replace(req, case=case, file=replace(req.file, bytes=case.bytes, sha256=case.sha256))
    consumer = ValidationConsumer(LocalIntake(tmp_path, path, pending), pin, pin, clock, case)
    assert consumer.active is None
    helper = OwnedHelper(clock, "decoder")
    delivered: list[bytes] = []

    def write(chunk: bytes) -> None:
        delivered.append(chunk)

    monkeypatch.setattr(helper, "write", write)
    consumer.active, consumer.phase = helper, "streaming"
    with pytest.raises(ProbeError, match="qualification_first_write_cancel"):
        consumer.write(b"synthetic checkpoint")
    original = clock.cleanup_start_utc
    with pytest.raises(ProbeError, match="consumer_stream_revoked"):
        consumer.write(b"must not be delivered")
    assert delivered == [b"synthetic checkpoint"] and consumer.first_write_cancelled
    assert clock.cleanup_start_utc == original and not consumer.finished
    assert encode_report(consumer.receipt())
