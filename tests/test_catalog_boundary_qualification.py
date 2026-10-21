"""Offline controls for WJ1's oracles and retained owner; never spawn a process."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from scripts.qualify_catalog_boundary import Case, ObservedChild
from sobafm.audio_budget import AudioBytes
from sobafm.catalog_audio import TrackError, TrackFailure
from sobafm.decoder_process import Child
from sobafm.track_deck import EndReason
from tests.test_decoder_check import Process


@pytest.mark.parametrize(
    ("job_empty", "stdout_eof", "stderr_eof", "expected"),
    [
        (False, False, False, True),
        (True, False, False, False),
        (False, True, False, False),
        (False, False, True, False),
    ],
)
async def test_descendant_requires_a_live_job_and_both_inherited_pipes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_empty: bool,
    stdout_eof: bool,
    stderr_eof: bool,
    expected: bool,
) -> None:
    case = Case("descendant", tmp_path, tmp_path / "never-executed.exe")
    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    if stdout_eof:
        stdout.feed_eof()
    if stderr_eof:
        stderr.feed_eof()
    process = cast(
        asyncio.subprocess.Process,
        SimpleNamespace(
            returncode=0,
            stdin=None,
            stdout=stdout,
            stderr=stderr,
        ),
    )
    case.child = ObservedChild(process, case.job)
    case.child.observed.extend(b"DH")
    case.spawned.set()
    monkeypatch.setattr(case.job, "empty", lambda: job_empty)
    await case.observe_descendant()
    assert case.descendant_observed is expected


async def test_missing_descendant_handshake_never_becomes_a_positive_control(
    tmp_path: Path,
) -> None:
    case = Case("descendant", tmp_path, tmp_path / "never-executed.exe")
    case.child = ObservedChild(cast(asyncio.subprocess.Process, Process()), case.job)
    case.child.observed.extend(b"D")
    case.spawned.set()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(case.observe_descendant(), 0.01)
    assert not case.descendant_observed


async def test_observed_child_still_uses_the_production_prebind_write_guard() -> None:
    process = Process()
    child = ObservedChild(cast(asyncio.subprocess.Process, process), MagicMock())
    with pytest.raises(TrackError, match="capacity"):
        await child.write(b"G")
    assert not process.stdin.blocks


@pytest.mark.parametrize("pipe", [1, 2])
async def test_oversize_fixture_output_fails_without_retaining_raw_content(
    monkeypatch: pytest.MonkeyPatch,
    pipe: int,
) -> None:
    async def oversized(self: Child, stream: int, size: int) -> bytes:
        return bytes(65)

    monkeypatch.setattr(Child, "read", oversized)
    child = ObservedChild(cast(asyncio.subprocess.Process, Process()), MagicMock())
    with pytest.raises(TrackError, match="output"):
        await child.read(pipe, 3840)
    assert not child.observed


@pytest.mark.parametrize("observer_raises", [False, True])
async def test_failed_decision_is_saved_before_waiting_and_cancellation_keeps_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observer_raises: bool,
) -> None:
    case = Case("ordering", tmp_path, tmp_path / "never-executed.exe")
    gate, saved = asyncio.Event(), asyncio.Event()
    token = case.rig.slots.acquire(case.rig.audio)
    assert token is not None
    case.rig.deck.cleanup_failed = True
    monkeypatch.setattr(case.rig.deck, "start", lambda: None)

    async def ended() -> None:
        pass

    monkeypatch.setattr(case.rig.deck, "wait_ended", ended)
    if observer_raises:
        case.child = ObservedChild(cast(asyncio.subprocess.Process, Process()), case.job)

        def uncertain() -> bool:
            raise OSError("untrusted fixture diagnostic")

        monkeypatch.setattr(case.child, "handles_closed", uncertain)

    async def cleanup() -> None:
        await gate.wait()
        case.rig.slots.release(token)

    owner = asyncio.create_task(cleanup())
    case.rig.supervisor.retain(owner)
    decisions: list[dict[str, object]] = []

    def emit(result: dict[str, object]) -> None:
        decisions.append(result)
        saved.set()

    running = asyncio.create_task(case.run(emit))
    await asyncio.wait_for(saved.wait(), 1)
    assert decisions[0]["status"] == "fail"
    if observer_raises:
        assert decisions[0]["reason"] == "observation_uncertain"
        assert "untrusted" not in str(decisions)
    else:
        assert decisions[0]["cleanup_deadline_failed"] is True
    assert not running.done()
    running.cancel()
    await asyncio.sleep(0)
    running.cancel()
    await asyncio.sleep(0)
    assert not owner.cancelled()
    assert case.rig.slots.count == 1
    gate.set()
    assert await asyncio.wait_for(running, 1) == "fail"
    assert not owner.cancelled()
    assert case.rig.slots.count == 0


@pytest.mark.parametrize(
    ("name", "category", "ending", "expected"),
    [
        ("ordering", TrackFailure.OUTPUT, EndReason.FAILED, "pass"),
        ("ordering", None, EndReason.FINISHED, "fail"),
        ("ordering", TrackFailure.DECODER, EndReason.FAILED, "fail"),
        ("memory", TrackFailure.DECODER, EndReason.FAILED, "pass"),
        ("memory", None, EndReason.FINISHED, "fail"),
        ("memory", TrackFailure.OUTPUT, EndReason.FAILED, "fail"),
    ],
)
async def test_invalid_pcm_must_reach_the_expected_production_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    category: TrackFailure | None,
    ending: EndReason,
    expected: str,
) -> None:
    case = Case(name, tmp_path, tmp_path / "never-executed.exe")
    case.child = ObservedChild(cast(asyncio.subprocess.Process, Process()), case.job)
    case.child.bound = case.child.prebind_refused = True
    case.child.observed.extend(b"O" if name == "ordering" else b"M")
    monkeypatch.setattr(case.child, "handles_closed", lambda: True)
    monkeypatch.setattr(case.rig.deck, "start", lambda: None)

    async def ended() -> None:
        pass

    monkeypatch.setattr(case.rig.deck, "wait_ended", ended)
    case.rig.deck.failure, case.rig.deck.end_reason = category, ending
    case.rig.deck.exit_status = 0 if name == "ordering" else 23
    case.rig.deck.allocation = case.rig.audio.reserve(AudioBytes(pcm=3840))
    decisions: list[dict[str, object]] = []
    assert await case.run(decisions.append) == expected
    assert decisions[0]["status"] == expected


async def test_dropped_fixture_charge_cannot_pass_a_mechanism_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = Case("ordering", tmp_path, tmp_path / "never-executed.exe")
    case.child = ObservedChild(cast(asyncio.subprocess.Process, Process()), case.job)
    case.child.bound = case.child.prebind_refused = True
    case.child.observed.extend(b"O")
    monkeypatch.setattr(case.child, "handles_closed", lambda: True)
    monkeypatch.setattr(case.rig.deck, "start", lambda: None)

    async def ended() -> None:
        pass

    monkeypatch.setattr(case.rig.deck, "wait_ended", ended)
    case.rig.deck.failure, case.rig.deck.end_reason = TrackFailure.OUTPUT, EndReason.FAILED
    case.rig.deck.exit_status = 0
    allocation = case.rig.audio.reserve(AudioBytes(pcm=3840))
    assert allocation is not None
    case.rig.deck.allocation = allocation
    allocation.release()
    decisions: list[dict[str, object]] = []
    assert await case.run(decisions.append) == "fail"
    assert decisions[0]["complete_audio_lease_still_charged"] is False
