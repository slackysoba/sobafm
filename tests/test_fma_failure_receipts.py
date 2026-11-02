"""Deterministic Python children only: no native decoder, media, Docker or sleeps."""

from __future__ import annotations

import fcntl
import hashlib
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from evaluations.fma_validation import consumer as consumer_module
from evaluations.fma_validation.cases import NIGHT_OWL, CaseBinding
from evaluations.fma_validation.consumer import ValidationConsumer
from evaluations.fma_validation.limits import FILE_BYTES, FILE_SHA256, CaseClock, encode_report
from evaluations.fma_validation.process import OwnedHelper, ProcessPin
from evaluations.linux_resource.probe import ProbeError
from sobafm.fma_intake import Acquisition, FileIdentity, IntakeRequest, LocalIntake


def terminal(pin: ProcessPin) -> dict[str, object]:
    return {
        "event": "full_stream_result",
        "schema": "sobafm-fma-decode-1",
        "case_binding": NIGHT_OWL.record(),
        "runner": pin.record(),
        "passed": False,
        "failure": "decoder_case_failed",
        "input_bytes": 17,
        "input_sha256": hashlib.sha256(b"synthetic").hexdigest(),
        "input_complete": False,
        "stdout_eof": True,
        "stderr_eof": True,
        "exit_code": 1,
        "pcm_bytes": 0,
        "pcm_sha256": hashlib.sha256(b"").hexdigest(),
        "measured_duration_seconds": 0,
        "web_duration_seconds": 194,
        "ready_seconds": None,
        "cleanup_debt": False,
        "cleanup": {
            "whole_end_utc": 200,
            "expires_utc": 300,
            "cleanup_start_utc": 100,
            "cleanup_deadline_met": False,
            "cleanup_timing_unknown": False,
            "case_deadline_met": True,
        },
        "memory_event_delta": {"oom": 0, "oom_kill": 0},
        "swap_current": "0",
        "diagnostics": {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()},
        "catalog_admitted": False,
        "listening": False,
    }


@pytest.fixture
def consumer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ValidationConsumer:
    def capture(pid: int) -> ProcessPin:
        return ProcessPin(pid, 1, os.getpid(), sys.executable, 1, 1, 1000, "/")

    def verify(_pin: ProcessPin) -> None:
        return

    def inventory(*_pins: ProcessPin) -> set[int]:
        return set()

    monkeypatch.setattr(ProcessPin, "capture", capture)
    monkeypatch.setattr(ProcessPin, "verify", verify)
    monkeypatch.setattr(consumer_module, "inventory", inventory)
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
        request,
        utc_now=lambda: datetime.fromtimestamp(105, UTC),
        monotonic=lambda: 10.0,
    )
    clock = CaseClock(200, 300, lambda: 105.0, lambda: 10.0)
    pin = capture(os.getpid())
    return ValidationConsumer(owner, pin, pin, clock)


def launch(consumer: ValidationConsumer, schedule: str = "finish") -> OwnedHelper:
    helper = OwnedHelper(consumer.clock, "decoder")
    consumer.active, consumer.phase = helper, "streaming"
    row = terminal(ProcessPin(1, 1, os.getpid(), sys.executable, 1, 1, 1000, "/"))
    raw = encode_report(row).decode()
    # Child substitutes only its actual PID into the fixture's closed role pin.
    program = (
        "import os,json; r=json.loads(" + repr(raw) + "); r['runner']['pid']=os.getpid(); "
        "os.write(1,"
        + ("b'\\n'*4096+" if schedule == "late" else "")
        + "json.dumps(r).encode()+b'\\n'); raise SystemExit(1)"
    )
    helper.launch([sys.executable, "-I", "-B", "-c", program])
    helper.admit()
    assert helper.process is not None
    assert helper.process.wait(timeout=3) == 1
    return helper


def retire(consumer: ValidationConsumer) -> None:
    consumer.cancel()
    end = time.monotonic() + 3
    while not consumer.join(0.01) and time.monotonic() < end:
        pass
    assert consumer.join(0)


@pytest.mark.parametrize("schedule", ["finish", "early", "late", "helper_failure"])
def test_failed_helper_retains_original_clock_before_retirement(
    consumer: ValidationConsumer,
    schedule: str,
) -> None:
    helper = launch(consumer, schedule)
    try:
        if schedule == "helper_failure":
            helper.failure = "synthetic_helper_failure"
        if schedule in ("finish", "helper_failure"):
            consumer.delivered = FILE_BYTES
            with pytest.raises(ProbeError):
                consumer.finish()
        else:
            with pytest.raises(ProbeError):
                consumer.write(b"owned synthetic")
        retire(consumer)
        assert consumer.decode is not None and consumer.decode["passed"] is False
        assert consumer.clock.cleanup_start_utc == 100 and consumer.clock.missed
        assert consumer.owner.cleanup_started_utc == "1970-01-01T00:01:40Z"
        assert consumer.owner.cleanup_missed
        assert not consumer.finished and consumer.active is None
        assert consumer.history[-1]["exit_code"] == 1 and helper.stdout == b""
        original = consumer.clock.cleanup_monotonic
        consumer.cancel()
        consumer.cancel()
        assert consumer.clock.cleanup_monotonic == original
    finally:
        retire(consumer)


def test_late_receipt_does_not_claim_held_descriptor_released(consumer: ValidationConsumer) -> None:
    helper = launch(consumer)
    original_fd = os.open("/dev/null", os.O_RDONLY)
    fd = fcntl.fcntl(original_fd, fcntl.F_DUPFD_CLOEXEC, 256)
    os.close(original_fd)
    helper.fds.add(fd)
    try:
        assert not consumer.join(0.01)
        assert consumer.clock.cleanup_start_utc == 100 and consumer.clock.missed
        assert consumer.active is helper and fd in helper.fds and not consumer.history
        assert not consumer.finished
    finally:
        helper.close(fd)
        retire(consumer)


@pytest.mark.parametrize("kind", ["absent", "partial", "malformed", "case", "runner", "clock"])
def test_invalid_terminal_is_sticky_unknown_even_after_settlement(
    consumer: ValidationConsumer,
    kind: str,
) -> None:
    helper = OwnedHelper(consumer.clock, "decoder")
    helper.pin = ProcessPin(123, 1, 2, "/python", 1, 1, 1000, "/")
    row = terminal(helper.pin)
    if kind == "case":
        row["case_binding"] = CaseBinding("metadata-rejection").record()
    elif kind == "runner":
        row["runner"] = consumer.owner_pin.record()
    elif kind == "clock":
        cast(dict[str, object], row["cleanup"])["expires_utc"] = 301
    raw = encode_report(row) + (b"" if kind == "partial" else b"\n")
    helper.stdout.extend(b"" if kind == "absent" else b"{}\n" if kind == "malformed" else raw)
    consumer.active = helper
    assert consumer.join(0)
    assert consumer.terminal_error is not None and consumer.clock.timing_unknown
    assert consumer.clock.missed and consumer.decode is None
    consumer.cancel()
    assert consumer.owner.cleanup_deadline_unknown and consumer.owner.cleanup_missed
    assert consumer.clock.cleanup_start_utc != 100


def test_unknown_and_missed_remain_sticky_when_later_valid_receipt_arrives(
    consumer: ValidationConsumer,
) -> None:
    consumer.clock.timing_unknown = True
    helper = OwnedHelper(consumer.clock, "decoder")
    helper.pin = consumer.owner_pin
    helper.stdout.extend(encode_report(terminal(helper.pin)) + b"\n")
    consumer.active = helper
    assert consumer.join(0)
    consumer.cancel()
    assert consumer.clock.cleanup_start_utc == 100 and consumer.clock.timing_unknown
    assert consumer.owner.cleanup_started_utc == "1970-01-01T00:01:40Z"
    assert consumer.owner.cleanup_deadline_unknown and consumer.owner.cleanup_missed


def test_valid_terminal_before_partial_trailer_preserves_earlier_clock(
    consumer: ValidationConsumer,
) -> None:
    helper = OwnedHelper(consumer.clock, "decoder")
    helper.pin = consumer.owner_pin
    helper.stdout.extend(encode_report(terminal(helper.pin)) + b"\n{partial")
    consumer.active = helper
    assert consumer.join(0)
    assert consumer.decode is not None and consumer.terminal_error is not None
    assert consumer.clock.cleanup_start_utc == 100 and consumer.clock.timing_unknown
    assert consumer.owner.cleanup_started_utc == "1970-01-01T00:01:40Z"
    assert consumer.owner.cleanup_missed
