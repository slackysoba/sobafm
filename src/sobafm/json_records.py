"""Compact sorted ASCII records with per-operation counting, backing and hashing.

Callers own stable values through traversal. The evaluation supplies its stricter
policy; intake keeps builtin scalar spellings. Hash chunks bound document storage,
not arbitrary direct-constructor scalar conversion or retained source graphs.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import cast

HASH_BYTES = 4096
_HEX = b"0123456789abcdef"
_SHORT_ESCAPES = {34: 34, 92: 92, 8: 98, 12: 102, 10: 110, 13: 114, 9: 116}


class RecordError(ValueError):
    """A neutral record failure; callers translate it at their workflow boundary."""


@dataclass(frozen=True)
class Policy:
    """Only the scalar/structure restrictions that differ between the two callers."""

    exact_types: bool = False
    integer_bits: int | None = None
    finite_numbers: bool = False
    max_depth: int | None = None
    max_nodes: int | None = None


COMPACT_POLICY = Policy()


class JsonBuffer:
    """One traversal; no recursive closure or fill frame aliases its work arena."""

    def __init__(
        self,
        limit: int | None,
        *,
        policy: Policy = COMPACT_POLICY,
        hashed: bool = False,
    ) -> None:
        self.limit, self.policy = limit, policy
        self.backing: bytearray | None = None
        self.offset = self.nodes = self.used = 0
        self.hasher = hashlib.sha256() if hashed else None
        self.active: set[int] = set()

    def put(self, number: int) -> None:
        if self.limit is not None and self.offset >= self.limit:
            raise RecordError("capacity_exceeded")
        if self.backing is not None:
            if self.hasher is None:
                self.backing[self.offset] = number
            else:
                self.backing[self.used] = number
                self.used += 1
                if self.used == HASH_BYTES:
                    self.flush_hash()
        self.offset += 1

    def flush_hash(self) -> None:
        assert self.hasher is not None and self.backing is not None
        if self.used:
            with memoryview(self.backing) as view, view[: self.used] as chunk:
                self.hasher.update(chunk)
            self.used = 0

    def ascii(self, text: str) -> None:
        # Fixed literals or builtin scalar spellings; no encoded scalar copy.
        for char in text:
            self.put(ord(char))

    def hex_escape(self, number: int) -> None:
        self.put(92)
        self.put(117)
        for shift in (12, 8, 4, 0):
            self.put(_HEX[(number >> shift) & 15])

    def string(self, text: str) -> None:
        self.put(34)
        # Read accepted str-subclass data without custom iteration/conversion or
        # a full-string normalization copy. Each temporary is one code point.
        for char in str.__iter__(text):
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

    def integer(self, item: int) -> None:
        bits = int.bit_length(item)
        if self.policy.integer_bits is not None and bits > self.policy.integer_bits:
            raise RecordError("integer_limit")
        if self.limit is not None:
            # log10(2) > 3/10: reject definitely impossible decimal spellings
            # before conversion, including size-only passes. Remaining admitted
            # scalar text is conservatively <= 2L, not a zero-payload counter.
            minimum = max(1, (bits - 1) * 3 // 10 + 1) + int(item < 0)
            if minimum > self.limit - self.offset:
                raise RecordError("capacity_exceeded")
        self.ascii(int.__repr__(item))

    def walk(self, item: object, depth: int) -> None:
        self.nodes += 1
        if (self.policy.max_depth is not None and depth > self.policy.max_depth) or (
            self.policy.max_nodes is not None and self.nodes > self.policy.max_nodes
        ):
            raise RecordError("structure_limit")
        if self.policy.exact_types and type(item) not in (
            type(None),
            bool,
            str,
            int,
            float,
            list,
            tuple,
            dict,
        ):
            raise RecordError("unsupported_value")
        if item is None:
            self.ascii("null")
        elif type(item) is bool:
            self.ascii("true" if item else "false")
        elif isinstance(item, str):
            self.string(item)
        elif isinstance(item, int):
            self.integer(item)
        elif isinstance(item, float):
            if math.isfinite(item):
                self.ascii(float.__repr__(item))
            elif self.policy.finite_numbers:
                raise RecordError("nonfinite_number")
            else:
                self.ascii("NaN" if math.isnan(item) else "-Infinity" if item < 0 else "Infinity")
        elif isinstance(item, (list, tuple, dict)):
            values = cast(list[object] | tuple[object, ...] | dict[object, object], item)
            if (
                self.policy.max_nodes is not None
                and len(values) > self.policy.max_nodes - self.nodes
            ):
                raise RecordError("structure_limit")
            # Bounded evaluation cycles retain their existing structure failure.
            # Ordinary intake JSON has no new depth/node limit or output cap.
            tracked = self.policy.max_depth is None
            if tracked:
                if id(values) in self.active:
                    raise RecordError("circular_value")
                self.active.add(id(values))
            try:
                if isinstance(item, dict):
                    self.mapping(cast(dict[object, object], item), depth)
                else:
                    self.put(91)
                    for index, child in enumerate(cast(list[object] | tuple[object, ...], item)):
                        if index:
                            self.put(44)
                        self.walk(child, depth + 1)
                    self.put(93)
            finally:
                if tracked:
                    self.active.remove(id(values))
        else:
            raise RecordError("unsupported_value")

    def mapping(self, fields: dict[object, object], depth: int) -> None:
        for key in fields:
            if not isinstance(key, str) or (self.policy.exact_types and type(key) is not str):
                raise RecordError("string_key_required")
        # Canonical order is independent of sink mode, including hash-only mode.
        # Sorting retains aliases, never normalized key copies.
        self.put(123)
        for index, key in enumerate(sorted(fields, key=lambda key: cast(str, key))):
            if not isinstance(key, str) or (self.policy.exact_types and type(key) is not str):
                raise RecordError("string_key_required")
            if index:
                self.put(44)
            self.string(key)
            self.put(58)
            self.walk(fields[key], depth + 1)
        self.put(125)


def json_size(value: object, limit: int, *, policy: Policy = COMPACT_POLICY) -> int:
    """Count within the caller's cap, without document backing or frozen output."""
    if type(limit) is not int or limit < 0:
        raise RecordError("invalid_limit")
    counter = JsonBuffer(limit, policy=policy)
    counter.walk(value, 0)
    return counter.offset


def encode_record(
    value: object, limit: int, *, newline: bool = False, policy: Policy = COMPACT_POLICY
) -> bytes:
    """Preflight before backing; recheck stable values before immutable freeze."""
    if type(newline) is not bool or type(limit) is not int or limit < 0:
        raise RecordError("invalid_limit")
    if limit < int(newline):
        raise RecordError("capacity_exceeded")
    size = json_size(value, limit - int(newline), policy=policy) + int(newline)
    work = JsonBuffer(size, policy=policy)
    try:
        work.backing = bytearray(size)
        work.walk(value, 0)
        if newline:
            work.put(10)
        if work.offset != size:
            raise RecordError("changed_during_encoding")
        assert work.backing is not None
        with memoryview(work.backing) as view:
            return bytes(view)
    finally:
        # Retained exceptions cannot keep rejected backing through this owner.
        # Scalar/sorted-key frames end before the separate immutable freeze.
        work.backing = None


def canonical_digest(value: object) -> str:
    """Hash the same compact wire through H bytes; no fingerprint-size ceiling."""
    work = JsonBuffer(None, hashed=True)
    try:
        work.backing = bytearray(HASH_BYTES)
        work.walk(value, 0)
        work.flush_hash()
        assert work.hasher is not None
        return work.hasher.hexdigest()
    finally:
        work.backing = None
