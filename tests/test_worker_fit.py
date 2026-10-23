"""The bootstrap uses fake data; all containment and process operations are owned fakes."""

import asyncio
import io
from pathlib import Path

import pytest

from sobafm import audio_worker, worker_fit
from sobafm.decoder_process import Launch, WindowsJob


class Job(WindowsJob):
    def __init__(self) -> None:
        super().__init__(commit_limit=worker_fit.MAXIMUM_CANDIDATE)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class Child:
    def __init__(self, payload: bytes, code: int = 0) -> None:
        self.payload, self.code = payload, code
        self.bound = self.killed = self.input_closed = self.released = False
        self.release_gate = True
        self.wait_gate = asyncio.Event()
        self.wait_gate.set()

    def bind(self) -> None:
        self.bound = True

    async def write(self, chunk: bytes) -> None:
        assert self.bound
        assert chunk == audio_worker.REQUEST

    async def close_input(self) -> None:
        self.input_closed = True

    async def read(self, pipe: int, size: int) -> bytes:
        if pipe == 2:
            return b""
        data, self.payload = self.payload[:size], self.payload[size:]
        return data

    async def wait(self) -> int:
        await self.wait_gate.wait()
        return self.code

    def terminate(self) -> None:
        self.kill()

    def kill(self) -> None:
        self.killed = True
        self.wait_gate.set()

    def empty(self) -> bool:
        return self.killed

    def close_pipes(self) -> bool:
        return self.killed and self.release_gate

    async def release_storage(self) -> None:
        assert self.close_pipes()
        assert self.input_closed
        assert not self.payload
        self.released = True


def check(child: Child, *, failed_launch: bool = False) -> worker_fit.FitCheck:
    async def spawn(argv: tuple[str, ...], boundary: WindowsJob, owner: Launch) -> Child:
        assert argv[-1] == "--bootstrap-check"
        owner.preparation = child
        owner.begin()
        if failed_launch:
            raise OSError("owned partial launch")
        owner.child = child
        return child

    return worker_fit.FitCheck(Path(__file__).absolute(), spawn=spawn, boundary=Job())


def test_bootstrap_actual_installed_sdk_offline() -> None:
    output = io.BytesIO()
    assert audio_worker.bootstrap(io.BytesIO(audio_worker.REQUEST), output) == 0
    assert output.getvalue() == b"BI" + bytes(audio_worker.FRAME_SIZE)


def test_bootstrap_rejects_any_other_input() -> None:
    output = io.BytesIO()
    assert audio_worker.bootstrap(io.BytesIO(b"\x01\x02"), output) == 72
    assert output.getvalue() == b"B"


@pytest.mark.parametrize(
    ("payload", "code", "status"),
    [
        (b"BI" + bytes(3840), 0, "useful-path"),
        (b"BM", 70, "allocation-failed"),
        (b"BE", 71, "bootstrap-failed"),
        (b"", 1, "exit-inconclusive"),
    ],
)
async def test_result_requires_actual_storage_release(
    payload: bytes,
    code: int,
    status: str,
) -> None:
    child = Child(payload, code)
    owner = check(child)
    result = await asyncio.wait_for(asyncio.shield(owner.decision), 1)
    await owner.task
    assert result.status == status
    assert result.storage_released
    assert not result.cap_cause_proven
    assert child.released
    assert owner.released


async def test_partial_launch_retains_and_retires_its_owner() -> None:
    child = Child(b"")
    owner = check(child, failed_launch=True)
    result = await asyncio.wait_for(asyncio.shield(owner.decision), 1)
    await owner.task
    assert result.status == "launch-or-io-failed"
    assert child.released


async def test_timeout_verdict_sticks_through_late_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_fit, "CLEANUP_SECONDS", 0.01)
    child = Child(b"BI" + bytes(3840))
    child.release_gate = False
    owner = check(child)
    result = await asyncio.wait_for(asyncio.shield(owner.decision), 1)
    assert result.status == "cleanup-timeout"
    assert not owner.released
    assert owner.retirement is not None
    assert not owner.retirement.done()
    owner.task.cancel()
    owner.task.cancel()
    await asyncio.gather(owner.task, return_exceptions=True)
    child.release_gate = True
    await asyncio.wait_for(asyncio.shield(owner.retirement), 1)
    assert owner.released
    assert owner.decision.result() is result


async def test_output_overflow_is_not_a_memory_limit_finding() -> None:
    child = Child(bytes(worker_fit.OUTPUT_LIMIT + 100))
    owner = check(child)
    result = await asyncio.wait_for(asyncio.shield(owner.decision), 1)
    await owner.task
    assert result.status == "launch-or-io-failed"
    assert not result.cap_cause_proven
    assert child.released
