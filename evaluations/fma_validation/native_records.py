"""Closed selected native evidence; raw proc parsing is not retained payload."""

from __future__ import annotations

from dataclasses import dataclass

from evaluations.linux_resource.decoder_startup import (
    ADDRESS_SPACE_BYTES,
    DATA_BYTES,
    STACK_BYTES,
)
from evaluations.linux_resource.owned_mp3_decode import MAPPED_ELF, SPECIAL_MAPS

NATIVE_RECORD_BYTES = 32_768
MAP_ROWS = 64
LIMIT_VALUES = (
    ("Max data size", DATA_BYTES),
    ("Max stack size", STACK_BYTES),
    ("Max address space", ADDRESS_SPACE_BYTES),
    ("Max core file size", 0),
)
LIMIT_KEYS = tuple((key.encode("ascii"), value) for key, value in LIMIT_VALUES)
ELF_PATHS = tuple(MAPPED_ELF)
MAP_PATHS = tuple(
    (path.encode("ascii"), path)
    for path in (*ELF_PATHS, *sorted(SPECIAL_MAPS), "[heap]", "[stack]", "")
)
MAP_PERMISSIONS = tuple(
    (value.encode("ascii"), value) for value in ("---p", "--xp", "-w-p", "r--p", "r-xp", "rw-p")
)
SPECIAL_SCOPE = "kernel code/time only; no numeric PCM allowance"


@dataclass(frozen=True)
class NativeLimits:
    values: tuple[int, ...]

    def record(self) -> dict[str, int]:
        return {key: self.values[index] for index, (key, _) in enumerate(LIMIT_VALUES)}


@dataclass(frozen=True)
class NativeMap:
    path: str
    permissions: str
    size: int

    def record(self) -> dict[str, object]:
        # Selected values alias their immutable backing; only containers are new.
        return {"path": self.path, "permissions": self.permissions, "bytes": self.size}


@dataclass(frozen=True)
class NativeMaps:
    records: tuple[NativeMap, ...]
    ordinary: int
    special: int
    sha256: str

    def record(self) -> dict[str, object]:
        return {
            "records": [item.record() for item in self.records],
            "ordinary_bytes_observed": self.ordinary,
            "special_bytes_observed": self.special,
            "maps_sha256": self.sha256,
            "special_scope": SPECIAL_SCOPE,
            "sampled_pcm_proof": False,
        }
