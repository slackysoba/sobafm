"""Windows creation-time Job containment and owned public asyncio pipe adoption."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import subprocess
import threading
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sobafm.catalog_audio import TrackError, TrackFailure
from sobafm.decoder_process import PIPE_LIMIT, InputCloser, Launch, WindowsJob, command, environment
from sobafm.native import VerifiedNativeRuntime
from sobafm.native_calls import GuardedAPI

# Fixed-width Windows ABI fields also let owned fakes inspect layouts on Linux.
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
JOB_LIST = 0x0002000D
HANDLE_LIST = 0x00020002
CREATION_FLAGS = 0x00080000 | 0x00000004 | 0x00000400
# Windows-only ctypes entries are absent from Linux stubs. The fixed-width ABI
# declarations remain typed; only the lazy native function boundary is dynamic.
_windows_ctypes: Any = ctypes


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("size", DWORD), ("descriptor", ctypes.c_void_p), ("inherit", BOOL)]


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("size", DWORD),
        ("reserved", ctypes.c_wchar_p),
        ("desktop", ctypes.c_wchar_p),
        ("title", ctypes.c_wchar_p),
        *[
            (name, DWORD)
            for name in ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")
        ],
        ("show", ctypes.c_uint16),
        ("reserved_size", ctypes.c_uint16),
        ("reserved_data", ctypes.c_void_p),
        ("stdin", HANDLE),
        ("stdout", HANDLE),
        ("stderr", HANDLE),
    ]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [("startup", StartupInfo), ("attributes", ctypes.c_void_p)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE), ("pid", DWORD), ("tid", DWORD)]


class Offset(ctypes.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


class OffsetUnion(ctypes.Union):
    _fields_ = [("offset", Offset), ("pointer", ctypes.c_void_p)]


class Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("high", ctypes.c_size_t),
        ("offset", OffsetUnion),
        ("event", HANDLE),
    ]


class Handle:
    """One real endpoint; a failed/unknown close is never a release certificate."""

    def __init__(self, api: Kernel32, value: int | None) -> None:
        self.api, self.value = api, value
        self.closed = self.uncertain = False
        self.overlapped: Overlapped | None = None
        self.connect_event: Handle | None = None
        self.pipe_info: tuple[DWORD, DWORD, DWORD, DWORD] | None = None

    @property
    def released(self) -> bool:
        return self.closed and (self.connect_event is None or self.connect_event.released)

    def fileno(self) -> int:
        if self.closed or self.value is None:
            raise ValueError("closed owned handle")
        return self.value

    def close(self) -> None:
        if self.closed:
            if self.connect_event is not None:
                self.connect_event.close()
            return
        if self.uncertain:
            raise TrackError(TrackFailure.CLEANUP)
        if self.value is None:
            raise TrackError(TrackFailure.CLEANUP)
        try:
            closed = self.api.close(self.value)
        except BaseException:
            self.uncertain = True
            raise
        if not closed:
            raise TrackError(TrackFailure.CLEANUP)
        self.closed = True
        if self.connect_event is not None:
            self.connect_event.close()


class CreationStorage:
    """Native argument storage, retained through attribute-list disposal."""

    def __init__(self, argv: Sequence[str], env: Mapping[str, str], job: int) -> None:
        if not argv or any("\0" in arg for arg in argv):
            raise TrackError(TrackFailure.CAPACITY)
        line = subprocess.list2cmdline(argv)
        if len(line.encode("utf-16-le")) // 2 + 1 > 32767:
            raise TrackError(TrackFailure.CAPACITY)
        keys = {key.casefold(): key for key in env}
        if len(keys) != len(env) or set(keys) != {"path", "systemroot"}:
            raise TrackError(TrackFailure.CAPACITY)
        if any("\0" in value or "\0" in key or "=" in key for key, value in env.items()):
            raise TrackError(TrackFailure.CAPACITY)
        self.application = argv[0]
        self.command = ctypes.create_unicode_buffer(line)
        self.environment = ctypes.create_unicode_buffer(
            "\0".join(f"{keys[key]}={env[keys[key]]}" for key in sorted(keys)) + "\0\0"
        )
        self.jobs = (HANDLE * 1)(job)
        self.inherited = (HANDLE * 3)()
        self.startup = StartupInfoEx()
        self.startup.startup.size = ctypes.sizeof(StartupInfoEx)
        self.startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
        self.attributes: Any = None  # opaque native storage, not a Python handle tuple
        self.initialized = self.delete_invoked = self.disposed = False
        self.initialize_invoked = self.initialize_returned = False


class Kernel32:
    """The only DLL boundary; construction and creation run in an owned worker."""

    def __init__(self) -> None:
        self.api: Any = _windows_ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.c_void_p
        for name, args, result in (
            (
                "CreateNamedPipeW",
                [ctypes.c_wchar_p, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, pointer],
                HANDLE,
            ),
            (
                "CreateFileW",
                [ctypes.c_wchar_p, DWORD, DWORD, pointer, DWORD, DWORD, HANDLE],
                HANDLE,
            ),
            ("ConnectNamedPipe", [HANDLE, pointer], BOOL),
            ("GetNamedPipeInfo", [HANDLE, *[ctypes.POINTER(DWORD)] * 4], BOOL),
            ("CreateEventW", [pointer, BOOL, BOOL, ctypes.c_wchar_p], HANDLE),
            ("InitializeProcThreadAttributeList", [pointer, DWORD, DWORD, pointer], BOOL),
            (
                "UpdateProcThreadAttribute",
                [pointer, DWORD, ctypes.c_size_t, pointer, ctypes.c_size_t, pointer, pointer],
                BOOL,
            ),
            ("DeleteProcThreadAttributeList", [pointer], None),
            (
                "CreateProcessW",
                [
                    ctypes.c_wchar_p,
                    ctypes.c_wchar_p,
                    pointer,
                    pointer,
                    BOOL,
                    DWORD,
                    pointer,
                    ctypes.c_wchar_p,
                    pointer,
                    pointer,
                ],
                BOOL,
            ),
            ("ResumeThread", [HANDLE], DWORD),
            ("WaitForSingleObject", [HANDLE, DWORD], DWORD),
            ("GetExitCodeProcess", [HANDLE, pointer], BOOL),
            ("TerminateProcess", [HANDLE, DWORD], BOOL),
            ("CloseHandle", [HANDLE], BOOL),
        ):
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result

    def pipe(self, endpoints: list[Handle], index: int) -> None:
        # No retry/name fallback. The three fresh local pipe instances have
        # overlapped non-inherited parent ends and synchronous inherited stdio.
        name = rf"\\.\pipe\sobafm-{uuid.uuid4().hex}"
        writing = index == 0
        parent_owner = Handle(self, None)
        endpoints.append(parent_owner)
        parent_owner.value = self.api.CreateNamedPipeW(
            name,
            # Proactor's write transport also reads for the peer-close probe.
            # Match asyncio's duplex stdin while outputs stay read-only here.
            (3 if writing else 1) | 0x40000000 | 0x80000,
            8,
            1,
            PIPE_LIMIT,
            PIPE_LIMIT,
            0,
            None,
        )
        if parent_owner.value in (None, 0, ctypes.c_void_p(-1).value):
            parent_owner.closed = True  # documented invalid-handle result
            raise TrackError(TrackFailure.CAPACITY)
        security = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, 1)
        child_owner = Handle(self, None)
        endpoints.append(child_owner)
        child_owner.value = self.api.CreateFileW(
            name,
            0xC0000000 if writing else 0x40000000,
            0,
            ctypes.byref(security),
            3,
            0,
            None,
        )
        if child_owner.value in (None, 0, ctypes.c_void_p(-1).value):
            child_owner.closed = True
            raise TrackError(TrackFailure.CAPACITY)
        # The just-opened client already connected to this single instance.
        parent_owner.overlapped = Overlapped()
        parent_owner.connect_event = Handle(self, None)
        parent_owner.connect_event.value = self.api.CreateEventW(None, True, False, None)
        if not parent_owner.connect_event.value:
            parent_owner.connect_event.closed = True
            raise TrackError(TrackFailure.CAPACITY)
        parent_owner.overlapped.event = parent_owner.connect_event.fileno()
        parent_owner.uncertain = True
        if not self.api.ConnectNamedPipe(
            parent_owner.fileno(), ctypes.byref(parent_owner.overlapped)
        ):
            error: int = _windows_ctypes.get_last_error()
            if error != 535:
                # Unexpected pending I/O must retain its endpoint, event and
                # native OVERLAPPED; closing a handle is not completion proof.
                parent_owner.uncertain = error == 997
                raise TrackError(TrackFailure.CAPACITY)
        parent_owner.uncertain = False
        parent_owner.connect_event.close()
        # Retain every native output before querying the rounded base quotas.
        # Zero in an active direction permits allocation as needed, not a bound.
        information = parent_owner.pipe_info = (DWORD(), DWORD(), DWORD(), DWORD())
        flags, outgoing, incoming, instances = information
        if (
            not self.api.GetNamedPipeInfo(
                parent_owner.fileno(),
                ctypes.byref(flags),
                ctypes.byref(outgoing),
                ctypes.byref(incoming),
                ctypes.byref(instances),
            )
            or flags.value != 1  # byte-type, server end
            or instances.value != 1
            or not 0 < incoming.value <= PIPE_LIMIT
            or outgoing.value > PIPE_LIMIT
            or (writing and outgoing.value == 0)
        ):
            raise TrackError(TrackFailure.CAPACITY)

    def attributes(self, storage: CreationStorage, stdio: Sequence[int]) -> None:
        size = ctypes.c_size_t()
        if self.api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size)):
            raise TrackError(TrackFailure.CAPACITY)
        if _windows_ctypes.get_last_error() != 122 or not 0 < size.value <= 65536:
            raise TrackError(TrackFailure.CAPACITY)
        storage.attributes = ctypes.create_string_buffer(size.value)
        storage.initialize_invoked = True
        initialized = self.api.InitializeProcThreadAttributeList(
            storage.attributes, 2, 0, ctypes.byref(size)
        )
        storage.initialize_returned = True
        if not initialized:
            raise TrackError(TrackFailure.CAPACITY)
        storage.initialized = True
        storage.inherited[:] = stdio
        (
            storage.startup.startup.stdin,
            storage.startup.startup.stdout,
            storage.startup.startup.stderr,
        ) = stdio
        storage.startup.attributes = ctypes.addressof(storage.attributes)
        for key, value in ((JOB_LIST, storage.jobs), (HANDLE_LIST, storage.inherited)):
            if not self.api.UpdateProcThreadAttribute(
                storage.attributes, 0, key, value, ctypes.sizeof(value), None, None
            ):
                raise TrackError(TrackFailure.CAPACITY)

    def create(self, storage: CreationStorage, info: ProcessInfo) -> bool:
        return bool(
            self.api.CreateProcessW(
                storage.application,
                storage.command,
                None,
                None,
                True,
                CREATION_FLAGS,
                storage.environment,
                None,
                ctypes.byref(storage.startup),
                ctypes.byref(info),
            )
        )

    def dispose(self, storage: CreationStorage) -> None:
        if storage.initialize_invoked and not storage.initialize_returned:
            raise TrackError(TrackFailure.CLEANUP)
        if storage.initialized and not storage.disposed:
            if storage.delete_invoked:
                # An exception after a void deletion call cannot justify a
                # second deletion of possibly freed native attribute storage.
                raise TrackError(TrackFailure.CLEANUP)
            storage.delete_invoked = True
            self.api.DeleteProcThreadAttributeList(storage.attributes)
        storage.disposed = True

    def resume(self, thread: int) -> None:
        if self.api.ResumeThread(thread) != 1:
            raise TrackError(TrackFailure.CAPACITY)

    def wait(self, process: int) -> int:
        code = DWORD()
        if self.api.WaitForSingleObject(
            process, 0xFFFFFFFF
        ) != 0 or not self.api.GetExitCodeProcess(process, ctypes.byref(code)):
            raise TrackError(TrackFailure.CLEANUP)
        return code.value

    def terminate(self, process: int) -> None:
        if not self.api.TerminateProcess(process, 1):
            raise TrackError(TrackFailure.CLEANUP)

    def close(self, handle: int) -> bool:
        return bool(self.api.CloseHandle(handle))


class PipeProtocol(asyncio.StreamReaderProtocol):
    """Capture the public transport callback even if its adoption await fails."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        super().__init__(reader)
        self.transport: asyncio.BaseTransport | None = None
        self.lost = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        super().connection_made(transport)

    def connection_lost(self, exc: Exception | None) -> None:
        try:
            super().connection_lost(exc)
        finally:
            self.lost = True


class WindowsProcess:
    """One pre-call owner for setup, creation, adoption, native waits and pipes.

    It is published to Launch before work is queued. Owning this adapter is not
    proof that creation returned: unresolved remains charged and bars admission.
    """

    def __init__(self, boundary: WindowsJob, owner: Launch) -> None:
        self.boundary, self.owner = boundary, owner
        self.api: Kernel32 | None = None
        self.info = ProcessInfo()  # the native output stays owned before invocation
        self.storage: CreationStorage | None = None
        self.endpoints: list[Handle] = []
        self.process_handle: Handle | None = None
        self.thread_handle: Handle | None = None
        self.creation: asyncio.Task[None] | None = None
        self.adoption: asyncio.Task[None] | None = None
        self.waiter: asyncio.Task[int] | None = None
        self.stop = threading.Event()
        self.known_absent = True  # no invocation yet; changed before the FFI call
        self.bound = False
        self.failed = False
        self.returncode: int | None = None
        self.readers: dict[int, asyncio.StreamReader] = {}
        self.transports: dict[int, asyncio.BaseTransport] = {}
        self.protocols: dict[int, PipeProtocol] = {}
        self.writer: asyncio.StreamWriter | None = None
        self.closer: InputCloser | None = None
        self._storage_released = False

    @classmethod
    async def launch(
        cls, runtime: VerifiedNativeRuntime, boundary: WindowsJob, owner: Launch
    ) -> WindowsProcess:
        return await cls.launch_command(command(runtime.executable), boundary, owner)

    @classmethod
    async def launch_command(
        cls, argv: Sequence[str], boundary: WindowsJob, owner: Launch
    ) -> WindowsProcess:
        """Create suspended in the supplied Job; bind verifies it before execution."""
        child = cls(boundary, owner)
        owner.preparation = child
        child.creation = asyncio.create_task(
            asyncio.to_thread(child._create, tuple(argv), environment())
        )
        await asyncio.shield(child.creation)
        if child.failed:
            raise TrackError(TrackFailure.CAPACITY)
        child.adoption = asyncio.create_task(child._adopt())
        await asyncio.shield(child.adoption)
        return child

    def _capture(self) -> None:
        if self.info.process:
            self.owner.child = self  # positive output outranks any negative certificate
        api = self.api
        if api is not None:
            if self.info.process and self.process_handle is None:
                self.process_handle = Handle(api, self.info.process)
            if self.info.thread and self.thread_handle is None:
                self.thread_handle = Handle(api, self.info.thread)
        if (
            self.process_handle is not None
            and self.thread_handle is not None
            and self.info.pid
            and self.info.tid
        ):
            self.owner.unresolved = False

    def _create(self, argv: Sequence[str], env: Mapping[str, str]) -> None:
        try:
            self.api = Kernel32()
            if self.boundary.operation_guard is not None:
                self.api.api = GuardedAPI(self.api.api, self.boundary.operation_guard)
            self.boundary.prepare()
            for index in (0, 1, 2):
                self.api.pipe(self.endpoints, index)
            self.storage = CreationStorage(argv, env, self.boundary.creation_handle)
            self.api.attributes(self.storage, [self.endpoints[i].fileno() for i in (1, 3, 5)])
            if self.stop.is_set():
                raise TrackError(TrackFailure.CAPACITY)
            self.owner.begin()
            self.owner.unresolved = True
            self.known_absent = False
            returned = self.api.create(self.storage, self.info)
            self._capture()  # before disposal, endpoint closes, adoption or await
            if not returned:
                # A documented FALSE return with an empty output is a positive
                # no-process result. An exception is deliberately not equivalent.
                self.known_absent = not self.info.process and not self.info.thread
                if self.known_absent:
                    self.owner.unresolved = False
                raise TrackError(TrackFailure.CAPACITY)
            if (
                self.process_handle is None
                or self.thread_handle is None
                or not self.info.pid
                or not self.info.tid
            ):
                raise TrackError(TrackFailure.CAPACITY)
        except BaseException as error:  # noqa: BLE001 - retain native state on every worker outcome
            self.failed = True
            self._capture()
            if not self.owner.invoked:
                self.known_absent = True
                self.owner.reject()  # setup is still owned, but creation was never invoked
            error.__traceback__ = error.__context__ = error.__cause__ = None
        finally:
            if self.api is not None and self.storage is not None:
                try:
                    self.api.dispose(self.storage)
                except BaseException as error:  # noqa: BLE001 - failed disposal remains a cleanup obligation
                    self.failed = True
                    error.__traceback__ = error.__context__ = error.__cause__ = None
            # Only the creating worker touches these parent copies of child stdio.
            for endpoint in self.endpoints[1::2]:
                try:
                    endpoint.close()
                except BaseException as error:  # noqa: BLE001 - failed closes remain owned
                    self.failed = True
                    error.__traceback__ = error.__context__ = error.__cause__ = None

    async def _adopt(self) -> None:
        loop = asyncio.get_running_loop()
        for index in (0, 1, 2):
            if self.stop.is_set():
                raise TrackError(TrackFailure.CAPACITY)
            reader = asyncio.StreamReader(limit=PIPE_LIMIT)
            protocol = PipeProtocol(reader)
            self.protocols[index] = protocol
            endpoint = self.endpoints[index * 2]
            self._charge()
            if index == 0:
                transport, _ = await loop.connect_write_pipe(
                    lambda protocol=protocol: protocol, endpoint
                )
                self.transports[index] = transport
                self.writer = asyncio.StreamWriter(transport, protocol, None, loop)
            else:
                transport, _ = await loop.connect_read_pipe(
                    lambda protocol=protocol: protocol, endpoint
                )
                self.transports[index] = transport
                self.readers[index] = reader

    def bind(self) -> None:
        if (
            self.bound
            or self.stop.is_set()
            or self.failed
            or self.api is None
            or self.process_handle is None
            or self.thread_handle is None
            or len(self.transports) != 3
        ):
            raise TrackError(TrackFailure.CAPACITY)
        self.boundary.verify_process(self.process_handle.fileno())
        self.api.resume(self.thread_handle.fileno())
        self.thread_handle.close()
        self.bound = True

    def _charge(self) -> None:
        if self.boundary.operation_guard is not None:
            self.boundary.operation_guard()

    async def write(self, chunk: bytes) -> None:
        if not self.bound or self.stop.is_set() or self.writer is None:
            raise TrackError(TrackFailure.CAPACITY)
        self._charge()
        self.writer.write(chunk)
        await self.writer.drain()

    async def _join_setup(self) -> None:
        for task in (self.creation, self.adoption):
            if task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.shield(task)

    async def close_input(self) -> None:
        await self._join_setup()
        if self.closer is None:
            self.closer = InputCloser(self.writer)
        self._charge()
        await self.closer.close()
        if self.endpoints:
            endpoint = self.endpoints[0]
            protocol = self.protocols.get(0)
            if self.writer is None and protocol is not None:
                if protocol.transport is not None:
                    self._charge()
                    protocol.transport.close()
                # Missing capture after an entered adoption remains unresolved.
            elif 0 not in self.transports:
                endpoint.close()
            while not endpoint.closed:
                self._charge()
                await asyncio.sleep(0.01)

    async def read(self, pipe: int, size: int) -> bytes:
        await self._join_setup()
        reader = self.readers.get(pipe)
        if reader is None:
            return b""  # no adoption: the still-owned raw endpoint is closed separately
        return await reader.read(size)

    async def wait(self) -> int:
        if self.creation is not None:
            await asyncio.shield(self.creation)
        if self.owner.unresolved and self.process_handle is None:
            await asyncio.Future[None]()  # no negative certificate after creation raised
        if self.process_handle is None:
            if self.known_absent:
                self.returncode = -1
                return -1
            raise TrackError(TrackFailure.CLEANUP)
        if self.waiter is None:
            api = self.api
            if api is None:
                raise TrackError(TrackFailure.CLEANUP)
            self.waiter = asyncio.create_task(
                asyncio.to_thread(api.wait, self.process_handle.fileno())
            )
        code = await asyncio.shield(self.waiter)
        self.returncode = code
        return code

    def terminate(self) -> None:
        self.kill()

    def kill(self) -> None:
        self.stop.set()
        try:
            self.boundary.kill()
        except Exception:
            if self.api is not None and self.process_handle is not None and self.returncode is None:
                self.api.terminate(self.process_handle.fileno())
            raise

    def empty(self) -> bool:
        return self.returncode is not None and not self.owner.unresolved and self.boundary.empty()

    def close_pipes(self) -> bool:
        if (
            self.creation is None
            or not self.creation.done()
            or (self.adoption is not None and not self.adoption.done())
            or not self.empty()
        ):
            return False
        for index, endpoint in enumerate(self.endpoints):
            protocol = self.protocols.get(index // 2) if index % 2 == 0 else None
            transport = protocol.transport if protocol is not None else None
            if transport is not None:
                self._charge()
                transport.close()
            elif protocol is not None:
                # An entered adoption with no capture is not proof that no
                # transport/overlapped I/O exists. Preserve this endpoint owner.
                return False
            else:
                endpoint.close()
        for handle in (self.thread_handle, self.process_handle):
            if handle is not None:
                if self.waiter is not None and not self.waiter.done():
                    return False
                handle.close()
        return (
            all(endpoint.released for endpoint in self.endpoints)
            and all(protocol.lost for protocol in self.protocols.values())
            and all(
                handle is None or handle.closed
                for handle in (self.process_handle, self.thread_handle)
            )
            and (self.storage is None or self.storage.disposed)
        )

    async def release_storage(self) -> None:
        """Retire terminal pipe backings after setup, I/O and native waits joined."""
        if self._storage_released:
            return
        if not self.close_pipes() or any(not reader.at_eof() for reader in self.readers.values()):
            raise TrackError(TrackFailure.CLEANUP)
        for task in (self.creation, self.adoption, self.waiter):
            if task is not None:
                if not task.done() or task.cancelled():
                    raise TrackError(TrackFailure.CLEANUP)
                if (error := task.exception()) is not None:
                    error.__traceback__ = error.__context__ = error.__cause__ = None
        for protocol in self.protocols.values():
            # StreamReaderProtocol.connection_lost already detaches its reader,
            # writer and transport. Drop our additional capture only after loss.
            protocol.transport = None
        self.readers.clear()
        self.protocols.clear()
        self.transports.clear()
        self.writer = self.closer = None
        self.creation = self.adoption = self.waiter = None
        self.storage = None
        self.endpoints.clear()
        self.process_handle = self.thread_handle = None
        self._storage_released = True
