"""Bounded MP3 envelope, embedded-evidence and PCM checks for one Jamendo attempt.

These checks establish the adopted structural subset, never undisclosed rights or
audible quality. A tag and a frame share one backing; the caller consumes each
borrowed output view before advancing the iterator or supplying another chunk.
"""

from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlsplit

from sobafm.catalog import Recording
from sobafm.jamendo_journal import Outcome
from sobafm.media_check import tag_conflict
from sobafm.pcm import FRAME_BYTES, SAMPLE_RATE, STEREO_SAMPLE_BYTES

TAG_BYTES = 131_072
TEXT_FRAME_BYTES = 8_192
TEXT_BYTES = 32_768
MAX_ID3_FRAMES = 256
REMAINDER_BYTES = 16_384


class StreamCheckError(ValueError):
    """A fixed safe failure; the digest binds rejected evidence without retaining it."""

    def __init__(self, outcome: Outcome, digest: str | None = None) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome
        self.digest = digest


def _refuse(outcome: Outcome, evidence: bytes | memoryview = b"") -> StreamCheckError:
    digest = hashlib.sha256(evidence).hexdigest() if outcome == Outcome.NOTICE else None
    return StreamCheckError(outcome, digest)


def _synchsafe(data: memoryview) -> int:
    if any(v & 0x80 for v in data):
        raise _refuse(Outcome.NOTICE, data)
    result = 0
    for value in data:
        result = result * 128 + value
    return result


def _normal(text: str) -> str:
    value = unicodedata.normalize("NFC", text).strip()
    # Admission already upgrades these public identity/license links. Preserve
    # exact case-sensitive paths while accepting the same embedded public URL.
    if value.startswith(("http://", "https://")):
        try:
            url = urlsplit(value)
            if (
                url.hostname in ("creativecommons.org", "www.jamendo.com")
                and url.username is None
                and url.password is None
                and url.port in (None, 80 if url.scheme == "http" else 443)
                and not url.query
                and not url.fragment
            ):
                return url._replace(
                    scheme="https", netloc=url.hostname, path=url.path.rstrip("/")
                ).geturl()
        except ValueError:
            pass  # It cannot match a validated bound link below.
    return value


class Notices:
    """Reconcile against the refreshed immutable projection, never append credits."""

    def __init__(self, recording: Recording) -> None:
        credits = recording.credits
        if credits is None or credits.license_url is None:
            raise _refuse(Outcome.NOTICE)
        self.recording = recording
        self.license_url = credits.license_url
        self.text_bytes = 0
        self.frames = 0
        self._known = frozenset(
            _normal(value)
            for value in (
                recording.title,
                *recording.creators,
                recording.provenance,
                self.license_url,
                *credits.notices,
                *recording.details,
                recording.composition or "",
                recording.composer or "",
            )
        )
        # Retained normalized evidence occupies the existing decoded-notice
        # subdivision, including CPython's widest Unicode representation.
        if sum(4 * len(value) for value in self._known) > TAG_BYTES:
            raise _refuse(Outcome.NOTICE)

    def text(
        self, value: str, evidence: memoryview, *, identity: str = "", count: bool = True
    ) -> None:
        # Account before normalization or comparison. Never truncate a supplied field.
        if count:
            self.text_bytes += len(value.encode("utf-8"))
        if self.text_bytes > TEXT_BYTES:
            raise _refuse(Outcome.NOTICE, evidence)
        value = _normal(value)
        if not value:
            return
        if any(unicodedata.category(c).startswith("C") for c in value):
            raise _refuse(Outcome.NOTICE, evidence)
        if tag_conflict([("embedded", value)], self.license_url) is not None:
            raise _refuse(Outcome.NOTICE, evidence)
        for link in re.finditer(r"https?://[^\s]+", value, re.I):
            try:
                parsed = urlsplit(link.group())
            except ValueError:
                raise _refuse(Outcome.NOTICE, evidence) from None
            if parsed.hostname == "creativecommons.org" and (
                parsed.path.rstrip("/") != urlsplit(self.license_url).path.rstrip("/")
            ):
                raise _refuse(Outcome.NOTICE, evidence)
        allowed = self._known
        if identity == "title":
            allowed = frozenset((_normal(self.recording.title),))
        elif identity == "creator":
            allowed = frozenset(_normal(c) for c in self.recording.creators)
        if value not in allowed:
            raise _refuse(Outcome.NOTICE, evidence)

    def frame(self, name: str, data: memoryview, version: int) -> None:
        self.frames += 1
        if self.frames > MAX_ID3_FRAMES:
            raise _refuse(Outcome.NOTICE, data)
        if name == "APIC":
            self._picture(data, version)
            return
        if len(data) > TEXT_FRAME_BYTES or not data:
            raise _refuse(Outcome.NOTICE, data)
        if name.startswith("T") and name != "TXXX":
            if name not in _TEXT_IDS:
                raise _refuse(Outcome.NOTICE, data)
            values = _decode(data[1:], data[0], version).rstrip("\0").split("\0")
            identity = "title" if name == "TIT2" else "creator" if name in _PEOPLE_IDS else ""
            if name in ("TIPL", "TMCL"):
                self._people(values, data)
            else:
                for value in values:
                    if identity == "creator" and version == 3 and _normal(value) not in self._known:
                        for person in value.split("/"):
                            self.text(person, data, identity=identity)
                    else:
                        self.text(value, data, identity=identity)
        elif name == "IPLS":
            self._people(_decode(data[1:], data[0], version).rstrip("\0").split("\0"), data)
        elif name == "TXXX":
            values = _decode(data[1:], data[0], version).rstrip("\0").split("\0")
            if len(values) < 2:
                raise _refuse(Outcome.NOTICE, data)
            for value in values:
                self.text(value, data)
        elif name.startswith("W"):
            if name not in _URL_IDS:
                raise _refuse(Outcome.NOTICE, data)
            if name == "WXXX":
                description, remainder = _terminated(data[1:], data[0], version)
                self.text(description, data)
                value = bytes(remainder).decode("latin-1").rstrip("\0")
            else:
                value = bytes(data).decode("latin-1").rstrip("\0")
            self.text(value, data)
        elif name in ("COMM", "USLT", "USER"):
            if len(data) < 4 or not bytes(data[1:4]).isalpha():
                raise _refuse(Outcome.NOTICE, data)
            if name == "USER":
                self.text(_decode(data[4:], data[0], version).rstrip("\0"), data)
            else:
                description, remainder = _terminated(data[4:], data[0], version)
                self.text(description, data)
                self.text(_decode(remainder, data[0], version).rstrip("\0"), data)
        else:
            raise _refuse(Outcome.NOTICE, data)

    def _people(self, values: list[str], evidence: memoryview) -> None:
        if len(values) % 2:
            raise _refuse(Outcome.NOTICE, evidence)
        # A role/name pair must already be bound as a complete credit, including
        # the source's explicit "composer: name" notice. Never discard its role.
        for role, person in zip(values[::2], values[1::2], strict=True):
            self.text_bytes += len(role.encode("utf-8")) + len(person.encode("utf-8"))
            self.text(f"{role}: {person}", evidence, count=False)

    def _picture(self, data: memoryview, version: int) -> None:
        if len(data) < 5:
            raise _refuse(Outcome.NOTICE, data)
        stop = next((i for i in range(1, min(len(data), 256)) if data[i] == 0), None)
        if stop is None or stop + 2 >= len(data):
            raise _refuse(Outcome.NOTICE, data)
        mime = bytes(data[1:stop])
        if mime not in (b"image/jpeg", b"image/png") or data[stop + 1] > 20:
            raise _refuse(Outcome.NOTICE, data)
        description, _image = _terminated(data[stop + 2 :], data[0], version)
        self.text(description, data)


_PEOPLE_IDS = frozenset(("TPE1", "TPE2", "TPE3", "TPE4", "TCOM", "TEXT", "TOLY", "TOPE"))
_TEXT_IDS = frozenset(
    [
        "TALB",
        "TBPM",
        "TCOM",
        "TCON",
        "TCOP",
        "TDAT",
        "TDEN",
        "TDLY",
        "TDOR",
        "TDRC",
        "TDRL",
        "TDTG",
        "TENC",
        "TEXT",
        "TFLT",
        "TIME",
        "TIPL",
        "TIT1",
        "TIT2",
        "TIT3",
        "TKEY",
        "TLAN",
        "TLEN",
        "TMCL",
        "TMED",
        "TMOO",
        "TOAL",
        "TOFN",
        "TOLY",
        "TOPE",
        "TORY",
        "TOWN",
        "TPE1",
        "TPE2",
        "TPE3",
        "TPE4",
        "TPOS",
        "TPRO",
        "TPUB",
        "TRCK",
        "TRDA",
        "TRSN",
        "TRSO",
        "TSIZ",
        "TSOA",
        "TSOP",
        "TSOT",
        "TSRC",
        "TSSE",
        "TSST",
        "TYER",
    ]
)
_URL_IDS = frozenset(["WCOM", "WCOP", "WOAF", "WOAR", "WOAS", "WORS", "WPAY", "WPUB", "WXXX"])


def _decode(data: memoryview, encoding: int, version: int) -> str:
    if len(data) > TEXT_FRAME_BYTES or encoding not in ((0, 1) if version == 3 else (0, 1, 2, 3)):
        raise _refuse(Outcome.NOTICE, data)
    codecs = ("latin-1", "utf-16", "utf-16-be", "utf-8")
    try:
        return bytes(data).decode(codecs[encoding], errors="strict")
    except UnicodeError:
        raise _refuse(Outcome.NOTICE, data) from None


def _terminated(data: memoryview, encoding: int, version: int) -> tuple[str, memoryview]:
    width = 2 if encoding in (1, 2) else 1
    for index in range(0, min(len(data), TEXT_FRAME_BYTES), width):
        if len(data) >= index + width and all(v == 0 for v in data[index : index + width]):
            return _decode(data[:index], encoding, version), data[index + width :]
    raise _refuse(Outcome.NOTICE, data)


@dataclass(frozen=True)
class Mp3Frame:
    length: int
    version: int
    rate: int
    channels: int


def mp3_frame(header: memoryview) -> Mp3Frame:
    if len(header) != 4:
        raise _refuse(Outcome.FRAMING)
    bits = int.from_bytes(header)
    version = (bits >> 19) & 3
    bitrate = (bits >> 12) & 15
    rate = (bits >> 10) & 3
    if (
        bits >> 21 != 0x7FF
        or version == 1
        or (bits >> 17) & 3 != 1
        or bitrate in (0, 15)
        or rate == 3
        or bits & 3 == 2
    ):
        raise _refuse(Outcome.FRAMING)
    rates = (44_100, 48_000, 32_000)
    sample_rate = rates[rate] // (1 if version == 3 else 2 if version == 2 else 4)
    bitrates = (
        (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
        if version == 3
        else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
    )
    length = (144_000 if version == 3 else 72_000) * bitrates[bitrate] // sample_rate
    length += (bits >> 9) & 1
    return Mp3Frame(length, version, sample_rate, 1 if (bits >> 6) & 3 == 3 else 2)


class Envelope:
    """Consume arbitrary fragments; yield complete checked frames/tags as borrowed views."""

    def __init__(self, recording: Recording) -> None:
        self.notices = Notices(recording)
        self._storage = bytearray(TAG_BYTES)
        self._used = 0
        self._want = 4
        self._kind = "boundary"
        self._format: tuple[int, int, int] | None = None
        self._leading = self._later = 0
        self.frames = 0
        self.complete = False
        self._version = 0
        self._footer = False
        self._id3v1 = False

    @property
    def leading_complete(self) -> bool:
        return self.frames > 0

    def feed(self, data: memoryview) -> Iterator[memoryview]:
        if self.complete:
            raise _refuse(Outcome.FRAMING)
        offset = 0
        while offset < len(data):
            if self._id3v1:
                raise _refuse(Outcome.FRAMING)
            count = min(self._want - self._used, len(data) - offset)
            self._storage[self._used : self._used + count] = data[offset : offset + count]
            self._used += count
            offset += count
            if self._used < self._want:
                continue
            with memoryview(self._storage)[: self._used] as part:
                if self._kind == "boundary":
                    if bytes(part) == b"APET":
                        raise _refuse(Outcome.NOTICE, part)
                    if bytes(part[:3]) == b"ID3":
                        self._kind, self._want = "tag_header", 10
                        continue
                    if bytes(part[:3]) == b"TAG":
                        self._charge_tag(128)
                        self._kind, self._want = "id3v1", 128
                        continue
                    frame = mp3_frame(part)
                    signature = (frame.version, frame.rate, frame.channels)
                    if self._format is not None and self._format != signature:
                        raise _refuse(Outcome.FRAMING)
                    self._format = signature
                    self._kind, self._want = "frame", frame.length
                    continue
                if self._kind == "tag_header":
                    self._version = part[3]
                    self._footer = bool(part[5] & 0x10)
                    if (
                        part[3] not in (3, 4)
                        or part[4] != 0
                        or part[5] & (0xFF if part[3] == 3 else 0xEF)
                    ):
                        raise _refuse(Outcome.NOTICE, part)
                    size = _synchsafe(part[6:10])
                    total = 10 + size + (10 if self._footer else 0)
                    self._charge_tag(total)
                    self._kind, self._want = "tag", total
                    if self._used < self._want:
                        continue
                if self._kind == "tag":
                    self._tag(part)
                elif self._kind == "id3v1":
                    if not self.frames:
                        raise _refuse(Outcome.FRAMING)
                    self._v1(part)
                    self._id3v1 = True
                else:
                    self.frames += 1
                yield part
            self._used, self._want, self._kind = 0, 4, "boundary"

    def _charge_tag(self, count: int) -> None:
        if self.frames:
            self._later += count
            total = self._later
        else:
            self._leading += count
            total = self._leading
        if total > TAG_BYTES:
            raise _refuse(Outcome.NOTICE)

    def _tag(self, tag: memoryview) -> None:
        end = len(tag) - (10 if self._footer else 0)
        if self._footer and (bytes(tag[end : end + 3]) != b"3DI" or tag[end + 3 :] != tag[3:10]):
            raise _refuse(Outcome.NOTICE, tag)
        position = 10
        if end == position:
            raise _refuse(Outcome.NOTICE, tag)
        while position < end:
            if tag[position] == 0:
                if position == 10 or self._footer or any(tag[position:end]):
                    raise _refuse(Outcome.NOTICE, tag)
                return
            if end - position < 10:
                raise _refuse(Outcome.NOTICE, tag)
            header = tag[position : position + 10]
            name = bytes(header[:4])
            status_flags = 0xE0 if self._version == 3 else 0x70
            if (
                re.fullmatch(rb"[A-Z0-9]{4}", name) is None
                or header[8] & ~status_flags
                or header[9]
            ):
                raise _refuse(Outcome.NOTICE, header)
            size = _synchsafe(header[4:8]) if self._version == 4 else int.from_bytes(header[4:8])
            position += 10
            if size == 0 or size > end - position:
                raise _refuse(Outcome.NOTICE, header)
            self.notices.frame(name.decode("ascii"), tag[position : position + size], self._version)
            position += size

    def _v1(self, tag: memoryview) -> None:
        for start, end, identity in (
            (3, 33, "title"),
            (33, 63, "creator"),
            (63, 93, ""),
            (93, 97, ""),
        ):
            self.notices.text(
                bytes(tag[start:end]).decode("latin-1").rstrip("\0 "), tag, identity=identity
            )
        end = 125 if tag[125] == 0 and tag[126] else 127
        self.notices.text(bytes(tag[97:end]).decode("latin-1").rstrip("\0 "), tag)
        # Numeric genre/track fields are not free-text notices.

    def finish(self) -> None:
        if self._used or not self.frames:
            if self._kind in ("tag_header", "tag", "id3v1"):
                raise _refuse(Outcome.NOTICE, memoryview(self._storage)[: self._used])
            raise _refuse(Outcome.FRAMING)
        self.complete = True


class PcmScreen:
    """Existing strict >30s silence / >1s clipping policy, before bank commit."""

    def __init__(self, duration: int) -> None:
        if type(duration) is not int or not 1 <= duration <= 86_400:
            raise ValueError("invalid duration")
        self.duration = duration
        self._hundredths = min(3000, max(200, duration), 10 * duration)
        self.tolerance = self._hundredths / 100
        self.limit = ((duration * 100 + self._hundredths + 1) // 2) * FRAME_BYTES
        self.silence = self.clipping = 0
        self.bytes = 0

    def add(self, frame: memoryview) -> None:
        if len(frame) != FRAME_BYTES:
            raise _refuse(Outcome.INTEGRITY)
        peak = clipped = 0
        for (sample,) in struct.iter_unpack("<h", frame):
            peak = max(peak, abs(sample))
            clipped += sample >= 32_767 or sample <= -32_768
        self.silence = self.silence + 1 if peak <= 16 else 0
        self.clipping = self.clipping + 1 if clipped >= 19 else 0
        if self.silence > 1_500 or self.clipping > 50:
            raise _refuse(Outcome.DEFECT)
        self.bytes += len(frame)
        if self.bytes > self.limit:
            raise _refuse(Outcome.INTEGRITY)

    def finish(self, total_bytes: int) -> None:
        if (
            total_bytes < FRAME_BYTES
            or total_bytes % STEREO_SAMPLE_BYTES
            or total_bytes > self.limit
        ):
            raise _refuse(Outcome.INTEGRITY)
        # Compare integer stereo samples to avoid rounding away one-sample failures.
        samples = total_bytes // STEREO_SAMPLE_BYTES
        delta = abs(samples - self.duration * SAMPLE_RATE)
        if delta * 100 > self._hundredths * SAMPLE_RATE:
            raise _refuse(Outcome.DURATION)
        if self.bytes != total_bytes - total_bytes % FRAME_BYTES:
            raise _refuse(Outcome.INTEGRITY)
