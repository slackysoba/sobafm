"""Owned necessary-condition check, not an audio reservation or readiness profile."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sobafm.audio_worker import BOOTED, FRAME_SIZE, IMPORTED, REQUEST
from sobafm.decoder_process import Launch, Process, StorageOwner, WindowsJob
from sobafm.windows_decoder import WindowsProcess

# Optimistically ignore every outside charge. Success cannot qualify this value.
MAXIMUM_CANDIDATE = 70_000_000 - 34_560_000 - 1
RUN_SECONDS = 15.0
CLEANUP_SECONDS = 3.0
OUTPUT_LIMIT = len(BOOTED + IMPORTED) + FRAME_SIZE
OPERATION_LIMIT = 512
DRAIN_LIMIT = 64 * 1024
CLOSE_ATTEMPTS = 64
Status = Literal[
    "useful-path",
    "allocation-failed",
    "bootstrap-failed",
    "exit-inconclusive",
    "launch-or-io-failed",
    "run-timeout",
    "cleanup-timeout",
    "cleanup-failed",
]
Spawn = Callable[[tuple[str, ...], WindowsJob, Launch], Awaitable[Process]]


class BudgetExhaustedError(RuntimeError):
    pass


class ControlBudget:
    """One shared, thread-safe pre-call budget; exhaustion is irreversible."""

    def __init__(self) -> None:
        self.operations = OPERATION_LIMIT
        self.read_bytes = DRAIN_LIMIT
        self.exhausted = False
        self._lock = threading.Lock()

    def charge(self, size: int = 0) -> None:
        with self._lock:
            if self.exhausted or self.operations < 1 or size > self.read_bytes:
                self.exhausted = True
                raise BudgetExhaustedError("finite control budget exhausted")
            self.operations -= 1
            self.read_bytes -= size


async def retained_wait[T](future: asyncio.Future[T]) -> T:
    """Caller cancellation cannot transfer an owned operation to loop shutdown."""
    while True:
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.done():
                raise


@dataclass(frozen=True)
class Result:
    status: Status
    exit_code: int | None
    storage_released: bool
    cap_cause_proven: bool = False


def command(python: Path) -> tuple[str, ...]:
    if not python.is_absolute():
        raise ValueError("absolute owned interpreter required")
    return (str(python), "-I", "-B", "-m", "sobafm.audio_worker", "--bootstrap-check")


class FitCheck:
    """One launch with a sticky cleanup verdict and retained late-release owner.

    Callers keep this object and join task even after decision reports a timeout.
    No process result alone proves a Job memory-limit event caused failure.
    """

    def __init__(
        self,
        python: Path,
        *,
        spawn: Spawn = WindowsProcess.launch_command,
        boundary: WindowsJob | None = None,
    ) -> None:
        self.argv = command(python)
        self.budget = ControlBudget()
        self.boundary = (
            boundary if boundary is not None else WindowsJob(commit_limit=MAXIMUM_CANDIDATE)
        )
        self.boundary.operation_guard = self.budget.charge
        self.launch = Launch(lambda: True)
        self.decision: asyncio.Future[Result] = asyncio.get_running_loop().create_future()
        self.exit_code: int | None = None
        self.released = False
        self.parked = asyncio.Event()
        self._park = asyncio.Event()
        self.workers: list[asyncio.Task[object]] = []
        self.cleaning: list[asyncio.Task[object]] = []
        self.spawn = asyncio.ensure_future(spawn(self.argv, self.boundary, self.launch))
        self.task = asyncio.create_task(self._run())
        self.retirement = asyncio.create_task(self._retire())

    async def _read(self, child: Process, pipe: int, limit: int) -> bytes:
        result = bytearray()
        while True:
            size = min(4096, limit + 1 - len(result))
            self.budget.charge(size)
            block = await child.read(pipe, size)
            if not block:
                break
            result.extend(block)
            if len(result) > limit:
                raise ValueError("fixed output exceeded")
        return bytes(result)

    async def _run(self) -> Status:
        status: Status = "launch-or-io-failed"
        try:
            async with asyncio.timeout(RUN_SECONDS):
                child = await asyncio.shield(self.spawn)
                self.launch.child = child
                self.budget.charge()
                child.bind()
                output = asyncio.create_task(self._read(child, 1, OUTPUT_LIMIT))
                self.workers.append(output)
                errors = asyncio.create_task(self._read(child, 2, 4096))
                self.workers.append(errors)
                self.budget.charge()
                wait = asyncio.create_task(child.wait())
                self.workers.append(wait)
                self.budget.charge()
                await child.write(REQUEST)
                self.budget.charge()
                await child.close_input()
                await asyncio.gather(*self.workers)
                self.exit_code = wait.result()
                payload = output.result()
                if self.exit_code == 0 and payload == BOOTED + IMPORTED + bytes(FRAME_SIZE):
                    status = "useful-path"
                elif self.exit_code == 70 and payload == BOOTED + b"M":
                    status = "allocation-failed"
                elif self.exit_code == 71 and payload == BOOTED + b"E":
                    status = "bootstrap-failed"
                else:
                    status = "exit-inconclusive"
        except TimeoutError:
            status = "run-timeout"
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - fixed outcome
            error.__traceback__ = error.__context__ = error.__cause__ = None
        return status

    async def _retire(self) -> None:
        status: Status = "launch-or-io-failed"
        # The work task can be cancelled before its first instruction.
        with contextlib.suppress(asyncio.CancelledError):
            status = await asyncio.shield(self.task)
        try:
            await self._cleanup()
        except Exception as error:  # noqa: BLE001 - unexpected failure still owns every resource
            error.__traceback__ = error.__context__ = error.__cause__ = None
            await self._park_owned()
        if not self.decision.done():
            self.decision.set_result(Result(status, self.exit_code, self.released))

    async def join(self) -> None:
        await retained_wait(self.retirement)

    async def _park_owned(self) -> None:
        self._cleanup_failed()
        self.parked.set()
        await self._park.wait()  # no production setter: a parked case cannot retry

    def _late(self) -> None:
        if not self.decision.done():
            self.decision.set_result(Result("cleanup-timeout", self.exit_code, False))

    def _cleanup_failed(self) -> None:
        if not self.decision.done():
            self.decision.set_result(Result("cleanup-failed", self.exit_code, False))

    async def _discard(self, child: Process, pipe: int) -> None:
        while True:
            self.budget.charge(4096)
            if not await child.read(pipe, 4096):
                return

    async def _cleanup(self) -> None:
        timer = asyncio.get_running_loop().call_later(CLEANUP_SECONDS, self._late)
        if self.budget.exhausted:
            await self._park_owned()
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        # Cancellation never owns creation. A partial launch retains preparation.
        if not self.spawn.done() and self.launch.cleanup_owner is None:
            await asyncio.gather(self.spawn, return_exceptions=True)
        child = self.launch.cleanup_owner
        if child is None and self.spawn.done() and not self.spawn.cancelled():
            try:
                child = self.spawn.result()
            except Exception as error:  # noqa: BLE001 - preserve unknown creation
                error.__traceback__ = error.__context__ = error.__cause__ = None
        if child is None and not self.launch.not_invoked:
            await self._park_owned()
        if child is not None:
            try:
                self.budget.charge()
                child.kill()  # exactly one request; native fallbacks share the guard
                self.budget.charge()
                close_input = asyncio.create_task(child.close_input())
                self.cleaning.append(close_input)
                self.budget.charge()
                wait = asyncio.create_task(child.wait())
                self.cleaning.append(wait)
                self.cleaning.extend(
                    [
                        asyncio.create_task(self._discard(child, 1)),
                        asyncio.create_task(self._discard(child, 2)),
                    ]
                )
                await asyncio.gather(self.spawn, return_exceptions=True)
                results = await asyncio.gather(*self.cleaning, return_exceptions=True)
                if any(isinstance(item, BaseException) for item in results):
                    await self._park_owned()
                for _ in range(CLOSE_ATTEMPTS):
                    self.budget.charge()
                    empty = child.empty()
                    self.budget.charge()
                    if empty and child.close_pipes():
                        if not isinstance(child, StorageOwner):
                            await self._park_owned()
                        else:
                            self.budget.charge()
                            await child.release_storage()
                            break
                    await asyncio.sleep(0.01)
                else:
                    await self._park_owned()
            except Exception as error:  # noqa: BLE001 - no native retry after uncertainty
                error.__traceback__ = error.__context__ = error.__cause__ = None
                await self._park_owned()
        await asyncio.gather(self.spawn, return_exceptions=True)
        try:
            self.budget.charge()
            self.boundary.close()
        except Exception as error:  # noqa: BLE001 - no close/query retry on failure
            error.__traceback__ = error.__context__ = error.__cause__ = None
            await self._park_owned()
        self.released = True
        timer.cancel()
