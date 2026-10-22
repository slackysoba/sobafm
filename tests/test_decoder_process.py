import asyncio
import ctypes
import io
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import sobafm.decoder_check as checks
import sobafm.decoder_process as processes
import sobafm.track_deck as tracks
from sobafm.catalog_audio import TrackError
from sobafm.decoder_process import (
    CHILD_MEMORY,
    PIPE_BLOCK_SIZE,
    Child,
    Launch,
    LinuxCgroup,
    WindowsJob,
    close_pipes,
    command,
)
from sobafm.native import SourceArchive, VerifiedNativeRuntime
from sobafm.pcm import FRAME_BYTES
from sobafm.track_deck import TrackDeck
from tests.test_decoder_check import Process, Writer
from tests.test_track_deck import Rig


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


def test_blocksize_is_scoped_once_per_pipe_endpoint(tmp_path: Path) -> None:
    assert PIPE_BLOCK_SIZE == 16_384
    argv = command(tmp_path / "reviewed decoder")
    input_index = argv.index("-i")
    assert argv.count("-blocksize") == 2
    assert argv[input_index - 2 : input_index] == ("-blocksize", "16384")
    assert argv[-3:-1] == ("-blocksize", "16384")


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
        assert args == command(runtime.executable)
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        assert kwargs["limit"] == 16384
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", spawn)
    owner = Launch(lambda: True)
    child = await Child.launch(runtime, boundary, owner)
    assert owner.owned is child
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


class WindowsPipe:
    """CPython PipeHandle shape: a handle/fileno API, without a .closed member."""

    def __init__(self) -> None:
        self.handle: int | None = 123

    def fileno(self) -> int:
        if self.handle is None:
            raise ValueError("I/O operation on closed pipe")
        return self.handle

    def close(self) -> None:
        self.handle = None


class WindowsPipeTransport:
    def __init__(self, handle: object) -> None:
        self.handle = handle

    def get_extra_info(self, name: str) -> object:
        assert name == "pipe"
        return self.handle


class WindowsTransport:
    def __init__(self) -> None:
        self.pipes = [WindowsPipe() for _ in range(3)]
        self.allow_close = True

    def get_pipe_transport(self, index: int) -> WindowsPipeTransport:
        return WindowsPipeTransport(self.pipes[index])

    def close(self) -> None:
        if self.allow_close:
            for pipe in self.pipes:
                pipe.close()


@pytest.fixture
def windows_pipe_api(monkeypatch: pytest.MonkeyPatch) -> None:
    # No OS handle exists: only the import/type boundary and documented CPython
    # shape are replaced. The actual loop and subprocess launcher stay untouched.
    module = ModuleType("asyncio.windows_utils")
    module.PipeHandle = WindowsPipe  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "asyncio.windows_utils", module)
    monkeypatch.setattr(processes, "sys", SimpleNamespace(platform="win32"))


def test_windows_owned_handle_must_close_before_release(windows_pipe_api: None) -> None:
    transport = WindowsTransport()
    process = cast(asyncio.subprocess.Process, SimpleNamespace(_transport=transport))
    assert all(not hasattr(pipe, "closed") for pipe in transport.pipes)
    transport.allow_close = False
    assert not close_pipes(process)
    assert all(pipe.fileno() == 123 for pipe in transport.pipes)
    transport.allow_close = True
    assert close_pipes(process)
    assert all(pipe.handle is None for pipe in transport.pipes)


@pytest.mark.parametrize("unknown", [None, object(), SimpleNamespace(closed=True)])
def test_unknown_pipe_metadata_never_proves_closure(
    windows_pipe_api: None, unknown: object
) -> None:
    def get_pipe_transport(index: int) -> WindowsPipeTransport:
        return WindowsPipeTransport(unknown)

    transport = SimpleNamespace(get_pipe_transport=get_pipe_transport, close=lambda: None)
    process = cast(asyncio.subprocess.Process, SimpleNamespace(_transport=transport))
    assert not close_pipes(process)


def test_arbitrary_fileno_error_is_not_owned_closure(windows_pipe_api: None) -> None:
    class Unknown:
        def fileno(self) -> int:
            raise ValueError("not a CPython pipe")

    def get_pipe_transport(index: int) -> WindowsPipeTransport:
        return WindowsPipeTransport(Unknown())

    transport = SimpleNamespace(get_pipe_transport=get_pipe_transport, close=lambda: None)
    process = cast(asyncio.subprocess.Process, SimpleNamespace(_transport=transport))
    assert not close_pipes(process)


async def test_windows_shape_capability_releases_all_four_owned_children(
    windows_pipe_api: None,
    runtime: VerifiedNativeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children: list[WindowsTransport] = []

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        child = Process(bytes(200620 if len(children) < 2 else 184320))
        transport = WindowsTransport()
        cast(Any, child)._transport = transport
        children.append(transport)
        return cast(asyncio.subprocess.Process, child)

    monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    result = await checks.check_decoder(runtime)
    await asyncio.sleep(0)
    assert len(result) == len(children) == 4
    assert not checks._owners  # pyright: ignore[reportPrivateUsage]
    assert all(pipe.handle is None for transport in children for pipe in transport.pipes)


async def test_windows_open_handle_keeps_failed_capability_debt(
    windows_pipe_api: None,
    runtime: VerifiedNativeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = WindowsTransport()
    transport.allow_close = False
    child = Process(bytes(200620))
    cast(Any, child)._transport = transport

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, child)

    monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(checks, "CLEANUP_SECONDS", 0.02)
    with pytest.raises(TrackError, match="decoder"):
        await checks.check_decoder(runtime)
    assert child.returncode == 0
    assert checks._owners  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(TrackError, match="capacity"):
        await checks.check_decoder(runtime)
    transport.allow_close = True
    await asyncio.gather(*checks._owners)  # pyright: ignore[reportPrivateUsage]
    assert not checks._owners  # pyright: ignore[reportPrivateUsage]
    assert all(pipe.handle is None for pipe in transport.pipes)


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
        self.drift: str | None = None
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
            if self.drift == "process":
                value._obj.process_memory += 4096
            elif self.drift == "job":
                value._obj.job_memory += 4096
            elif self.drift == "flags":
                value._obj.basic.flags ^= 0x2000
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
    monkeypatch.setattr(
        processes, "os", SimpleNamespace(name="nt", environ={"SYSTEMROOT": "C:/Windows"})
    )

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


@pytest.mark.parametrize("limit", [1, 8 * 1024**2, CHILD_MEMORY])
def test_explicit_job_commit_limit_is_set_and_read_back_before_and_after_bind(
    api: API, limit: int
) -> None:
    # These are API mock values, not selected production profiles or native runs.
    job = WindowsJob(commit_limit=limit)
    job.prepare()
    job.bind(123)
    limits = processes._Limits.from_buffer_copy(api.limits)  # pyright: ignore[reportPrivateUsage]
    assert limits.process_memory == limits.job_memory == limit
    assert limits.basic.flags == 0x100 | 0x200 | 0x2000
    assert [call.args[1] for call in api.QueryInformationJobObject.call_args_list] == [9, 9]
    api.IsProcessInJob.assert_called_once()


@pytest.mark.parametrize("limit", [True, False, 0, -1, CHILD_MEMORY + 1, 1.0, "4096", None])
def test_invalid_job_commit_limit_fails_before_os_calls(api: API, limit: object) -> None:
    with pytest.raises(ValueError, match="child commit limit"):
        WindowsJob(commit_limit=cast(int, limit))
    for function in vars(api).values():
        if isinstance(function, MagicMock):
            function.assert_not_called()


def test_job_commit_limit_is_keyword_only(api: API) -> None:
    with pytest.raises(TypeError):
        WindowsJob(4096)  # pyright: ignore[reportCallIssue] - invalid public call shape
    api.CreateJobObjectW.assert_not_called()


@pytest.mark.parametrize("failure", ["set", "limit", "assignment", "membership", "query", "open"])
def test_job_policy_or_query_failure_never_grants_binding(api: API, failure: str) -> None:
    job = WindowsJob()
    if failure == "set":
        api.SetInformationJobObject.return_value = 0
        api.SetInformationJobObject.side_effect = None
    elif failure == "limit":
        api.corrupt_limit = True
    elif failure == "query":
        api.QueryInformationJobObject.return_value = 0
        api.QueryInformationJobObject.side_effect = None
    if failure in ("set", "limit", "query"):
        with pytest.raises(TrackError, match="capacity"):
            job.prepare()
        job.kill()
        api.TerminateJobObject.assert_called_once_with(100, 1)
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


@pytest.mark.parametrize("stage", ["prepare", "bind"])
@pytest.mark.parametrize("failure", ["process", "job", "flags", "query"])
async def test_limit_drift_blocks_input_and_preserves_owned_cleanup(
    api: API, stage: str, failure: str
) -> None:
    job = WindowsJob(commit_limit=8 * 1024**2)
    if stage == "bind":
        job.prepare()
    if failure == "query":
        api.QueryInformationJobObject.return_value = 0
        api.QueryInformationJobObject.side_effect = None
    else:
        api.drift = failure
    raw = Process()
    raw.pid = 123  # pyright: ignore[reportAttributeAccessIssue] - synthetic subprocess
    child = Child(cast(asyncio.subprocess.Process, raw), job)
    operation = job.prepare if stage == "prepare" else child.bind
    with pytest.raises(TrackError, match="capacity"):
        operation()
    assert not child.bound
    with pytest.raises(TrackError, match="capacity"):
        await child.write(b"must not reach stdin")
    assert not raw.stdin.blocks
    if stage == "bind":
        api.CloseHandle.assert_called_once_with(200)
    child.terminate()
    child.kill()
    assert api.TerminateJobObject.call_count == 2
    assert raw.terminated == raw.killed == 1
    await child.close_input()


async def test_post_assignment_drift_retains_slot_and_audio_until_actual_cleanup(
    api: API, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tracks, "CLEANUP_SECONDS", 0.01)
    # Use only this fixture's declared source PCM: a full runtime PCM parent
    # plus8MiB correctly refuses at the unchanged40M ordinary gate first.
    rig = Rig(tmp_path, pcm_bytes=(400 + 8) * FRAME_BYTES)
    limit = 8 * 1024**2  # fake admission only; no production/native qualification
    allocation = rig.supervisor.envelope.allocation
    assert allocation is not None
    rig.supervisor.envelope = replace(
        rig.supervisor.envelope, allocation=replace(allocation, native=limit)
    )
    job = WindowsJob(commit_limit=limit)
    raw = Process()
    raw.pid = 123  # pyright: ignore[reportAttributeAccessIssue] - synthetic subprocess

    async def create(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        # Prepare succeeded, but a post-assignment read-back must now reject drift.
        api.drift = "job"
        api.active = 1
        return cast(asyncio.subprocess.Process, raw)

    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", create)
    rig.supervisor.boundary = lambda: job
    rig.supervisor.spawn = Child.launch
    rig.deck = TrackDeck(
        rig.track, rig.deck.program, rig.audio, rig.supervisor, input_factory=rig.input
    )
    rig.deck.start()
    await rig.deck.wait_ended()
    assert rig.deck.cleanup_failed
    assert raw.returncode == 0
    assert not raw.stdin.blocks
    assert not rig.source.closed  # the input factory was never reached
    reserved = rig.audio.reserved
    assert reserved.native == limit
    assert rig.slots.count == rig.supervisor.debt_count == 1
    assert not job.empty()
    api.CloseHandle.assert_called_once_with(200)  # only the temporary process handle
    api.active = 0  # the owned fake tree has now actually emptied
    await asyncio.gather(*tuple(rig.supervisor.cleanups))
    assert job.empty()
    assert api.CloseHandle.call_args_list[-1].args == (100,)
    assert rig.slots.count == rig.supervisor.debt_count == 0
    assert rig.deck.cleanup_failed  # later closure cannot erase the missed verdict
    assert rig.audio.reserved == reserved  # child release is not audio-storage proof
    await rig.source.aclose()  # its creator closes the never-transferred fixture


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


@pytest.mark.parametrize("drift", [None, "process", "job", "flags", "membership"])
def test_creation_time_membership_uses_captured_handle_without_reassignment(
    api: API, drift: str | None
) -> None:
    job = WindowsJob(commit_limit=8 * 1024**2)
    job.prepare()
    api.drift = drift
    api.member = drift != "membership"
    if drift is None:
        job.verify_process(321)
    else:
        with pytest.raises(TrackError, match="capacity"):
            job.verify_process(321)
    api.IsProcessInJob.assert_called_once()
    assert api.IsProcessInJob.call_args.args[:2] == (321, job.creation_handle)
    api.OpenProcess.assert_not_called()
    api.AssignProcessToJobObject.assert_not_called()
