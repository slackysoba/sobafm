"""Validate one local MP3 before admission: decode it as playback does and read its tags.

``check_decode`` runs the real ``DecodeProducer``, so a file that passes went through the
same decoder arguments, limits and completion rules that playback uses. It screens the
decoded audio for unintended long silence and sustained clipping without keeping any of
it. ``embedded_notices`` reads the copyright and license text in the file's ID3 tags. Run
this in the application's image, with no network. It's ingestion, not part of playback.
"""

from __future__ import annotations

import re
import struct
import threading
import time
from array import array
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sobafm.decode import DecodeInput, DecodeProducer, DecoderCommand
from sobafm.pcm import BYTES_PER_SECOND, FRAME_BYTES

FRAME_SECONDS = FRAME_BYTES / BYTES_PER_SECOND
MAX_SILENCE_SECONDS = 30.0
# A frame this quiet at its loudest sample is silence: about -66 dBFS.
SILENCE_PEAK = 16
# One percent of a frame's samples at full scale makes a clipped frame.
CLIPPED_SAMPLES_PER_FRAME = FRAME_BYTES // 2 // 100
MAX_CLIPPING_SECONDS = 1.0
MAX_TAG_BYTES = 4_000_000
MAX_TEXT_FRAME_BYTES = 8_192
_POLL_SECONDS = 0.002


class TagError(ValueError):
    """The file's ID3 tag can't be read, so its notices can't be established."""


@dataclass(frozen=True)
class DecodeCheck:
    """What one complete decode established. ``failure`` is None only for a valid stream."""

    failure: str | None
    pcm_bytes: int
    longest_silence_seconds: float
    longest_clipping_seconds: float

    @property
    def problem(self) -> str | None:
        if self.failure is not None:
            return "decode failed: " + self.failure
        if self.longest_silence_seconds > MAX_SILENCE_SECONDS:
            return "silence over 30 seconds"
        if self.longest_clipping_seconds > MAX_CLIPPING_SECONDS:
            return "sustained full-scale clipping"
        return None


class _Screen:
    """Running silence and clipping lengths, in whole frames."""

    def __init__(self) -> None:
        self.silence = self.longest_silence = 0
        self.clipping = self.longest_clipping = 0

    def add(self, frame: bytes) -> None:
        samples = array("h", frame)
        peak = max(max(samples), -min(samples))
        self.silence = self.silence + 1 if peak <= SILENCE_PEAK else 0
        self.longest_silence = max(self.longest_silence, self.silence)
        clipped = 0
        if peak >= 32_767:
            clipped = sum(1 for sample in samples if sample >= 32_767 or sample <= -32_768)
        self.clipping = self.clipping + 1 if clipped >= CLIPPED_SAMPLES_PER_FRAME else 0
        self.longest_clipping = max(self.longest_clipping, self.clipping)


def check_decode(source: DecodeInput, *, command: DecoderCommand | None = None) -> DecodeCheck:
    """Decode the whole file through the real producer and settle it before returning."""
    producer = DecodeProducer(source, command=command)
    screen = _Screen()
    producer.start()
    finished = threading.Event()

    def finish() -> None:
        producer.finish()
        finished.set()

    finisher = threading.Thread(target=finish, name="media-check-finish", daemon=True)
    finisher.start()
    bank = producer.bank
    while bank is not None:
        frame = bank.read_frame()
        if frame == b"":
            break
        if frame is None:
            time.sleep(_POLL_SECONDS)
            continue
        screen.add(frame)
    finisher.join()
    report = producer.report()
    written = report.pcm.written_bytes if report.pcm is not None else 0
    failure = report.failure
    if failure is None and (report.pcm is None or not report.pcm.pcm_valid):
        failure = "output_read"  # no failure was named, but no valid stream was established
    while not producer.release(1.0):
        pass  # an expired cleanup stays owned until the process and threads settle
    return DecodeCheck(
        failure,
        written,
        screen.longest_silence * FRAME_SECONDS,
        screen.longest_clipping * FRAME_SECONDS,
    )


def _synchsafe(raw: bytes) -> int:
    if any(byte & 0x80 for byte in raw):
        raise TagError("malformed tag size")
    value = 0
    for byte in raw:
        value = value << 7 | byte
    return value


def _decode_text(data: bytes) -> str:
    """One ID3 text value: its first byte names the encoding."""
    if not data:
        return ""
    encoding = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(data[0])
    if encoding is None:
        raise TagError("unknown text encoding")
    try:
        return data[1:].decode(encoding).replace("\x00", " ")
    except UnicodeDecodeError as exc:
        raise TagError("undecodable text") from exc


def _notice_value(frame: str, data: bytes) -> tuple[str, str] | None:
    """The (kind, text) of a frame that can carry a notice, or None for any other frame."""
    if frame in ("TCOP", "TCR"):
        return "copyright", _decode_text(data)
    if frame in ("WCOP", "WCP"):
        return "copyright link", data.split(b"\x00", 1)[0].decode("latin-1")
    if frame == "USER":
        return "terms of use", _decode_text(data[:1] + data[4:])  # skip the 3-byte language
    if frame in ("COMM", "COM"):
        return "comment", _decode_text(data[:1] + data[4:])
    if frame in ("TXXX", "TXX", "WXXX", "WXX"):
        return "other", _decode_text(data)
    return None


def _tag_frames(tag: bytes, version: int) -> list[tuple[str, str]]:
    frames: list[tuple[str, str]] = []
    header_bytes = 6 if version == 2 else 10
    offset = 0
    while offset + header_bytes <= len(tag) and tag[offset] != 0:
        if version == 2:
            name = tag[offset : offset + 3].decode("latin-1")
            size = int.from_bytes(tag[offset + 3 : offset + 6])
            flags = 0
        else:
            name = tag[offset : offset + 4].decode("latin-1")
            raw = tag[offset + 4 : offset + 8]
            size = _synchsafe(raw) if version == 4 else int.from_bytes(raw)
            flags = int.from_bytes(tag[offset + 8 : offset + 10])
        if re.fullmatch(r"[A-Z0-9]{3,4}", name) is None or offset + header_bytes + size > len(tag):
            raise TagError("malformed frame")
        data = tag[offset + header_bytes : offset + header_bytes + size]
        offset += header_bytes + size
        kept = _frame_notice(name, data, flags, version)
        if kept is not None:
            frames.append(kept)
    return frames


def _frame_notice(name: str, data: bytes, flags: int, version: int) -> tuple[str, str] | None:
    if len(data) > MAX_TEXT_FRAME_BYTES and name[0] in "TWUC":
        if name in ("TCOP", "TCR", "WCOP", "WCP", "USER"):
            raise TagError("oversized notice frame")
        return None
    # Compressed or encrypted frames: v2.3 flags 0x80/0x40 and v2.4 flags 0x08/0x04.
    if flags & (0x00C0 if version == 3 else 0x000C if version == 4 else 0):
        if name in ("TCOP", "TCR", "WCOP", "WCP", "USER", "COMM", "COM", "TXXX", "WXXX"):
            raise TagError("a notice frame is compressed or encrypted")
        return None
    return _notice_value(name, data)


def read_tag_values(path: Path) -> list[tuple[str, str]]:
    """Every (kind, text) value in the file's ID3v2 tag and ID3v1 comment that can hold a notice.

    An unreadable tag raises ``TagError``, because its notices can't then be established.
    A file with no tag has no embedded notices.
    """
    values: list[tuple[str, str]] = []
    with path.open("rb") as file:
        header = file.read(10)
        if header[:3] == b"ID3" and len(header) == 10:
            version, flags = header[3], header[5]
            if version not in (2, 3, 4):
                raise TagError("unsupported tag version")
            size = _synchsafe(header[6:10])
            if size > MAX_TAG_BYTES:
                raise TagError("tag over the size limit")
            tag = file.read(size)
            if len(tag) != size:
                raise TagError("truncated tag")
            if flags & 0x80:  # whole-tag unsynchronisation
                tag = tag.replace(b"\xff\x00", b"\xff")
            if flags & 0x40 and version in (3, 4):  # extended header
                extended = struct.unpack(">I", tag[:4])[0]
                skip = extended + 4 if version == 3 else _synchsafe(tag[:4])
                tag = tag[skip:]
            values.extend(_tag_frames(tag, version))
        file.seek(0, 2)
        if file.tell() >= 128:
            file.seek(-128, 2)
            trailer = file.read(128)
            if trailer[:3] == b"TAG":
                comment = trailer[97:127].split(b"\x00", 1)[0].decode("latin-1").strip()
                if comment:
                    values.append(("comment", comment))
    return [(kind, " ".join(text.split())) for kind, text in values if text.strip()]


_ALL_RIGHTS = re.compile(r"all\s+rights\s+reserved", re.IGNORECASE)
# FMA's own license for tracks that weren't Creative Commons, written as
# "FMA-Limited: http://freemusicarchive.org/FMA_License" in the tags it added.
_FMA_LIMITED = re.compile(r"FMA[-_ ]?(?:Limited|License)", re.IGNORECASE)
_CC_LICENSE = re.compile(
    r"creativecommons\.org/(?:licenses/([a-z+-]+)/([0-9.]+)|publicdomain/[a-z]+/[0-9.]+)",
    re.IGNORECASE,
)


def tag_conflict(values: list[tuple[str, str]], license_url: str) -> str | None:
    """Why the tags contradict the recording's published license, or None."""
    expected = _CC_LICENSE.search(license_url)
    for _kind, text in values:
        if _ALL_RIGHTS.search(text):
            return "embedded tag says all rights reserved"
        if _FMA_LIMITED.search(text):
            return "embedded tag names FMA's limited license"
        for found in _CC_LICENSE.finditer(text):
            if expected is None or found.group(0).casefold() != expected.group(0).casefold():
                return "embedded tag names a different license"
    return None


_FMA_PAGE = re.compile(r"freemusicarchive\.org(/music/\S+)", re.IGNORECASE)


def tag_page_conflict(values: list[tuple[str, str]], page: str) -> str | None:
    """Why the tags name a different FMA page from the listing, or None if they don't."""
    expected = unquote(urlsplit(page).path).rstrip("/").casefold()
    for _kind, text in values:
        for found in _FMA_PAGE.findall(text):
            if unquote(found).rstrip("/.,;:)").casefold() != expected:
                return "embedded tag names a different track page"
    return None


def embedded_notices(values: list[tuple[str, str]]) -> list[str]:
    """The tag text a licensee must keep intact: copyright, license link and terms of use."""
    kept = ("copyright", "copyright link", "terms of use")
    return list(dict.fromkeys(text for kind, text in values if kind in kept))
