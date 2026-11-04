"""#62: a parser-scope metadata rejection is retained and the decoder still runs.

Deterministic Python children only: no native decoder, media, Docker or sleeps.
The decoder start is a recorded stub; only metadata-helper settlement is real.
"""

from __future__ import annotations

import fcntl
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from evaluations.fma_validation import consumer as consumer_module
from evaluations.fma_validation.cases import NIGHT_OWL, REJECTION_SHA256, CaseBinding
from evaluations.fma_validation.consumer import (
    ValidationConsumer,
    carried_rejection,
    expected_outcome,
)
from evaluations.fma_validation.ingress import VALIDATION, Scan
from evaluations.fma_validation.limits import FILE_BYTES, FILE_SHA256, CaseClock, encode_report
from evaluations.fma_validation.metadata import (
    PARSER_SCOPE_REASONS,
    REJECTION_REASONS,
    REJECTION_STDERR,
    BoundedFile,
    MetadataRejected,
    preflight,
)
from evaluations.fma_validation.observation import ObservationWorkspace
from evaluations.fma_validation.process import OwnedHelper, ProcessPin
from evaluations.linux_resource.probe import ProbeError
from sobafm.fma_intake import Acquisition, FileIdentity, IntakeRequest, LocalIntake

CARRIED = sorted(PARSER_SCOPE_REASONS)
TERMINAL = sorted((REJECTION_REASONS - PARSER_SCOPE_REASONS) | {"metadata_inspection_failed"})
REJECTION_CASE = CaseBinding("metadata-rejection")


class DecoderStarted(ProbeError):
    """Stops the synthetic run at the decoder boundary; no decoder is launched."""


class Harness:
    def __init__(self, consumer: ValidationConsumer) -> None:
        self.consumer = consumer
        self.helper: OwnedHelper | None = None
        self.decoder_snapshots: list[dict[str, object]] = []
        self.reason = "unsupported_id3_version"
        self.stderr = REJECTION_STDERR
        self.exit_code = 1
        self.hold = False
        self.held: int | None = None

    def program(self) -> str:
        case = self.consumer.case
        row = {
            "schema": case.schema("metadata-rejection"),
            "reason": self.reason,
            "credits_complete": False,
            "case_binding": case.record(),
            "helper_pid": 0,
        }
        raw = encode_report(row).decode()
        return (
            "import os,sys,json; "
            "os.read(int(sys.argv[sys.argv.index('--admit-fd')+1]),1); "
            f"r=json.loads({raw!r}); r['helper_pid']=os.getpid(); "
            "os.write(1,json.dumps(r,separators=(',',':')).encode()+b'\\n'); "
            f"os.write(2,{self.stderr!r}); raise SystemExit({self.exit_code})"
        )

    def start(self, role: str, _args: list[str], _inherited: tuple[int, ...] = ()) -> OwnedHelper:
        consumer = self.consumer
        if role == "decoder":
            # The real _start refuses while a helper remains active.
            self.decoder_snapshots.append(
                {
                    "active": consumer.active,
                    "history": [dict(item) for item in consumer.history],
                    "cleanup_start_utc": consumer.clock.cleanup_start_utc,
                    "phase": consumer.phase,
                }
            )
            raise DecoderStarted("synthetic_decoder_boundary")
        assert role == "metadata" and consumer.active is None
        helper = OwnedHelper(consumer.clock, "metadata", consumer.observation)
        consumer.active = self.helper = helper
        helper.launch([sys.executable, "-I", "-B", "-c", self.program()])
        if self.hold:
            original = os.open("/dev/null", os.O_RDONLY)
            self.held = fcntl.fcntl(original, fcntl.F_DUPFD_CLOEXEC, 256)
            os.close(original)
            helper.fds.add(self.held)
        helper.admit()
        return helper


def build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: CaseBinding = NIGHT_OWL,
    monotonic: list[float] | None = None,
) -> Harness:
    def capture(pid: int, _workspace: ObservationWorkspace) -> ProcessPin:
        return ProcessPin(pid, 1, os.getpid(), "/synthetic/python", 1, 1, 1000, "/")

    def verify(_pin: ProcessPin, _workspace: ObservationWorkspace) -> None:
        return

    def inventory(
        *_pins: ProcessPin, workspace: ObservationWorkspace, cleanup: bool = False
    ) -> set[int]:
        return set()

    monkeypatch.setattr(ProcessPin, "capture", capture)
    monkeypatch.setattr(ProcessPin, "verify", verify)
    monkeypatch.setattr(consumer_module, "inventory", inventory)
    if case.synthetic:
        from evaluations.fma_validation.cases import OwnedQualificationRequest

        request: object = OwnedQualificationRequest(
            case,
            "1970-01-01T00:00:00Z",
            "1970-01-01T00:03:00Z",
            FileIdentity(1, 2, case.bytes, 0, 0, REJECTION_SHA256),
        )
    else:
        request = IntakeRequest(
            "unused",
            "unused",
            "unused",
            Acquisition("1970-01-01T00:00:00Z", "exact", "synthetic"),
            "1970-01-01T00:05:00Z",
            (),
            FileIdentity(1, 2, FILE_BYTES, 0, 0, FILE_SHA256),
        )
    owner = LocalIntake(
        tmp_path,
        None,
        request,  # pyright: ignore[reportArgumentType]
        utc_now=lambda: datetime.fromtimestamp(105, UTC),
        monotonic=lambda: 10.0,
    )
    if monotonic is None:
        clock = CaseClock(200, 300, lambda: 105.0, lambda: 10.0)
    else:
        # Advance only until just past the original case end, then hold still,
        # so later observations cannot tighten (or renew) the adopted clock.
        ticks = [*monotonic, float("inf")]

        def advancing() -> float:
            if ticks[0] < ticks[1]:
                ticks[0] += 1.0
            return ticks[0]

        clock = CaseClock(200, 300, lambda: 105.0, advancing)
        ticks[1] = clock.end_monotonic + 1
    pin = capture(os.getpid(), ObservationWorkspace(clock))
    consumer = ValidationConsumer(owner, pin, pin, clock, case)
    harness = Harness(consumer)
    monkeypatch.setattr(consumer, "_start", harness.start)
    return harness


def release(harness: Harness) -> None:
    consumer = harness.consumer
    helper = harness.helper
    if harness.held is not None and helper is not None:
        helper.close(harness.held)
        harness.held = None
    consumer.cancel()
    for _ in range(500):
        if consumer.join(0.01):
            break
    assert consumer.join(0)


@pytest.mark.parametrize("reason", CARRIED)
def test_carried_reason_starts_decoder_only_after_actual_helper_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    harness = build(tmp_path, monkeypatch)
    harness.reason = reason
    consumer = harness.consumer
    try:
        with pytest.raises(DecoderStarted):
            consumer.inspect(3)
        assert len(harness.decoder_snapshots) == 1
        snapshot = harness.decoder_snapshots[0]
        # Actual exit and every close were observed, then the slot was retired.
        assert snapshot["active"] is None and snapshot["phase"] == "decoder_preparation"
        assert snapshot["cleanup_start_utc"] is None
        history = cast(list[dict[str, object]], snapshot["history"])
        assert len(history) == 1
        receipt = history[0]
        assert receipt["role"] == "metadata" and receipt["exit_code"] == 1
        assert receipt["stdout_eof"] is True and receipt["stderr_eof"] is True
        assert receipt["retained_fds"] == receipt["in_flight_fds"] == []
        assert receipt["uncertain_closes"] == [] and receipt["failure"] is None
        assert harness.helper is not None and harness.helper.process is not None
        assert harness.helper.process.returncode == 1
        # The rejection is retained as the metadata verdict; never complete credits.
        assert consumer.metadata == {
            "schema": NIGHT_OWL.schema("metadata-rejection"),
            "reason": reason,
            "credits_complete": False,
        }
        report = consumer.receipt()
        assert report["metadata_verdict"] == "rejected"
        assert report["decode_verdict"] == "incomplete"
        assert carried_rejection(consumer.metadata, NIGHT_OWL)
    finally:
        release(harness)


@pytest.mark.parametrize("reason", TERMINAL)
def test_terminal_reason_starts_no_decoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    harness = build(tmp_path, monkeypatch)
    harness.reason = reason
    consumer = harness.consumer
    try:
        with pytest.raises(ProbeError, match="metadata_helper_rejected"):
            consumer.inspect(3)
        assert not harness.decoder_snapshots and not consumer.decoder_started
        assert consumer.metadata is not None and consumer.metadata["reason"] == reason
        report = consumer.receipt()
        assert report["metadata_verdict"] == "rejected"
        assert report["decode_verdict"] == "not_run"
    finally:
        release(harness)
    assert not harness.decoder_snapshots


@pytest.mark.parametrize("schedule", ["stderr", "exit_zero", "exit_two"])
def test_carried_reason_with_unexpected_helper_settlement_starts_no_decoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schedule: str
) -> None:
    harness = build(tmp_path, monkeypatch)
    if schedule == "stderr":
        harness.stderr = REJECTION_STDERR + b"x"
    else:
        harness.exit_code = 0 if schedule == "exit_zero" else 2
    try:
        with pytest.raises(ProbeError, match="metadata_helper_failed"):
            harness.consumer.inspect(3)
        assert not harness.decoder_snapshots
    finally:
        release(harness)


def test_held_helper_blocks_decoder_and_keeps_original_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build(tmp_path, monkeypatch, monotonic=[10.0])
    harness.hold = True
    consumer = harness.consumer
    try:
        with pytest.raises(ProbeError, match="metadata_deadline"):
            consumer.inspect(3)
        helper = harness.helper
        assert helper is not None and helper.process is not None
        assert helper.process.wait(timeout=3) == 1
        assert not harness.decoder_snapshots and not consumer.decoder_started
        assert consumer.active is helper and not consumer.history
        original = consumer.clock.cleanup_start_utc
        assert original is not None and consumer.clock.case_missed
        # Still held: no retirement, no renewal of the original obligation.
        assert not consumer.join(0.01)
        consumer.cancel()
        assert consumer.clock.cleanup_start_utc == original
    finally:
        release(harness)
    assert consumer.clock.cleanup_start_utc == original and consumer.clock.case_missed
    assert len(consumer.history) == 1 and not harness.decoder_snapshots
    assert consumer.receipt()["decode_verdict"] == "not_run"


@pytest.mark.parametrize("reason", ["unsupported_id3_text_encoding", "unsupported_id3_version"])
def test_metadata_rejection_case_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    harness = build(tmp_path, monkeypatch, REJECTION_CASE)
    harness.reason = reason
    consumer = harness.consumer
    try:
        with pytest.raises(ProbeError, match="metadata_helper_rejected"):
            consumer.inspect(3)
        assert not harness.decoder_snapshots and consumer.phase == "metadata"
        assert not carried_rejection(consumer.metadata, REJECTION_CASE)
    finally:
        release(harness)
    validation = consumer.receipt()
    assert validation["metadata_verdict"] == "rejected"
    assert validation["decode_verdict"] == "not_run"
    assert expected_outcome(validation, REJECTION_CASE) is (
        reason == "unsupported_id3_text_encoding"
    )


def test_report_verdicts_are_distinct_and_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build(tmp_path, monkeypatch)
    consumer = harness.consumer
    try:
        with pytest.raises(DecoderStarted):
            consumer.inspect(3)
    finally:
        release(harness)
    validation = consumer.receipt()
    # The closed owner-report wire admits both verdicts (the full owner record is
    # covered with actual intake reports in test_fma_ingress_controller).
    with memoryview(encode_report(validation)) as view:
        scan = Scan(view, VALIDATION)
        scan.run()
        assert scan.valid
    # Decode completion alone decides the decode verdict; metadata stays rejected.
    finished = {**validation, "finished": True, "decode_verdict": "passed"}
    assert expected_outcome(finished, NIGHT_OWL)
    assert not expected_outcome({**finished, "metadata_verdict": "inspected"}, NIGHT_OWL)
    assert not expected_outcome({**finished, "decode_verdict": "incomplete"}, NIGHT_OWL)
    terminal = {**cast(dict[str, object], finished["metadata"]), "reason": "metadata_read_limit"}
    assert not carried_rejection(terminal, NIGHT_OWL)
    assert not expected_outcome({**finished, "metadata": terminal}, NIGHT_OWL)
    assert not carried_rejection({**terminal, "credits_complete": True}, NIGHT_OWL)


@pytest.mark.parametrize(
    ("declared", "reason"),
    [(600_000, "metadata_copy_reservation"), (1_048_577, "metadata_tag_limit")],
)
def test_tag_size_limits_are_header_only_rejections(
    tmp_path: Path, declared: int, reason: str
) -> None:
    path = tmp_path / "synthetic-header"
    size = (declared >> 21 & 127, declared >> 14 & 127, declared >> 7 & 127, declared & 127)
    path.write_bytes((b"ID3\x03\0\0" + bytes(size)).ljust(1_100_000, b"\0"))
    fd = os.open(path, os.O_RDONLY)
    try:
        stream = BoundedFile(fd, 1_100_000)
        with pytest.raises(MetadataRejected, match=reason):
            preflight(stream)
        # Only the ten header bytes were read; the parser reservation is untouched.
        assert stream.read_bytes == 10
    finally:
        os.close(fd)
