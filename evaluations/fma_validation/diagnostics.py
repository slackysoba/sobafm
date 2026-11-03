"""Closed mpg123 grammar with source tags confined by the pinned terminal filter."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

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
DIAGNOSTIC_BYTES = 65_536
READ_BYTES = 4096
FACT_BYTES = 4096
FORMAT_BYTES = 34


@dataclass(frozen=True)
class DiagnosticFacts:
    format: str
    count: int
    sha256: str
    tag_sha256: str | None

    def record(self) -> dict[str, object]:
        # Projections alias the bounded immutable values, never source tags.
        return {
            "format": self.format,
            "diagnostic_bytes": self.count,
            "diagnostic_sha256": self.sha256,
            "tag_block_sha256": self.tag_sha256,
            "terminal_source_filter_required": True,
        }


def matches(data: memoryview, start: int, end: int, value: bytes) -> bool:
    return end - start == len(value) and all(
        data[start + i] == byte for i, byte in enumerate(value)
    )


def line_range(data: memoryview, start: int) -> tuple[int, int]:
    end = start
    while end < len(data) and data[end] != 10:
        end += 1
    return end, min(end + 1, len(data))


def completion(data: memoryview, start: int, end: int) -> bool:
    if start == end or data[start] != 91:
        return False
    cursor = start + 1
    while cursor < end and 48 <= data[cursor] <= 57:
        cursor += 1
    # Minutes may span the entire admitted input; never copy or convert them.
    return (
        cursor > start + 1
        and cursor + 3 < end
        and data[cursor] == 58
        and 48 <= data[cursor + 1] <= 53
        and 48 <= data[cursor + 2] <= 57
        and matches(data, cursor + 3, end, b"] Decoding of - finished.")
    )


def scan(data: memoryview) -> DiagnosticFacts:
    """Borrow one byte view; retain no line, suffix, value or tag-join copy."""
    if len(data) > DIAGNOSTIC_BYTES or not matches(data, 0, min(len(data), len(BANNER)), BANNER):
        raise ProbeError("decoder_diagnostic_banner_or_bound")
    seen: set[str] = set()
    tag_sha256: str | None = None
    format_line = ""
    cursor = len(BANNER)
    while cursor < len(data):
        start = cursor
        end, cursor = line_range(data, start)
        if start == end:
            continue
        if "complete" in seen:
            raise ProbeError("post_completion_diagnostic")
        if matches(data, start, min(end, start + len(TAG_PREFIXES[0])), TAG_PREFIXES[0]):
            if "format" not in seen or tag_sha256 is not None:
                raise ProbeError("decoder_tag_block_repeated_or_incomplete")
            tag_hash = hashlib.sha256()
            for index, prefix in enumerate(TAG_PREFIXES):
                if index:
                    start = cursor
                    end, cursor = line_range(data, start)
                    tag_hash.update(b"\n")
                value_start = start + len(prefix)
                if not matches(data, start, min(end, value_start), prefix) or any(
                    data[position] < 32 or data[position] > 126
                    for position in range(value_start, end)
                ):
                    raise ProbeError("decoder_unframed_source_text")
                with data[start:end] as row:
                    tag_hash.update(row)
            tag_sha256 = tag_hash.hexdigest()
            continue
        if matches(data, start, end, b"Directory: ."):
            key = "directory"
        elif matches(data, start, end, b"Note: forcing output encoding s16"):
            key = "encoding"
        elif matches(data, start, end, b"Playing MPEG stream 1 of 1: - ..."):
            key = "playing"
        elif completion(data, start, end):
            key = "complete"
        else:
            # Only the finite format alternative enters the regex/ASCII copy.
            if end - start > FORMAT_BYTES:
                raise ProbeError("unexpected_decoder_diagnostic")
            with data[start:end] as view:
                small = bytes(view)
            if not FORMAT.fullmatch(small):
                raise ProbeError("unexpected_decoder_diagnostic")
            if not {"encoding", "playing"} <= seen:
                raise ProbeError("misplaced_decoder_format")
            key = "format"
            format_line = small.decode("ascii")
        if key in seen:
            raise ProbeError("repeated_or_late_decoder_diagnostic")
        seen.add(key)
    if not {"encoding", "playing", "format", "complete"} <= seen:
        raise ProbeError("missing_decoder_completion_evidence")
    return DiagnosticFacts(format_line, len(data), hashlib.sha256(data).hexdigest(), tag_sha256)


def validate(data: bytes, _format: str = "") -> dict[str, object]:
    with memoryview(data) as view:
        return scan(view).record()


def check(data: bytes, format_line: str) -> None:
    validate(data, format_line)


DECODER = NativeDecoder(
    (*ARGV[:-1], "--skip-id3v2", "--no-icy-meta", "--long-tag", "-"),
    BANNER,
    tuple(MAPPED_ELF.items()),
    check,
)
