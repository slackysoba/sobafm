"""Minimal native launch and per-child OS containment; no audio or credentials here."""

import asyncio
import contextlib
import ctypes
import io
import os
import signal
import sys
import threading
import uuid
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable

from sobafm.catalog_audio import TrackError, TrackFailure
from sobafm.native import VerifiedNativeRuntime
from sobafm.native_calls import BudgetExhaustedError, GuardedAPI

CHILD_MEMORY = 128 * 1024**2
PIPE_BLOCK_SIZE = 16 * 1024  # FFmpeg protocol operation size, not pipe capacity
PIPE_LIMIT = 16 * 1024  # flow control only; readiness needs a qualified overshoot envelope


def command(executable: Path) -> tuple[str, ...]:
    return (
        str(executable),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-f",
        "mp3",
        "-protocol_whitelist",
        "pipe",
        "-blocksize",
        str(PIPE_BLOCK_SIZE),
        "-i",
        "pipe:0",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "-blocksize",
        str(PIPE_BLOCK_SIZE),
        "pipe:1",
    )


def environment() -> dict[str, str]:
    if os.name == "nt":
        root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        return {"SystemRoot": root, "PATH": str(Path(root) / "System32")}
    return {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}


class Boundary(Protocol):
    def prepare(self) -> None: ...
    def bind(self, pid: int) -> None: ...
    def terminate(self, pid: int) -> None: ...
    def kill(self) -> None: ...
    def empty(self) -> bool: ...
    def close(self) -> None: ...


class Process(Protocol):
    def bind(self) -> None: ...
    async def write(self, chunk: bytes) -> None: ...
    async def close_input(self) -> None: ...
    async def read(self, pipe: int, size: int) -> bytes: ...
    async def wait(self) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def empty(self) -> bool: ...
    def close_pipes(self) -> bool: ...


@runtime_checkable
class StorageOwner(Protocol):
    """Terminal teardown of covered storage after all borrowers have joined.

    Completion acknowledges release; absence, failure or a pending operation keeps
    the reservation. Resource closure alone does not implement this contract.
    """

    async def release_storage(self) -> None: ...


type FailurePhase = Literal[
    "native-setup",
    "job-setup",
    "job-create",
    "job-limits",
    "job-verify",
    "pipe-stdin",
    "pipe-stdout",
    "pipe-stderr",
    "attributes",
    "create-process",
    "capture",
    "dispose-attributes",
    "close-child-endpoints",
    "adopt-pipes",
    "launch",
    "bind",
    "adopt-stdin",
    "adopt-stdout",
    "adopt-stderr",
    "verify-job",
    "resume",
    "close-thread",
    "write-request",
    "close-input",
    "stdout-read",
    "stderr-read",
    "wait-exit",
    "io-join",
]
type ErrorCategory = Literal[
    "control-budget",
    "memory",
    "timeout",
    "cancelled",
    "os",
    "capacity",
    "cleanup",
    "track-other",
    "validation",
    "runtime",
    "attribute",
    "type",
    "other",
]


@dataclass(frozen=True)
class FailureDiagnostic:
    phase: FailurePhase
    category: ErrorCategory
    win32_error: int | None


def error_category(error: BaseException) -> ErrorCategory:
    """A closed vocabulary; never retain exception names, text, args or traces."""
    if isinstance(error, BudgetExhaustedError):
        return "control-budget"
    if isinstance(error, MemoryError):
        return "memory"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, OSError):
        return "os"
    if isinstance(error, TrackError):
        if error.category == TrackFailure.CAPACITY:
            return "capacity"
        return "cleanup" if error.category == TrackFailure.CLEANUP else "track-other"
    if isinstance(error, ValueError):
        return "validation"
    if isinstance(error, RuntimeError):
        return "runtime"
    if isinstance(error, AttributeError):
        return "attribute"
    if isinstance(error, TypeError):
        return "type"
    return "other"


class Launch:
    """Retained positive handoff, not a universal negative-creation certificate.

    An injected factory must call begin immediately before invoking creation.
    Only a positively rejected pre-call path may certify not_invoked. Any other
    no-owner outcome stays unknown, including a factory that never marked begin.
    """

    def __init__(self, admitted: Callable[[], bool]) -> None:
        self._admitted = admitted
        self.invoked = self.not_invoked = False
        # A backend may already own setup resources while creation itself is
        # unresolved. That still bars replacement admission after a failed wait.
        self.unresolved = False
        self.child: Process | None = None
        self.pending: Child | None = None
        self.preparation: Process | None = None
        self.failure: FailureDiagnostic | None = None
        self._failure_lock = threading.Lock()

    def record_failure(self, phase: FailurePhase, error: BaseException) -> None:
        # Creation runs on its retained thread; cleanup must not replace the
        # first observed fault with a later, less informative close failure.
        with self._failure_lock:
            if self.failure is None:
                code = getattr(error, "winerror", None) if isinstance(error, OSError) else None
                code = code if type(code) is int and 0 <= code <= 0xFFFFFFFF else None
                self.failure = FailureDiagnostic(phase, error_category(error), code)

    def reject(self) -> None:
        if self.invoked:
            raise TrackError(TrackFailure.CLEANUP)
        self.not_invoked = True

    def begin(self) -> None:
        if self.invoked or self.not_invoked:
            raise TrackError(TrackFailure.CAPACITY)
        if not self._admitted():
            self.reject()
            raise TrackError(TrackFailure.CAPACITY)
        self.invoked = True
        self.not_invoked = False

    @property
    def owned(self) -> Process | None:
        if self.child is not None:
            return self.child
        if self.pending is not None and self.pending.captured:
            return self.pending
        return None

    @property
    def cleanup_owner(self) -> Process | None:
        return self.owned if self.owned is not None else self.preparation


class LinuxCgroup:
    """A new delegated cgroup, never the bot's group or a container-wide limit."""

    def __init__(self, parent: Path) -> None:
        self.parent = parent
        self.path: Path | None = None

    def prepare(self) -> None:
        mount = Path("/sys/fs/cgroup")
        if (
            os.name != "posix"
            or not self.parent.is_absolute()
            or self.parent.resolve() == mount
            or not self.parent.resolve().is_relative_to(mount)
            or not any(
                row.split()[1:3] == [str(mount), "cgroup2"]
                for row in Path("/proc/mounts").read_text().splitlines()
            )
        ):
            raise TrackError(TrackFailure.CAPACITY)
        path = self.parent.resolve() / f"sobafm-{uuid.uuid4().hex}"
        path.mkdir()
        self.path = path
        # Kernel-created control files must exist: never create ordinary stand-ins.
        for name, value in (("memory.max", str(CHILD_MEMORY)), ("memory.swap.max", "0")):
            with (path / name).open("r+") as stream:
                stream.write(value)
            if (path / name).read_text().strip() != value:
                raise TrackError(TrackFailure.CAPACITY)
        if not (path / "cgroup.kill").is_file():
            raise TrackError(TrackFailure.CAPACITY)

    def bind(self, pid: int) -> None:
        if self.path is None:
            raise TrackError(TrackFailure.CAPACITY)
        (self.path / "cgroup.procs").write_text(str(pid))
        if str(pid) not in (self.path / "cgroup.procs").read_text().split():
            raise TrackError(TrackFailure.CAPACITY)
        if (self.path / "memory.max").read_text().strip() != str(CHILD_MEMORY) or (
            self.path / "memory.swap.max"
        ).read_text().strip() != "0":
            raise TrackError(TrackFailure.CAPACITY)

    def terminate(self, pid: int) -> None:
        if not self.empty():
            with contextlib.suppress(ProcessLookupError):
                if os.name != "nt":
                    os.killpg(pid, signal.SIGTERM)

    def kill(self) -> None:
        if self.path is not None:
            (self.path / "cgroup.kill").write_text("1")

    def empty(self) -> bool:
        return (
            self.path is None
            or "populated 0" in (self.path / "cgroup.events").read_text().splitlines()
        )

    def close(self) -> None:
        if not self.empty():
            raise TrackError(TrackFailure.CLEANUP)
        if self.path is not None:
            self.path.rmdir()  # this instance's fresh, empty leaf only
            self.path = None


class _IO(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("min_working", ctypes.c_size_t),
        ("max_working", ctypes.c_size_t),
        ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _Limits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IO),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user", ctypes.c_int64),
        ("kernel", ctypes.c_int64),
        ("period_user", ctypes.c_int64),
        ("period_kernel", ctypes.c_int64),
        ("faults", wintypes.DWORD),
        ("total", wintypes.DWORD),
        ("active", wintypes.DWORD),
        ("terminated", wintypes.DWORD),
    ]


class WindowsJob:
    """Private Job handle with enforced memory limits, membership and tree queries."""

    def __init__(
        self,
        *,
        commit_limit: int = CHILD_MEMORY,
        operation_guard: Callable[[], None] | None = None,
    ) -> None:
        if type(commit_limit) is not int or not 0 < commit_limit <= CHILD_MEMORY:
            raise ValueError("invalid child commit limit")
        self._commit_limit = commit_limit
        self.operation_guard = operation_guard
        self.setup_phase: FailurePhase = "job-setup"
        self._handle: int | None = None
        self._api: Any = None  # typed at each explicit Win32 FFI boundary below

    def prepare(self) -> None:
        if os.name != "nt":
            raise TrackError(TrackFailure.CAPACITY)
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, arguments, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            (
                "SetInformationJobObject",
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            (
                "QueryInformationJobObject",
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            (
                "IsProcessInJob",
                [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
                wintypes.BOOL,
            ),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(api, name)
            function.argtypes, function.restype = arguments, result
        if self.operation_guard is not None:
            api = GuardedAPI(api, self.operation_guard)
        self._api = api
        self.setup_phase = "job-create"
        handle = api.CreateJobObjectW(None, None)
        if not handle:
            raise TrackError(TrackFailure.CAPACITY)
        self._handle = int(handle)
        limits = _Limits()
        limits.basic.flags = 0x100 | 0x200 | 0x2000  # process/job memory, kill-on-close
        limits.process_memory = limits.job_memory = self._commit_limit
        self.setup_phase = "job-limits"
        if not api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise TrackError(TrackFailure.CAPACITY)
        self.setup_phase = "job-verify"
        self._verify_limits()

    def _verify_limits(self) -> None:
        actual = _Limits()
        if (
            self._handle is None
            or not self._api.QueryInformationJobObject(
                self._handle, 9, ctypes.byref(actual), ctypes.sizeof(actual), None
            )
            or (
                actual.basic.flags != 0x100 | 0x200 | 0x2000
                or actual.process_memory != self._commit_limit
                or actual.job_memory != self._commit_limit
            )
        ):
            raise TrackError(TrackFailure.CAPACITY)

    @property
    def creation_handle(self) -> int:
        if self._handle is None:
            raise TrackError(TrackFailure.CAPACITY)
        return self._handle

    def verify_process(self, process: int) -> None:
        """Verify creation-time membership using the still-owned process handle."""
        member = wintypes.BOOL()
        if (
            not self._api.IsProcessInJob(process, self.creation_handle, ctypes.byref(member))
            or not member.value
        ):
            raise TrackError(TrackFailure.CAPACITY)
        self._verify_limits()

    def bind(self, pid: int) -> None:
        api, job = self._api, self._handle
        if job is None:
            raise TrackError(TrackFailure.CAPACITY)
        process = api.OpenProcess(0x100 | 0x1 | 0x1000, False, pid)
        if not process:
            raise TrackError(TrackFailure.CAPACITY)
        try:
            member = wintypes.BOOL()
            if (
                not api.AssignProcessToJobObject(job, process)
                or not api.IsProcessInJob(process, job, ctypes.byref(member))
                or not member.value
            ):
                raise TrackError(TrackFailure.CAPACITY)
            self._verify_limits()
        finally:
            api.CloseHandle(process)

    def terminate(self, pid: int) -> None:
        self.kill()  # Win32 has no SIGTERM; terminate the owned Job, including descendants.

    def kill(self) -> None:
        if self._handle is not None and not self._api.TerminateJobObject(self._handle, 1):
            raise TrackError(TrackFailure.CLEANUP)

    def empty(self) -> bool:
        if self._handle is None:
            return True
        value = _Accounting()
        if not self._api.QueryInformationJobObject(
            self._handle, 1, ctypes.byref(value), ctypes.sizeof(value), None
        ):
            raise TrackError(TrackFailure.CLEANUP)
        return value.active == 0

    def close(self) -> None:
        if not self.empty():
            raise TrackError(TrackFailure.CLEANUP)
        if self._handle is not None:
            if not self._api.CloseHandle(self._handle):
                raise TrackError(TrackFailure.CLEANUP)
            self._handle = None


class _ProcessTransport(Protocol):
    _transport: asyncio.SubprocessTransport


def _pipe_closed(handle: object) -> bool:
    if isinstance(handle, io.IOBase):
        return handle.closed
    if sys.platform == "win32":
        from asyncio.windows_utils import PipeHandle

        if isinstance(handle, PipeHandle):
            # Proactor pipes have no .closed attribute. This exact CPython
            # wrapper raises ValueError after its owned handle has closed.
            try:
                handle.fileno()
            except ValueError:
                return True
    return False


def close_pipes(process: asyncio.subprocess.Process) -> bool:
    """Confirm the owned CPython pipe handles actually closed, not just closing."""
    transport = cast(_ProcessTransport, process)._transport  # pyright: ignore[reportPrivateUsage] - narrow CPython pipe ownership adapter
    pipes = [transport.get_pipe_transport(index) for index in (0, 1, 2)]
    transport.close()
    return all(pipe is None or _pipe_closed(pipe.get_extra_info("pipe")) for pipe in pipes)


class InputCloser:
    """One owned close wait; cancelling a borrower cannot poison CPython's Future."""

    def __init__(self, writer: asyncio.StreamWriter | None) -> None:
        self._writer = writer
        self._waiter: asyncio.Task[None] | None = None

    async def close(self) -> None:
        if self._writer is None:
            return
        if self._waiter is None:
            self._writer.close()
            self._waiter = asyncio.create_task(self._writer.wait_closed())
        # Subprocess StreamWriter.wait_closed directly awaits a shared protocol
        # Future. Keep its one waiter alive through feeder/caller cancellation;
        # the cleanup owner awaits this same task before refunding resources.
        await asyncio.shield(self._waiter)


class Child:
    """One awaited process and its owned pipes. Binding precedes every input write."""

    def __init__(self, process: asyncio.subprocess.Process | None, boundary: Boundary) -> None:
        self._process, self.boundary = process, boundary
        self.bound = False
        self._input_closer: InputCloser | None = None

    @property
    def captured(self) -> bool:
        return self._process is not None

    @property
    def process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise TrackError(TrackFailure.CLEANUP)
        return self._process

    @classmethod
    async def launch(
        cls, runtime: VerifiedNativeRuntime, boundary: Boundary, owner: Launch
    ) -> Child:
        try:
            # Construct and retain the adapter BEFORE creation. The raw returned
            # Process is assigned directly into it, before any further call/await.
            child = owner.pending = cls(None, boundary)
            boundary.prepare()
            argv, env = command(runtime.executable), environment()
            owner.begin()
        except Exception:
            owner.reject()  # no creation API has been invoked on this path
            raise
        child._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=PIPE_LIMIT,
            start_new_session=os.name != "nt",
        )
        return child

    def bind(self) -> None:
        self.boundary.bind(self.process.pid)
        self.bound = True

    async def write(self, chunk: bytes) -> None:
        if not self.bound or self.process.stdin is None:
            raise TrackError(TrackFailure.CAPACITY)
        self.process.stdin.write(chunk)
        await self.process.stdin.drain()

    async def close_input(self) -> None:
        if self._input_closer is None:
            self._input_closer = InputCloser(self.process.stdin)
        await self._input_closer.close()

    async def read(self, pipe: int, size: int) -> bytes:
        reader = self.process.stdout if pipe == 1 else self.process.stderr
        if reader is None:
            raise TrackError(TrackFailure.DECODER)
        return await reader.read(size)

    async def wait(self) -> int:
        return await self.process.wait()

    def terminate(self) -> None:
        try:
            if self.bound and self.process.returncode is None:
                self.boundary.terminate(self.process.pid)
            else:
                # Partial assignment can own a tree even when readback failed.
                # After leader exit, use that boundary rather than a stale PGID.
                self.boundary.kill()
        finally:
            if not self.bound and self.process.returncode is None:
                self.process.terminate()

    def kill(self) -> None:
        try:
            self.boundary.kill()
        finally:
            if self.process.returncode is None:
                self.process.kill()

    def empty(self) -> bool:
        return self.process.returncode is not None and self.boundary.empty()

    def close_pipes(self) -> bool:
        return close_pipes(self.process)


async def launch(runtime: VerifiedNativeRuntime, boundary: Boundary, owner: Launch) -> Process:
    if isinstance(boundary, WindowsJob):
        from sobafm.windows_decoder import WindowsProcess

        return await WindowsProcess.launch(runtime, boundary, owner)
    return await Child.launch(runtime, boundary, owner)
