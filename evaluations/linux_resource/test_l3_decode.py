"""Exercise the case stimuli and observations without mpg123, an image or media."""

from __future__ import annotations

import hashlib
import io
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
from collections.abc import Buffer
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from evaluations.linux_resource import l3_decode
from sobafm.crash_dumps import disable_crash_dumps
from sobafm.decode import DecodeInput, DecodeProducer, DecoderCommand
from sobafm.pcm import BANK_BYTES, FRAME_BYTES, GLOBAL_PCM_BANKS, PcmBank


def python_child(script: str) -> DecoderCommand:
    return DecoderCommand(argv=(sys.executable, "-I", "-c", script), limits=(), env={})


def one_byte(path: Path, source: DecodeInput) -> DecodeInput:
    path.write_bytes(b"x")
    return replace(source, path=path, size=1, sha256=hashlib.sha256(b"x").hexdigest())


@pytest.mark.parametrize(
    ("case", "failure", "witness"),
    [
        ("truncated", "input_truncated", None),
        ("corrupt", "empty_pcm", None),
        ("oversized", "input_oversized", None),
        ("feed", None, "second_read_on_input_fifo"),
        ("reader", None, "live_decoder_full_unread_bank"),
        ("stderr", None, None),
    ],
)
def test_case_observes_the_stimulus_and_refunds_both_leases(
    case: str,
    failure: str | None,
    witness: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def no_media(
        source: DecodeInput, *, command: DecoderCommand | None, bank: PcmBank
    ) -> DecodeProducer:
        if case == "stderr":
            assert command is not None and command.argv == ("/bin/sh", "-c", l3_decode.FLOOD)
        else:
            assert command is None
            child = "import os, sys\nsys.stdin.buffer.read()\n"
            if case == "reader":
                source = one_byte(tmp_path / "byte", source)
                child += "for _ in range(600): os.write(1, bytes(4096))\n"
            command = python_child(child)
        return DecodeProducer(source, command=command, bank=bank)

    monkeypatch.setattr(l3_decode, "DecodeProducer", no_media)
    assert l3_decode.observe(case, tmp_path) is None
    report, release = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert report["case"] == case and report["failure"] == failure and report["witness"] == witness
    assert (report["cancelled_at"] is not None) == (witness is not None)
    assert report["cancel_exit_expected"] is (True if witness else None)
    assert report["cleanup_deadline"] == report["cleanup_started_at"] + 3
    assert report["cleanup"] == "clean" and report["settled"]
    assert release["released_at"] <= report["cleanup_deadline"] and release["settled"]
    assert release["old_frames_preserved"] and release["end_of_stream"]
    assert release["diagnostic_frame"] == release["audible_drain"] == (case == "stderr")
    assert release["old_refunded"] and release["leases_left"] == 0


def unprivileged_feed_control(paused: bool) -> None:
    """Invoked only in an isolated subprocess. Use the candidate's UID and crash controls."""
    assert os.geteuid() == os.getegid() == 1000
    capabilities = next(
        row
        for row in Path("/proc/self/status").read_text().splitlines()
        if row.startswith("CapEff:")
    )
    assert int(capabilities.split()[1], 16) == 0
    disable_crash_dumps()
    pre_read = Event()

    class WaitForClose:
        def __init__(self, path: Path) -> None:
            self.file = io.FileIO(path, "r")
            self.first = True

        def readinto(self, buffer: Buffer, /) -> int | None:
            if not self.first:
                # Block in poll BEFORE entering read. HUP means the writer has closed.
                poller = select.poll()
                poller.register(self.file, select.POLLHUP)
                pre_read.set()
                assert poller.poll(3000)
            self.first = False
            return self.file.readinto(buffer)

        def close(self) -> None:
            self.file.close()

    class PausedBeforeRead(DecodeProducer):
        def start(self) -> None:
            super().start()
            assert pre_read.wait(3)

    def feeder(source: DecodeInput, *, command: None, bank: PcmBank) -> DecodeProducer:
        child = DecoderCommand(argv=("/bin/sh", "-c", "while read -r line; do :; done"), env={})
        if not paused:
            return DecodeProducer(source, command=child, bank=bank)
        return PausedBeforeRead(source, command=child, bank=bank, open_input=WaitForClose)

    with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory(dir="/tmp") as work:
        patch.setattr(l3_decode, "DecodeProducer", feeder)
        patch.setattr(l3_decode, "STIMULUS_SECONDS", 1)
        assert l3_decode.observe("feed", Path(work)) is None


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.skipif(
    os.geteuid() != 0 and (os.geteuid(), os.getegid()) != (1000, 1000),
    reason="needs UID/GID 1000 or a subprocess that can drop to them",
)
def test_feed_witness_under_target_identity(paused: bool) -> None:
    script = (
        "import sys; sys.path[:0] = ['/proc/self/cwd', '/proc/self/cwd/src']; "
        "from evaluations.linux_resource.test_l3_decode import unprivileged_feed_control; "
        f"unprivileged_feed_control({paused})"
    )
    # Exec as UID 1000, as the container does. The checkout's own files and environment
    # remain reachable from its inherited cwd even under a root-only parent directory.
    executable = Path(sys.executable)
    if executable.is_relative_to(Path.cwd()):
        executable = Path("/proc/self/cwd") / executable.relative_to(Path.cwd())
    result = subprocess.run(
        [executable, "-I", "-c", script],
        user=1000,
        group=1000,
        extra_groups=[] if os.geteuid() == 0 else None,
        env={},
        capture_output=True,
        check=True,
        timeout=8,
    )
    report, release = (json.loads(line) for line in result.stdout.splitlines())
    assert report["input_bytes"] == 1
    assert report["witness"] == (None if paused else "second_read_on_input_fifo")
    assert report["cancelled_at"] is not None
    assert release["leases_left"] == 0


@pytest.mark.parametrize("status", [0, -signal.SIGUSR1])
def test_child_exited_before_full_bank_observation(
    status: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class AlreadyExited(DecodeProducer):
        def start(self) -> None:
            super().start()
            pidfd = self._pidfd  # pyright: ignore[reportPrivateUsage]
            assert pidfd is not None
            poller = select.poll()
            poller.register(pidfd, select.POLLIN)
            assert poller.poll(3000)  # leave the leader unreaped

    def reader(source: DecodeInput, *, command: None, bank: PcmBank) -> DecodeProducer:
        script = (
            "import os, sys\nsys.stdin.buffer.read()\n"
            f"for _ in range({BANK_BYTES // FRAME_BYTES}): os.write(1, bytes({FRAME_BYTES}))\n"
            + (f"os.kill(os.getpid(), {-status})" if status else "")
        )
        return AlreadyExited(
            one_byte(tmp_path / "byte", source), command=python_child(script), bank=bank
        )

    monkeypatch.setattr(l3_decode, "DecodeProducer", reader)
    l3_decode.observe("reader", tmp_path)
    report, release = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert report["pcm"]["buffered_bytes"] == BANK_BYTES and report["returncode"] == status
    assert report["witness"] is None and release["leases_left"] == 0


def test_live_reader_with_unexplained_signal_fails_exit_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def reader(source: DecodeInput, *, command: None, bank: PcmBank) -> DecodeProducer:
        script = (
            "import os, signal, sys\nsys.stdin.buffer.read()\n"
            "signal.signal(signal.SIGTERM, lambda *_: os.kill(os.getpid(), signal.SIGUSR1))\n"
            f"for _ in range({BANK_BYTES // FRAME_BYTES}): os.write(1, bytes({FRAME_BYTES}))\n"
            "signal.pause()"
        )
        return DecodeProducer(
            one_byte(tmp_path / "byte", source), command=python_child(script), bank=bank
        )

    monkeypatch.setattr(l3_decode, "DecodeProducer", reader)
    l3_decode.observe("reader", tmp_path)
    report, release = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert report["witness"] == "live_decoder_full_unread_bank"
    assert report["returncode"] == -signal.SIGUSR1 and report["cancel_exit_expected"] is False
    assert release["leases_left"] == 0


def test_surviving_view_returns_debt_until_actual_refund(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    views: list[memoryview] = []

    def held(source: DecodeInput, *, command: DecoderCommand, bank: PcmBank) -> DecodeProducer:
        assert command.argv == ("/bin/sh", "-c", l3_decode.FLOOD)
        region = bank.reserve_write(1)
        assert region is not None
        views.append(region[:])
        bank.commit_write(0)
        return DecodeProducer(source, command=command, bank=bank)

    monkeypatch.setattr(l3_decode, "DecodeProducer", held)
    with ThreadPoolExecutor(1) as executor:
        observed = executor.submit(l3_decode.observe, "stderr", tmp_path)
        try:
            debt = observed.result(5)
            assert debt is not None
            report, release = (json.loads(line) for line in capsys.readouterr().out.splitlines())
            assert report["cleanup"] == "clean" and report["settled"]
            assert release["release_outstanding"]
            assert release["released_at"] is None and release["old_refunded"]
            assert release["leases_left"] == GLOBAL_PCM_BANKS.leased == 1
        finally:
            for view in views:
                view.release()
        assert debt.release_by(report["cleanup_deadline"])
        assert debt.released_at is not None and debt.released_at > report["cleanup_deadline"]
        assert debt.deadline_missed
        assert GLOBAL_PCM_BANKS.leased == 0


@pytest.mark.parametrize("held", ["candidate", "current"])
def test_last_refund_between_polls_exits_failed_even_when_settled(
    held: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    release_by = l3_decode.CaseOwners.release_by

    def cross_deadline(owners: l3_decode.CaseOwners, deadline: float) -> bool:
        bank = owners.candidate if held == "candidate" else owners.current
        backing = bank._backing  # pyright: ignore[reportPrivateUsage]
        assert backing is not None
        view = memoryview(backing)
        now = deadline - 0.001

        def wake_late(_delay: float) -> None:
            nonlocal now
            # The view goes away between polls. The bank is still charged until refund.
            assert GLOBAL_PCM_BANKS.leased == 1
            view.release()
            now = deadline + 0.031

        with view, pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                l3_decode, "time", SimpleNamespace(monotonic=lambda: now, sleep=wake_late)
            )
            assert release_by(owners, deadline)
            assert release_by(owners, deadline + 1)  # a recovery check cannot erase the miss
        return True

    monkeypatch.setattr(l3_decode.CaseOwners, "release_by", cross_deadline)
    # The stderr case always uses the no-media fd-2 shell witness.
    assert l3_decode.run_case("stderr", tmp_path) == 1
    report, release = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert report["cleanup"] == "clean" and report["settled"]
    assert release["released_at"] == report["cleanup_deadline"] + 0.031
    assert release["release_deadline_missed"] and not release["release_outstanding"]
    assert release["old_refunded"] and release["leases_left"] == 0
