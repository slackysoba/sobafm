"""The one candidate's fixed reservations and immutable operation clocks."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from evaluations.linux_resource.probe import COPY_BYTES, GROUP_BYTES, PCM_BYTES, ProbeError

FILE_BYTES = 8_054_458
FILE_SHA256 = (
    "7952dce61b28f454ff3ee0183f85df52b2a9f2edfcd6a0ebafc12f981f666ed5"  # pragma: allowlist secret
)
PCM_LIMIT = 46_080_000
READY_BYTES = 1_152_000
CHUNK_BYTES = 65_536
TAG_BYTES = 1_048_576
REPORT_BYTES = 65_536
METADATA_SECONDS = 30.0
PREPARE_SECONDS = 15.0
WHOLE_SECONDS = 210.0
DECODE_SECONDS = 150.0
CLEANUP_SECONDS = 3.0
MAX_SEEKS = 512
MAX_FRAMES = 512
# Partition one global copy allowance, including simultaneous serialization and
# IPC copies. Parser object/allocator overhead stays in the whole-group charge.
COPY_PARTITIONS = {
    "intake_owner": 786_432,
    "metadata_helper": 2_097_152,
    "decoder_runner": 786_432,
    "outer_controller": 524_288,
}
assert sum(COPY_PARTITIONS.values()) == COPY_BYTES
assert GROUP_BYTES + PCM_BYTES + COPY_BYTES == 98_979_840


_HEX = b"0123456789abcdef"
_SHORT_ESCAPES = {34: 34, 92: 92, 8: 98, 12: 102, 10: 110, 13: 114, 9: 116}


class JsonBuffer:
    """One bounded traversal; no recursive closure can retain its work arena."""

    def __init__(self, limit: int, backing: bytearray | None = None) -> None:
        self.limit, self.backing = limit, backing
        self.offset = self.nodes = 0

    def put(self, number: int) -> None:
        if self.offset >= self.limit:
            raise ProbeError("report_capacity_exceeded")
        if self.backing is not None:
            self.backing[self.offset] = number
        self.offset += 1

    def ascii(self, text: str) -> None:
        # Only fixed literals or bounded builtin int/float spellings enter here.
        for char in text:
            self.put(ord(char))

    def hex_escape(self, number: int) -> None:
        self.put(92)
        self.put(117)
        for shift in (12, 8, 4, 0):
            self.put(_HEX[(number >> shift) & 15])

    def string(self, text: str) -> None:
        self.put(34)
        for char in text:
            number = ord(char)
            short = _SHORT_ESCAPES.get(number)
            if short is not None:
                self.put(92)
                self.put(short)
            elif 32 <= number <= 126:
                self.put(number)
            elif number <= 0xFFFF:
                self.hex_escape(number)
            else:
                number -= 0x10000
                self.hex_escape(0xD800 | (number >> 10))
                self.hex_escape(0xDC00 | (number & 1023))
        self.put(34)

    def walk(self, item: object, depth: int) -> None:
        self.nodes += 1
        if depth > 12 or self.nodes > 8192:
            raise ProbeError("report_structure_limit")
        if item is None:
            self.ascii("null")
        elif type(item) is bool:
            self.ascii("true" if item else "false")
        elif type(item) is str:
            self.string(item)
        elif type(item) is int:
            if item.bit_length() > 128:
                raise ProbeError("report_integer_limit")
            self.ascii(str(item))
        elif type(item) is float:
            if not math.isfinite(item):
                raise ProbeError("report_nonfinite_number")
            self.ascii(repr(item))
        elif type(item) in (list, tuple):
            values = cast(list[object] | tuple[object, ...], item)
            if len(values) > 8192 - self.nodes:
                raise ProbeError("report_structure_limit")
            self.put(91)
            for index, child in enumerate(values):
                if index:
                    self.put(44)
                self.walk(child, depth + 1)
            self.put(93)
        elif type(item) is dict:
            fields = cast(dict[object, object], item)
            if len(fields) > 8192 - self.nodes:
                raise ProbeError("report_structure_limit")
            for key in fields:
                if type(key) is not str:
                    raise ProbeError("report_string_key_required")
            strings = cast(dict[str, object], fields)
            # Sorting retains aliases to original exact strings, never copies.
            keys = strings if self.backing is None else sorted(strings)
            self.put(123)
            for index, key in enumerate(keys):
                if type(key) is not str:
                    raise ProbeError("report_string_key_required")
                if index:
                    self.put(44)
                self.string(key)
                self.put(58)
                self.walk(strings[key], depth + 1)
            self.put(125)
        else:
            raise ProbeError("unsupported_report_value")


def json_size(value: object, limit: int = REPORT_BYTES) -> int:
    """Validate closed exact builtins before allocating a record-sized buffer."""
    if type(limit) is not int or not 0 <= limit <= REPORT_BYTES:
        raise ProbeError("invalid_report_limit")
    counter = JsonBuffer(limit)
    counter.walk(value, 0)
    return counter.offset


def encode_report(value: object, limit: int = REPORT_BYTES, *, newline: bool = False) -> bytes:
    """Canonical ASCII JSON from stable caller-owned values, with optional LF.

    Fill revalidates shape/scalars and the proved length before output. This is
    not synchronization or detection of every same-size concurrent mutation.
    Scalar spellings and sorted-key scopes end before the immutable freeze.
    """
    if type(newline) is not bool or type(limit) is not int or not 0 <= limit <= REPORT_BYTES:
        raise ProbeError("invalid_report_limit")
    if limit < int(newline):
        raise ProbeError("report_capacity_exceeded")
    size = json_size(value, limit - int(newline)) + int(newline)
    work = JsonBuffer(size)
    try:
        work.backing = bytearray(size)
        work.walk(value, 0)
        if newline:
            work.put(10)
        if work.offset != size:
            raise ProbeError("report_changed_during_encoding")
        assert work.backing is not None
        with memoryview(work.backing) as view:
            return bytes(view)
    finally:
        # Also release rejected arenas when an exception/traceback is retained.
        # No fill frame keeps a separate alias to backing or a recursive closure.
        work.backing = None


def digest(value: object) -> str:
    return hashlib.sha256(encode_report(value)).hexdigest()


def utc_seconds(text: str) -> float:
    if not text.endswith("Z"):
        raise ProbeError("utc_timestamp_required")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ProbeError("invalid_utc_timestamp") from error
    if parsed.tzinfo != UTC:
        raise ProbeError("utc_timestamp_required")
    return parsed.timestamp()


@dataclass
class CaseClock:
    """An earlier obligation can tighten this clock; no phase can renew it.

    UTC carries the original obligation across host/container clock namespaces.
    Each process also fixes a local monotonic bound once, protecting rollback.
    Uncertain old cleanup timing is sticky debt, even after physical settlement.
    """

    whole_end_utc: float
    expires_utc: float
    wall: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    cleanup_start_utc: float | None = None
    timing_unknown: bool = False
    missed: bool = False
    case_missed: bool = False

    def __post_init__(self) -> None:
        self.lock = threading.RLock()
        now, mono = self.wall(), self.monotonic()
        if not all(math.isfinite(value) for value in (self.whole_end_utc, self.expires_utc)):
            raise ProbeError("invalid_case_deadline")
        self.end_monotonic = mono + max(0, min(self.whole_end_utc, self.expires_utc) - now)
        self.cleanup_monotonic: float | None = None
        if self.cleanup_start_utc is not None:
            self.adopt_cleanup(self.cleanup_start_utc)

    def remaining(self, phase_end: float | None = None) -> float:
        end = self.end_monotonic if phase_end is None else min(self.end_monotonic, phase_end)
        remaining = min(
            end - self.monotonic(), min(self.whole_end_utc, self.expires_utc) - self.wall()
        )
        if remaining <= 0:
            self.adopt_cleanup(
                min(
                    self.wall() + min(0, end - self.monotonic()),
                    self.whole_end_utc,
                    self.expires_utc,
                )
            )
        if self.cleanup_start_utc is not None or self.timing_unknown:
            return 0
        return max(0, remaining)

    def cancel(self) -> None:
        self.adopt_cleanup(min(self.wall(), self.whole_end_utc, self.expires_utc))

    def adopt_cleanup(self, started: float) -> None:
        with self.lock:
            self._adopt_cleanup(started)

    def _adopt_cleanup(self, started: float) -> None:
        if not math.isfinite(started) or started > self.wall():
            self.timing_unknown = True
            return
        tightened = self.cleanup_start_utc is None or started < self.cleanup_start_utc
        if tightened:
            self.cleanup_start_utc = started
        assert self.cleanup_start_utc is not None
        if tightened or self.cleanup_monotonic is None:
            candidate = self.monotonic() + self.cleanup_start_utc + CLEANUP_SECONDS - self.wall()
            if self.cleanup_monotonic is None or candidate < self.cleanup_monotonic:
                self.cleanup_monotonic = candidate
        self.observe()

    def observe(self) -> None:
        self.case_missed |= self.monotonic() > self.end_monotonic or self.wall() > min(
            self.whole_end_utc, self.expires_utc
        )
        if (
            self.timing_unknown
            or (self.cleanup_monotonic is not None and self.monotonic() > self.cleanup_monotonic)
            or (
                self.cleanup_start_utc is not None
                and self.wall() > self.cleanup_start_utc + CLEANUP_SECONDS
            )
        ):
            self.missed = True

    def receipt(self) -> dict[str, object]:
        self.observe()
        return {
            "whole_end_utc": self.whole_end_utc,
            "expires_utc": self.expires_utc,
            "cleanup_start_utc": self.cleanup_start_utc,
            "cleanup_deadline_met": not self.missed and not self.timing_unknown,
            "cleanup_timing_unknown": self.timing_unknown,
            "case_deadline_met": not self.case_missed,
        }
