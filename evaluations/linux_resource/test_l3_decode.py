"""Exercise the case stimuli and observations without mpg123, an image or media."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from evaluations.linux_resource import l3_decode
from sobafm.decode import DecodeInput, DecodeProducer, DecoderCommand
from sobafm.pcm import PcmBank


@pytest.mark.parametrize(
    ("case", "failure", "witness"),
    [
        ("truncated", "input_truncated", None),
        ("corrupt", "empty_pcm", None),
        ("oversized", "input_oversized", None),
        ("feed", None, "one_byte_waiting_for_more"),
        ("reader", None, "full_unread_bank"),
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
                path = tmp_path / "byte"
                path.write_bytes(b"x")
                source = replace(source, path=path, size=1, sha256=hashlib.sha256(b"x").hexdigest())
                child += "for _ in range(600): os.write(1, bytes(4096))\n"
            command = DecoderCommand(argv=(sys.executable, "-I", "-c", child), limits=(), env={})
        return DecodeProducer(source, command=command, bank=bank)

    monkeypatch.setattr(l3_decode, "DecodeProducer", no_media)
    l3_decode.observe(case, tmp_path)
    report, release = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert report["case"] == case and report["failure"] == failure and report["witness"] == witness
    assert (report["cancelled_at"] is not None) == (witness is not None)
    assert report["cleanup_deadline"] == report["cleanup_started_at"] + 3
    assert report["cleanup"] == "clean" and report["settled"]
    assert release["released_at"] <= report["cleanup_deadline"] and release["settled"]
    assert release["old_frames_preserved"] and release["end_of_stream"]
    assert release["diagnostic_frame"] == release["audible_drain"] == (case == "stderr")
    assert release["old_refunded"] and release["leases_left"] == 0
