"""Owned necessary-condition check, not an audio reservation or readiness profile."""

from __future__ import annotations

import asyncio
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
        self.boundary = (
            boundary if boundary is not None else WindowsJob(commit_limit=MAXIMUM_CANDIDATE)
        )
        self.launch = Launch(lambda: True)
        self.decision: asyncio.Future[Result] = asyncio.get_running_loop().create_future()
        self.exit_code: int | None = None
        self.released = False
        self.spawn = asyncio.ensure_future(spawn(self.argv, self.boundary, self.launch))
        self.retirement: asyncio.Task[None] | None = None
        self.task = asyncio.create_task(self._run())

    async def _read(self, child: Process, pipe: int, limit: int) -> bytes:
        result = bytearray()
        while block := await child.read(pipe, min(4096, limit + 1 - len(result))):
            result.extend(block)
            if len(result) > limit:
                raise ValueError("fixed output exceeded")
        return bytes(result)

    async def _run(self) -> None:
        status: Status = "launch-or-io-failed"
        workers: list[asyncio.Task[object]] = []
        try:
            async with asyncio.timeout(RUN_SECONDS):
                child = await asyncio.shield(self.spawn)
                self.launch.child = child
                child.bind()
                output = asyncio.create_task(self._read(child, 1, OUTPUT_LIMIT))
                errors = asyncio.create_task(self._read(child, 2, 4096))
                wait = asyncio.create_task(child.wait())
                workers.extend((output, errors, wait))
                await child.write(REQUEST)
                await child.close_input()
                await asyncio.gather(*workers)
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
        finally:
            self.retirement = asyncio.create_task(self._retire(workers, status))
            await asyncio.shield(self.retirement)

    async def _retire(self, workers: list[asyncio.Task[object]], status: Status) -> None:
        await self._cleanup(workers)
        if not self.decision.done():
            self.decision.set_result(Result(status, self.exit_code, self.released))

    def _late(self) -> None:
        if not self.decision.done():
            self.decision.set_result(Result("cleanup-timeout", self.exit_code, False))

    def _cleanup_failed(self) -> None:
        if not self.decision.done():
            self.decision.set_result(Result("cleanup-failed", self.exit_code, False))

    @staticmethod
    async def _discard(child: Process, pipe: int) -> None:
        while await child.read(pipe, 4096):
            pass

    async def _cleanup(self, workers: list[asyncio.Task[object]]) -> None:
        timer = asyncio.get_running_loop().call_later(CLEANUP_SECONDS, self._late)
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        # Cancellation never owns creation. A partial launch retains preparation.
        while not self.spawn.done() and self.launch.cleanup_owner is None:  # noqa: ASYNC110 - positive native handoff can precede task completion
            await asyncio.sleep(0.01)
        child = self.launch.cleanup_owner
        if child is None and self.spawn.done() and not self.spawn.cancelled():
            try:
                child = self.spawn.result()
            except Exception as error:  # noqa: BLE001 - preserve unknown creation
                error.__traceback__ = error.__context__ = error.__cause__ = None
        while child is None and not self.launch.not_invoked:
            self._cleanup_failed()
            await asyncio.sleep(0.01)
            child = self.launch.cleanup_owner
        if child is not None:
            cleaning: list[asyncio.Task[object]] = [
                asyncio.create_task(self._discard(child, 1)),
                asyncio.create_task(self._discard(child, 2)),
                asyncio.create_task(child.close_input()),
                asyncio.create_task(child.wait()),
            ]
            while True:
                try:
                    child.kill()
                    if (
                        self.spawn.done()
                        and all(task.done() for task in cleaning)
                        and child.empty()
                        and child.close_pipes()
                    ):
                        results = await asyncio.gather(*cleaning, return_exceptions=True)
                        if any(isinstance(item, BaseException) for item in results):
                            self._cleanup_failed()
                        if not isinstance(child, StorageOwner):
                            self._cleanup_failed()
                        else:
                            await child.release_storage()
                            break
                except Exception as error:  # noqa: BLE001 - retain owner on partial failure
                    error.__traceback__ = error.__context__ = error.__cause__ = None
                    self._cleanup_failed()
                await asyncio.sleep(0.01)
        await asyncio.gather(self.spawn, return_exceptions=True)
        while True:
            try:
                self.boundary.close()
                break
            except Exception as error:  # noqa: BLE001 - retain Job debt until real closure
                error.__traceback__ = error.__context__ = error.__cause__ = None
                self._cleanup_failed()
            await asyncio.sleep(0.01)
        self.released = True
        timer.cancel()
