"""Owned fakes exercise production launch/cleanup interfaces; no native execution."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import sobafm.decoder_process as processes
import sobafm.track_deck as tracks
from sobafm.audio_budget import AudioLedger
from sobafm.catalog_audio import TrackError, TrackFailure
from sobafm.decoder_process import Boundary, Child, Launch, Process
from sobafm.native import VerifiedNativeRuntime
from sobafm.track_deck import CloseOwner, EndReason, TrackDeck
from tests.test_decoder_check import Process as RawProcess
from tests.test_track_deck import FakeInput, Rig, settle


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():  # noqa: ASYNC110 - observe bounded interleaved fake state
            await asyncio.sleep(0)


async def leave_uncertain(rig: Rig) -> None:
    # Test teardown only: cancelling the fake loop owner is NOT resource release.
    for task in tuple(rig.supervisor.cleanups):
        task.cancel()
    await asyncio.gather(*tuple(rig.supervisor.cleanups), return_exceptions=True)
    assert rig.supervisor.debt_count > 0
    assert rig.slots.count > 0


@pytest.mark.parametrize("failure", ["exception", "cancellation", "unmarked-exception"])
async def test_unknown_launch_keeps_full_debt_and_bars_available_capacity(
    tmp_path: Path, failure: str
) -> None:
    rig = Rig(tmp_path)
    inputs = 0

    def source(_track: object) -> FakeInput:
        nonlocal inputs
        inputs += 1
        return rig.source

    async def spawn(_runtime: VerifiedNativeRuntime, boundary: Boundary, owner: Launch) -> Process:
        boundary.prepare()
        if failure != "unmarked-exception":
            owner.begin()
        if failure == "cancellation":
            raise asyncio.CancelledError
        raise OSError("not a negative creation certificate")

    rig.supervisor.spawn = spawn
    rig.deck._input_factory = source  # pyright: ignore[reportPrivateUsage]
    rig.deck.start()
    await rig.deck.wait_ended()
    reserved = rig.audio.reserved
    assert rig.deck.failure is TrackFailure.CLEANUP
    assert reserved.total > 0
    assert rig.slots.count == rig.supervisor.debt_count == 1
    assert not rig.boundary.closed  # empty boundary is not no-child proof
    assert inputs == 0
    assert not rig.source.closed  # creator still owns unused fixture
    for ledger in (rig.audio, AudioLedger()):
        other = TrackDeck(rig.track, rig.deck.program, ledger, rig.supervisor)
        other.start()
        assert other.failure is TrackFailure.CAPACITY
    assert rig.audio.reserved == reserved
    assert rig.slots.acquire(rig.audio) is None  # also bars a replacement supervisor's ledger
    unrelated = rig.slots.acquire(AudioLedger())
    assert unrelated is not None
    rig.slots.release(unrelated)
    await rig.source.aclose()  # explicit creator-side unused fixture closure
    await leave_uncertain(rig)


async def test_queued_creation_rechecks_uncertainty_before_api(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    first, second = asyncio.Event(), asyncio.Event()
    calls = api_calls = 0

    async def spawn(_runtime: VerifiedNativeRuntime, boundary: Boundary, owner: Launch) -> Process:
        nonlocal calls, api_calls
        calls += 1
        which = calls
        await (first if which == 1 else second).wait()
        owner.begin()
        api_calls += 1
        raise OSError("invoked unknown")

    rig.supervisor.spawn = spawn
    rig.deck.start()
    replacement = TrackDeck(rig.track, rig.deck.program, rig.audio, rig.supervisor)
    replacement.start()
    await until(lambda: calls == 2)
    first.set()
    await rig.deck.wait_ended()
    second.set()
    await replacement.wait_ended()
    await until(lambda: rig.slots.count == 1)
    assert api_calls == 1
    assert replacement.failure is TrackFailure.CAPACITY
    assert replacement.allocation is None  # positive pre-call rejection, no input
    assert rig.supervisor.debt_count == 1
    await leave_uncertain(rig)


@pytest.mark.parametrize("held_close", [False, True])
async def test_positive_pre_call_rejection_requires_boundary_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_close: bool
) -> None:
    monkeypatch.setattr(tracks, "CLEANUP_SECONDS", 0.01)
    rig = Rig(tmp_path)
    can_close = not held_close
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1
        if not can_close:
            raise OSError("boundary handle still owned")
        rig.boundary.closed = True

    async def spawn(_runtime: VerifiedNativeRuntime, boundary: Boundary, owner: Launch) -> Process:
        boundary.prepare()
        owner.reject()
        raise TrackError(TrackFailure.CAPACITY)

    monkeypatch.setattr(rig.boundary, "close", close)
    rig.supervisor.spawn = spawn
    rig.deck.start()
    await rig.deck.wait_ended()
    if held_close:
        assert rig.deck.cleanup_failed
        assert rig.slots.count == rig.supervisor.debt_count == 1
        assert rig.audio.reserved.total > 0
        can_close = True
    await asyncio.gather(*tuple(rig.supervisor.cleanups))
    assert close_calls > 0
    assert rig.boundary.closed
    assert rig.slots.count == rig.supervisor.debt_count == 0
    assert rig.audio.reserved.pcm == 34_560_000
    assert rig.audio.reserved.other == rig.audio.reserved.native == 0
    assert rig.deck.cleanup_failed is held_close
    assert not rig.source.closed
    assert not rig.child.writes
    await rig.source.aclose()


@pytest.mark.parametrize("failure", ["handoff", "bind"])
async def test_raw_process_captured_before_further_handoff_or_bind_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    rig = Rig(tmp_path)
    raw = RawProcess()
    raw.pid = 123  # pyright: ignore[reportAttributeAccessIssue] - owned synthetic process
    owner_seen: Launch | None = None

    async def create(*_args: Any, **_kwargs: Any) -> asyncio.subprocess.Process:
        assert owner_seen is not None
        assert owner_seen.pending is not None
        assert not owner_seen.pending.captured
        assert owner_seen.invoked
        return cast(asyncio.subprocess.Process, raw)

    async def spawn(runtime: VerifiedNativeRuntime, boundary: Boundary, owner: Launch) -> Process:
        nonlocal owner_seen
        owner_seen = owner
        child = await Child.launch(runtime, boundary, owner)
        assert owner.owned is child
        assert child.process is raw
        if failure == "handoff":
            raise OSError("observer failed after positive process return")
        return child

    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", create)
    rig.supervisor.spawn = spawn
    rig.boundary.bind_error = failure == "bind"
    rig.deck.start()
    await settle(rig)
    assert raw.returncode == 0
    assert raw.terminated > 0
    assert all(pipe.closed for pipe in raw._transport.pipes)  # pyright: ignore[reportPrivateUsage]
    assert rig.boundary.closed
    assert rig.slots.count == rig.supervisor.debt_count == 0
    assert rig.audio.reserved.total > 0  # positive process/pipe storage is not refunded
    assert not raw.stdin.blocks
    assert not rig.source.closed
    await rig.source.aclose()


async def test_child_construction_failure_is_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    api_calls = 0

    async def create(*_args: Any, **_kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal api_calls
        api_calls += 1
        raise AssertionError("creation must not be invoked")

    class BrokenChild(Child):
        def __init__(self, process: asyncio.subprocess.Process | None, boundary: Boundary) -> None:
            raise RuntimeError("adapter construction failed")

    owner = Launch(lambda: True)
    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", create)
    with pytest.raises(RuntimeError, match="adapter construction"):
        await BrokenChild.launch(rig.supervisor.runtime, rig.boundary, owner)
    assert api_calls == 0
    assert owner.not_invoked
    assert owner.owned is None


async def test_late_handoff_after_repeated_borrower_cancel_releases_only_its_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tracks, "CLEANUP_SECONDS", 0.01)
    rig = Rig(tmp_path)
    rig.gate = asyncio.Event()
    unrelated = rig.slots.acquire(AudioLedger())
    assert unrelated is not None
    rig.deck.start()
    await until(lambda: rig.boundary.prepared)
    waiter = asyncio.create_task(rig.deck.wait_ended())
    await asyncio.sleep(0)
    waiter.cancel()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    rig.deck.retire()
    await rig.deck.wait_ended()
    assert not rig.supervisor.admitted(rig.audio)
    assert rig.slots.count == 2
    assert not rig.source.closed
    rig.gate.set()
    await asyncio.gather(*tuple(rig.supervisor.cleanups))
    assert rig.child.reaped
    assert rig.child.pipes_closed
    assert rig.supervisor.admitted(rig.audio)
    assert rig.slots.count == 1  # unrelated owner's token is untouched
    assert rig.deck.failure is TrackFailure.CLEANUP
    assert rig.deck.end_reason is EndReason.FAILED
    assert rig.audio.reserved.total > 0
    assert not rig.source.closed  # no factory call after retirement/failed launch wait
    rig.slots.release(unrelated)
    await rig.source.aclose()


async def test_close_owner_survives_borrower_cancel_without_duplicate_close() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def close() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    owner = CloseOwner(close)
    borrower = asyncio.create_task(owner.close())
    await entered.wait()
    borrower.cancel()
    borrower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await borrower
    assert not owner.succeeded
    second = asyncio.create_task(owner.close())
    release.set()
    await second
    await owner.close()
    assert owner.succeeded
    assert calls == 1


@pytest.mark.parametrize("kind", ["stdin", "encoded"])
@pytest.mark.parametrize("failure", [False, True])
async def test_owned_close_must_succeed_not_merely_set_legacy_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, failure: bool
) -> None:
    monkeypatch.setattr(tracks, "CLEANUP_SECONDS", 0.01)
    rig = Rig(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def close() -> None:
        nonlocal calls
        calls += 1
        rig.source.closed = True  # deliberately misleading legacy flag
        rig.child.input_closed.set()
        entered.set()
        await release.wait()
        if failure:
            raise OSError("close did not prove release")

    if kind == "stdin":
        monkeypatch.setattr(rig.child, "close_input", close)
    else:
        monkeypatch.setattr(rig.source, "aclose", close)
    rig.deck.start()
    await entered.wait()
    rig.deck.retire()
    await rig.deck.wait_ended()
    assert rig.deck.cleanup_failed
    assert rig.slots.count == rig.supervisor.debt_count == 1
    assert calls == 1
    release.set()
    if failure:
        await asyncio.sleep(0.03)
        assert rig.slots.count == rig.supervisor.debt_count == 1
        assert rig.audio.reserved.total > 0
        await leave_uncertain(rig)
    else:
        await asyncio.gather(*tuple(rig.supervisor.cleanups))
        assert calls == 1
        assert rig.slots.count == rig.supervisor.debt_count == 0
        assert rig.deck.failure is TrackFailure.CLEANUP


async def test_production_prepare_failure_has_positive_pre_call_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    calls = 0

    def prepare() -> None:
        rig.boundary.prepared = True
        raise OSError("partial prepared boundary")

    async def create(*_args: Any, **_kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal calls
        calls += 1
        raise AssertionError("pre-call rejection must not invoke creation")

    rig.supervisor.spawn = Child.launch
    monkeypatch.setattr(rig.boundary, "prepare", prepare)
    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", create)
    rig.deck.start()
    await settle(rig)
    assert calls == 0
    assert rig.boundary.prepared
    assert rig.boundary.closed
    assert rig.slots.count == rig.supervisor.debt_count == 0
    assert rig.audio.reserved.pcm == 34_560_000
    assert rig.audio.reserved.other == rig.audio.reserved.native == 0
    assert not rig.source.closed  # no factory transfer, still creator owned
    await rig.source.aclose()


async def test_positive_child_cannot_be_refunded_as_uninvoked(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def spawn(_runtime: VerifiedNativeRuntime, boundary: Boundary, owner: Launch) -> Process:
        boundary.prepare()
        owner.reject()  # contradictory injected factory; positive child still wins
        return rig.child

    rig.supervisor.spawn = spawn
    rig.deck.start()
    await settle(rig)
    assert rig.child.reaped
    assert rig.source.closed
    assert rig.audio.reserved.total > 0
    assert rig.slots.count == rig.supervisor.debt_count == 0


def test_launch_certificate_cannot_be_reused_for_another_creation() -> None:
    owner = Launch(lambda: True)
    owner.begin()
    with pytest.raises(TrackError, match="capacity"):
        owner.begin()
    rejected = Launch(lambda: False)
    with pytest.raises(TrackError, match="capacity"):
        rejected.begin()
    assert rejected.not_invoked
    with pytest.raises(TrackError, match="capacity"):
        rejected.begin()


async def test_positive_process_cleanup_does_not_wait_for_factory_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tracks, "CLEANUP_SECONDS", 0.01)
    rig = Rig(tmp_path)
    raw = RawProcess()
    captured, handoff = asyncio.Event(), asyncio.Event()

    async def create(*_args: Any, **_kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, raw)

    async def spawn(runtime: VerifiedNativeRuntime, boundary: Boundary, owner: Launch) -> Process:
        child = await Child.launch(runtime, boundary, owner)
        captured.set()
        await handoff.wait()
        return child

    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", create)
    rig.supervisor.spawn = spawn
    rig.deck.start()
    await captured.wait()
    rig.deck.retire()
    await rig.deck.wait_ended()
    await until(lambda: raw.returncode == 0)
    assert raw.terminated > 0  # cleanup reaches the captured child before handoff
    assert rig.slots.count == rig.supervisor.debt_count == 1  # factory still pending
    assert rig.deck.failure is TrackFailure.CLEANUP
    handoff.set()
    await asyncio.gather(*tuple(rig.supervisor.cleanups))
    assert rig.slots.count == rig.supervisor.debt_count == 0
    assert rig.audio.reserved.total > 0
    assert rig.deck.failure is TrackFailure.CLEANUP
    assert not rig.source.closed
    await rig.source.aclose()
