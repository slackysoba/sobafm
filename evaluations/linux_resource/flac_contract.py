"""Pure contracts for the one owned 120-second native FLAC qualification case."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .probe import ProbeError

SAMPLE_FRAMES = 5_760_000
RAW_BYTES = SAMPLE_FRAMES * 4
RAW_SHA256 = (
    # Recorded pre-encoder PCM digest, not the lossy MP3 output digest.
    "0feb0dec8b6eb253893680342a58d1abb37727ef0da8f8aace849608ff41fa54"  # pragma: allowlist secret
)
MAX_FILE_BYTES = 4_000_000
MAX_METADATA_BYTES = 65_536
MAX_DIAGNOSTIC_BYTES = 65_536
ARGV = (
    "/usr/bin/flac",
    "--decode",
    "--stdout",
    "--force-raw-format",
    "--endian=little",
    "--sign=signed",
    "--warnings-as-errors",
    "--",
    "-",
)
# A source-derived candidate only. Actual compiled version/initializer binding is still required.
BANNER = (
    b"\nflac 1.5.0\n"
    b"Copyright (C) 2000-2009  Josh Coalson, 2011-2025  Xiph.Org Foundation\n"
    b"flac comes with ABSOLUTELY NO WARRANTY.  This is free software, and you are\n"
    b"welcome to redistribute it under certain conditions.  Type `flac' for details.\n\n"
)


@dataclass(frozen=True)
class FlacHeader:
    sample_rate: int
    channels: int
    bits_per_sample: int
    sample_frames: int
    raw_md5: str
    metadata_bytes: int
    metadata_sha256: str
    block_types: tuple[int, ...]


def checksum(value: str, digits: int) -> bool:
    return re.fullmatch(r"[0-9a-f]{" + str(digits) + "}", value) is not None and any(
        char != "0" for char in value
    )


def validate_comments(payload: memoryview) -> None:
    """The output muxer's bitexact path writes vendor ffmpeg and zero user comments."""
    expected = b"\x06\x00\x00\x00ffmpeg\x00\x00\x00\x00"
    if len(payload) != len(expected) or bytes(payload) != expected:
        raise ProbeError("FLAC vendor/comments differ from the fixed bitexact encoder")


def inspect_fixture(payload: memoryview, raw_md5: str) -> FlacHeader:
    """Inspect existing prepaid compressed storage without copying the complete payload."""
    if (
        payload.ndim != 1
        or payload.format != "B"
        or not 42 < len(payload) <= MAX_FILE_BYTES
        or bytes(payload[:4]) != b"fLaC"
        or not checksum(raw_md5, 32)
    ):
        raise ProbeError("invalid owned native FLAC envelope or raw MD5")
    position = 4
    types: list[int] = []
    rate = channels = bits = frames = 0
    digest = ""
    while True:
        if len(types) >= 8 or position + 4 > min(len(payload), MAX_METADATA_BYTES):
            raise ProbeError("FLAC metadata header exceeds bound")
        flags = payload[position]
        kind = flags & 0x7F
        size = int.from_bytes(payload[position + 1 : position + 4], "big")
        position += 4
        end = position + size
        if end > min(len(payload), MAX_METADATA_BYTES):
            raise ProbeError("truncated or oversized FLAC metadata")
        if (not types and kind != 0) or kind in types or kind not in (0, 1, 4):
            raise ProbeError("unexpected, duplicate or foreign FLAC metadata")
        with payload[position:end] as block:
            if kind == 0:
                if size != 34:
                    raise ProbeError("FLAC STREAMINFO must contain exactly34 bytes")
                minimum = int.from_bytes(block[:2], "big")
                maximum = int.from_bytes(block[2:4], "big")
                packed = int.from_bytes(block[10:18], "big")
                rate = packed >> 44
                channels = ((packed >> 41) & 7) + 1
                bits = ((packed >> 36) & 31) + 1
                frames = packed & ((1 << 36) - 1)
                digest = bytes(block[18:34]).hex()
                if (
                    not 16 <= minimum <= maximum <= 65_535
                    or (rate, channels, bits, frames) != (48_000, 2, 16, SAMPLE_FRAMES)
                    or digest != raw_md5
                ):
                    raise ProbeError("FLAC STREAMINFO differs from the owned raw recipe")
            elif kind == 1:
                if size != 8192 or any(block):
                    raise ProbeError("FLAC padding differs from the fixed zero8192 bytes")
            else:
                validate_comments(block)
        types.append(kind)
        position = end
        if flags & 0x80:
            break
    if types != [0, 4, 1] or position >= len(payload):
        raise ProbeError("FLAC metadata order or frame payload differs from the fixed encoder")
    with payload[:position] as metadata:
        metadata_digest = hashlib.sha256(metadata).hexdigest()
    return FlacHeader(rate, channels, bits, frames, digest, position, metadata_digest, tuple(types))


def validate_diagnostics(data: bytes, format_line: str = "") -> None:
    """Recognize exact source-backed progress erasure; never strip arbitrary controls."""
    prefix = BANNER + b"-: "
    if format_line or len(data) > MAX_DIAGNOSTIC_BYTES or not data.startswith(prefix):
        raise ProbeError("FLAC banner or diagnostic bound differs")
    position = len(prefix)
    previous = -1
    while data[position:] != b"done         \n":
        match = re.match(rb"(0|[1-9][0-9]?)% complete", data[position:])
        if match is None:
            raise ProbeError("unexpected FLAC diagnostic or missing completion")
        percent = int(match[1])
        if percent < previous:
            raise ProbeError("FLAC progress decreased")
        previous = percent
        width = match.end()
        position += width
        if data[position : position + width] != b"\b" * width:
            raise ProbeError("FLAC progress erasure differs")
        position += width
