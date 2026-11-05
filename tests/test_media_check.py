"""Pre-admission file checks: decoded-audio screening and embedded tags. Offline.

A small Python child stands in for mpg123, as in the decode tests. This doesn't qualify the
native decoder; the real run does that in the application's image.
"""

from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

import pytest

from sobafm.decode import DecodeInput, DecoderCommand
from sobafm.media_check import (
    MAX_SILENCE_SECONDS,
    DecodeCheck,
    TagError,
    check_decode,
    embedded_notices,
    read_tag_values,
    tag_conflict,
)
from sobafm.pcm import BYTES_PER_SECOND, FRAME_BYTES, GLOBAL_PCM_BANKS

BY_NC = "http://creativecommons.org/licenses/by-nc/3.0/"
LOUD_SAMPLE = struct.pack("<h", 12_000)
SILENT_SAMPLE = struct.pack("<h", 0)
FULL_SCALE = struct.pack("<h", 32_767)


def child(segments: list[tuple[bytes, int]], *, status: int = 0) -> DecoderCommand:
    """A decoder that drains its input, then writes each (sample, seconds) segment."""
    script = (
        "import os,sys\n"
        "while sys.stdin.buffer.read(65536):\n"
        "    pass\n"
        f"for sample, seconds in {segments!r}:\n"
        f"    frame = sample * ({FRAME_BYTES} // 2)\n"
        f"    for _ in range(seconds * {BYTES_PER_SECOND // FRAME_BYTES}):\n"
        "        os.write(1, frame)\n"
        f"sys.exit({status})\n"
    )
    return DecoderCommand(argv=(sys.executable, "-I", "-c", script), limits=(), env={})


def source(tmp_path: Path, seconds: int = 300) -> DecodeInput:
    path = tmp_path / "input.mp3"
    path.write_bytes(b"not really an mp3")
    data = path.read_bytes()
    return DecodeInput(
        path, len(data), hashlib.sha256(data).hexdigest(), seconds * BYTES_PER_SECOND
    )


def test_a_clean_decode_measures_its_length_and_settles(tmp_path: Path) -> None:
    check = check_decode(source(tmp_path), command=child([(LOUD_SAMPLE, 8)]))
    assert check.failure is None and check.problem is None
    assert check.pcm_bytes == 8 * BYTES_PER_SECOND
    assert GLOBAL_PCM_BANKS.leased == 0


def test_a_short_decode_is_read_through_its_verified_end(tmp_path: Path) -> None:
    check = check_decode(source(tmp_path), command=child([(LOUD_SAMPLE, 2)]))
    assert check.problem is None and check.pcm_bytes == 2 * BYTES_PER_SECOND


def test_a_failing_decoder_fails_the_check_and_still_settles(tmp_path: Path) -> None:
    check = check_decode(source(tmp_path), command=child([(LOUD_SAMPLE, 1)], status=3))
    assert check.problem == "decode failed: decoder_exit"
    assert GLOBAL_PCM_BANKS.leased == 0


def test_a_decoder_that_cannot_start_fails_the_check(tmp_path: Path) -> None:
    command = DecoderCommand(argv=("/nonexistent/decoder",), limits=(), env={})
    assert check_decode(source(tmp_path), command=command).problem == "decode failed: decoder_spawn"
    assert GLOBAL_PCM_BANKS.leased == 0


def test_output_past_the_bound_fails_the_check(tmp_path: Path) -> None:
    check = check_decode(source(tmp_path, seconds=1), command=child([(LOUD_SAMPLE, 2)]))
    assert check.problem == "decode failed: output_overrun"


def test_a_short_silence_passes_and_a_long_one_fails(tmp_path: Path) -> None:
    fine = check_decode(
        source(tmp_path), command=child([(LOUD_SAMPLE, 1), (SILENT_SAMPLE, 2), (LOUD_SAMPLE, 1)])
    )
    assert fine.problem is None and 1.9 < fine.longest_silence_seconds < 2.1
    long = check_decode(
        source(tmp_path),
        command=child([(LOUD_SAMPLE, 1), (SILENT_SAMPLE, int(MAX_SILENCE_SECONDS) + 1)]),
    )
    assert long.problem == "silence over 30 seconds"


def test_only_sustained_clipping_fails(tmp_path: Path) -> None:
    brief = check_decode(source(tmp_path), command=child([(LOUD_SAMPLE, 1), (FULL_SCALE, 1)]))
    assert brief.problem is None
    sustained = check_decode(source(tmp_path), command=child([(FULL_SCALE, 3)]))
    assert sustained.problem == "sustained full-scale clipping"


def test_problem_prefers_the_decode_failure() -> None:
    check = DecodeCheck("decoder_exit", 0, 99.0, 99.0)
    assert check.problem == "decode failed: decoder_exit"


def synchsafe(value: int) -> bytes:
    return bytes((value >> shift) & 0x7F for shift in (21, 14, 7, 0))


def text_frame(name: bytes, text: str, *, version: int = 3, flags: bytes = b"\x00\x00") -> bytes:
    body = b"\x03" + text.encode("utf-8") if version == 4 else b"\x00" + text.encode("latin-1")
    size = synchsafe(len(body)) if version == 4 else struct.pack(">I", len(body))
    return name + size + flags + body


def id3(frames: bytes, *, version: int = 3, flags: int = 0) -> bytes:
    return b"ID3" + bytes([version, 0, flags]) + synchsafe(len(frames)) + frames


def tagged(tmp_path: Path, tag: bytes, trailer: bytes = b"") -> Path:
    path = tmp_path / "tagged.mp3"
    path.write_bytes(tag + b"\xff\xfb" + b"\x00" * 400 + trailer)
    return path


def test_no_tag_means_no_embedded_notices(tmp_path: Path) -> None:
    assert read_tag_values(tagged(tmp_path, b"")) == []


@pytest.mark.parametrize("version", [3, 4])
def test_copyright_terms_and_comment_frames_are_read(tmp_path: Path, version: int) -> None:
    frames = (
        text_frame(b"TIT2", "Title is not a notice", version=version)
        + text_frame(b"TCOP", "2012 Artist", version=version)
        + text_frame(b"WCOP", "x", version=version)[:0]
        + b"WCOP"
        + (synchsafe(11) if version == 4 else struct.pack(">I", 11))
        + b"\x00\x00http://a.b/"
        + b"USER"
        + (synchsafe(9) if version == 4 else struct.pack(">I", 9))
        + b"\x00\x00\x00eng"
        + b"Terms"
        + b"COMM"
        + (synchsafe(11) if version == 4 else struct.pack(">I", 11))
        + b"\x00\x00\x00eng"
        + b"\x00"
        + b"A note"
    )
    values = read_tag_values(tagged(tmp_path, id3(frames, version=version)))
    assert ("copyright", "2012 Artist") in values
    assert ("copyright link", "http://a.b/") in values
    assert ("terms of use", "Terms") in values
    assert ("comment", "A note") in values
    assert not any("Title" in text for _kind, text in values)


def test_v22_tags_are_read(tmp_path: Path) -> None:
    body = b"\x002012 Old"
    frame = b"TCR" + len(body).to_bytes(3) + body
    assert read_tag_values(tagged(tmp_path, id3(frame, version=2))) == [("copyright", "2012 Old")]


def test_id3v1_comment_is_read(tmp_path: Path) -> None:
    trailer = b"TAG" + b"\x00" * 94 + b"All rights reserved".ljust(30, b"\x00") + b"\x00"
    assert read_tag_values(tagged(tmp_path, b"", trailer)) == [("comment", "All rights reserved")]


def test_unsynchronised_tag_is_read(tmp_path: Path) -> None:
    frame = text_frame(b"TCOP", "2012 Artist")
    tag = id3(frame.replace(b"\xff", b"\xff\x00"), flags=0x80)
    assert read_tag_values(tagged(tmp_path, tag)) == [("copyright", "2012 Artist")]


@pytest.mark.parametrize(
    "tag",
    [
        b"ID3" + bytes([9, 0, 0]) + synchsafe(0),
        b"ID3" + bytes([3, 0, 0]) + b"\x80\x00\x00\x00",
        b"ID3" + bytes([3, 0, 0]) + synchsafe(100),
        id3(b"!!!!" + struct.pack(">I", 1) + b"\x00\x00x"),
        id3(b"TCOP" + struct.pack(">I", 500) + b"\x00\x00x"),
        id3(text_frame(b"TCOP", "secret", flags=b"\x00\x80")),
        id3(b"TCOP" + struct.pack(">I", 2) + b"\x00\x00\x09x"),
    ],
)
def test_a_tag_that_cannot_be_read_is_an_error_not_an_empty_result(
    tmp_path: Path, tag: bytes
) -> None:
    with pytest.raises(TagError):
        read_tag_values(tagged(tmp_path, tag))


def test_unrelated_frames_may_be_large_or_compressed(tmp_path: Path) -> None:
    art = b"APIC" + struct.pack(">I", 20_000) + b"\x00\x80" + b"x" * 20_000
    tag = id3(art + text_frame(b"TCOP", "2012 Artist"))
    assert read_tag_values(tagged(tmp_path, tag)) == [("copyright", "2012 Artist")]


@pytest.mark.parametrize(
    "text",
    [
        "2012 Artist. All Rights Reserved.",
        "all  rights   reserved",
        "Licensed under http://creativecommons.org/licenses/by-nc-nd/3.0/",
        "See http://creativecommons.org/licenses/by-nc/2.5/ for terms",
        "http://creativecommons.org/publicdomain/zero/1.0/",
        "FMA-Limited: http://freemusicarchive.org/FMA_License",
        "URL: http://freemusicarchive.org/a Copyright: FMA-Limited: http://example.org/",
    ],
)
def test_conflicting_tags_are_named(text: str) -> None:
    assert tag_conflict([("copyright", text)], BY_NC) is not None


@pytest.mark.parametrize(
    "text",
    [
        "2012 Artist",
        "Licensed to the public under http://creativecommons.org/licenses/by-nc/3.0/ Verify at x",
        "HTTPS://CREATIVECOMMONS.ORG/licenses/by-nc/3.0",
    ],
)
def test_matching_or_silent_tags_do_not_conflict(text: str) -> None:
    assert tag_conflict([("copyright", text)], BY_NC) is None


def test_a_comment_can_conflict_but_only_notices_are_kept() -> None:
    values = [
        ("copyright", "2012 Artist"),
        ("copyright link", "http://a.b/"),
        ("terms of use", "Terms"),
        ("comment", "Visit my site"),
        ("copyright", "2012 Artist"),
    ]
    assert embedded_notices(values) == ["2012 Artist", "http://a.b/", "Terms"]
    assert tag_conflict([("comment", "All rights reserved")], BY_NC) is not None
