"""The bootstrap uses fake data; all containment and process operations are owned fakes."""

import asyncio
import io
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts import check_worker_fit as runner
from sobafm import audio_worker, worker_fit
from sobafm.decoder_process import Launch, PipeDiagnostic, WindowsJob
from sobafm.native_calls import GuardedAPI
from sobafm.windows_decoder import Handle, Kernel32, WindowsProcess


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
        self.kills = self.queries = self.closes = self.requested = 0

    def bind(self) -> None:
        self.bound = True

    async def write(self, chunk: bytes) -> None:
        assert self.bound
        assert chunk == audio_worker.REQUEST

    async def close_input(self) -> None:
        self.input_closed = True

    async def read(self, pipe: int, size: int) -> bytes:
        self.requested += size
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
        self.kills += 1
        self.killed = True
        self.wait_gate.set()

    def empty(self) -> bool:
        self.queries += 1
        return self.killed

    def close_pipes(self) -> bool:
        self.closes += 1
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
    assert result.failure_phase == "stdout-read"
    assert result.error_category == "validation"
    assert result.exit_code == 0  # existing waiter survives the aggregate read failure


async def test_finite_close_attempts_park_without_retry_or_owner_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_fit, "CLOSE_ATTEMPTS", 2)
    child = Child(b"BI" + bytes(3840))
    child.release_gate = False
    owner = check(child)
    await asyncio.wait_for(owner.parked.wait(), 1)
    assert owner.decision.result().status == "cleanup-failed"
    counts = child.kills, child.queries, child.closes
    await asyncio.sleep(0.03)
    assert (child.kills, child.queries, child.closes) == counts
    assert child.kills == 1
    assert owner.launch.cleanup_owner is child
    assert not owner.released
    assert not owner.retirement.done()
    # Only these owned fakes are discarded by the test, never a real control.
    owner.retirement.cancel()
    await asyncio.gather(owner.retirement, return_exceptions=True)


async def test_cumulative_drain_is_reserved_before_forwarding() -> None:
    class Endless(Child):
        async def read(self, pipe: int, size: int) -> bytes:
            self.requested += size
            return bytes(size)

    child = Endless(b"")
    owner = check(child)
    await asyncio.wait_for(owner.parked.wait(), 1)
    assert owner.budget.exhausted
    assert child.requested <= worker_fit.DRAIN_LIMIT
    count = child.requested
    await asyncio.sleep(0.03)
    assert child.requested == count
    assert owner.cleaning
    assert not owner.released
    owner.retirement.cancel()
    await asyncio.gather(owner.retirement, return_exceptions=True)


def native_fakes() -> tuple[worker_fit.ControlBudget, MagicMock, WindowsJob, WindowsProcess]:
    budget = worker_fit.ControlBudget()
    raw = MagicMock()
    raw.QueryInformationJobObject.return_value = True
    raw.CloseHandle.return_value = True
    job = WindowsJob(operation_guard=budget.charge)
    job._api = GuardedAPI(raw, budget.charge)  # pyright: ignore[reportPrivateUsage]
    job._handle = 7  # pyright: ignore[reportPrivateUsage]
    api = object.__new__(Kernel32)  # never load or call a DLL
    api.api = GuardedAPI(raw, budget.charge)
    child = WindowsProcess(job, Launch(lambda: True))
    child.api = api
    child.process_handle = Handle(api, 101)
    return budget, raw, job, child


def test_failed_job_termination_cannot_escape_budget_through_fallback() -> None:
    budget, raw, _, child = native_fakes()
    raw.TerminateJobObject.return_value = False
    budget.operations = 1
    with pytest.raises(worker_fit.BudgetExhaustedError):
        child.kill()
    raw.TerminateJobObject.assert_called_once()
    raw.TerminateProcess.assert_not_called()
    assert child.process_handle is not None
    assert not child.process_handle.closed


def test_nested_job_close_charges_query_and_close_separately() -> None:
    budget, raw, job, _ = native_fakes()
    budget.operations = 1
    with pytest.raises(worker_fit.BudgetExhaustedError):
        job.close()
    raw.QueryInformationJobObject.assert_called_once()
    raw.CloseHandle.assert_not_called()
    assert job.creation_handle == 7


async def test_release_storage_cannot_bypass_nested_close_budget() -> None:
    budget, raw, _, child = native_fakes()
    child.creation = asyncio.create_task(asyncio.sleep(0))
    await child.creation
    child.returncode = 0
    budget.operations = 1
    with pytest.raises(worker_fit.BudgetExhaustedError):
        await child.release_storage()
    raw.QueryInformationJobObject.assert_called_once()
    raw.CloseHandle.assert_not_called()
    assert child.creation is not None
    assert child.process_handle is not None
    assert not child.process_handle.closed


@pytest.mark.parametrize("report_failure", [False, True])
async def test_runner_survives_repeated_cancellation_and_report_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    report_failure: bool,
) -> None:
    monkeypatch.setattr(worker_fit, "CLEANUP_SECONDS", 0.005)
    child = Child(b"BI" + bytes(3840))
    child.release_gate = False
    owner = check(child)

    def factory(python: Path, *, boundary: WindowsJob) -> worker_fit.FitCheck:
        assert boundary._commit_limit == 65_439_999  # pyright: ignore[reportPrivateUsage]
        return owner

    monkeypatch.setattr(runner, "FitCheck", factory)
    path = tmp_path / "result.json"
    if report_failure:
        path.write_text("preserve existing result")
    reported = asyncio.Event()
    original_report = runner.report

    def report(path: Path, result: worker_fit.Result) -> None:
        reported.set()
        original_report(path, result)

    monkeypatch.setattr(runner, "report", report)
    executing = asyncio.create_task(runner.execute(Path(__file__), path))
    await asyncio.wait_for(asyncio.shield(owner.decision), 1)
    await reported.wait()
    for _ in range(3):
        executing.cancel()
        await asyncio.sleep(0)
    assert not executing.done()
    assert not owner.retirement.done()
    assert not owner.retirement.cancelled()
    child.release_gate = True
    result = await asyncio.wait_for(asyncio.gather(executing, return_exceptions=True), 1)
    assert owner.released
    assert owner.retirement.done()
    assert not owner.retirement.cancelled()
    assert owner.decision.result().status == "cleanup-timeout"
    if report_failure:
        assert isinstance(result[0], RuntimeError)
        assert path.read_text() == "preserve existing result"


async def test_cancel_before_work_starts_still_has_independent_retirement() -> None:
    child = Child(b"")
    owner = check(child)
    owner.task.cancel()
    await asyncio.wait_for(owner.join(), 1)
    assert owner.released
    assert child.released


def test_comparison_identity_has_one_fixed_cap_and_unchanged_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_digest(path: Path) -> str:
        return "a" * 64

    monkeypatch.setattr(runner, "digest", fake_digest)
    monkeypatch.setattr(runner.importlib.metadata, "distributions", lambda: ())
    identity = runner.identity()
    assert identity["schema"] == 4
    assert identity["case"] == "242-worker-bootstrap-higher-cap-comparison"
    assert identity["cap"] == runner.COMPARISON_LIMIT == 65_439_999
    assert worker_fit.MAXIMUM_CANDIDATE == 35_439_999
    assert identity["pcm_retained_for_arithmetic"] == 34_560_000
    assert identity["outside_charges_optimistically_omitted"] is True
    assert identity["qualified_profile"] is False
    assert identity["operation_limit"] == 512
    assert identity["cumulative_read_request_bytes"] == 65_536
    assert identity["close_attempts"] == 64
    assert identity["run_seconds"] == 15
    assert identity["cleanup_seconds"] == 3


def test_runner_rejects_arbitrary_cap_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.sys, "argv", ["runner", "prepare", "unused.json", "--cap", "1"])
    with pytest.raises(SystemExit) as rejected:
        runner.main()
    assert rejected.value.code == 2


def test_first_diagnostic_is_fixed_and_does_not_retain_raw_exception() -> None:
    launch = Launch(lambda: True)
    error = OSError("owned secret-like text must not survive")
    error.winerror = 5  # pyright: ignore[reportAttributeAccessIssue] - Windows-only metadata on an owned fake
    launch.record_failure("write-request", error)
    launch.record_failure("close-input", RuntimeError("secondary raw detail"))
    assert launch.failure is not None
    assert launch.failure.phase == "write-request"
    assert launch.failure.category == "os"
    assert launch.failure.win32_error == 5
    assert "secret" not in json.dumps(asdict(launch.failure))
    assert "secondary" not in json.dumps(asdict(launch.failure))


@pytest.mark.parametrize("code", [-1, 2**32, "raw"])
def test_diagnostic_rejects_unbounded_or_non_numeric_winerror(code: object) -> None:
    launch = Launch(lambda: True)
    error = OSError("owned fake")
    error.winerror = code  # pyright: ignore[reportAttributeAccessIssue] - reject malformed exception metadata
    launch.record_failure("launch", error)
    assert launch.failure is not None
    assert launch.failure.win32_error is None


@pytest.mark.parametrize("value", [True, -1, 2**32, "raw", 1.5, None])
def test_pipe_diagnostic_sanitizes_every_numeric_field(value: Any) -> None:
    diagnostic = PipeDiagnostic("info-validate", value, value, value, value, value)
    assert asdict(diagnostic) == {
        "stage": "info-validate",
        "win32_error": None,
        "flags": None,
        "outgoing": None,
        "incoming": None,
        "instances": None,
    }


async def test_first_pipe_diagnostic_reaches_result_without_cleanup_overwrite() -> None:
    owner = check(Child(b""))
    diagnostic = PipeDiagnostic(
        "info-validate", flags=9, outgoing=16385, incoming=16384, instances=1
    )
    owner.launch.record_failure("pipe-stdin", RuntimeError("private"), pipe=diagnostic)
    owner.launch.record_failure("pipe-stderr", OSError("later"), pipe=PipeDiagnostic("connect", 5))
    result = await asyncio.wait_for(asyncio.shield(owner.decision), 1)
    await owner.join()
    assert result.pipe is diagnostic
    assert result.failure_phase == "pipe-stdin"
    assert "private" not in json.dumps(asdict(result))
    assert "later" not in json.dumps(asdict(result))


async def test_write_failure_keeps_phase_and_already_owned_retirement_exit() -> None:
    class BrokenWrite(Child):
        async def write(self, chunk: bytes) -> None:
            raise BrokenPipeError("owned fake text")

    child = BrokenWrite(b"", 23)
    owner = check(child)
    result = await asyncio.wait_for(asyncio.shield(owner.decision), 1)
    await owner.join()
    assert result.status == "launch-or-io-failed"
    assert result.failure_phase == "write-request"
    assert result.error_category == "os"
    assert result.exit_code == 23
    assert result.storage_released
    assert not result.cap_cause_proven
