"""Closed mpg123 grammar with source tags confined by the pinned terminal filter."""

from __future__ import annotations

import hashlib
import re

from evaluations.linux_resource.owned_mp3_decode import ARGV, BANNER, MAPPED_ELF, NativeDecoder
from evaluations.linux_resource.probe import ProbeError

TAG_PREFIXES = (
    b"\tTitle:   ",
    b"\tArtist:  ",
    b"\tAlbum:   ",
    b"\tYear:    ",
    b"\tGenre:   ",
    b"\tComment: ",
)
FORMAT = re.compile(
    rb"MPEG (?:1\.0|2\.0|2\.5) L III (?:vbr|cbr[0-9]{1,3}|abr[0-9]{1,3}) "
    rb"(?:8000|11025|12000|16000|22050|24000|32000|44100|48000) "
    rb"(?:mono|stereo|j-s|dual)"
)


def validate(data: bytes, _format: str = "") -> dict[str, object]:
    if not data.startswith(BANNER) or len(data) > 65_536:
        raise ProbeError("decoder_diagnostic_banner_or_bound")
    lines = data[len(BANNER) :].split(b"\n")
    seen: set[str] = set()
    tags: list[bytes] = []
    format_line = b""
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line:
            continue
        if "complete" in seen:
            raise ProbeError("post_completion_diagnostic")
        if line.startswith(TAG_PREFIXES[0]):
            if "format" not in seen or tags or index + 5 > len(lines):
                raise ProbeError("decoder_tag_block_repeated_or_incomplete")
            block = [line, *lines[index : index + 5]]
            for value, prefix in zip(block, TAG_PREFIXES, strict=True):
                if not value.startswith(prefix) or any(
                    byte < 32 or byte > 126 for byte in value[len(prefix) :]
                ):
                    raise ProbeError("decoder_unframed_source_text")
            tags = block
            index += 5
            continue
        if line == b"Directory: .":
            key = "directory"
        elif line == b"Note: forcing output encoding s16":
            key = "encoding"
        elif line == b"Playing MPEG stream 1 of 1: - ...":
            key = "playing"
        elif FORMAT.fullmatch(line):
            if not {"encoding", "playing"} <= seen:
                raise ProbeError("misplaced_decoder_format")
            key = "format"
            format_line = line
        elif re.fullmatch(rb"\[[0-9]+:[0-5][0-9]\] Decoding of - finished\.", line):
            key = "complete"
        else:
            raise ProbeError("unexpected_decoder_diagnostic")
        if key in seen or "complete" in seen:
            raise ProbeError("repeated_or_late_decoder_diagnostic")
        seen.add(key)
    if not {"encoding", "playing", "format", "complete"} <= seen:
        raise ProbeError("missing_decoder_completion_evidence")
    return {
        "format": format_line.decode("ascii"),
        "diagnostic_bytes": len(data),
        "diagnostic_sha256": hashlib.sha256(data).hexdigest(),
        "tag_block_sha256": hashlib.sha256(b"\n".join(tags)).hexdigest() if tags else None,
        "terminal_source_filter_required": True,
    }


def check(data: bytes, format_line: str) -> None:
    validate(data, format_line)


DECODER = NativeDecoder(
    (*ARGV[:-1], "--skip-id3v2", "--no-icy-meta", "--long-tag", "-"),
    BANNER,
    tuple(MAPPED_ELF.items()),
    check,
)
