"""Finite WJ1 mechanism qualification; never grants FFmpeg/audio readiness.

Requires the locked development environment and an independently reviewed packet.
No native execution happens on import. The existing TrackDeck test rig supplies
explicitly synthetic admission metadata; its placeholder native identity is never
executed or reported as a verified decoder. Real Child/WindowsJob adapters and
the production cleanup owner operate the new isolated Python fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from sobafm.catalog_audio import TrackError, TrackFailure
from sobafm.decoder_process import (
    CHILD_MEMORY,
    PIPE_LIMIT,
    Boundary,
    Child,
    WindowsJob,
    _pipe_closed,  # pyright: ignore[reportPrivateUsage] - read-only pinned handle observer
    _ProcessTransport,  # pyright: ignore[reportPrivateUsage] - exact pinned transport shape
    environment,
)
from sobafm.native import VerifiedNativeRuntime
from sobafm.track_deck import EndReason
from tests.test_track_deck import Rig

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts/catalog_boundary_fixture.py"
CASES = ("ordering", "memory", "descendant")
PINS = (
    "docs/catalog-qualification.md",
    "scripts/qualify_catalog_boundary.py",
    "scripts/catalog_boundary_fixture.py",
    "src/sobafm/decoder_process.py",
    "src/sobafm/track_deck.py",
    "src/sobafm/audio_budget.py",
    "src/sobafm/catalog_audio.py",
    "src/sobafm/catalog.py",
    "src/sobafm/program.py",
    "src/sobafm/source.py",
    "src/sobafm/pcm.py",
    "tests/test_track_deck.py",
    "tests/test_catalog.py",
    "tests/test_decoder_check.py",
    "uv.lock",
)
type Status = Literal["pass", "fail", "incomplete"]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_commit() -> str:
    git = shutil.which("git")
    if git is None:
        raise ValueError("source identity unavailable")
    result = (
        subprocess.run(  # noqa: S603 - fixed read-only local Git query
            [git, "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        .stdout.decode("ascii")
        .strip()
    )
    if re.fullmatch(r"[a-f0-9]{40}", result) is None:
        raise ValueError("source identity unavailable")
    return result


class ObservedChild(Child):
    def __init__(self, process: asyncio.subprocess.Process, boundary: Boundary) -> None:
        super().__init__(process, boundary)
        self.observed = bytearray()
        self.stderr_count = 0
        self.prebind_refused = False
        self.signal = asyncio.Event()

    async def read(self, pipe: int, size: int) -> bytes:
        chunk = await super().read(pipe, min(size, 64))
        if pipe == 1:
            if len(self.observed) + len(chunk) > 64:
                raise TrackError(TrackFailure.OUTPUT)
            self.observed.extend(chunk)
            self.signal.set()
        else:
            self.stderr_count += len(chunk)
            if self.stderr_count > 64:
                raise TrackError(TrackFailure.OUTPUT)
        return chunk

    def handles_closed(self) -> bool:
        transport = cast(_ProcessTransport, self.process)._transport  # pyright: ignore[reportPrivateUsage] - never closes or changes a handle
        pipes = [transport.get_pipe_transport(index) for index in (0, 1, 2)]
        return all(pipe is None or _pipe_closed(pipe.get_extra_info("pipe")) for pipe in pipes)


class Case:
    def __init__(self, name: str, directory: Path, executable: Path) -> None:
        self.name, self.executable = name, executable
        self.rig = Rig(directory)
        self.job = WindowsJob()
        self.child: ObservedChild | None = None
        self.spawned = asyncio.Event()
        self.rig.source.blocks = [b"G"]
        self.rig.supervisor.boundary = lambda: self.job
        self.rig.supervisor.spawn = self.spawn
        self.descendant_observed = False

    async def spawn(self, runtime: VerifiedNativeRuntime, boundary: Boundary) -> ObservedChild:
        # The rig's synthetic runtime marker is intentionally unused. This is a
        # Python mechanism control, with no native supply/package assertion.
        boundary.prepare()
        process = await asyncio.create_subprocess_exec(
            str(self.executable),
            "-I",
            "-S",
            str(FIXTURE),
            self.name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment(),
            limit=PIPE_LIMIT,
        )
        child = self.child = ObservedChild(process, boundary)
        self.spawned.set()
        try:
            await child.write(b"G")
        except TrackError as error:
            child.prebind_refused = error.category is TrackFailure.CAPACITY
        return child

    async def observe_descendant(self) -> None:
        await self.spawned.wait()
        child = self.child
        if child is None:
            raise ValueError("fixture unavailable")
        while set(child.observed) != {ord("D"), ord("H")}:
            child.signal.clear()
            await child.signal.wait()
        # CPython wait() can await inherited pipe closure as well as leader exit.
        # Observe its separate returncode within the enclosing five-second bound.
        while child.process.returncode is None:  # noqa: ASYNC110 - no separate public leader-exit event
            await asyncio.sleep(0.01)
        stdout, stderr = child.process.stdout, child.process.stderr
        self.descendant_observed = (
            child.process.returncode == 0
            and not self.job.empty()
            and stdout is not None
            and not stdout.at_eof()
            and stderr is not None
            and not stderr.at_eof()
        )
        self.rig.deck.retire()

    async def run(self, emit: Callable[[dict[str, object]], None]) -> Status:
        status: Status = "fail"
        try:
            try:
                status = await self._run(emit)
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - uncertainty is a fixed failed observation
                error.__traceback__ = error.__context__ = error.__cause__ = None
                self.rig.deck.retire()
                emit(
                    {
                        "case": self.name,
                        "status": "fail",
                        "reason": "observation_uncertain",
                        "elapsed_ms": None,
                        "prebind_write_refused": None,
                        "bound": None,
                        "descendant_positive_control": None,
                        "exit_status": None,
                        "production_failure": None,
                        "cleanup_decision_ms": None,
                        "cleanup_deadline_failed": None,
                        "child_slot_released": None,
                        "pipe_handles_closed": None,
                        "job_handle_closed": None,
                        "complete_audio_lease_still_charged": None,
                        "catalog_ready": False,
                    }
                )
        finally:
            # Cover every post-start observation and reporting exception. A
            # report failure cannot let asyncio.run tear down a charged owner.
            if await self._retain_cleanup():
                status = "fail"
        return status

    async def _retain_cleanup(self) -> bool:
        cancelled = False
        while self.rig.supervisor.cleanups or self.rig.slots.count:
            tasks = tuple(self.rig.supervisor.cleanups)
            if tasks:
                join = asyncio.gather(*tasks, return_exceptions=True)
                while not join.done():
                    try:
                        await asyncio.shield(join)
                    except asyncio.CancelledError:
                        cancelled = True
            else:
                # A completed task with charged capacity is not verified
                # release. Keep this process/rig/Job alive for attention.
                try:
                    await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    cancelled = True
        return cancelled

    async def _run(self, emit: Callable[[dict[str, object]], None]) -> Status:
        started = time.monotonic()
        failure = "none"
        self.rig.deck.start()
        try:
            async with asyncio.timeout(5):
                if self.name == "descendant":
                    await self.observe_descendant()
                await self.rig.deck.wait_ended()
        except TimeoutError:
            failure = "response_deadline"
            self.rig.deck.retire()
        except asyncio.CancelledError:
            failure = "external_cancellation"
            self.rig.deck.retire()
        except Exception as error:  # noqa: BLE001 - fixed result only; never persist native text
            error.__traceback__ = error.__context__ = error.__cause__ = None
            failure = "fixture_or_boundary_failure"
            self.rig.deck.retire()
        # wait_ended observes the production three-second decision. It never
        # cancels the retained cleanup owner on a failed decision.
        ending = asyncio.create_task(self.rig.deck.wait_ended())
        while not ending.done():
            try:
                await asyncio.shield(ending)
            except asyncio.CancelledError:
                failure = "external_cancellation"
        ending.result()
        child = self.child
        expected = {
            "ordering": (b"O", 0, TrackFailure.OUTPUT),
            "memory": (b"M", 23, TrackFailure.DECODER),
        }
        observed_ok = child is not None and child.prebind_refused and child.bound
        if child is not None and self.name in expected:
            output, code, category = expected[self.name]
            observed_ok = (
                observed_ok and bytes(child.observed) == output and child.stderr_count == 0
            )
            observed_ok = observed_ok and self.rig.deck.exit_status == code
            observed_ok = observed_ok and self.rig.deck.failure is category
            observed_ok = observed_ok and self.rig.deck.end_reason is EndReason.FAILED
        elif self.name == "descendant":
            observed_ok = (
                observed_ok
                and self.descendant_observed
                and child is not None
                and bytes(child.observed) in {b"DH", b"HD"}
                and child.stderr_count == 0
                and self.rig.deck.end_reason is EndReason.RETIRED
                and self.rig.deck.failure is None
            )
        allocation = self.rig.deck.allocation
        charged = (
            allocation is not None
            and allocation.size is not None
            and self.rig.audio.reserved == allocation.size
        )
        observed_ok = observed_ok and not self.rig.deck.completed and charged
        job_closed = self.job._handle is None  # pyright: ignore[reportPrivateUsage] - read-only exact Job adapter observation
        pipes_closed = child is not None and child.handles_closed()
        clean = (
            not self.rig.deck.cleanup_failed
            and self.rig.slots.count == 0
            and job_closed
            and (child is None or pipes_closed)
        )
        if failure == "none" and not observed_ok:
            failure = "fixture_oracle_failed"
        if failure == "none" and not clean:
            failure = "cleanup_not_verified"
        status: Status = "pass" if failure == "none" and observed_ok and clean else "fail"
        if child is None and clean:
            status, failure = "incomplete", "native_launch_unavailable"
        cleanup_at = self.rig.deck._cleanup_at  # pyright: ignore[reportPrivateUsage] - original production cleanup origin, never reset
        decision: dict[str, object] = {
            "case": self.name,
            "status": status,
            "reason": failure,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "prebind_write_refused": child is not None and child.prebind_refused,
            "bound": child is not None and child.bound,
            "descendant_positive_control": self.descendant_observed
            if self.name == "descendant"
            else None,
            "exit_status": self.rig.deck.exit_status,
            "production_failure": self.rig.deck.failure,
            "cleanup_decision_ms": None
            if cleanup_at is None
            else round((time.monotonic() - cleanup_at) * 1000, 3),
            "cleanup_deadline_failed": self.rig.deck.cleanup_failed,
            "child_slot_released": self.rig.slots.count == 0,
            "pipe_handles_closed": pipes_closed if child is not None else None,
            "job_handle_closed": job_closed,
            "complete_audio_lease_still_charged": charged,
            "catalog_ready": False,
        }
        # Save a failed decision before the outer owner waits for actual release.
        emit(decision)
        return status


async def run(directory: Path) -> None:
    executable = Path(sys.base_prefix) / "python.exe"
    pins = {path: digest(ROOT / path) for path in PINS}
    header: dict[str, object] = {
        "protocol": "WJ1",
        "source_commit": source_commit(),
        "source_sha256": pins,
        "platform": "windows-amd64",
        "python_version": platform.python_version(),
        "parent_executable_sha256": digest(Path(sys.executable)),
        "fixture_executable_sha256": digest(executable),
        "runtime_kind": "owned_python_fixture_not_ffmpeg",
        "child_memory_bytes": CHILD_MEMORY,
        "overall": "incomplete",
        "catalog_ready": False,
    }
    (directory / "identity.json").write_text(json.dumps(header, indent=2) + "\n", encoding="utf-8")
    started = time.monotonic()
    owners: list[Case] = []
    stopped = False
    for name in CASES:
        result_path = directory / f"{name}.json"
        if stopped or time.monotonic() - started >= 30:
            result_path.write_text(
                json.dumps(
                    {
                        "case": name,
                        "status": "incomplete",
                        "reason": "batch_stopped",
                        "catalog_ready": False,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            continue
        case = Case(name, directory, executable)
        owners.append(case)

        def emit(result: dict[str, object], path: Path = result_path) -> None:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(result, indent=2) + "\n")

        stopped = await case.run(emit) != "pass"
    unchanged = all(digest(ROOT / path) == value for path, value in pins.items())
    (directory / "finished.json").write_text(
        json.dumps(
            {
                "source_unchanged": unchanged,
                "actual_cleanup_owners_finished": all(
                    not owner.rig.supervisor.cleanups for owner in owners
                ),
                "overall": "incomplete",
                "catalog_ready": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-wj1", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("WJ1 requires native Windows x86-64")
    directory = arguments.output.resolve()
    if not directory.is_relative_to((ROOT / ".local").resolve()):
        raise SystemExit("WJ1 requires a fresh task-local evidence directory")
    directory.mkdir(parents=True, exist_ok=False)
    asyncio.run(run(directory))


if __name__ == "__main__":
    main()
