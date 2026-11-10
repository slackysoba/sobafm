"""Exercise the case stimuli and observations without mpg123, an image or media."""

from __future__ import annotations

import hashlib
import io
import json
import select
import signal
import sys
from collections.abc import Buffer
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from evaluations.linux_resource import l3_decode
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


def test_pause_before_second_fifo_read_is_not_a_stalled_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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
        return PausedBeforeRead(
            source,
            command=python_child("import sys; sys.stdin.buffer.read()"),
            bank=bank,
            open_input=WaitForClose,
        )

    monkeypatch.setattr(l3_decode, "DecodeProducer", feeder)
    monkeypatch.setattr(l3_decode, "STIMULUS_SECONDS", 0.5)
    l3_decode.observe("feed", tmp_path)
    report, release = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert report["input_bytes"] == 1
    assert report["witness"] is None and report["cancelled_at"] is not None
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
        assert GLOBAL_PCM_BANKS.leased == 0
