import asyncio
import io
import threading
from pathlib import Path
from typing import Any, cast

import pytest

import sobafm.decoder_check as checks
from sobafm.catalog_audio import TrackError
from sobafm.native import SourceArchive, VerifiedNativeRuntime


class Writer:
    def __init__(self) -> None:
        self.blocks: list[bytes] = []
        self.closed = asyncio.Event()
        self.waiting = asyncio.Event()
        self.close_waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.defer_close = False
        self.wait_calls = 0

    def write(self, chunk: bytes) -> None:
        self.blocks.append(chunk)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        if not self.closed.is_set():
            self.closed.set()
            if not self.defer_close:
                asyncio.get_running_loop().call_soon(self.complete_close)

    def complete_close(self) -> None:
        # Match CPython SubprocessStreamProtocol's unconditional callback. A
        # poisoned/cancelled shared Future raises InvalidStateError here.
        self.close_waiter.set_result(None)

    async def wait_closed(self) -> None:
        self.wait_calls += 1
        self.waiting.set()
        await self.close_waiter


class Pipe(io.BytesIO):
    def get_extra_info(self, name: str) -> Pipe:
        assert name == "pipe"
        return self


class Transport:
    def __init__(self) -> None:
        self.pipes = [Pipe() for _ in range(3)]
        self.allow_close = True
        self.close_calls = 0
        self.closing = asyncio.Event()

    def get_pipe_transport(self, index: int) -> Pipe:
        return self.pipes[index]

    def close(self) -> None:
        self.close_calls += 1
        self.closing.set()
        for pipe in self.pipes:
            if self.allow_close:
                pipe.close()


class Process:
    def __init__(self, pcm: bytes = bytes(192000), stderr: bytes = b"", exit_code: int = 0) -> None:
        self.stdin = Writer()
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(pcm)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.returncode: int | None = None
        self.exit_code = exit_code
        self._transport = Transport()
        self.killed = self.terminated = 0
        self.wait_gate: asyncio.Event | None = None

    async def wait(self) -> int:
        if self.wait_gate is not None:
            await self.wait_gate.wait()
        await self.stdin.closed.wait()
        self.returncode = self.exit_code
        return self.exit_code

    def terminate(self) -> None:
        self.terminated += 1
        self.stdin.close()

    def kill(self) -> None:
        self.killed += 1
        self.stdin.close()


@pytest.fixture
def runtime(tmp_path: Path) -> VerifiedNativeRuntime:
    return VerifiedNativeRuntime(
        executable=tmp_path / "never-executed.exe",
        manifest_sha256="a" * 64,
        target="windows-amd64",
        source_bundle=SourceArchive(name="owned.zip", sha256="b" * 64, size=1),
    )


def test_four_owned_fixtures_are_small_fixed_and_distinct() -> None:
    fixtures = checks.fixtures()
    assert set(fixtures) == {"44100-mono", "44100-stereo", "48000-mono", "48000-stereo"}
    assert len(set(fixtures.values())) == 4
    for name, encoded in fixtures.items():
        size = 417 if name.startswith("44100") else 384
        assert len(encoded) == size * 40
        assert not any(encoded[4:size])


async def test_installed_check_uses_only_fixed_pipe_argv_and_finite_owned_input(
    runtime: VerifiedNativeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children: list[Process] = []

    async def spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        assert args[0] == str(runtime.executable)
        assert args[args.index("-i") + 1] == "pipe:0"
        assert args[-1] == "pipe:1"
        assert args[args.index("-protocol_whitelist") + 1] == "pipe"
        assert kwargs["limit"] == 16384
        assert "GEMINI_API_KEY" not in kwargs["env"]
        child = Process(bytes(200620 if len(children) < 2 else 184320))
        children.append(child)
        return cast(asyncio.subprocess.Process, child)

    monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    result = await checks.check_decoder(runtime)
    await asyncio.sleep(0)
    assert len(result) == 4
    assert not checks._owners  # pyright: ignore[reportPrivateUsage] - cleanup ownership regression
    for child, encoded in zip(children, checks.fixtures().values(), strict=True):
        assert b"".join(child.stdin.blocks) == encoded
        assert all(len(block) <= 16384 for block in child.stdin.blocks)
        assert all(pipe.closed for pipe in child._transport.pipes)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "failure", ["nonzero", "nonsilent", "oversize", "stderr", "unaligned", "short"]
)
async def test_capability_failure_never_becomes_success_or_raw_error(
    runtime: VerifiedNativeRuntime,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    pcm = bytes(200620)
    if failure == "nonsilent":
        pcm = b"\x01" + pcm[1:]
    elif failure == "oversize":
        pcm = bytes(checks.PCM_CAP + 4)
    elif failure == "unaligned":
        pcm = pcm[:-1]
    elif failure == "short":
        pcm = bytes(4)
    child = Process(
        pcm, b"private diagnostic" if failure == "stderr" else b"", int(failure == "nonzero")
    )

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, child)

    monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TrackError, match=r"^decoder$"):
        await checks.check_decoder(runtime)
    await asyncio.sleep(0)
    assert not checks._owners  # pyright: ignore[reportPrivateUsage]


async def test_repeated_cancellation_waits_for_owned_cleanup(
    runtime: VerifiedNativeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = Process()
    child.stdout = asyncio.StreamReader()

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, child)

    monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    caller = asyncio.create_task(checks.check_decoder(runtime))
    await asyncio.sleep(0.001)
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    assert not caller.done()
    # The reaped fake leaves stdout without EOF: transport closure and task
    # cancellation must still release a failed probe, never claim success.
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.sleep(0)
    assert not checks._owners  # pyright: ignore[reportPrivateUsage]
    assert all(pipe.closed for pipe in child._transport.pipes)  # pyright: ignore[reportPrivateUsage]


async def test_feeder_cancellation_cannot_poison_shared_stdin_close_future(
    runtime: VerifiedNativeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = Process()
    child.stdin.defer_close = True

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, child)

    monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    caller = asyncio.create_task(checks.check_decoder(runtime))
    await child.stdin.waiting.wait()
    caller.cancel()
    await asyncio.sleep(0.01)
    assert not child.stdin.close_waiter.cancelled()
    assert not caller.done()
    assert checks._owners  # pyright: ignore[reportPrivateUsage]
    child.stdin.complete_close()  # must not raise CPython's InvalidStateError
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.sleep(0)
    assert child.stdin.wait_calls == 1
    assert not checks._owners  # pyright: ignore[reportPrivateUsage]


async def test_failed_closure_blocks_another_check_until_actual_release(
    runtime: VerifiedNativeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checks, "CLEANUP_SECONDS", 0.02)
    child = Process(bytes(200620))
    child._transport.allow_close = False  # pyright: ignore[reportPrivateUsage]

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, child)

    monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TrackError, match="decoder"):
        await checks.check_decoder(runtime)
    with pytest.raises(TrackError, match="capacity"):
        await checks.check_decoder(runtime)
    child._transport.allow_close = True  # pyright: ignore[reportPrivateUsage]
    await asyncio.gather(*checks._owners)  # pyright: ignore[reportPrivateUsage]


async def test_startup_uses_reviewed_verifier_off_loop_and_keeps_failures_catalog_only(
    runtime: VerifiedNativeRuntime,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop_thread = threading.get_ident()
    threads: list[int] = []
    roots: list[tuple[Path, Path | None]] = []

    def verify(root: Path, *, registry: Path | None = None) -> VerifiedNativeRuntime:
        threads.append(threading.get_ident())
        roots.append((root, registry))
        return runtime

    async def probe(value: VerifiedNativeRuntime) -> dict[str, int]:
        assert value is runtime
        return {"owned": 1}

    monkeypatch.setattr(checks, "verify_approved_native_runtime", verify)
    monkeypatch.setattr(checks, "check_decoder", probe)
    root, registry = runtime.executable.parent, runtime.executable.parent.parent / "trust.json"
    assert await checks.prepare_decoder(root, registry) is runtime
    assert roots == [(root, registry)]
    assert threads[0] != loop_thread
    assert await checks.prepare_decoder(None, registry) is None

    def unavailable(*args: Any, **kwargs: Any) -> VerifiedNativeRuntime:
        raise OSError("private package path")

    monkeypatch.setattr(checks, "verify_approved_native_runtime", unavailable)
    assert await checks.prepare_decoder(root, registry) is None
    assert "private package path" not in caplog.text
    assert "Catalog decoder unavailable" in caplog.text


async def test_first_caller_cancel_during_cleanup_cannot_cancel_debt_owner(
    runtime: VerifiedNativeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checks, "CLEANUP_SECONDS", 0.03)
    child = Process(bytes(200620))
    transport = child._transport  # pyright: ignore[reportPrivateUsage]
    transport.allow_close = False

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, child)

    monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    caller = asyncio.create_task(checks.check_decoder(runtime))
    await transport.closing.wait()
    caller.cancel()  # first cancel arrives after normal work entered cleanup
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, 1)
    assert checks._owners  # pyright: ignore[reportPrivateUsage]
    transport.allow_close = True
    await asyncio.gather(*checks._owners)  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0)
    assert not checks._owners  # pyright: ignore[reportPrivateUsage]


async def test_kill_reap_subdeadline_does_not_get_a_fresh_three_seconds(
    runtime: VerifiedNativeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checks, "CHECK_SECONDS", 0.01)
    monkeypatch.setattr(checks, "TERMINATE_SECONDS", 0.01)
    monkeypatch.setattr(checks, "KILL_SECONDS", 0.01)
    child = Process()
    child.wait_gate = asyncio.Event()

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, child)

    monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TrackError, match="decoder"):
        await asyncio.wait_for(checks.check_decoder(runtime), 1)
    assert child.killed > 0
    assert checks._owners  # pyright: ignore[reportPrivateUsage]
    child.wait_gate.set()
    await asyncio.gather(*checks._owners)  # pyright: ignore[reportPrivateUsage]
