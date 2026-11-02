"""Owned Python/PTY fixtures only; no Docker, native decoder or supplied recording."""

from __future__ import annotations

import errno
import json
import os
import select
import sys
import threading
import time
from pathlib import Path
from typing import IO, cast

import pytest

from evaluations.fma_validation import process as process_module
from evaluations.fma_validation.consumer import reports, validate_decode
from evaluations.fma_validation.controller import Command
from evaluations.fma_validation.decoder import StreamRun
from evaluations.fma_validation.limits import FILE_BYTES, FILE_SHA256, CaseClock, encode_report
from evaluations.fma_validation.metadata import BoundedFile
from evaluations.fma_validation.process import OwnedHelper, ProcessPin
from evaluations.fma_validation.pty_channel import open_diagnostics, read_diagnostics
from evaluations.linux_resource.owned_mp3_overlap import make_pipes
from evaluations.linux_resource.probe import ProbeError


def clock() -> CaseClock:
    return CaseClock(time.time() + 30, time.time() + 60)


def test_raw_pty_preserves_bytes_and_requires_last_slave_close() -> None:
    owned: list[int] = []
    master, slave = open_diagnostics(owned)
    duplicate = os.dup(slave)
    owned.append(duplicate)
    try:
        assert read_diagnostics(master, 20) is None
        payload = b"\x00\r\n\x1b\xff\x11\x13synthetic"
        assert os.write(slave, payload) == len(payload)
        assert select.select([master], [], [], 2)[0] == [master]
        assert read_diagnostics(master, 100) == payload
        os.close(slave)
        owned.remove(slave)
        assert read_diagnostics(master, 100) is None
        os.close(duplicate)
        owned.remove(duplicate)
        assert select.select([master], [], [], 2)[0] == [master]
        assert read_diagnostics(master, 100) == b""
    finally:
        for fd in owned:
            os.close(fd)


def test_pty_backpressure_has_no_hidden_drain_or_flush() -> None:
    owned: list[int] = []
    master, slave = open_diagnostics(owned)
    os.set_blocking(slave, False)
    written = 0
    try:
        for _ in range(1024):
            try:
                written += os.write(slave, b"a" * 4096)
            except BlockingIOError:
                break
        else:
            pytest.fail("synthetic terminal did not reach bounded backpressure")
        received = 0
        while received < written:
            assert select.select([master], [], [], 2)[0]
            chunk = read_diagnostics(master, 4096)
            assert chunk and set(chunk) == {97}
            received += len(chunk)
        assert received == written and read_diagnostics(master, 1) is None
    finally:
        for fd in owned:
            os.close(fd)


def test_pty_does_not_treat_arbitrary_io_errors_as_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_fd: int, _size: int) -> bytes:
        raise OSError(errno.EBADF, "synthetic")

    monkeypatch.setattr(os, "read", fail)
    with pytest.raises(OSError):
        read_diagnostics(12345, 1)


@pytest.fixture
def synthetic_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    def capture(pid: int) -> ProcessPin:
        return ProcessPin(pid, 1, os.getpid(), sys.executable, 1, 1, 1000, "/")

    def verify(_self: ProcessPin) -> None:
        return

    # Host fixtures deliberately do not claim container security qualification.
    monkeypatch.setattr(ProcessPin, "capture", capture)
    monkeypatch.setattr(ProcessPin, "verify", verify)


def settle(helper: OwnedHelper) -> None:
    helper.cancel()
    end = time.monotonic() + 5
    while not helper.join(0.01) and time.monotonic() < end:
        helper.cancel()
    assert helper.join(0), helper.receipt()


def test_actual_contained_metadata_helper_settles_before_borrowed_fd_reuse(
    tmp_path: Path,
    synthetic_pins: None,
) -> None:
    path = tmp_path / "synthetic-metadata"
    data = b"owned synthetic nonaudio input" * 20
    path.write_bytes(data)
    fd = os.open(path, os.O_RDONLY)
    helper = OwnedHelper(clock(), "metadata")
    try:
        helper.launch(
            [
                sys.executable,
                "-m",
                "evaluations.fma_validation.metadata",
                "--execute",
                "--owned-fd",
                str(fd),
                "--bytes",
                str(len(data)),
            ],
            (fd,),
        )
        helper.finish_input()
        helper.admit()
        end = time.monotonic() + 5
        while not helper.join(0.01) and time.monotonic() < end:
            pass
        assert helper.join(0), helper.receipt()
        assert helper.process is not None and helper.process.returncode == 0
        result = reports(bytes(helper.stdout))
        assert len(result) == 1 and result[0]["credits_complete"] is False
        assert helper.output_eof and helper.error_eof and not helper.fds
        os.fstat(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        assert os.read(fd, len(data)) == data
    finally:
        settle(helper)
        os.close(fd)


def test_blocked_close_does_not_block_timed_join_or_renew_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    helper = OwnedHelper(clock(), "synthetic")
    helper.fds.add(write_fd)
    entered, release = threading.Event(), threading.Event()
    actual_close = os.close

    def held(fd: int) -> None:
        if fd == write_fd:
            entered.set()
            assert release.wait(3)
        actual_close(fd)

    monkeypatch.setattr(os, "close", held)
    worker = threading.Thread(target=helper.close, args=(write_fd,))
    worker.start()
    try:
        assert entered.wait(2)
        assert not helper.join(0)
        assert not helper.join(0.01)
        helper.cancel()
        original = helper.clock.cleanup_monotonic
        helper.cancel()
        assert helper.clock.cleanup_monotonic == original
        assert write_fd in helper.fds and write_fd in helper.busy
    finally:
        release.set()
        worker.join(3)
        actual_close(read_fd)
    assert not worker.is_alive() and helper.join(0)


def test_metadata_helper_reports_bounded_rejection_and_still_settles(
    tmp_path: Path,
    synthetic_pins: None,
) -> None:
    path = tmp_path / "unsupported-synthetic-version"
    data = b"ID3\x04\0\0\0\0\0\0" + b"owned synthetic nonaudio"
    path.write_bytes(data)
    fd = os.open(path, os.O_RDONLY)
    helper = OwnedHelper(clock(), "metadata")
    try:
        helper.launch(
            [
                sys.executable,
                "-m",
                "evaluations.fma_validation.metadata",
                "--execute",
                "--owned-fd",
                str(fd),
                "--bytes",
                str(len(data)),
            ],
            (fd,),
        )
        helper.finish_input()
        helper.admit()
        end = time.monotonic() + 5
        while not helper.join(0.01) and time.monotonic() < end:
            pass
        assert helper.join(0) and helper.process is not None
        assert helper.process.returncode == 1
        assert reports(bytes(helper.stdout)) == [
            {
                "schema": "sobafm-fma-metadata-rejection-1",
                "reason": "unsupported_id3_version",
                "credits_complete": False,
            }
        ]
        assert helper.output_eof and helper.error_eof and not helper.fds
        os.fstat(fd)
    finally:
        settle(helper)
        os.close(fd)


def test_cancellation_during_registered_launch_retains_actual_child(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_pins: None,
) -> None:
    helper = OwnedHelper(clock(), "synthetic")
    entered, release = threading.Event(), threading.Event()
    actual_popen = process_module.subprocess.Popen
    errors: list[Exception] = []

    def held(*args: object, **kwargs: object) -> object:
        entered.set()
        assert release.wait(3)
        return actual_popen(*args, **kwargs)  # pyright: ignore[reportArgumentType, reportCallIssue]

    def launch() -> None:
        try:
            helper.launch([sys.executable, "-c", "import time; time.sleep(30)"])
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(process_module.subprocess, "Popen", held)
    worker = threading.Thread(target=launch)
    worker.start()
    try:
        assert entered.wait(2)
        helper.cancel()
        original = helper.clock.cleanup_monotonic
        assert not helper.join(0)
        release.set()
        worker.join(3)
        assert not worker.is_alive() and not errors
        settle(helper)
        assert helper.process is not None and helper.process.returncode is not None
        assert helper.clock.cleanup_monotonic == original
    finally:
        release.set()
        worker.join(3)
        settle(helper)


def test_metadata_requires_read_only_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "synthetic"
    path.write_bytes(b"hello")
    fd = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(ProbeError, match="metadata_file_binding"):
            BoundedFile(fd, 5)
    finally:
        os.close(fd)


def good_decode() -> dict[str, object]:
    return {
        "event": "full_stream_result",
        "schema": "sobafm-fma-decode-1",
        "passed": True,
        "failure": None,
        "input_bytes": FILE_BYTES,
        "input_sha256": FILE_SHA256,
        "input_complete": True,
        "stdout_eof": True,
        "stderr_eof": True,
        "exit_code": 0,
        "pcm_bytes": 1_152_000,
        "pcm_sha256": "a" * 64,
        "measured_duration_seconds": 6.0,
        "web_duration_seconds": 194,
        "ready_seconds": 1.0,
        "cleanup_debt": False,
        "cleanup": {
            "cleanup_start_utc": 1000.0,
            "cleanup_deadline_met": True,
            "cleanup_timing_unknown": False,
            "case_deadline_met": True,
        },
        "memory_event_delta": {"oom": 0, "oom_kill": 0},
        "swap_current": "0",
        "diagnostics": {
            "terminal_source_filter_required": True,
            "format": "MPEG 1.0 L III vbr 44100 j-s",
        },
        "catalog_admitted": False,
        "listening": False,
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("input_bytes", FILE_BYTES - 1),
        ("input_complete", False),
        ("input_sha256", "f" * 64),
        ("stdout_eof", False),
        ("stderr_eof", False),
        ("exit_code", True),
        ("exit_code", 1),
        ("pcm_bytes", 1_152_001),
        ("pcm_bytes", 46_080_004),
        ("pcm_sha256", "unknown"),
        ("ready_seconds", float("nan")),
        ("ready_seconds", 15.01),
        ("ready_seconds", True),
        ("measured_duration_seconds", 194),
        ("memory_event_delta", {"oom": 1, "oom_kill": 0}),
        ("cleanup", {"cleanup_deadline_met": False}),
        ("catalog_admitted", True),
        ("diagnostics", {}),
        ("cleanup_debt", True),
    ],
)
def test_completion_requires_independent_exact_evidence(key: str, value: object) -> None:
    result = good_decode()
    validate_decode(result)
    result[key] = value
    with pytest.raises(ProbeError):
        validate_decode(result)


def test_duplicate_and_extra_reports_fail_closed() -> None:
    with pytest.raises(ProbeError):
        reports(b'{"passed":true,"passed":false}\n')
    with pytest.raises(ProbeError):
        reports(b"{}\n{}\n{}\n")
    result = good_decode()
    result["unexpected"] = True
    with pytest.raises(ProbeError):
        validate_decode(cast(dict[str, object], json.loads(encode_report(result))))


def test_controller_command_bounds_output_and_observes_actual_exit() -> None:
    command = Command([sys.executable, "-c", "import os; os.write(1, b'x' * 70000)"])
    end = time.monotonic() + 5
    while not command.settled() and time.monotonic() < end:
        command.pump()
        select.select([], [], [], 0.001)
    if not command.settled():
        command.stop()
        while not command.settled() and time.monotonic() < end + 3:
            command.pump()
    assert command.settled()
    assert command.error == "management_output_limit"
    assert len(command.output) <= 65_536 and not command.retained


def test_stream_writer_continues_past_old_fixture_prefix(synthetic_pins: None) -> None:
    cancel_read, cancel_write = os.pipe()
    pin = ProcessPin.capture(os.getpid())
    run = StreamRun(pin, pin, clock(), cancel_read)
    owned: list[int] = []
    run.pipes = make_pipes(owned)
    os.set_blocking(run.pipes.input_write, False)
    try:
        for _ in range(20):
            run.input_buffer.extend(b"s" * 4096)
            assert run.can_feed()
            run.write_input()
            assert os.read(run.pipes.input_read, 4096) == b"s" * 4096
            assert not run.can_feed() and not run.input_buffer
        assert run.input_count == 81_920
        run.request("synthetic_cancel")
        run.input_buffer.extend(b"not-admitted")
        assert not run.can_feed()
    finally:
        for fd in [*owned, cancel_read, cancel_write]:
            os.close(fd)


def test_native_zero_exit_without_exact_input_eof_fails(
    synthetic_pins: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    pin = ProcessPin.capture(os.getpid())
    run = StreamRun(pin, pin, clock(), read_fd)

    def empty() -> set[int]:
        return set()

    monkeypatch.setattr(run, "members", empty)
    run.child.exit_code = 0
    try:
        with pytest.raises(ProbeError, match="native_exit_before_exact_eof"):
            run.check_trigger()
        run.input_complete = True
        run.check_trigger()
    finally:
        os.close(read_fd)
        os.close(write_fd)


class UncertainClose:
    def __init__(self, stream: IO[bytes], schedule: str) -> None:
        self.stream, self.schedule = stream, schedule
        self.calls = 0
        self.reused: int | None = None

    @property
    def closed(self) -> bool:
        return self.stream.closed

    def fileno(self) -> int:
        return self.stream.fileno()

    def close(self) -> None:
        self.calls += 1
        fd = self.stream.fileno()
        if self.schedule != "still_live":
            self.stream.close()
        if self.schedule == "reused":
            replacement = os.open("/dev/null", os.O_RDONLY)
            if replacement != fd:
                os.dup2(replacement, fd)
                os.close(replacement)
            self.reused = fd
        raise OSError(errno.EINTR, "owned synthetic uncertain close")


@pytest.mark.parametrize("schedule", ["released", "still_live", "reused"])
def test_controller_uncertain_close_reconciles_original_fd_without_retry_or_assumption(
    schedule: str,
) -> None:
    command = Command([sys.executable, "-I", "-B", "-c", "pass"])
    command.worker.join(3)
    assert not command.worker.is_alive()
    process = command.process
    assert process is not None and process.stdout is not None
    assert process.wait(timeout=3) == 0
    original = process.stdout
    fd = original.fileno()
    wrapped = UncertainClose(original, schedule)
    process.stdout = cast(IO[bytes], wrapped)
    command.stop()
    started = command.stopping
    try:
        command.pump()
        assert fd in command.uncertain and fd in command.retained
        assert command.error == "management_close_debt"
        command.pump()
        command.stop()
        assert command.stopping == started and wrapped.calls == 1
        if schedule == "released":
            with pytest.raises(OSError) as error:
                os.fstat(fd)
            assert error.value.errno == errno.EBADF
            assert command.settled()
        else:
            os.fstat(fd)
            assert fd in command.uncertain and not command.settled()
            for _ in range(5):
                command.pump()
            assert wrapped.calls == 1 and fd in command.retained
    finally:
        # The fixture releases only its known live backing/replacement. The
        # production owner never retries an uncertain close on a numeric fd.
        if not original.closed:
            original.close()
        if wrapped.reused is not None:
            os.close(wrapped.reused)
        command.pump()
        assert command.settled()
        assert command.error == "management_close_debt" and command.stopping == started
