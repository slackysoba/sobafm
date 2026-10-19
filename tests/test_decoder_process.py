import asyncio
import ctypes
import io
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import sobafm.decoder_process as processes
from sobafm.catalog_audio import TrackError
from sobafm.decoder_process import (
    CHILD_MEMORY,
    Child,
    LinuxCgroup,
    WindowsJob,
    close_pipes,
    command,
)
from sobafm.native import SourceArchive, VerifiedNativeRuntime
from tests.test_decoder_check import Process, Writer


@pytest.fixture
def runtime(tmp_path: Path) -> VerifiedNativeRuntime:
    return VerifiedNativeRuntime(
        executable=tmp_path / "never-executed.exe",
        manifest_sha256="a" * 64,
        target="windows-amd64",
        source_bundle=SourceArchive(name="owned.zip", sha256="b" * 64, size=1),
    )


def test_argv_is_forced_mp3_first_audio_only_pipe_output(tmp_path: Path) -> None:
    argv = command(tmp_path / "reviewed decoder")
    assert argv[0] == str(tmp_path / "reviewed decoder")
    assert argv[argv.index("-f") + 1] == "mp3"
    assert argv[argv.index("-protocol_whitelist") + 1] == "pipe"
    assert argv[argv.index("-i") + 1] == "pipe:0"
    assert argv[argv.index("-map") + 1] == "0:a:0"
    assert argv[-1] == "pipe:1"
    assert "-nostdin" in argv
    assert not any("://" in value for value in argv)


async def test_child_launch_claims_boundary_before_spawn_and_bind_before_write(
    runtime: VerifiedNativeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = MagicMock()
    process = Process()
    process.pid = 123  # pyright: ignore[reportAttributeAccessIssue] - synthetic subprocess

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        boundary.prepare.assert_called_once_with()
        boundary.bind.assert_not_called()
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        assert kwargs["limit"] == 16384
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", spawn)
    child = await Child.launch(runtime, boundary)
    with pytest.raises(TrackError, match="capacity"):
        await child.write(b"untrusted")
    assert not process.stdin.blocks
    child.bind()
    boundary.bind.assert_called_once_with(123)
    await child.write(b"owned")
    assert process.stdin.blocks == [b"owned"]
    child.terminate()
    boundary.terminate.assert_called_once_with(123)
    child.kill()
    boundary.kill.assert_called_once_with()
    await child.close_input()
    assert await child.wait() == 0
    assert child.empty()
    assert child.close_pipes()


async def test_pipe_closing_flag_is_not_actual_handle_closure() -> None:
    process = Process()
    process._transport.allow_close = False  # pyright: ignore[reportPrivateUsage]
    assert not close_pipes(cast(asyncio.subprocess.Process, process))
    process._transport.allow_close = True  # pyright: ignore[reportPrivateUsage]
    assert close_pipes(cast(asyncio.subprocess.Process, process))


async def test_child_reuses_close_owner_after_borrower_cancellation() -> None:
    process = Process()
    process.stdin.defer_close = True
    child = Child(cast(asyncio.subprocess.Process, process), MagicMock())
    first = asyncio.create_task(child.close_input())
    await process.stdin.waiting.wait()
    first.cancel()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not process.stdin.close_waiter.cancelled()
    cleanup = asyncio.create_task(child.close_input())
    await asyncio.sleep(0)
    assert not cleanup.done()
    process.stdin.complete_close()  # shared protocol callback remains valid
    await cleanup
    assert process.stdin.wait_calls == 1
    assert process.stdin.close_waiter.done()


async def test_shared_protocol_future_negative_control_exposes_poisoning() -> None:
    writer = Writer()
    writer.defer_close = True
    writer.close()
    unprotected = asyncio.create_task(writer.wait_closed())
    await writer.waiting.wait()
    unprotected.cancel()
    with pytest.raises(asyncio.CancelledError):
        await unprotected
    assert writer.close_waiter.cancelled()
    with pytest.raises(asyncio.InvalidStateError):
        writer.complete_close()


class API:
    def __init__(self) -> None:
        self.limits = b""
        self.active = 0
        self.member = True
        self.corrupt_limit = False
        self.CreateJobObjectW = MagicMock(return_value=100)
        self.SetInformationJobObject = MagicMock(side_effect=self.set_limits)
        self.QueryInformationJobObject = MagicMock(side_effect=self.query)
        self.OpenProcess = MagicMock(return_value=200)
        self.AssignProcessToJobObject = MagicMock(return_value=1)
        self.IsProcessInJob = MagicMock(side_effect=self.membership)
        self.TerminateJobObject = MagicMock(return_value=1)
        self.CloseHandle = MagicMock(return_value=1)

    def set_limits(self, job: int, kind: int, value: Any, size: int) -> int:
        assert kind == 9
        self.limits = ctypes.string_at(value, size)
        return 1

    def query(self, job: int, kind: int, value: Any, size: int, returned: Any) -> int:
        if kind == 9:
            ctypes.memmove(value, self.limits, size)
            if self.corrupt_limit:
                value._obj.job_memory = 0
        else:
            assert kind == 1
            value._obj.active = self.active
        return 1

    def membership(self, process: int, job: int, value: Any) -> int:
        value._obj.value = self.member
        return 1


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> API:
    api = API()
    monkeypatch.setattr(processes, "os", SimpleNamespace(name="nt"))

    def library(*args: Any, **kwargs: Any) -> API:
        return api

    monkeypatch.setattr(processes.ctypes, "WinDLL", library, raising=False)
    return api


def test_job_limits_membership_and_tree_queries_are_exact(api: API) -> None:
    job = WindowsJob()
    job.prepare()
    job.bind(123)
    limits = processes._Limits.from_buffer_copy(api.limits)  # pyright: ignore[reportPrivateUsage]
    assert limits.process_memory == CHILD_MEMORY
    assert limits.job_memory == CHILD_MEMORY
    assert limits.basic.flags == 0x100 | 0x200 | 0x2000
    api.active = 1
    assert not job.empty()
    with pytest.raises(TrackError, match="cleanup"):
        job.close()
    job.kill()
    api.TerminateJobObject.assert_called_once_with(100, 1)
    api.active = 0
    job.close()
    assert job.empty()
    assert api.CloseHandle.call_args_list[-1].args == (100,)


@pytest.mark.parametrize("failure", ["limit", "assignment", "membership", "query", "open"])
def test_job_policy_or_query_failure_never_grants_binding(api: API, failure: str) -> None:
    job = WindowsJob()
    if failure == "limit":
        api.corrupt_limit = True
    elif failure == "query":
        api.QueryInformationJobObject.return_value = 0
        api.QueryInformationJobObject.side_effect = None
    if failure in ("limit", "query"):
        with pytest.raises(TrackError, match="capacity"):
            job.prepare()
        return
    job.prepare()
    if failure == "assignment":
        api.AssignProcessToJobObject.return_value = 0
    elif failure == "membership":
        api.member = False
    elif failure == "open":
        api.OpenProcess.return_value = 0
    with pytest.raises(TrackError, match="capacity"):
        job.bind(123)


class KernelPath:
    files: dict[str, str]
    removed: list[str]
    corrupt = False

    def __init__(self, value: str) -> None:
        self.value = PurePosixPath(value)

    def __truediv__(self, part: str) -> KernelPath:
        return KernelPath(str(self.value / part))

    def __str__(self) -> str:
        return str(self.value)

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, KernelPath) and self.value == other.value

    def is_absolute(self) -> bool:
        return self.value.is_absolute()

    def resolve(self) -> KernelPath:
        return self

    def is_relative_to(self, other: KernelPath) -> bool:
        return self.value.is_relative_to(other.value)

    def mkdir(self) -> None:
        for name in (
            "memory.max",
            "memory.swap.max",
            "cgroup.procs",
            "cgroup.kill",
            "cgroup.events",
        ):
            self.files[str(self / name)] = "populated 0" if name == "cgroup.events" else "max"

    def is_file(self) -> bool:
        return str(self) in self.files

    def read_text(self) -> str:
        if self.corrupt and self.value.name == "memory.max":
            return "max"
        return self.files[str(self)]

    def write_text(self, value: str) -> None:
        self.files[str(self)] = value
        if self.value.name in ("cgroup.kill", "cgroup.procs"):
            self.files[str(self.value.parent / "cgroup.events")] = (
                "populated 0" if self.value.name == "cgroup.kill" else "populated 1"
            )

    def open(self, mode: str) -> Any:
        assert mode == "r+"
        path = self

        class Control(io.StringIO):
            def close(self) -> None:
                path.write_text(self.getvalue())
                super().close()

        return Control()

    def rmdir(self) -> None:
        self.removed.append(str(self))


@pytest.fixture
def kernel(monkeypatch: pytest.MonkeyPatch) -> type[KernelPath]:
    KernelPath.files = {"/proc/mounts": "cgroup2 /sys/fs/cgroup cgroup2 rw 0 0"}
    KernelPath.removed = []
    KernelPath.corrupt = False
    monkeypatch.setattr(processes, "Path", KernelPath)
    monkeypatch.setattr(processes, "os", SimpleNamespace(name="posix", killpg=MagicMock()))
    return KernelPath


def test_cgroup_requires_delegated_leaf_membership_and_limit_readback(
    kernel: type[KernelPath],
) -> None:
    group = LinuxCgroup(cast(Path, kernel("/sys/fs/cgroup/delegated")))
    group.prepare()
    group.bind(123)
    assert not group.empty()
    with pytest.raises(TrackError, match="cleanup"):
        group.close()
    group.kill()
    assert group.empty()
    group.close()
    assert len(kernel.removed) == 1
    assert kernel.removed[0].startswith("/sys/fs/cgroup/delegated/sobafm-")


@pytest.mark.parametrize("parent", ["/sys/fs/cgroup", "/ordinary", "relative"])
def test_cgroup_cannot_target_bot_wide_or_ordinary_directory(
    kernel: type[KernelPath],
    parent: str,
) -> None:
    with pytest.raises(TrackError, match="capacity"):
        LinuxCgroup(cast(Path, kernel(parent))).prepare()
    assert len(kernel.files) == 1


def test_cgroup_readback_mismatch_refuses_before_input(kernel: type[KernelPath]) -> None:
    kernel.corrupt = True
    with pytest.raises(TrackError, match="capacity"):
        LinuxCgroup(cast(Path, kernel("/sys/fs/cgroup/delegated"))).prepare()


async def test_partial_binding_failure_still_kills_owned_boundary_and_direct_process() -> None:
    process = Process()
    process.pid = 123  # pyright: ignore[reportAttributeAccessIssue]
    process.terminate = MagicMock()
    process.kill = MagicMock()
    boundary = MagicMock()
    boundary.bind.side_effect = TrackError(processes.TrackFailure.CAPACITY)
    child = Child(cast(asyncio.subprocess.Process, process), boundary)
    with pytest.raises(TrackError, match="capacity"):
        child.bind()
    child.terminate()
    child.kill()
    assert boundary.kill.call_count == 2
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()


async def test_reaped_leader_never_signals_reused_process_group() -> None:
    process = Process()
    process.pid = 123  # pyright: ignore[reportAttributeAccessIssue]
    process.returncode = 0
    boundary = MagicMock()
    child = Child(cast(asyncio.subprocess.Process, process), boundary)
    child.bind()
    child.terminate()
    boundary.terminate.assert_not_called()
    boundary.kill.assert_called_once_with()
