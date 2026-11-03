"""Decoder report capacity/ownership only; no native decoder or media execution."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Iterator
from typing import cast

import pytest

from evaluations.fma_validation import decoder, limits
from evaluations.fma_validation.limits import REPORT_BYTES, CaseClock
from evaluations.fma_validation.observation import ObservationWorkspace
from evaluations.fma_validation.process import ProcessPin
from evaluations.linux_resource.owned_mp3_overlap import make_pipes
from evaluations.linux_resource.probe import ProbeError
from tests.test_fma_diagnostic_capture import runner


@pytest.mark.parametrize(
    "value",
    [
        {"value": "a" * REPORT_BYTES},
        {"value": "\x00" * (REPORT_BYTES // 6)},
        {"value": "\U0001f600" * (REPORT_BYTES // 12)},
        {"value": float("nan")},
        {"value": 1 << 129},
        {"value": b"unsupported"},
        {1: "invalid key"},
    ],
)
def test_decoder_rejects_before_allocating_report_backing(
    value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> str:
        pytest.fail("rejected report reached work backing allocation")

    monkeypatch.setattr(limits, "bytearray", forbidden, raising=False)
    with pytest.raises(ProbeError):
        decoder.report_line(value)


@pytest.mark.parametrize("escaped", [False, True])
def test_report_limit_includes_the_newline(escaped: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    # {"value":""} costs 12 ASCII bytes before its value and final newline.
    room = REPORT_BYTES - 13
    value = "\x00" * (room // 6) + "a" * (room % 6) if escaped else "a" * room
    data = decoder.report_line({"value": value})
    assert len(data) == REPORT_BYTES and data.endswith(b"\n")
    assert data.count(b"\n") == 1 and json.loads(data) == {"value": value}

    def forbidden(*_args: object, **_kwargs: object) -> str:
        pytest.fail("newline overflow reached work backing allocation")

    monkeypatch.setattr(limits, "bytearray", forbidden, raising=False)
    with pytest.raises(ProbeError, match="report_capacity_exceeded"):
        decoder.report_line({"value": value + "a"})


def test_partial_report_write_retains_payload_and_original_cancel_clock(
    stopped_run: decoder.StreamRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = stopped_run
    read_fd, write_fd = os.pipe()
    actual_write = os.write
    entered, release = threading.Event(), threading.Event()
    payloads: list[bytes] = []
    held: list[memoryview] = []
    errors: list[Exception] = []
    calls = 0

    def partial(fd: int, data: memoryview) -> int:
        nonlocal calls
        assert fd == 1
        if not payloads:
            assert isinstance(data.obj, bytes)
            payloads.append(data.obj)
        assert data.obj is payloads[0]
        calls += 1
        if calls == 1:
            raise InterruptedError
        if calls == 2:
            with data[:5] as prefix:
                return actual_write(write_fd, prefix)
        held.append(data)
        entered.set()
        assert release.wait(3)
        return actual_write(write_fd, data)

    def send() -> None:
        try:
            run.admit_stream()
        except Exception as error:
            errors.append(error)

    def resume(_pid: int, _sig: int) -> None:
        pass

    def keep_blocking(_fd: int, _blocking: bool) -> None:
        pass

    monkeypatch.setattr(decoder.os, "write", partial)
    monkeypatch.setattr(decoder.os, "kill", resume)
    monkeypatch.setattr(decoder.os, "set_blocking", keep_blocking)
    worker = threading.Thread(target=send)
    worker.start()
    try:
        assert entered.wait(2)
        payload = payloads[0]
        prefix = os.read(read_fd, 5)
        assert prefix == payload[:5] and worker.is_alive()
        assert run.input_released and run.buffers
        run.request("synthetic_cancel")
        original = run.clock.cleanup_monotonic
        run.request("repeat")
        assert run.clock.cleanup_monotonic == original
        assert held[0].obj is payload and bytes(held[0]) == payload[5:]
        release.set()
        worker.join(3)
        assert not worker.is_alive() and not errors
        assert prefix + os.read(read_fd, len(payload)) == payload
        assert calls == 3 and run.clock.cleanup_monotonic == original
        assert json.loads(payload)["event"] == "decoder_admitted"
        with pytest.raises(ValueError, match="released memoryview"):
            _ = held[0].obj
    finally:
        release.set()
        worker.join(3)
        os.close(read_fd)
        os.close(write_fd)


def test_report_zero_write_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def stalled(_fd: int, _data: memoryview) -> int:
        return 0

    monkeypatch.setattr(decoder.os, "write", stalled)
    with pytest.raises(ProbeError, match="decoder_report_write"):
        decoder.write_report(decoder.report_line({"event": "synthetic"}))


@pytest.mark.parametrize("stage", ["entry", "return", "error"])
def test_admission_report_expiry_keeps_original_phase_before_bank_or_close(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    run, env = runner(monkeypatch)
    run.phase_deadline = 115
    actual_report = decoder.report_line
    calls: list[str] = []

    def report(value: object) -> bytes:
        calls.append("report")
        env.now[0] = 119
        if stage == "error":
            raise RuntimeError("synthetic late report build")
        return actual_report(value)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("expired report reached allocation, resume or write")

    if stage == "entry":
        env.now[0] = 119
    monkeypatch.setattr(decoder, "report_line", report)
    monkeypatch.setattr(decoder, "bytearray", forbidden, raising=False)
    monkeypatch.setattr(decoder.os, "kill", forbidden)
    monkeypatch.setattr(decoder.os, "set_blocking", forbidden)
    monkeypatch.setattr(decoder, "write_report", forbidden)
    with pytest.raises((ProbeError, RuntimeError)):
        try:
            run.admit_stream()
        except Exception as error:
            run.fail(error)
            raise
    assert calls == ([] if stage == "entry" else ["report"])
    assert run.clock.cleanup_start_utc == 115 and run.cancel.deadline == 118
    assert run.clock.missed and run.failure and not run.buffers and not run.input_released


@pytest.fixture
def stopped_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[decoder.StreamRun]:
    def capture(_cls: type[ProcessPin], pid: int, _workspace: ObservationWorkspace) -> ProcessPin:
        return ProcessPin(pid, 1, 2, "/synthetic/python", 1, 1, 1000, "/")

    monkeypatch.setattr(ProcessPin, "capture", classmethod(capture))
    pin = ProcessPin(3, 1, 2, "/synthetic/python", 1, 1, 1000, "/")
    run = decoder.StreamRun(pin, pin, CaseClock(time.time() + 30, time.time() + 60), -1)
    owned: list[int] = []
    run.pipes = make_pipes(owned)
    run.child.pid = 12345
    run.native_gate = {"mappings": {"records": [{"path": "/synthetic/native"}]}}
    try:
        yield run
    finally:
        for view in reversed(run.views):
            view.release()
        run.views.clear()
        run.buffers.clear()
        for fd in owned:
            os.close(fd)


@pytest.mark.parametrize("failure", ["capacity", "cancel"])
def test_admission_rejection_precedes_bank_allocation_and_native_resume(
    stopped_run: decoder.StreamRun, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = stopped_run
    actual_report = decoder.report_line

    def prepare(value: object) -> bytes:
        data = actual_report(value)
        run.request("synthetic_cancel")
        return data

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("rejected admission allocated, resumed or wrote")

    if failure == "capacity":
        run.native_gate = {"oversized": "\x00" * REPORT_BYTES}
        monkeypatch.setattr(limits, "bytearray", forbidden, raising=False)
    else:
        monkeypatch.setattr(decoder, "report_line", prepare)
    monkeypatch.setattr(decoder, "bytearray", forbidden, raising=False)
    monkeypatch.setattr(decoder.os, "kill", forbidden)
    monkeypatch.setattr(decoder.os, "set_blocking", forbidden)
    monkeypatch.setattr(decoder, "write_report", forbidden)
    with pytest.raises(ProbeError):
        run.admit_stream()
    assert not run.buffers and not run.input_released
    if failure == "cancel":
        original = run.clock.cleanup_monotonic
        run.request("repeat")
        assert run.clock.cleanup_monotonic == original


def test_admission_preserves_evidence_and_serializes_before_resume(
    stopped_run: decoder.StreamRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = stopped_run
    actual_report = decoder.report_line
    events: list[str] = []
    records: list[dict[str, object]] = []

    def prepare(value: object) -> bytes:
        assert not run.buffers and not run.input_released
        events.append("serialize")
        return actual_report(value)

    def resume(pid: int, sig: int) -> None:
        assert pid == run.child.pid and sig == signal.SIGCONT
        assert len(run.buffers) == 2 and events == ["serialize"]
        events.append("resume")

    def write(data: bytes) -> None:
        assert run.input_released and events == ["serialize", "resume"]
        records.append(cast(dict[str, object], json.loads(data)))
        events.append("write")

    def keep_blocking(_fd: int, _blocking: bool) -> None:
        pass

    monkeypatch.setattr(decoder, "report_line", prepare)
    monkeypatch.setattr(decoder.os, "kill", resume)
    monkeypatch.setattr(decoder.os, "set_blocking", keep_blocking)
    monkeypatch.setattr(decoder, "write_report", write)
    run.admit_stream()
    assert events == ["serialize", "resume", "write"]
    assert records == [
        {
            "event": "decoder_admitted",
            "case_binding": run.case.record(),
            "native_gate": run.native_gate,
            "init": run.init_pin.record(),
            "owner": run.owner_pin.record(),
            "runner": run.helper_pin.record(),
        }
    ]
