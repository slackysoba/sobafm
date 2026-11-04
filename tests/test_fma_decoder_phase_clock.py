"""Decoder phase expiry starts cleanup at the phase boundary, not its late observation."""

from __future__ import annotations

import os
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from evaluations.fma_validation import decoder
from evaluations.fma_validation.limits import READY_BYTES
from evaluations.linux_resource import owned_mp3_cancel as cancellation
from evaluations.linux_resource import owned_mp3_decode as native
from evaluations.linux_resource.decoder_startup import Child
from evaluations.linux_resource.owned_mp3_overlap import Pipes
from evaluations.linux_resource.probe import ProbeError
from tests.test_fma_diagnostic_capture import runner
from tests.test_fma_environment_observation import Environment

LATE = 117.5  # First observed 2.5 seconds after the 115-second phase deadline.
PIPES = Pipes(*range(10, 20))
NATIVE_PID = 4


class SleepingClock:
    """Fake monotonic time whose bounded sleep is the late observation."""

    def __init__(self, now: list[float]) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now[0]

    def sleep(self, _seconds: float) -> None:
        self.now[0] = LATE


def decode_end(run: decoder.StreamRun, env: Environment, _mp: pytest.MonkeyPatch) -> None:
    # Control: this neighbouring check already back-dates through the clock.
    run.input_released = True
    run.decode_end = run.phase_deadline
    env.now[0] = LATE
    run.check_trigger()


def readiness_deadline(run: decoder.StreamRun, env: Environment, _mp: pytest.MonkeyPatch) -> None:
    run.input_released = True
    env.now[0] = LATE
    run.check_trigger()


def readiness_late(run: decoder.StreamRun, env: Environment, mp: pytest.MonkeyPatch) -> None:
    run.input_released = True
    run.pipes = PIPES
    run.bank = memoryview(bytearray(READY_BYTES))
    run.output_view = memoryview(bytearray(4))
    run.bank_filled = READY_BYTES - 4

    def readv(_fd: int, views: list[memoryview]) -> int:
        env.now[0] = LATE
        views[0][:4] = b"\0\0\0\0"
        return 4

    mp.setattr(decoder.os, "readv", readv)
    run.read_pcm()


def wait_ready(run: decoder.StreamRun, env: Environment, mp: pytest.MonkeyPatch) -> None:
    def select(
        _reads: list[int], _writes: list[int], _errors: list[int], _timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        env.now[0] = LATE
        return [], [], []

    def reap(_child: Child) -> None:
        pass

    mp.setattr(cancellation.select, "select", select)
    mp.setattr(Child, "reap", reap)
    run.wait_ready(PIPES.ready_read)


def stopped_gate(run: decoder.StreamRun, env: Environment, mp: pytest.MonkeyPatch) -> None:
    """Drive real preparation to the real stopped-state gate; nothing forks or executes."""
    real_read, real_write, real_fstat = os.read, os.write, os.fstat
    run.fds.clear()
    run.child.pid = 0
    workspace = run.observation

    def noop(*_args: object, **_kwargs: object) -> None:
        pass

    def members() -> set[int]:
        live = run.child.pid and run.child.exit_code is None
        return {run.child.pid} if live else set()

    def fork() -> int:
        return NATIVE_PID

    def start_identity(_pid: int) -> tuple[str, int]:
        return "S", 30

    def pipes(_fds: list[int]) -> Pipes:
        return PIPES

    def diagnostics(_fds: list[int]) -> tuple[int, int]:
        return 20, 21

    def uname() -> object:
        return SimpleNamespace(machine="x86_64")

    def sysconf(_name: str) -> int:
        return 4096

    def fstat(fd: int) -> object:
        return SimpleNamespace(st_dev=1, st_ino=2) if fd == 21 else real_fstat(fd)

    def read(fd: int, size: int) -> bytes:
        return b"R" if fd == PIPES.ready_read else real_read(fd, size)

    def write(fd: int, data: bytes) -> int:
        return 1 if fd == PIPES.gate_write else real_write(fd, data)

    def kill(pid: int, _sig: int) -> None:
        assert pid == NATIVE_PID

    def waitpid(pid: int, _flags: int) -> tuple[int, int]:
        assert pid == NATIVE_PID
        return 0, 0

    def ready(
        reads: list[int], _writes: list[int], _errors: list[int], _timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        return reads, [], []

    for name in ("single_thread", "environment", "child_identity", "native_limits"):
        mp.setattr(workspace, name, noop)
    mp.setattr(run, "members", members)
    mp.setattr(run, "process_start_identity", start_identity)
    mp.setattr(run, "receive_diagnostic_banner", noop)
    mp.setattr(decoder, "verify_native_files", noop)
    mp.setattr(decoder, "make_pipes", pipes)
    mp.setattr(decoder, "open_diagnostics", diagnostics)
    mp.setattr(decoder.os, "uname", uname)
    mp.setattr(decoder.os, "sysconf", sysconf)
    mp.setattr(decoder.os, "fork", fork)
    mp.setattr(decoder.os, "fstat", fstat)
    mp.setattr(decoder.os, "read", read)
    mp.setattr(decoder.os, "write", write)
    mp.setattr(native.os, "kill", kill)
    mp.setattr(native.os, "waitpid", waitpid)
    mp.setattr(native, "time", SleepingClock(env.now))
    mp.setattr(cancellation.select, "select", ready)
    try:
        run.prepare()
    finally:
        assert run.child.pid == NATIVE_PID and run.stderr


Trigger = Callable[[decoder.StreamRun, Environment, pytest.MonkeyPatch], None]


@pytest.mark.parametrize(
    ("trigger", "message"),
    [
        (decode_end, "whole_or_decode_deadline"),
        (readiness_deadline, "readiness_deadline"),
        (readiness_late, "readiness_late"),
        (wait_ready, "decoder preparation expired or was cancelled"),
        (stopped_gate, "native stopped-state deadline exceeded"),
    ],
    ids=["decode_end", "readiness_deadline", "readiness_late", "wait_ready", "stopped_gate"],
)
def test_late_phase_expiry_starts_cleanup_at_phase_deadline(
    monkeypatch: pytest.MonkeyPatch, trigger: Trigger, message: str
) -> None:
    run, env = runner(monkeypatch, allocate=False)
    assert run.started == 100 and run.phase_deadline == 115
    with pytest.raises(ProbeError, match=f"^{message}$") as error:
        trigger(run, env, monkeypatch)
    # The production loops record the failure only after the raise returns.
    run.fail(error.value)
    assert env.now[0] == LATE and run.failure
    assert run.clock.cleanup_start_utc == 115
    assert run.clock.cleanup_monotonic == 118 and run.cancel.deadline == 118
    assert not run.clock.missed  # Not yet observed after the original deadline.
    run.fds.clear()  # Every descriptor here is synthetic and was never opened.
    run.child.exit_code = -9  # Any synthetic native is settled for release.
    env.now[0] = 119  # Between the original 118 and the late 120.5 deadline.
    assert run.release_if_done()
    assert run.clock.missed and run.cancel.deadline_missed
    assert run.clock.receipt()["cleanup_deadline_met"] is False
    assert run.clock.cleanup_start_utc == 115 and run.clock.cleanup_monotonic == 118
