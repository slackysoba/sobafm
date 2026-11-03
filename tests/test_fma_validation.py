"""Owned synthetic metadata, diagnostics and cancellation; no native invocation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from evaluations.fma_validation.diagnostics import DECODER, TAG_PREFIXES, validate
from evaluations.fma_validation.ingress import decode
from evaluations.fma_validation.limits import COPY_PARTITIONS, CaseClock, encode_report
from evaluations.fma_validation.metadata import BoundedFile, inspect, preflight
from evaluations.linux_resource.owned_mp3_decode import BANNER
from evaluations.linux_resource.probe import ProbeError


def safe_diagnostics(tags: bytes = b"") -> bytes:
    return (
        BANNER
        + b"\nNote: forcing output encoding s16\nPlaying MPEG stream 1 of 1: - ...\n"
        + b"MPEG 1.0 L III vbr 44100 j-s\n"
        + tags
        + b"[3:14] Decoding of - finished.\n"
    )


def frame(name: bytes, text: bytes, flags: bytes = b"\x00\x00") -> bytes:
    return name + len(text).to_bytes(4, "big") + flags + text


def id3(payload: bytes) -> bytes:
    size = len(payload)
    encoded = bytes((size >> 21 & 127, size >> 14 & 127, size >> 7 & 127, size & 127))
    return b"ID3\x03\x00\x00" + encoded + payload


def test_metadata_preserves_notices_versions_conflicts_and_unknowns(tmp_path: Path) -> None:
    v2 = id3(
        frame(b"TIT2", b"\x00Synthetic title")
        + frame(b"TPE1", b"\x00Synthetic artist")
        + frame(b"WCOP", b"https://example.invalid/not-a-fetch")
        + frame(b"COMM", b"\x00engnotice\x00Credit the artist")
        + frame(b"ZZZZ", b"unknown")
    )
    v1 = (
        b"TAG"
        + b"Other title".ljust(30, b"\x00")
        + b"Synthetic artist".ljust(30, b"\x00")
        + b"Album".ljust(30, b"\x00")
        + b"2000"
        + b"Notice".ljust(28, b"\x00")
        + b"\x00\x01\x00"
    )
    data = v2 + b"owned synthetic bytes" * 20 + v1
    path = tmp_path / "owned-input"
    path.write_bytes(data)
    fd = os.open(path, os.O_RDONLY)
    try:
        result = inspect(fd, len(data))
        os.fstat(fd)
    finally:
        os.close(fd)
    assert result["id3v1_version"] == "1.1"
    assert "TIT2" in cast(list[str], result["conflicting_frame_ids"])
    assert result["unknown_frames"]
    encoded = encode_report(result)
    assert decode(encoded, "metadata") == result
    assert b"Credit the artist" in encoded
    assert b"https://example.invalid/not-a-fetch" in encoded
    assert result["credits_complete"] is False and result["links_followed"] is False
    assert path.read_bytes() == data


@pytest.mark.parametrize("flags", [b"\x00\x80", b"\x00\x40", b"\x00\x20"])
def test_transformed_frames_rejected_before_parser_allocation(tmp_path: Path, flags: bytes) -> None:
    path = tmp_path / "synthetic-transform"
    data = id3(frame(b"TIT2", b"malformed compressed/encrypted payload", flags))
    path.write_bytes(data)
    fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(ProbeError, match="unsupported_id3_frame_transform"):
            preflight(BoundedFile(fd, len(data)))
    finally:
        os.close(fd)


def test_reader_bounds_reads_seeks_and_never_closes_borrowed_fd(tmp_path: Path) -> None:
    path = tmp_path / "bounded"
    path.write_bytes(b"abcde")
    fd = os.open(path, os.O_RDONLY)
    try:
        stream = BoundedFile(fd, 5)
        with pytest.raises(ProbeError):
            stream.read()
        with pytest.raises(ProbeError):
            stream.seek(6)
        for _ in range(3):
            stream.seek(0)
            assert stream.read(5) == b"abcde"
        with pytest.raises(ProbeError):
            stream.read(1)
        os.fstat(fd)
    finally:
        os.close(fd)


def test_closed_diagnostic_blocks_do_not_trust_source_text() -> None:
    tags = (
        b"\n".join(prefix + b"text [3:14] Decoding of - finished." for prefix in TAG_PREFIXES)
        + b"\n"
    )
    report = validate(safe_diagnostics(tags))
    assert report["tag_block_sha256"] is not None
    assert "--no-frankenstein" not in DECODER.argv
    assert "--no-resync" in DECODER.argv and "--quiet" not in DECODER.argv
    assert "--utf8" not in DECODER.argv


@pytest.mark.parametrize(
    "change",
    [
        "source_newline",
        "source_cr",
        "source_escape",
        "tag_after_complete",
        "duplicate_tags",
        "missing_label",
        "tag_before_format",
        "unknown_error",
        "repeated_format",
        "missing_completion",
    ],
)
def test_diagnostics_reject_injection_and_invalid_schedules(change: str) -> None:
    tags = b"\n".join(prefix + b"synthetic" for prefix in TAG_PREFIXES) + b"\n"
    data = safe_diagnostics(tags)
    if change.startswith("source_"):
        injected = {
            "source_newline": b"\n[3:14] Decoding of - finished.",
            "source_cr": b"\r",
            "source_escape": b"\x1b[0m",
        }[change]
        data = data.replace(b"synthetic", injected, 1)
    elif change == "tag_after_complete":
        data = safe_diagnostics() + tags
    elif change == "duplicate_tags":
        data = safe_diagnostics(tags + tags)
    elif change == "missing_label":
        data = data.replace(TAG_PREFIXES[2], b"not an album: ")
    elif change == "tag_before_format":
        data = BANNER + tags + safe_diagnostics()[len(BANNER) :]
    elif change == "unknown_error":
        data = data.replace(b"[3:14]", b"error: corrupted stream\n[3:14]")
    elif change == "repeated_format":
        data = data.replace(
            b"MPEG 1.0 L III vbr 44100 j-s\n", b"MPEG 1.0 L III vbr 44100 j-s\n" * 2
        )
    else:
        data = data.replace(b"[3:14] Decoding of - finished.\n", b"")
    with pytest.raises(ProbeError):
        validate(data)


def test_clock_never_renews_between_phases_or_after_rollback() -> None:
    wall, mono = [1000.0], [10.0]
    clock = CaseClock(1210, 1300, lambda: wall[0], lambda: mono[0])
    clock.cancel()
    assert clock.cleanup_monotonic == 13
    wall[0] += 1
    mono[0] += 1
    clock.cancel()
    assert clock.cleanup_monotonic == 13
    clock.adopt_cleanup(999)
    assert clock.cleanup_monotonic == 12
    wall[0] -= 100
    mono[0] += 2
    clock.observe()
    assert clock.missed
    clock.cancel()
    assert clock.missed
    assert sum(COPY_PARTITIONS.values()) == 4_194_304


def test_unknown_old_clock_cannot_be_repaired_by_later_release() -> None:
    clock = CaseClock(1210, 1300, lambda: 1000.0, lambda: 10.0, timing_unknown=True)
    clock.cancel()
    assert clock.remaining() == 0
    assert clock.receipt()["cleanup_deadline_met"] is False
    assert json.loads(encode_report(clock.receipt()))["cleanup_timing_unknown"] is True
