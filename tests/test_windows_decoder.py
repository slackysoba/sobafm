"""Creation/adoption ownership through owned API fakes; never call Win32."""

import asyncio
import ctypes
import threading
from collections.abc import AsyncGenerator, Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import sobafm.track_deck as tracks
import sobafm.windows_decoder as windows
from sobafm.catalog_audio import TrackError, TrackFailure
from sobafm.decoder_process import Launch, WindowsJob, launch
from sobafm.native import VerifiedNativeRuntime
from tests.test_decoder_process import runtime as runtime
from tests.test_track_deck import Rig as DeckRig


class Job(WindowsJob):
    def __init__(self) -> None:
        super().__init__(commit_limit=8 * 1024**2)  # invented test profile only
        self.events: list[str] = []
        self.active = False
        self.fail_verify = self.fail_kill = False

    def prepare(self) -> None:
        self.events.append("prepare")

    @property
    def creation_handle(self) -> int:
        return 90

    def verify_process(self, process: int) -> None:
        assert process == 101
        self.events.append("verify")
        if self.fail_verify:
            raise TrackError(TrackFailure.CAPACITY)

    def kill(self) -> None:
        self.events.append("kill")
        if self.fail_kill:
            raise TrackError(TrackFailure.CLEANUP)
        self.active = False

    def empty(self) -> bool:
        return not self.active

    def close(self) -> None:
        assert not self.active
        self.events.append("job-close")


class API:
    def __init__(self, job: Job) -> None:
        self.job = job
        self.events: list[str] = []
        self.closed: list[int] = []
        self.fail = ""
        self.created = threading.Event()
        self.create_gate = threading.Event()
        self.create_gate.set()
        self.wait_gate = threading.Event()
        self.wait_gate.set()
        self.wait_entered = threading.Event()
        self.wait_calls = 0
        self.storage: windows.CreationStorage | None = None

    def pipe(self, endpoints: list[windows.Handle], index: int) -> None:
        self.events.append(f"pipe{index}")
        endpoints.append(windows.Handle(cast(windows.Kernel32, self), 10 + index * 2))
        if self.fail == f"pipe{index}":
            raise OSError("owned setup failure")
        endpoints.append(windows.Handle(cast(windows.Kernel32, self), 11 + index * 2))

    def attributes(self, storage: windows.CreationStorage, stdio: Sequence[int]) -> None:
        self.storage = storage
        self.events.append("attributes")
        storage.attributes = ctypes.create_string_buffer(128)
        storage.initialized = True
        storage.inherited[:] = stdio
        if self.fail == "attributes":
            raise OSError("owned attribute failure")

    def create(self, storage: windows.CreationStorage, info: windows.ProcessInfo) -> bool:
        self.events.append("create")
        assert storage is self.storage
        assert list(storage.jobs) == [90]
        assert list(storage.inherited) == [11, 13, 15]
        assert storage.attributes is not None
        assert not storage.disposed
        self.created.set()
        self.create_gate.wait()
        if self.fail == "create-unknown":
            raise OSError("not a negative creation certificate")
        if self.fail == "create-false":
            return False
        info.process, info.thread, info.pid, info.tid = 101, 102, 123, 456
        self.job.active = True
        if self.fail == "capture":
            raise OSError("after native output population")
        return True

    def dispose(self, storage: windows.CreationStorage) -> None:
        self.events.append("dispose")
        storage.delete_invoked = True
        if self.fail == "dispose":
            raise OSError("unknown void-call disposal")
        storage.disposed = True

    def resume(self, thread: int) -> None:
        assert thread == 102
        assert self.job.events[-1] == "verify"
        self.events.append("resume")
        if self.fail == "resume":
            raise TrackError(TrackFailure.CAPACITY)

    def wait(self, process: int) -> int:
        assert process == 101
        assert 101 not in self.closed
        self.wait_calls += 1
        self.wait_entered.set()
        self.wait_gate.wait()
        assert 101 not in self.closed
        self.events.append("reap")
        return 0

    def terminate(self, process: int) -> None:
        assert process == 101
        self.events.append("direct-kill")

    def close(self, handle: int) -> bool:
        if self.fail == f"close{handle}":
            return False
        assert handle not in self.closed
        self.closed.append(handle)
        return True


class Transport(asyncio.Transport):
    def __init__(self, protocol: windows.PipeProtocol, endpoint: windows.Handle) -> None:
        super().__init__()
        self.protocol, self.endpoint = protocol, endpoint
        self.blocks: list[bytes] = []
        self.closing = False
        self.release = True

    def write(self, data: Any) -> None:
        assert not self.closing
        self.blocks.append(bytes(data))

    def is_closing(self) -> bool:
        return self.closing

    def close(self) -> None:
        self.closing = True
        if self.release and not self.endpoint.closed:
            asyncio.get_running_loop().call_soon(self.finish)

    def finish(self) -> None:
        if not self.endpoint.closed:
            self.protocol.connection_lost(None)
            self.endpoint.close()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass


class Rig:
    def __init__(self) -> None:
        self.job = Job()
        self.api = API(self.job)
        self.transports: list[Transport] = []
        self.adopt_fail = -1
        self.adopt_gate = asyncio.Event()
        self.adopt_gate.set()
        self.owner = Launch(lambda: True)

    async def adopt(
        self, factory: Callable[[], windows.PipeProtocol], pipe: windows.Handle
    ) -> tuple[asyncio.Transport, windows.PipeProtocol]:
        protocol = factory()
        transport = Transport(protocol, pipe)
        self.transports.append(transport)
        protocol.connection_made(transport)
        await self.adopt_gate.wait()
        if len(self.transports) - 1 == self.adopt_fail:
            transport.close()  # public connection API closes a failed adoption
            raise OSError("owned partial adoption failure")
        return transport, protocol

    def child(self) -> windows.WindowsProcess:
        assert isinstance(self.owner.cleanup_owner, windows.WindowsProcess)
        return self.owner.cleanup_owner

    async def release(self) -> None:
        child = self.child()
        self.api.fail = ""
        self.api.create_gate.set()
        self.api.wait_gate.set()
        self.adopt_gate.set()
        child.kill()
        await child.wait()
        if not child.empty():
            child.kill()  # a late captured creation can follow the first tree kill
        await child.close_input()
        for transport in self.transports:
            transport.release = True
        async with asyncio.timeout(1):
            while not child.close_pipes():  # noqa: ASYNC110 - deterministic fake closure callbacks
                await asyncio.sleep(0)


@pytest.fixture
async def rig(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Rig]:
    rig = Rig()
    monkeypatch.setattr(windows, "Kernel32", lambda: rig.api)
    monkeypatch.setattr(
        windows, "environment", lambda: {"PATH": "C:/Windows/System32", "SystemRoot": "C:/Windows"}
    )
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "connect_read_pipe", rig.adopt)
    monkeypatch.setattr(loop, "connect_write_pipe", rig.adopt)
    yield rig
    rig.api.create_gate.set()
    rig.api.wait_gate.set()
    rig.adopt_gate.set()


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():  # noqa: ASYNC110 - bounded owned-fake interleaving
            await asyncio.sleep(0)


def test_native_layout_and_owned_mutable_storage() -> None:
    assert ctypes.sizeof(windows.ProcessInfo) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
    assert ctypes.sizeof(windows.StartupInfoEx) == (
        112 if ctypes.sizeof(ctypes.c_void_p) == 8 else 72
    )
    storage = windows.CreationStorage(
        [r"C:\spaced path\ffmpeg.exe", 'a"b', "tail\\"],
        {"SystemRoot": "C:/Windows", "PATH": "C:/Windows/System32"},
        90,
    )
    assert storage.command.value == '"C:\\spaced path\\ffmpeg.exe" a\\"b tail\\'
    assert storage.startup.startup.size == ctypes.sizeof(windows.StartupInfoEx)
    assert storage.startup.startup.flags == 0x100
    assert list(storage.jobs) == [90]
    assert windows.CREATION_FLAGS == 0x80404


async def test_windows_dispatch_verifies_suspended_then_allows_input(
    rig: Rig, runtime: VerifiedNativeRuntime
) -> None:
    child = await launch(runtime, rig.job, rig.owner)
    assert child is rig.child()
    assert rig.api.storage is not None
    assert rig.api.storage.command.value.startswith(str(runtime.executable))
    assert rig.api.events[:5] == ["pipe0", "pipe1", "pipe2", "attributes", "create"]
    assert rig.api.closed == [11, 13, 15]
    with pytest.raises(TrackError, match="capacity"):
        await child.write(b"not admitted")
    child.bind()
    assert rig.job.events == ["prepare", "verify"]
    assert rig.api.events[-1] == "resume"
    assert 102 in rig.api.closed
    await child.write(b"owned")
    assert rig.transports[0].blocks == [b"owned"]
    rig.child().readers[1].feed_data(b"pcm")
    rig.child().readers[1].feed_eof()
    assert await child.read(1, 3) == b"pcm"
    await rig.release()
    assert len(rig.api.closed) == 8


@pytest.mark.parametrize(
    "failure", ["pipe0", "pipe1", "pipe2", "attributes", "create-false", "capture"]
)
async def test_partial_setup_or_capture_failure_retains_cleanup_owner(
    rig: Rig, runtime: VerifiedNativeRuntime, failure: str
) -> None:
    rig.api.fail = failure
    with pytest.raises(TrackError):
        await windows.WindowsProcess.launch(runtime, rig.job, rig.owner)
    child = rig.child()
    assert not child.bound
    assert rig.owner.cleanup_owner is child
    assert child.process_handle is not None if failure == "capture" else child.known_absent
    assert not rig.owner.unresolved
    await rig.release()
    assert all(endpoint.closed for endpoint in child.endpoints)


@pytest.mark.parametrize("failure", ["resume", "close102", "verify"])
async def test_bind_failure_never_feeds_input(
    rig: Rig, runtime: VerifiedNativeRuntime, failure: str
) -> None:
    child = await windows.WindowsProcess.launch(runtime, rig.job, rig.owner)
    rig.api.fail = failure
    rig.job.fail_verify = failure == "verify"
    with pytest.raises(TrackError):
        child.bind()
    assert not child.bound
    with pytest.raises(TrackError):
        await child.write(b"never")
    assert not rig.transports[0].blocks
    await rig.release()


@pytest.mark.parametrize("index", [0, 1, 2])
async def test_partial_adoption_keeps_endpoint_and_callback_owner(
    rig: Rig, runtime: VerifiedNativeRuntime, index: int
) -> None:
    rig.adopt_fail = index
    with pytest.raises(OSError, match="owned partial adoption failure"):
        await windows.WindowsProcess.launch(runtime, rig.job, rig.owner)
    child = rig.child()
    assert child.process_handle is not None
    assert not child.bound
    await rig.release()
    assert all(p.lost for p in child.protocols.values())


async def test_creation_caller_cancel_does_not_abandon_worker(
    rig: Rig, runtime: VerifiedNativeRuntime
) -> None:
    rig.api.create_gate.clear()
    borrower = asyncio.create_task(windows.WindowsProcess.launch(runtime, rig.job, rig.owner))
    await until(rig.api.created.is_set)
    child = rig.child()
    borrower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await borrower
    assert child.creation is not None
    assert not child.creation.done()
    assert rig.owner.unresolved
    child.kill()
    rig.api.create_gate.set()
    await rig.release()
    assert child.creation.done()
    assert not child.creation.cancelled()


async def test_repeated_waiter_cancel_keeps_exact_process_handle(
    rig: Rig, runtime: VerifiedNativeRuntime
) -> None:
    child = await windows.WindowsProcess.launch(runtime, rig.job, rig.owner)
    rig.api.wait_gate.clear()
    borrower = asyncio.create_task(child.wait())
    await until(rig.api.wait_entered.is_set)
    borrower.cancel()
    borrower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await borrower
    assert child.waiter is not None
    assert not child.waiter.done()
    assert not child.close_pipes()
    assert 101 not in rig.api.closed
    await rig.release()
    assert rig.api.wait_calls == 1
    assert child.waiter.done()


async def test_delayed_actual_pipe_close_is_not_refunded(
    rig: Rig, runtime: VerifiedNativeRuntime
) -> None:
    child = await windows.WindowsProcess.launch(runtime, rig.job, rig.owner)
    child.bind()
    rig.transports[1].release = False
    child.kill()
    await child.wait()
    await child.close_input()
    assert not child.close_pipes()
    await asyncio.sleep(0)
    assert not child.close_pipes()
    assert not child.endpoints[2].closed
    rig.transports[1].release = True
    await rig.release()


async def test_unknown_create_exception_is_not_a_negative_certificate(
    rig: Rig, runtime: VerifiedNativeRuntime
) -> None:
    rig.api.fail = "create-unknown"
    with pytest.raises(TrackError):
        await windows.WindowsProcess.launch(runtime, rig.job, rig.owner)
    child = rig.child()
    assert rig.owner.invoked
    assert rig.owner.unresolved
    assert not child.known_absent
    waiter = asyncio.create_task(child.wait())
    await asyncio.sleep(0)
    assert not waiter.done()
    assert not child.empty()
    assert not child.close_pipes()
    waiter.cancel()  # fake teardown, never a release certificate
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert rig.owner.cleanup_owner is child
    assert rig.owner.unresolved


def test_real_ffi_call_arguments_keep_native_arrays_and_explicit_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_error = 0
    deletes = 0
    created_parents: list[tuple[Any, ...]] = []
    created_children: list[tuple[Any, ...]] = []
    updates: list[tuple[int, Any]] = []
    storage = windows.CreationStorage(
        [r"C:\spaced path\ffmpeg.exe", "-i", "pipe:0"],
        {"SystemRoot": "C:/Windows", "PATH": "C:/Windows/System32"},
        90,
    )

    def named(*args: Any) -> int:
        created_parents.append(args)
        return 8 + len(created_parents) * 2

    def file(*args: Any) -> int:
        created_children.append(args)
        security = args[3]._obj
        assert security.inherit == 1
        return 9 + len(created_children) * 2

    def connect(*args: Any) -> int:
        nonlocal last_error
        assert isinstance(args[1]._obj, windows.Overlapped)
        assert args[1]._obj.event == 80
        last_error = 535  # client connected before ConnectNamedPipe
        return 0

    def initialize(buffer: Any, count: int, flags: int, size: Any) -> int:
        nonlocal last_error
        assert count == 2
        assert flags == 0
        if buffer is None:
            last_error = 122
            size._obj.value = 128
            return 0
        assert ctypes.sizeof(buffer) == 128
        return 1

    def update(buffer: Any, flags: int, key: int, values: Any, size: int, *unused: Any) -> int:
        assert buffer is storage.attributes
        assert size == ctypes.sizeof(values)
        updates.append((key, values))
        return 1

    def create(*args: Any) -> int:
        assert args[0] == storage.application
        assert args[1] is storage.command
        assert args[2:4] == (None, None)
        assert args[4:6] == (True, windows.CREATION_FLAGS)
        assert args[6] is storage.environment
        assert args[7] is None
        assert args[8]._obj is storage.startup
        assert [list(values) for _, values in updates] == [[90], [11, 13, 15]]
        assert [key for key, _ in updates] == [windows.JOB_LIST, windows.HANDLE_LIST]
        info = args[9]._obj
        info.process, info.thread, info.pid, info.tid = 101, 102, 123, 456
        return 1

    def delete(buffer: Any) -> None:
        nonlocal deletes
        deletes += 1
        assert buffer is storage.attributes
        assert updates[0][1] is storage.jobs
        assert updates[1][1] is storage.inherited
        assert storage.command.value.endswith('"-i" pipe:0') is False

    dll = SimpleNamespace(
        CreateNamedPipeW=MagicMock(side_effect=named),
        CreateFileW=MagicMock(side_effect=file),
        ConnectNamedPipe=MagicMock(side_effect=connect),
        CreateEventW=MagicMock(return_value=80),
        InitializeProcThreadAttributeList=MagicMock(side_effect=initialize),
        UpdateProcThreadAttribute=MagicMock(side_effect=update),
        DeleteProcThreadAttributeList=MagicMock(side_effect=delete),
        CreateProcessW=MagicMock(side_effect=create),
        ResumeThread=MagicMock(return_value=1),
        WaitForSingleObject=MagicMock(return_value=0),
        GetExitCodeProcess=MagicMock(return_value=1),
        TerminateProcess=MagicMock(return_value=1),
        CloseHandle=MagicMock(return_value=1),
    )

    def library(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return dll

    monkeypatch.setattr(windows.ctypes, "WinDLL", library, raising=False)
    monkeypatch.setattr(windows.ctypes, "get_last_error", lambda: last_error, raising=False)
    api = windows.Kernel32()
    endpoints: list[windows.Handle] = []
    for index in (0, 1, 2):
        api.pipe(endpoints, index)
    assert [p[1] for p in created_parents] == [0x40080002, 0x40080001, 0x40080001]
    assert all(p[7] is None for p in created_parents)  # parent not inheritable
    assert all(p[2:4] == (8, 1) for p in created_parents)  # byte/local, one instance
    assert [p[1] for p in created_children] == [0x80000000, 0x40000000, 0x40000000]
    assert all(p[5] == 0 for p in created_children)  # synchronous child stdio
    api.attributes(storage, [endpoint.fileno() for endpoint in endpoints[1::2]])
    info = windows.ProcessInfo()
    assert api.create(storage, info)
    assert (info.process, info.thread, info.pid, info.tid) == (101, 102, 123, 456)
    api.dispose(storage)
    assert deletes == 1
    assert storage.disposed


@pytest.mark.parametrize("value", [0, 2, 0xFFFFFFFF])
def test_resume_requires_the_expected_single_suspend_count(value: int) -> None:
    api = object.__new__(windows.Kernel32)

    def resume(handle: int) -> int:
        return value

    api.api = SimpleNamespace(ResumeThread=resume)
    with pytest.raises(TrackError, match="capacity"):
        api.resume(102)


async def test_cancel_during_adoption_keeps_setup_task_and_endpoint_owners(
    rig: Rig, runtime: VerifiedNativeRuntime
) -> None:
    rig.adopt_gate.clear()
    borrower = asyncio.create_task(windows.WindowsProcess.launch(runtime, rig.job, rig.owner))
    await until(lambda: len(rig.transports) == 1)
    child = rig.child()
    borrower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await borrower
    assert child.adoption is not None
    assert not child.adoption.done()
    assert not child.bound
    child.kill()
    rig.adopt_gate.set()
    await rig.release()
    assert child.adoption.done()


async def test_void_attribute_disposal_exception_keeps_storage_and_debt(
    rig: Rig, runtime: VerifiedNativeRuntime
) -> None:
    rig.api.fail = "dispose"
    with pytest.raises(TrackError):
        await windows.WindowsProcess.launch(runtime, rig.job, rig.owner)
    child = rig.child()
    assert child.storage is not None
    assert not child.storage.disposed
    child.kill()
    await child.wait()
    await child.close_input()
    assert not child.close_pipes()
    assert not child.close_pipes()
    assert rig.api.events.count("dispose") == 1
    assert rig.owner.owned is child  # deliberate unresolved storage, no fake release


async def test_late_creation_keeps_trackdeck_debt_then_releases_only_child_slot(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tracks, "CLEANUP_SECONDS", 0.01)
    deck = DeckRig(tmp_path)
    deck.supervisor.boundary = lambda: rig.job
    deck.supervisor.spawn = launch
    rig.api.create_gate.clear()
    deck.deck.start()
    await until(rig.api.created.is_set)
    deck.deck.retire()
    await deck.deck.wait_ended()
    assert deck.deck.failure is TrackFailure.CLEANUP
    assert deck.slots.count == deck.supervisor.debt_count == 1
    assert not deck.supervisor.admitted(deck.audio)
    reserved = deck.audio.reserved
    assert reserved.total > 0
    assert not deck.source.closed  # the input factory was never reached
    rig.api.create_gate.set()
    async with asyncio.timeout(2):
        await asyncio.gather(*tuple(deck.supervisor.cleanups))
    assert deck.slots.count == deck.supervisor.debt_count == 0
    assert deck.audio.reserved == reserved
    assert deck.deck.cleanup_failed  # the original verdict never resets
    assert "job-close" in rig.job.events
    await deck.source.aclose()


async def test_proven_pre_call_failure_closes_preparation_before_any_refund(
    rig: Rig, tmp_path: Path
) -> None:
    deck = DeckRig(tmp_path)
    deck.supervisor.boundary = lambda: rig.job
    deck.supervisor.spawn = launch
    rig.api.fail = "attributes"
    deck.deck.start()
    await deck.deck.wait_ended()
    await asyncio.gather(*tuple(deck.supervisor.cleanups))
    assert "create" not in rig.api.events
    assert "job-close" in rig.job.events
    assert deck.slots.count == deck.supervisor.debt_count == 0
    assert deck.audio.reserved.total == 0  # positive no-invocation + all setup closed
    assert not deck.source.closed
    await deck.source.aclose()


async def test_first_stdin_close_cancel_keeps_one_shared_protocol_waiter(
    rig: Rig, runtime: VerifiedNativeRuntime
) -> None:
    child = await windows.WindowsProcess.launch(runtime, rig.job, rig.owner)
    child.bind()
    rig.transports[0].release = False
    first = asyncio.create_task(child.close_input())
    await until(lambda: rig.transports[0].closing)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not child.endpoints[0].closed
    rig.transports[0].release = True
    rig.transports[0].finish()  # unconditional real protocol Future completion
    await child.close_input()
    assert child.endpoints[0].closed
    await rig.release()


def test_uncertain_attribute_initialization_is_not_declared_disposed() -> None:
    api = object.__new__(windows.Kernel32)
    delete = MagicMock()
    api.api = SimpleNamespace(DeleteProcThreadAttributeList=delete)
    storage = windows.CreationStorage(["owned.exe"], {"PATH": "owned", "SystemRoot": "owned"}, 90)
    storage.attributes = ctypes.create_string_buffer(128)
    storage.initialize_invoked = True
    with pytest.raises(TrackError, match="cleanup"):
        api.dispose(storage)
    assert not storage.disposed
    delete.assert_not_called()


@pytest.mark.parametrize(("wait_result", "exit_result"), [(0xFFFFFFFF, 1), (258, 1), (0, 0)])
def test_bad_native_wait_or_exit_query_is_not_reaping_proof(
    wait_result: int, exit_result: int
) -> None:
    api = object.__new__(windows.Kernel32)
    api.api = SimpleNamespace(
        WaitForSingleObject=MagicMock(return_value=wait_result),
        GetExitCodeProcess=MagicMock(return_value=exit_result),
    )
    with pytest.raises(TrackError, match="cleanup"):
        api.wait(101)


def test_handle_close_exception_is_not_retried_against_a_reused_number() -> None:
    api = object.__new__(windows.Kernel32)
    close = MagicMock(side_effect=OSError("unknown close result"))
    api.api = SimpleNamespace(CloseHandle=close)
    handle = windows.Handle(api, 101)
    with pytest.raises(OSError, match="unknown close result"):
        handle.close()
    with pytest.raises(TrackError, match="cleanup"):
        handle.close()
    close.assert_called_once_with(101)
    assert handle.uncertain
    assert not handle.released


def test_unexpected_pending_connect_retains_its_native_storage_and_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = object.__new__(windows.Kernel32)
    close = MagicMock(return_value=1)
    api.api = SimpleNamespace(
        CreateNamedPipeW=MagicMock(return_value=10),
        CreateFileW=MagicMock(return_value=11),
        CreateEventW=MagicMock(return_value=80),
        ConnectNamedPipe=MagicMock(return_value=0),
        CloseHandle=close,
    )
    monkeypatch.setattr(windows.ctypes, "get_last_error", lambda: 997, raising=False)
    endpoints: list[windows.Handle] = []
    with pytest.raises(TrackError, match="capacity"):
        api.pipe(endpoints, 0)
    parent = endpoints[0]
    assert parent.overlapped is not None
    assert parent.connect_event is not None
    assert parent.uncertain
    with pytest.raises(TrackError, match="cleanup"):
        parent.close()
    assert not parent.released
    close.assert_not_called()
