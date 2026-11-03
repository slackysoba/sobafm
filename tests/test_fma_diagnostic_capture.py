"""Fixed diagnostic payload ownership and byte-range grammar, entirely synthetic."""

from __future__ import annotations

import errno
import hashlib
import threading
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from evaluations.fma_validation import decoder, diagnostics
from evaluations.fma_validation.diagnostics import (
    DIAGNOSTIC_BYTES,
    FACT_BYTES,
    FORMAT_BYTES,
    READ_BYTES,
    TAG_PREFIXES,
    validate,
)
from evaluations.fma_validation.limits import READY_BYTES
from evaluations.fma_validation.observation import ObservationWorkspace, ProcessPin
from evaluations.linux_resource import owned_mp3_cancel as cancellation
from evaluations.linux_resource.decoder_startup import Child
from evaluations.linux_resource.owned_mp3_decode import BANNER
from evaluations.linux_resource.owned_mp3_overlap import Pipes
from evaluations.linux_resource.probe import ProbeError
from tests.test_fma_environment_observation import Environment, environment
from tests.test_fma_process_observation import MonotonicClock
from tests.test_fma_validation import safe_diagnostics


def tagged(size: int = DIAGNOSTIC_BYTES) -> tuple[bytes, bytes]:
    empty = b"\n".join(TAG_PREFIXES)
    room = size - len(safe_diagnostics(empty + b"\n"))
    tags = TAG_PREFIXES[0] + b"x" * room + b"\n" + b"\n".join(TAG_PREFIXES[1:])
    return safe_diagnostics(tags + b"\n"), tags


def runner(
    monkeypatch: pytest.MonkeyPatch, *, allocate: bool = True
) -> tuple[decoder.StreamRun, Environment]:
    env = environment(monkeypatch)
    pin = ProcessPin(2, 30, 1, "/synthetic/python", 3, 4, 1000, "/")

    def capture(_cls: type[ProcessPin], _pid: int, _workspace: ObservationWorkspace) -> ProcessPin:
        return pin

    def members() -> set[int]:
        return set()

    monkeypatch.setattr(ProcessPin, "capture", classmethod(capture))
    monkeypatch.setattr(cancellation, "time", MonotonicClock(env.now))
    monkeypatch.setattr(decoder, "time", MonotonicClock(env.now))
    run = decoder.StreamRun(pin, pin, env.observation.clock, -1, workspace=env.observation)
    run.fds.clear()  # All descriptors below are synthetic; none are opened.
    run.pipes = Pipes(*range(10, 20))
    run.fds.append(run.pipes.error_read)
    run.child.pid = 12345
    if allocate:
        run.allocate_diagnostics()
    run.resources_before = env.observation.resources()
    monkeypatch.setattr(run, "members", members)
    return run, env


class Reader:
    def __init__(self, run: decoder.StreamRun, data: bytes, chunk: int = READ_BYTES) -> None:
        self.run, self.data, self.chunk = run, data, chunk
        self.offset = 0
        self.requests: list[int] = []
        self.hold: Callable[[memoryview], None] | None = None

    def readv(self, fd: int, views: list[memoryview]) -> int:
        assert self.run.pipes is not None and fd == self.run.pipes.error_read
        assert fd in self.run.busy
        target = views[0]
        assert target.obj is self.run.diagnostic_slab
        self.requests.append(len(target))
        if self.hold is not None:
            self.hold(target)
        count = min(self.chunk, len(target), len(self.data) - self.offset)
        target[:count] = self.data[self.offset : self.offset + count]
        self.offset += count
        return count


def capture_all(
    monkeypatch: pytest.MonkeyPatch, run: decoder.StreamRun, data: bytes, chunk: int = READ_BYTES
) -> Reader:
    reader = Reader(run, data, chunk)
    monkeypatch.setattr(decoder.os, "readv", reader.readv)
    while not run.error_eof:
        run.read_error()
    return reader


def complete(run: decoder.StreamRun) -> None:
    run.input_released = run.input_complete = run.output_eof = True
    run.child.exit_code = 0
    run.output_count = READY_BYTES
    run.complete_diagnostics()


@pytest.mark.parametrize("size", [DIAGNOSTIC_BYTES - 1, DIAGNOSTIC_BYTES])
@pytest.mark.parametrize("chunk", [1, 17, 4095, 4096])
def test_near_capacity_fixed_capture_and_exact_hashes(
    monkeypatch: pytest.MonkeyPatch, size: int, chunk: int
) -> None:
    run, _ = runner(monkeypatch)
    data, tags = tagged(size)
    backing, slab = run.stderr, run.diagnostic_slab
    reader = capture_all(monkeypatch, run, data, chunk)
    assert run.stderr is backing and run.diagnostic_slab is slab
    assert len(backing) == DIAGNOSTIC_BYTES and slab is not None and len(slab) == READ_BYTES
    assert run.stderr_count == size
    if size == DIAGNOSTIC_BYTES:
        assert reader.requests[-1] == 1  # EOF probe has its own one-byte room.
    complete(run)
    facts = run.diagnostic_facts
    assert facts is not None
    assert facts.record() == validate(data)
    assert facts.sha256 == hashlib.sha256(data).hexdigest()
    assert facts.tag_sha256 == hashlib.sha256(tags).hexdigest()
    assert DIAGNOSTIC_BYTES + READ_BYTES + FACT_BYTES == 73_728 < 196_608


@pytest.mark.parametrize("chunk", [4095, 4096])
def test_overflow_rejects_and_retains_exact_bounded_prefix(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    run, env = runner(monkeypatch)
    data, _ = tagged()
    reader = Reader(run, data + b"X", chunk)
    monkeypatch.setattr(decoder.os, "readv", reader.readv)
    with pytest.raises(ProbeError, match="diagnostic_overflow"):
        while True:
            run.read_error()
    assert reader.requests[-1] == (17 if chunk == 4095 else 1)
    assert reader.offset == DIAGNOSTIC_BYTES + 1 and run.stderr_count == DIAGNOSTIC_BYTES
    assert run.failure and not run.error_eof and env.observation.clock.cleanup_monotonic == 103
    assert run.diagnostic_report(False) == {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    assert len(run.stderr) == DIAGNOSTIC_BYTES


@pytest.mark.parametrize("suffix", [b"", b"\n", b"\n\n\n"])
def test_long_completion_digits_and_optional_newline(suffix: bytes) -> None:
    data = (
        safe_diagnostics().rstrip(b"\n").replace(b"[3:14]", b"[" + b"0" * 60_000 + b":14]") + suffix
    )
    assert validate(data)["diagnostic_sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "line",
    [
        b"MPEG 1.0 L III cbr999 48000 stereo",
        b"MPEG 2.5 L III abr000 11025 stereo",
        b"MPEG 2.0 L III vbr 8000 dual",
        b"MPEG 1.0 L III cbr0 12000 mono",
    ],
)
def test_format_grammar_bound(line: bytes) -> None:
    assert len(line) <= FORMAT_BYTES
    data = safe_diagnostics().replace(b"MPEG 1.0 L III vbr 44100 j-s", line)
    assert validate(data)["format"] == line.decode()


@pytest.mark.parametrize(
    "change",
    [
        "nonascii",
        "tab",
        "empty_row",
        "label",
        "control",
        "post",
        "format_width",
        "seconds",
        "minutes",
    ],
)
def test_large_tag_and_line_rejections(change: str) -> None:
    data, _ = tagged()
    if change in ("nonascii", "tab", "empty_row", "label", "control"):
        injection = {
            "nonascii": b"\x7f",
            "tab": b"\t",
            "empty_row": b"\n",
            "label": b"\n\tTitle:   ",
            "control": b"\n[0:00] Decoding of - finished.",
        }[change]
        data = data.replace(b"x" * 64, injection, 1)
    elif change == "post":
        data = safe_diagnostics() + b"Directory: .\n"
    elif change == "format_width":
        data = safe_diagnostics().replace(b"j-s\n", b"stereo-too-long\n")
    elif change == "seconds":
        data = safe_diagnostics().replace(b":14]", b":60]")
    else:
        data = safe_diagnostics().replace(b"[3:", b"[:")
    with pytest.raises(ProbeError):
        validate(data)


def test_legacy_order_blanks_directory_and_empty_values_remain_accepted() -> None:
    tags = b"\n".join(TAG_PREFIXES) + b"\n"
    data = (
        safe_diagnostics(tags)
        .replace(
            b"Note: forcing output encoding s16\nPlaying MPEG stream 1 of 1: - ...",
            b"Playing MPEG stream 1 of 1: - ...\n\nNote: forcing output encoding s16",
        )
        .replace(b"[3:14]", b"Directory: .\n[3:14]")
    )
    report = validate(data)
    assert report["tag_block_sha256"] == hashlib.sha256(tags[:-1]).hexdigest()


@pytest.mark.parametrize(
    "err,eof", [(errno.EIO, True), (errno.EAGAIN, False), (errno.EINTR, False)]
)
def test_read_errors_are_not_conflated(
    monkeypatch: pytest.MonkeyPatch, err: int, eof: bool
) -> None:
    run, _ = runner(monkeypatch)

    def readv(_fd: int, _views: list[memoryview]) -> int:
        raise OSError(err, "synthetic")

    monkeypatch.setattr(decoder.os, "readv", readv)
    run.read_error()
    assert run.error_eof is eof and run.stderr_count == 0 and not run.busy
    assert run.failure is None


@pytest.mark.parametrize("chunk", [1, 7, len(BANNER) - 1, len(BANNER)])
def test_banner_does_not_consume_following_diagnostics(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    run, _ = runner(monkeypatch)
    reader = Reader(run, BANNER + b"not banner yet", chunk)

    def select(
        _reads: list[int], _writes: list[int], _errors: list[int], _timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        return [14], [], []

    def reap(_child: Child) -> None:
        pass

    monkeypatch.setattr(decoder.os, "readv", reader.readv)
    monkeypatch.setattr(decoder.select, "select", select)
    monkeypatch.setattr(Child, "reap", reap)
    run.receive_diagnostic_banner()
    assert reader.offset == run.stderr_count == len(BANNER)
    assert not run.error_eof and len(run.stderr) == DIAGNOSTIC_BYTES


@pytest.mark.parametrize("case", ["stdout", "closed", "changed", "exit", "deadline"])
def test_banner_rejects_activity_missing_identity_and_deadline(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    run, env = runner(monkeypatch)
    data = b"" if case == "closed" else (b"X" + BANNER[1:] if case == "changed" else BANNER)
    reader = Reader(run, data)

    def select(
        _reads: list[int], _writes: list[int], _errors: list[int], _timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        return ([12, 14] if case == "stdout" else [14]), [], []

    def reap(child: Child) -> None:
        if case == "exit":
            child.exit_code = 0

    if case == "deadline":
        env.now[0] = 116
    monkeypatch.setattr(decoder.os, "readv", reader.readv)
    monkeypatch.setattr(decoder.select, "select", select)
    monkeypatch.setattr(Child, "reap", reap)
    with pytest.raises(ProbeError):
        run.receive_diagnostic_banner()
    if case in ("stdout", "exit", "deadline"):
        assert reader.offset == 0


@pytest.mark.parametrize("stage", ["stdout", "exit", "reap_error", "select_error"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_banner_exception_adopts_phase_before_close(
    monkeypatch: pytest.MonkeyPatch, stage: str, close_fails: bool
) -> None:
    run, env = runner(monkeypatch)
    run.phase_deadline = 115
    backing, slab = run.stderr, run.diagnostic_slab
    error = OSError(errno.EBADF, "synthetic late banner operation")
    calls: list[str] = []

    def reap(child: Child) -> None:
        calls.append("reap")
        if stage in ("exit", "reap_error"):
            env.now[0] = 119
            if stage == "reap_error":
                raise error
            child.exit_code = 0

    def select(
        _reads: list[int], _writes: list[int], _errors: list[int], _timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        calls.append("select")
        assert stage in ("stdout", "select_error")
        env.now[0] = 119
        if stage == "select_error":
            raise error
        return [12], [], []

    def readv(_fd: int, _views: list[memoryview]) -> int:
        pytest.fail("banner error must not consume or fabricate diagnostics")

    def close(_fd: int) -> None:
        assert run.clock.cleanup_start_utc == 115 and run.cancel.deadline == 118
        assert run.clock.missed and run.failure
        if close_fails:
            raise OSError(errno.EIO, "synthetic uncertain close")

    monkeypatch.setattr(Child, "reap", reap)
    monkeypatch.setattr(decoder.select, "select", select)
    monkeypatch.setattr(decoder.os, "readv", readv)
    monkeypatch.setattr(decoder.os, "close", close)
    expected = {
        "stdout": "native_stdout_before_input_gate",
        "exit": "native_exit_before_banner",
        "reap_error": "synthetic late banner operation",
        "select_error": "synthetic late banner operation",
    }
    with pytest.raises((OSError, ProbeError), match=expected[stage]) as caught:
        try:
            run.receive_diagnostic_banner()
        except Exception as failure:
            run.fail(failure)  # Match main's outer failure path.
            raise
    if stage.endswith("error"):
        assert caught.value is error
    assert calls == (["reap"] if stage in ("exit", "reap_error") else ["reap", "select"])
    assert run.clock.cleanup_start_utc == 115 and run.clock.cleanup_monotonic == 118
    assert run.clock.missed and not run.error_eof and run.stderr_count == 0
    assert run.stderr is backing and run.diagnostic_slab is slab
    assert run.diagnostic_facts is None and not run.diagnostic_attempted
    run.child.exit_code = 0  # Synthetic later child settlement; no native process.
    run.close(14)
    assert (14 in run.uncertain_closes) is close_fails
    assert run.release_if_done() is not close_fails
    assert run.failure and run.clock.cleanup_monotonic == 118 and run.clock.missed


@pytest.mark.parametrize("failed_size", [DIAGNOSTIC_BYTES, READ_BYTES])
@pytest.mark.parametrize("close_fails", [False, True])
def test_allocation_exception_adopts_phase_and_retains_partial_backing(
    monkeypatch: pytest.MonkeyPatch, failed_size: int, close_fails: bool
) -> None:
    run, env = runner(monkeypatch, allocate=False)
    run.phase_deadline = 115
    error = MemoryError("synthetic late diagnostic allocation")
    allocated: list[bytearray] = []

    def allocate(size: int) -> bytearray:
        if size == failed_size:
            env.now[0] = 119
            raise error
        backing = bytearray(size)
        allocated.append(backing)
        return backing

    def close(_fd: int) -> None:
        assert run.clock.cleanup_start_utc == 115 and run.cancel.deadline == 118
        assert run.clock.missed and run.failure
        if close_fails:
            raise OSError(errno.EIO, "synthetic uncertain close")

    monkeypatch.setattr(decoder, "bytearray", allocate, raising=False)
    monkeypatch.setattr(decoder.os, "close", close)
    with pytest.raises(MemoryError) as caught:
        try:
            run.allocate_diagnostics()
        except Exception as failure:
            run.fail(failure)
            raise
    assert caught.value is error
    assert run.clock.cleanup_start_utc == 115 and run.clock.cleanup_monotonic == 118
    assert run.clock.missed and run.diagnostic_slab is None and run.stderr_count == 0
    if failed_size == READ_BYTES:
        assert len(allocated) == 1 and run.stderr is allocated[0]
        assert len(run.stderr) == DIAGNOSTIC_BYTES
    else:
        assert not allocated and not run.stderr
    run.child.exit_code = 0
    run.close(14)
    assert (14 in run.uncertain_closes) is close_fails
    assert run.release_if_done() is not close_fails
    assert run.failure and run.clock.cleanup_monotonic == 118 and run.clock.missed


@pytest.mark.parametrize("stage", ["before", "backing", "slab"])
@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize("returned_at,cancel_at", [(115, None), (119, None), (119, 101)])
def test_allocation_deadline_boundaries_stop_before_caller_continues(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    close_fails: bool,
    returned_at: float,
    cancel_at: float | None,
) -> None:
    run, env = runner(monkeypatch, allocate=False)
    run.phase_deadline = 115
    allocated: list[bytearray] = []
    continuation: list[str] = []
    original = 115 if cancel_at is None else cancel_at
    deadline = original + 3
    missed = returned_at > deadline

    def advance() -> None:
        if cancel_at is not None:
            env.now[0] = cancel_at
            run.request("synthetic_cancel_during_allocation")
        env.now[0] = returned_at

    if stage == "before":
        advance()

    def allocate(size: int) -> bytearray:
        backing = bytearray(size)
        allocated.append(backing)
        if (stage == "backing" and size == DIAGNOSTIC_BYTES) or (
            stage == "slab" and size == READ_BYTES
        ):
            advance()
        return backing

    def identity(_pid: int) -> tuple[str, int]:
        continuation.append("identity")
        return "synthetic", 1

    def close(_fd: int) -> None:
        assert run.clock.cleanup_start_utc == original and run.cancel.deadline == deadline
        assert run.clock.missed is missed and run.failure
        if close_fails:
            raise OSError(errno.EIO, "synthetic uncertain close")

    monkeypatch.setattr(decoder, "bytearray", allocate, raising=False)
    monkeypatch.setattr(run, "process_start_identity", identity)
    monkeypatch.setattr(decoder.os, "close", close)
    with pytest.raises(ProbeError, match="diagnostic_deadline_or_cancel"):
        run.allocate_diagnostics()
        # prepare's next operation must not run after late allocation success.
        run.process_start_identity(run.child.pid)
    assert not continuation
    assert run.clock.cleanup_start_utc == original and run.clock.cleanup_monotonic == deadline
    assert run.clock.missed is missed and run.stderr_count == 0 and not run.diagnostic_attempted
    count = {"before": 0, "backing": 1, "slab": 2}[stage]
    assert len(allocated) == count
    if count:
        assert run.stderr is allocated[0] and len(run.stderr) == DIAGNOSTIC_BYTES
    else:
        assert not run.stderr
    if count == 2:
        assert run.diagnostic_slab is not None
        assert run.diagnostic_slab is allocated[1] and len(run.diagnostic_slab) == READ_BYTES
    else:
        assert run.diagnostic_slab is None
    run.child.exit_code = 0
    run.close(14)
    assert (14 in run.uncertain_closes) is close_fails
    assert run.release_if_done() is not close_fails
    assert run.failure and run.clock.cleanup_monotonic == deadline and run.clock.missed is missed


@pytest.mark.parametrize("stage", ["before", "backing", "slab"])
def test_allocation_succeeds_before_phase_boundary(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    run, env = runner(monkeypatch, allocate=False)
    run.phase_deadline = 115
    allocated: list[bytearray] = []
    if stage == "before":
        env.now[0] = 114

    def allocate(size: int) -> bytearray:
        backing = bytearray(size)
        allocated.append(backing)
        if (stage == "backing" and size == DIAGNOSTIC_BYTES) or (
            stage == "slab" and size == READ_BYTES
        ):
            env.now[0] = 114
        return backing

    monkeypatch.setattr(decoder, "bytearray", allocate, raising=False)
    run.allocate_diagnostics()
    assert len(allocated) == 2 and run.stderr is allocated[0]
    assert run.diagnostic_slab is allocated[1] and run.stderr_count == 0
    assert run.failure is None and run.cancel.started is None
    assert run.clock.cleanup_start_utc is None and not run.clock.missed


def test_prepare_allocates_diagnostics_only_after_native_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _ = runner(monkeypatch, allocate=False)
    forked: list[bool] = []

    def no_op(*_args: object, **_kwargs: object) -> None:
        pass

    def pipes(fds: list[int]) -> Pipes:
        fds.extend(range(10, 20))
        return Pipes(*range(10, 20))

    def pty(fds: list[int]) -> tuple[int, int]:
        fds.extend((20, 21))
        return 20, 21

    def fork() -> int:
        assert not run.stderr and run.diagnostic_slab is None
        forked.append(True)
        return 12345

    def stop(_pid: int) -> tuple[str, int]:
        assert forked and len(run.stderr) == DIAGNOSTIC_BYTES
        assert run.diagnostic_slab is not None and len(run.diagnostic_slab) == READ_BYTES
        raise ProbeError("synthetic_after_fork")

    def fstat(_fd: int) -> object:
        return SimpleNamespace(st_dev=1, st_ino=2)

    def page_size(_name: str) -> int:
        return 4096

    monkeypatch.setattr(run.observation, "single_thread", no_op)
    monkeypatch.setattr(run.observation, "environment", no_op)
    monkeypatch.setattr(decoder, "verify_native_files", no_op)
    monkeypatch.setattr(decoder, "make_pipes", pipes)
    monkeypatch.setattr(decoder, "open_diagnostics", pty)
    monkeypatch.setattr(decoder.os, "uname", lambda: SimpleNamespace(machine="x86_64"))
    monkeypatch.setattr(decoder.os, "sysconf", page_size)
    monkeypatch.setattr(decoder.os, "fstat", fstat)
    monkeypatch.setattr(decoder.os, "fork", fork)
    monkeypatch.setattr(run, "close", no_op)
    monkeypatch.setattr(run, "process_start_identity", stop)
    with pytest.raises(ProbeError, match="synthetic_after_fork"):
        run.prepare()
    assert run.child.pid == 12345 and forked == [True]


def test_held_read_keeps_fixed_backing_fd_and_original_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, env = runner(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    views: list[memoryview] = []
    reader = Reader(run, b"partial")

    def hold(view: memoryview) -> None:
        views.append(view)
        entered.set()
        assert release.wait(3)

    def forbidden_close(_fd: int) -> None:
        pytest.fail("busy diagnostic descriptor reached close")

    reader.hold = hold
    monkeypatch.setattr(decoder.os, "readv", reader.readv)
    monkeypatch.setattr(decoder.os, "close", forbidden_close)
    worker = threading.Thread(target=run.read_error)
    worker.start()
    try:
        assert entered.wait(3)
        run.request("cancel")
        run.close(14)  # Busy ownership prevents an actual close attempt.
        assert 14 in run.busy and 14 in run.fds and not run.release_if_done()
        assert views[0].obj is run.diagnostic_slab
        env.now[0] = 104
        run.request("repeat")
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and run.stderr_count == 7 and not run.error_eof
    assert run.clock.cleanup_monotonic == 103 and run.clock.missed
    with pytest.raises(ValueError, match="released memoryview"):
        _ = views[0].obj


@pytest.mark.parametrize("outcome", ["data", "eof", "error"])
def test_normal_read_expiry_starts_original_clock_before_close(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    run, env = runner(monkeypatch)
    run.input_released = run.input_eof = True
    run.decode_end = 150
    closed: list[int] = []

    def readv(_fd: int, views: list[memoryview]) -> int:
        env.now[0] = 154
        if outcome == "error":
            raise OSError(errno.EBADF, "synthetic read failure")
        views[0][0] = 65
        return int(outcome == "data")

    def close(fd: int) -> None:
        assert run.failure and run.clock.cleanup_start_utc == 150
        assert run.cancel.deadline == 153 and run.clock.missed
        closed.append(fd)
        raise OSError(errno.EIO, "synthetic uncertain close")

    def select(
        _reads: list[int], _writes: list[int], _errors: list[int], _timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        return [14], [], []

    def reap(_child: Child) -> None:
        pass

    monkeypatch.setattr(decoder.os, "readv", readv)
    monkeypatch.setattr(decoder.os, "close", close)
    monkeypatch.setattr(decoder.select, "select", select)
    monkeypatch.setattr(Child, "reap", reap)
    run.io_step()
    assert closed == [14] and 14 in run.uncertain_closes
    assert not run.release_if_done() and run.clock.cleanup_monotonic == 153


def test_overflow_starts_failure_before_uncertain_close(monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = runner(monkeypatch)
    run.stderr_count = DIAGNOSTIC_BYTES
    reader = Reader(run, b"X")

    def close(_fd: int) -> None:
        assert run.failure and run.clock.cleanup_monotonic == 103
        raise OSError(errno.EIO, "synthetic uncertain close")

    monkeypatch.setattr(decoder.os, "readv", reader.readv)
    monkeypatch.setattr(decoder.os, "close", close)
    with pytest.raises(ProbeError):
        run.read_error()
    run.close(14)
    assert 14 in run.uncertain_closes and 14 in run.fds
    assert not run.release_if_done() and len(run.stderr) == DIAGNOSTIC_BYTES


def test_held_hash_retains_view_and_rejects_cancelled_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _ = runner(monkeypatch)
    data, _ = tagged()
    capture_all(monkeypatch, run, data)
    run.input_released = run.input_complete = run.output_eof = True
    run.child.exit_code = 0
    run.fds.clear()
    entered, release = threading.Event(), threading.Event()
    views: list[memoryview] = []
    errors: list[Exception] = []

    def sha256(data: bytes | memoryview = b"") -> object:
        if isinstance(data, memoryview):
            assert data.obj is run.stderr
            views.append(data)
            entered.set()
            assert release.wait(3)
        return hashlib.sha256(data)

    def validate_once() -> None:
        try:
            run.complete_diagnostics()
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(diagnostics, "hashlib", SimpleNamespace(sha256=sha256))
    worker = threading.Thread(target=validate_once)
    worker.start()
    try:
        assert entered.wait(3)
        run.request("cancel")
        assert run.diagnostic_active and not run.release_if_done()
        assert views[0].obj is run.stderr
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and len(errors) == 1
    assert run.diagnostic_facts is None and run.failure and run.clock.cleanup_monotonic == 103
    with pytest.raises(ValueError, match="released memoryview"):
        _ = views[0].obj
    with pytest.raises(ProbeError, match="already_attempted"):
        run.complete_diagnostics()


@pytest.mark.parametrize("stage", ["scan", "hash"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_validation_exception_adopts_phase_before_close(
    monkeypatch: pytest.MonkeyPatch, stage: str, close_fails: bool
) -> None:
    run, env = runner(monkeypatch)
    capture_all(monkeypatch, run, safe_diagnostics())
    run.input_released = run.input_complete = run.output_eof = True
    run.child.exit_code = 0
    run.decode_end = 150
    backing, slab = run.stderr, run.diagnostic_slab
    calls: list[str] = []

    def delayed_failure(data: memoryview) -> object:
        assert data.obj is backing and run.diagnostic_active
        calls.append(stage)
        env.now[0] = 154
        raise RuntimeError("synthetic scan/hash failure after original phase")

    def close(_fd: int) -> None:
        assert run.clock.cleanup_monotonic == 153 and run.cancel.deadline == 153
        assert run.clock.missed and run.failure
        if close_fails:
            raise OSError(errno.EIO, "synthetic uncertain close")

    if stage == "scan":
        monkeypatch.setattr(decoder, "scan", delayed_failure)
    else:
        monkeypatch.setattr(diagnostics, "hashlib", SimpleNamespace(sha256=delayed_failure))
    monkeypatch.setattr(decoder.os, "close", close)
    with pytest.raises(RuntimeError, match="synthetic scan/hash"):
        run.complete_diagnostics()
    assert run.clock.cleanup_start_utc == 150
    assert run.clock.cleanup_monotonic == 153 and run.clock.missed
    assert run.diagnostic_facts is None and run.diagnostic_attempted and not run.diagnostic_active
    assert run.stderr is backing and run.diagnostic_slab is slab
    with pytest.raises(ProbeError, match="already_attempted"):
        run.complete_diagnostics()
    assert calls == [stage] and run.clock.cleanup_monotonic == 153
    run.close(14)
    assert (14 in run.uncertain_closes) is close_fails
    assert run.release_if_done() is not close_fails
    assert run.failure and run.clock.missed


def test_failure_hash_completion_precedes_terminal_cleanup_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, env = runner(monkeypatch)
    capture_all(monkeypatch, run, b"partial diagnostic")
    run.child.exit_code = 0
    run.fds.clear()
    run.fail("synthetic_failure")
    assert run.release_if_done()
    entered, release = threading.Event(), threading.Event()
    results: list[dict[str, object]] = []

    def sha256(data: bytes | memoryview = b"") -> object:
        assert isinstance(data, memoryview) and data.obj is run.stderr
        entered.set()
        assert release.wait(3)
        return hashlib.sha256(data)

    monkeypatch.setattr(decoder, "hashlib", SimpleNamespace(sha256=sha256))
    worker = threading.Thread(target=lambda: results.append(run.finish()))
    worker.start()
    try:
        assert entered.wait(3)
        assert run.diagnostic_active and len(run.stderr) == DIAGNOSTIC_BYTES
        env.now[0] = 104
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and len(results) == 1
    assert results[0]["passed"] is False and run.clock.missed
    assert results[0]["cleanup"] == run.clock.receipt()
    assert run.clock.cleanup_monotonic == 103


def test_cached_facts_survive_rejected_and_held_terminal_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _ = runner(monkeypatch)
    data, _ = tagged()
    capture_all(monkeypatch, run, data)
    complete(run)
    facts, backing, slab = run.diagnostic_facts, run.stderr, run.diagnostic_slab
    run.request("complete")
    run.fds.clear()
    assert run.release_if_done()

    def forbidden(_data: memoryview) -> object:
        pytest.fail("finish reparsed diagnostic backing")

    monkeypatch.setattr(decoder, "scan", forbidden)
    result = run.finish()
    assert facts is not None and result["diagnostics"] == facts.record()
    with pytest.raises(ProbeError):
        decoder.report_line({**result, "oversized": "x" * 65536})
    assert run.diagnostic_facts is facts and run.stderr is backing and run.diagnostic_slab is slab
    entered, release = threading.Event(), threading.Event()
    payload = decoder.report_line(result)
    writes: list[int] = []

    def write(_fd: int, view: memoryview) -> int:
        entered.set()
        assert release.wait(3)
        writes.append(len(view))
        return len(view)

    monkeypatch.setattr(decoder.os, "write", write)
    worker = threading.Thread(target=lambda: decoder.write_report(payload))
    worker.start()
    try:
        assert entered.wait(3)
        assert run.diagnostic_facts is facts and run.stderr is backing and len(backing) == 65536
        assert run.clock.cleanup_monotonic == 103
        run.request("repeat")
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and writes == [len(payload)]
    assert run.diagnostic_facts is facts and run.stderr is backing and run.diagnostic_slab is slab
