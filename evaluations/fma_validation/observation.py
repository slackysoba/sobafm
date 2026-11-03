"""Finite FMA process observations with one retained owner and selection boundary.

Local proc/libc control scratch stays in the full group charge. Selected identity
evidence and report copies remain charged payload in their existing partitions.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Protocol, cast

from evaluations.linux_resource.probe import ProbeError

from .limits import CaseClock
from .native_records import (
    ADDRESS_SPACE_BYTES,
    ELF_PATHS,
    LIMIT_KEYS,
    MAP_PATHS,
    MAP_PERMISSIONS,
    MAP_ROWS,
    NATIVE_RECORD_BYTES,
    SPECIAL_MAPS,
    NativeLimits,
    NativeMap,
    NativeMaps,
)

INPUT_LIMIT = 65_536
SELECTED_PIN_BYTES = 8192
PID_MAX = (1 << 31) - 1
WORD_MAX = (1 << 64) - 1
PIN_KEYS = frozenset(
    (
        "pid",
        "start",
        "parent",
        "executable",
        "executable_device",
        "executable_inode",
        "uid",
        "group",
    )
)


def bounded_integer(value: object, maximum: int, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProbeError("observation_integer")
    return value


def executable_text(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 64
        or not value.startswith("/")
        or any(not 32 <= ord(char) <= 126 for char in value)
    ):
        raise ProbeError("observation_executable_text")
    return value


@dataclass(frozen=True)
class ProcessPin:
    pid: int
    start: int
    parent: int
    executable: str
    executable_device: int
    executable_inode: int
    uid: int
    group: str

    @classmethod
    def from_record(cls, value: object) -> ProcessPin:
        if not isinstance(value, dict):
            raise ProbeError("invalid_process_record")
        fields = cast(dict[str, object], value)
        if (
            len(fields) != len(PIN_KEYS)
            or fields.keys() != PIN_KEYS
            or type(fields["group"]) is not str
            or fields["group"] != "/"
        ):
            raise ProbeError("invalid_process_record")
        return cls(
            bounded_integer(fields["pid"], PID_MAX, 1),
            bounded_integer(fields["start"], WORD_MAX, 1),
            bounded_integer(fields["parent"], PID_MAX),
            executable_text(fields["executable"]),
            bounded_integer(fields["executable_device"], WORD_MAX),
            bounded_integer(fields["executable_inode"], WORD_MAX, 1),
            bounded_integer(fields["uid"], 1000, 1000),
            "/",
        )

    @classmethod
    def capture(cls, pid: int, workspace: ObservationWorkspace) -> ProcessPin:
        return workspace.capture(pid)

    def verify(self, workspace: ObservationWorkspace) -> None:
        workspace.verify(self)

    def record(self) -> dict[str, object]:
        # Eight fixed keys, at most 64 ASCII executable characters. The caller
        # owns/charges this new projection and its later serialization copies.
        return {
            "pid": self.pid,
            "start": self.start,
            "parent": self.parent,
            "executable": self.executable,
            "executable_device": self.executable_device,
            "executable_inode": self.executable_inode,
            "uid": self.uid,
            "group": self.group,
        }


@dataclass(frozen=True)
class StatFacts:
    pid: int
    state: str
    parent: int
    group: int
    session: int
    tty: int
    threads: int
    start: int


@dataclass(frozen=True)
class SecurityFacts:
    parent: int
    threads: int


@dataclass(frozen=True)
class ExecutableFacts:
    text: str
    device: int
    inode: int


@dataclass(frozen=True)
class DirectoryFacts:
    # Total includes discarded nonnumeric entries; enforce while enumerating.
    entries: int
    identifiers: tuple[int, ...]


class ObservationBridge(Protocol):
    @property
    def debt(self) -> bool: ...

    def executable(self, pid: int, charge: Callable[[int], None]) -> ExecutableFacts: ...

    def directory(
        self, pid: int | None, name: str, limit: int, charge: Callable[[int], None]
    ) -> DirectoryFacts: ...


class DirectoryIterator(Protocol):
    def __iter__(self) -> Iterator[os.DirEntry[bytes]]: ...

    def close(self) -> None: ...


class LinuxObservationBridge:
    """Maintained local APIs; no claim about a 32KiB libc/stack/RSS maximum.

    Returned names are control scratch until bounded selection. Retain at most
    64 executable bytes plus their 64-character ASCII projection simultaneously;
    directory results retain only <=64 bounded numeric identifiers. This owner
    keeps the actual iterator and pending selected IDs through uncertain close.
    """

    def __init__(self, clock: CaseClock) -> None:
        self.clock = clock
        self.iterator: DirectoryIterator | None = None
        self.pending: tuple[int, ...] = ()
        self.uncertain_close = False

    @property
    def debt(self) -> bool:
        return self.iterator is not None or self.uncertain_close

    def executable(self, pid: int, charge: Callable[[int], None]) -> ExecutableFacts:
        path = f"/proc/{pid}/exe".encode("ascii")
        raw = os.readlink(path)
        charge(len(raw))
        if not 1 <= len(raw) <= 64 or raw[0] != 47 or any(not 32 <= byte <= 126 for byte in raw):
            raise ProbeError("observation_executable_text")
        # The bounded returned bytes become selected payload at this boundary;
        # raw+decoded ASCII coexist until this method actually returns.
        text = raw.decode("ascii")
        info = os.stat(path)
        return ExecutableFacts(text, info.st_dev, info.st_ino)

    def directory(
        self, pid: int | None, name: str, limit: int, charge: Callable[[int], None]
    ) -> DirectoryFacts:
        if self.debt or name not in ("proc", "task", "fd"):
            raise ProbeError("observation_directory_owner")
        path = b"/proc" if pid is None and name == "proc" else f"/proc/{pid}/{name}".encode("ascii")
        selected: list[int] = []
        entries = 0
        iterator = os.scandir(path)
        self.iterator = iterator
        try:
            for entry in iterator:
                entries += 1
                raw = entry.name
                charge(len(raw))
                if entries > 256:
                    raise ProbeError("observation_directory_limit")
                if not raw or any(byte < 48 or byte > 57 for byte in raw):
                    if name != "proc":
                        raise ProbeError("observation_directory_entry")
                    continue
                if len(raw) > 10 or len(selected) == limit:
                    raise ProbeError("observation_directory_limit")
                value = 0
                for byte in raw:
                    digit = byte - 48
                    if value > (PID_MAX - digit) // 10:
                        raise ProbeError("observation_directory_pid")
                    value = value * 10 + digit
                if (value == 0 and name != "fd") or value in selected:
                    raise ProbeError("observation_directory_pid")
                selected.append(value)
            self.pending = tuple(selected)
        except BaseException:
            self.clock.cancel()
            self.pending = tuple(selected)
            raise
        finally:
            try:
                iterator.close()
                self.iterator = None
            except BaseException:
                self.clock.cancel()
                self.uncertain_close = True
                raise
        result = DirectoryFacts(entries, self.pending)
        self.pending = ()
        return result


class FileIO(Protocol):
    def open(self, path: bytes) -> int: ...

    def read(self, fd: int, target: memoryview) -> int: ...

    def close(self, fd: int) -> None: ...


class ProcFileIO:
    def open(self, path: bytes) -> int:
        return os.open(path, os.O_RDONLY | os.O_CLOEXEC)

    def read(self, fd: int, target: memoryview) -> int:
        return os.readv(fd, (target,))

    def close(self, fd: int) -> None:
        os.close(fd)


@dataclass(frozen=True)
class ObservationFacts:
    pins: tuple[ProcessPin, ...] = ()
    stats: tuple[StatFacts, ...] = ()
    members: tuple[int, ...] = ()
    limits: NativeLimits | None = None
    maps: NativeMaps | None = None


class ObservationWorkspace:
    """One actual caller's lease, including blocked reads/closes and staged facts.

    Fixed byte layout: [0:2048] read, [2048:4096] whole stat/cgroup,
    [4096:4608] line carry; remaining bytes reserved, never a growth allowance.
    Committed + staged + temporary file facts coexist. Each snapshot has <=4
    pins, <=64 stat facts and <=64 PIDs; executable text <=64 ASCII characters.
    An 8192-byte selected-pin reservation is prepaid within this owner's existing
    copy partition, separate from report serialization/IPC and native map evidence.
    It remains charged with external immutable aliases after snapshot replacement.
    Raw local scan/token/stat/iterator scratch remains in the full group charge;
    these policy limits are neither universal proc maxima nor an RSS/fit proof.
    """

    def __init__(
        self, clock: CaseClock, bridge: ObservationBridge | None = None, io: FileIO | None = None
    ) -> None:
        self.clock = clock
        self.bridge: ObservationBridge | None = (
            LinuxObservationBridge(clock) if bridge is None else bridge
        )
        self.io = ProcFileIO() if io is None else io
        self.selected_pin_capacity = SELECTED_PIN_BYTES
        self.selected_native_capacity = NATIVE_RECORD_BYTES
        self.arena = bytearray(8192)
        self.lock = threading.Lock()
        self.fd: int | None = None
        self.active = False
        self.uncertain_close = False
        self.failure: str | None = None
        self.input_bytes = 0
        self.cleanup = False
        self.phase_end: float | None = None
        self.committed = ObservationFacts()
        self.staged = ObservationFacts()
        self.pending: StatFacts | SecurityFacts | tuple[int, ...] | None = None
        self.pending_maps: tuple[NativeMap, ...] = ()

    @property
    def debt(self) -> bool:
        return (
            self.active
            or self.fd is not None
            or self.uncertain_close
            or (self.bridge is not None and self.bridge.debt)
        )

    def _fail(self) -> None:
        self.failure = self.failure or "process_observation_failed"
        self.clock.cancel()

    def _check(self) -> None:
        self.clock.observe()
        if not self.cleanup and self.clock.remaining(self.phase_end) <= 0:
            raise ProbeError("observation_cancelled")

    def _run(
        self, action: Callable[[], None], cleanup: bool = False, phase_end: float | None = None
    ) -> ObservationFacts:
        if not self.lock.acquire(blocking=False):
            raise ProbeError("observation_lease_retained")
        if self.debt:
            self.lock.release()
            raise ProbeError("observation_debt_retained")
        self.active, self.cleanup = True, cleanup
        self.phase_end = phase_end
        self.input_bytes = 0
        self.staged = ObservationFacts()
        failed = False
        try:
            self._check()
            action()
            self._check()
        except BaseException:
            # Never retain an exception, raw input copy, traceback or error body.
            self._fail()
            failed = True
        finally:
            if self.fd is not None and not self.uncertain_close:
                try:
                    self.io.close(self.fd)
                    self.fd = None
                except BaseException:
                    self._fail()
                    self.uncertain_close = True
                    failed = True
            self.active = False
            bridge_debt = self.bridge is not None and self.bridge.debt
            if not failed and self.fd is None and not bridge_debt:
                self.committed = self.staged
            if not self.uncertain_close and not bridge_debt:
                self.staged = ObservationFacts()
                self.pending = None
                self.pending_maps = ()
            self.lock.release()
        if failed:
            raise ProbeError(self.failure or "process_observation_failed")
        return self.committed

    def _file(
        self,
        path: bytes,
        limit: int,
        consume: Callable[[int], None],
        finish: Callable[[], None] | None = None,
        chunk: Callable[[memoryview], None] | None = None,
    ) -> None:
        self._check()
        self.fd = self.io.open(path)
        count = 0
        try:
            while True:
                self._check()
                room = min(2048, limit - count + 1, INPUT_LIMIT - self.input_bytes + 1)
                with memoryview(self.arena)[:room] as target:
                    amount = self.io.read(self.fd, target)
                if not 0 <= amount <= room:
                    raise ProbeError("observation_read_contract")
                self.input_bytes += amount
                count += amount
                if count > limit or self.input_bytes > INPUT_LIMIT:
                    raise ProbeError("observation_input_limit")
                if amount == 0:
                    if finish is not None:
                        finish()
                    self._check()
                    break
                self._check()
                if chunk is not None:
                    with memoryview(self.arena)[:amount] as view:
                        chunk(view)
                for index in range(amount):
                    consume(self.arena[index])
        except BaseException:
            # First error starts cleanup BEFORE potentially blocked close.
            self._fail()
            raise
        finally:
            try:
                self.io.close(self.fd)
                self.fd = None
            except BaseException:
                self._fail()
                self.uncertain_close = True
                raise

    def _record(
        self,
        path: bytes,
        limit: int,
        finish: Callable[[int], None] | None = None,
    ) -> int:
        if not 0 < limit <= 2048:
            raise ProbeError("observation_record_storage")
        count = 0

        def consume(value: int) -> None:
            nonlocal count
            self.arena[2048 + count] = value
            count += 1

        def complete() -> None:
            if finish is not None:
                finish(count)

        self._file(path, limit, consume, complete)
        return count

    def _lines(
        self,
        path: bytes,
        limit: int,
        rows: int,
        width: int,
        row: Callable[[int], None],
        finish: Callable[[], None] | None = None,
        chunk: Callable[[memoryview], None] | None = None,
    ) -> None:
        count = used = 0

        def consume(value: int) -> None:
            nonlocal count, used
            if value == 10:
                if count == rows or used == 0:
                    raise ProbeError("observation_row_limit")
                row(used)
                count += 1
                used = 0
            else:
                if used == width or value > 127 or value < 9 or 10 < value < 32:
                    raise ProbeError("observation_row_encoding")
                self.arena[4096 + used] = value
                used += 1

        def complete() -> None:
            if used or count == 0:
                raise ProbeError("observation_incomplete_rows")
            if finish is not None:
                finish()

        self._file(path, limit, consume, complete, chunk)

    def _number(
        self, start: int, end: int, maximum: int, signed: bool = False, base: int = 10
    ) -> int:
        negative = signed and start < end and self.arena[start] == 45
        start += int(negative)
        if not 1 <= end - start <= (16 if base == 16 else 20):
            raise ProbeError("observation_number_width")
        ceiling = min(maximum, 1 << 63) if negative else maximum
        value = 0
        for index in range(start, end):
            digit = self.arena[index] - 48
            if base == 16 and 49 <= digit <= 54:
                digit -= 39
            elif base == 16 and 17 <= digit <= 22:
                digit -= 7
            if not 0 <= digit < base or value > (ceiling - digit) // base:
                raise ProbeError("observation_number_range")
            value = value * base + digit
        return -value if negative else value

    def _matches(self, start: int, end: int, expected: bytes) -> bool:
        return end - start == len(expected) and all(
            self.arena[start + index] == value for index, value in enumerate(expected)
        )

    def _tokens(self, start: int, end: int, count: int) -> tuple[list[tuple[int, int]], int]:
        # At most five bounded spans, never a decoded row or an unbounded split.
        values: list[tuple[int, int]] = []
        while start < end and len(values) < count:
            while start < end and self.arena[start] in (9, 32):
                start += 1
            stop = start
            while stop < end and self.arena[stop] not in (9, 32):
                stop += 1
            if start < stop:
                values.append((start, stop))
            start = stop
        while start < end and self.arena[start] in (9, 32):
            start += 1
        return values, start

    def _limits(self, pid: int) -> None:
        bounded_integer(pid, PID_MAX, 1)
        values = [0] * len(LIMIT_KEYS)
        seen = 0

        def row(size: int) -> None:
            nonlocal seen
            end = 4096 + size
            for index, (key, expected) in enumerate(LIMIT_KEYS):
                key_end = 4096 + len(key)
                if key_end >= end or not self._matches(4096, key_end, key):
                    continue
                if self.arena[key_end] not in (9, 32):
                    raise ProbeError("observation_limit_key")
                if seen & (1 << index):
                    raise ProbeError("observation_duplicate_limit")
                tokens, tail = self._tokens(key_end, end, 3)
                if len(tokens) != 3 or tail != end or not self._matches(*tokens[2], b"bytes"):
                    raise ProbeError("observation_limit_units")
                soft, hard = (self._number(*token, WORD_MAX) for token in tokens[:2])
                if (
                    soft != expected
                    or hard != expected
                    or any(stop - start != len(str(expected)) for start, stop in tokens[:2])
                ):
                    raise ProbeError("observation_limit_value")
                values[index] = soft
                seen |= 1 << index
                return

        def complete() -> None:
            if seen != (1 << len(LIMIT_KEYS)) - 1:
                raise ProbeError("observation_limit_missing")
            self.staged = replace(self.staged, limits=NativeLimits(tuple(values)))

        self._lines(f"/proc/{pid}/limits".encode("ascii"), 16_384, 64, 256, row, complete)

    def native_limits(self, pid: int, deadline: float) -> NativeLimits:
        result = self._run(lambda: self._limits(pid), phase_end=deadline).limits
        assert result is not None
        return result

    def _hex(self, start: int, end: int, width: int = 16) -> int:
        if end - start > width or any(
            not (48 <= self.arena[index] <= 57 or 97 <= self.arena[index] <= 102)
            for index in range(start, end)
        ):
            raise ProbeError("observation_map_hex")
        return self._number(start, end, WORD_MAX, base=16)

    def _maps(self, pid: int) -> None:
        bounded_integer(pid, PID_MAX, 1)
        digest = hashlib.sha256()
        previous = ordinary = special = 0
        files: set[str] = set()
        self.pending_maps = ()

        def row(size: int) -> None:
            nonlocal previous, ordinary, special
            if len(self.pending_maps) == MAP_ROWS:
                raise ProbeError("observation_map_count")
            end = 4096 + size
            tokens, path_start = self._tokens(4096, end, 5)
            if len(tokens) != 5:
                raise ProbeError("observation_map_fields")
            address, permissions, offset, device, inode_span = tokens
            dash = self.arena.find(b"-", *address)
            colon = self.arena.find(b":", *device)
            if dash < 0 or colon < 0:
                raise ProbeError("observation_map_fields")
            low, high = self._hex(address[0], dash), self._hex(dash + 1, address[1])
            self._hex(*offset)
            self._hex(device[0], colon, 8)
            self._hex(colon + 1, device[1], 8)
            inode = self._number(*inode_span, WORD_MAX)
            perms = next(
                (text for raw, text in MAP_PERMISSIONS if self._matches(*permissions, raw)), None
            )
            name = next(
                (text for raw, text in MAP_PATHS if self._matches(path_start, end, raw)), None
            )
            if perms is None or name is None:
                raise ProbeError("observation_map_scope")
            extent = high - low
            if extent <= 0 or low < previous or low % 4096 or high % 4096:
                raise ProbeError("observation_map_extent")
            previous = high
            if name in SPECIAL_MAPS:
                if "w" in perms or inode != 0:
                    raise ProbeError("observation_special_map")
                special = bounded_integer(special + extent, WORD_MAX)
            else:
                ordinary = bounded_integer(ordinary + extent, ADDRESS_SPACE_BYTES)
                if name in ELF_PATHS:
                    if inode == 0:
                        raise ProbeError("observation_map_inode")
                    files.add(name)
                elif inode != 0 or "x" in perms:
                    raise ProbeError("observation_anonymous_map")
            # Every admitted row is selected, including anonymous/special rows.
            self.pending_maps = (*self.pending_maps, NativeMap(name or "anonymous", perms, extent))

        def complete() -> None:
            if files != set(ELF_PATHS) or not self.pending_maps:
                raise ProbeError("observation_map_inventory")
            self.staged = replace(
                self.staged,
                maps=NativeMaps(self.pending_maps, ordinary, special, digest.hexdigest()),
            )

        self._lines(
            f"/proc/{pid}/maps".encode("ascii"), 65_536, MAP_ROWS, 512, row, complete, digest.update
        )

    def _stat(self, pid: int) -> StatFacts:
        bounded_integer(pid, PID_MAX, 1)

        def complete(size: int) -> None:
            self.pending = self._parse_stat(pid, size)

        self._record(f"/proc/{pid}/stat".encode("ascii"), 2048, complete)
        assert isinstance(self.pending, StatFacts)
        return self.pending

    def _parse_stat(self, pid: int, size: int) -> StatFacts:
        start, end = 2048, 2048 + size
        closing = self.arena.rfind(b")", start, end)
        opening = self.arena.find(b" (", start, end)
        if (
            opening < 0
            or closing < opening + 2
            or closing - opening - 2 > 64
            or size == 0
            or self.arena[end - 1] != 10
            or any(value > 127 or value == 0 for value in memoryview(self.arena)[start:end])
            or self._number(start, opening, PID_MAX) != pid
            or closing + 4 >= end
            or self.arena[closing + 1] != 32
            or self.arena[closing + 3] != 32
        ):
            raise ProbeError("observation_stat_record")
        state = chr(self.arena[closing + 2])
        if state not in "RSDZTWtXxKWPIN":
            raise ProbeError("observation_stat_state")
        values: list[int] = []
        cursor = closing + 4
        while cursor < end - 1:
            stop = self.arena.find(b" ", cursor, end - 1)
            if stop < 0:
                stop = end - 1
            if len(values) == 61:
                raise ProbeError("observation_stat_fields")
            values.append(self._number(cursor, stop, WORD_MAX, signed=True))
            cursor = stop + 1
        # Linux fields 1..52, with room for bounded future trailing fields.
        if len(values) < 49:
            raise ProbeError("observation_stat_fields")
        result = StatFacts(
            pid,
            state,
            bounded_integer(values[0], PID_MAX),
            bounded_integer(values[1], PID_MAX),
            bounded_integer(values[2], PID_MAX),
            values[3],
            bounded_integer(values[16], PID_MAX, 1),
            bounded_integer(values[18], WORD_MAX, 1),
        )
        self.pending = result
        return result

    def _security(self, pid: int) -> SecurityFacts:
        keys = (b"PPid", b"Uid", b"Gid", b"NoNewPrivs", b"Seccomp", b"CapEff", b"Threads")
        seen: list[int] = [0]
        parent = threads = 0

        def row(size: int) -> None:
            nonlocal parent, threads
            end = 4096 + size
            colon = self.arena.find(b":", 4096, end)
            if not 4096 < colon <= 4160:
                raise ProbeError("observation_status_key")
            with memoryview(self.arena)[4096:colon] as key:
                selected = next(
                    (
                        index
                        for index, expected in enumerate(keys)
                        if len(key) == len(expected)
                        and all(key[index] == value for index, value in enumerate(expected))
                    ),
                    -1,
                )
            if selected < 0:
                return
            if seen[0] & (1 << selected):
                raise ProbeError("observation_duplicate_status")
            seen[0] |= 1 << selected
            cursor = colon + 1
            values: list[int] = []
            while cursor < end:
                while cursor < end and self.arena[cursor] in (9, 32):
                    cursor += 1
                if cursor == end:
                    break
                stop = cursor
                while stop < end and self.arena[stop] not in (9, 32):
                    stop += 1
                if len(values) == 4:
                    raise ProbeError("observation_status_tokens")
                values.append(
                    self._number(cursor, stop, WORD_MAX, base=16 if selected == 5 else 10)
                )
                cursor = stop
            if selected in (1, 2):
                valid = values == [1000, 1000, 1000, 1000]
            else:
                valid = len(values) == 1
                if valid and selected in (3, 4, 5):
                    valid = values[0] == {3: 1, 4: 2, 5: 0}[selected]
            if not valid:
                raise ProbeError("process_security_identity")
            if selected == 0:
                parent = bounded_integer(values[0], PID_MAX)
            if selected == 6:
                threads = bounded_integer(values[0], PID_MAX, 1)

        def complete() -> None:
            if seen[0] != 127:
                raise ProbeError("observation_status_missing")
            self.pending = SecurityFacts(parent, threads)

        self._lines(f"/proc/{pid}/status".encode("ascii"), 16_384, 128, 512, row, complete)
        assert isinstance(self.pending, SecurityFacts)
        return self.pending

    def _pids(self) -> tuple[int, ...]:
        values: list[int] = []

        def row(size: int) -> None:
            value = self._number(4096, 4096 + size, PID_MAX)
            if not value or value in values:
                raise ProbeError("observation_duplicate_pid")
            values.append(value)

        def complete() -> None:
            self.pending = tuple(values)

        self._lines(b"/sys/fs/cgroup/cgroup.procs", 704, 64, 10, row, complete)
        assert isinstance(self.pending, tuple)
        return self.pending

    def _bridge(self) -> ObservationBridge:
        if self.bridge is None:
            self.failure = self.failure or "observation_bridge_unavailable"
            raise ProbeError("observation_bridge_unavailable")
        return self.bridge

    def _executable(self, pid: int) -> ExecutableFacts:
        result = self._bridge().executable(pid, self._charge_bridge)
        executable_text(result.text)
        bounded_integer(result.device, WORD_MAX)
        bounded_integer(result.inode, WORD_MAX, 1)
        return result

    def _directory(self, pid: int | None, name: str, limit: int) -> tuple[int, ...]:
        if pid is not None:
            bounded_integer(pid, PID_MAX, 1)
        result = self._bridge().directory(pid, name, limit, self._charge_bridge)
        bounded_integer(result.entries, 256)
        if len(result.identifiers) > limit or result.entries < len(result.identifiers):
            raise ProbeError("observation_directory_limit")
        for index, value in enumerate(result.identifiers):
            bounded_integer(value, PID_MAX, 0 if name == "fd" else 1)
            if any(result.identifiers[previous] == value for previous in range(index)):
                raise ProbeError("observation_directory_duplicate")
        return result.identifiers

    def _charge_bridge(self, amount: int) -> None:
        bounded_integer(amount, INPUT_LIMIT)
        self.input_bytes += amount
        if self.input_bytes > INPUT_LIMIT:
            raise ProbeError("observation_input_limit")
        self._check()

    def _capture(self, pid: int) -> ProcessPin:
        self._bridge()
        if len(self.staged.pins) == 4:
            raise ProbeError("observation_pin_limit")
        first = self._stat(pid)
        executable = self._executable(pid)
        security = self._security(pid)
        self._cgroup(pid)
        last = self._stat(pid)
        if (
            (
                first.pid,
                first.start,
                first.parent,
                first.group,
                first.session,
                first.tty,
                first.threads,
            )
            != (last.pid, last.start, last.parent, last.group, last.session, last.tty, last.threads)
            or executable != self._executable(pid)
            or security.parent != first.parent
            or security.threads != first.threads
        ):
            raise ProbeError("process_identity_changed")
        pin = ProcessPin(
            pid,
            first.start,
            first.parent,
            executable.text,
            executable.device,
            executable.inode,
            1000,
            "/",
        )
        self.staged = replace(self.staged, pins=(*self.staged.pins, pin))
        return pin

    def _cgroup(self, pid: int) -> None:
        def complete(size: int) -> None:
            if size != 5 or any(
                self.arena[2048 + index] != value for index, value in enumerate(b"0::/\n")
            ):
                raise ProbeError("observation_cgroup")

        self._record(f"/proc/{pid}/cgroup".encode("ascii"), 16, complete)

    def capture(self, pid: int) -> ProcessPin:
        return self._run(lambda: self._capture_action(pid)).pins[0]

    def _capture_action(self, pid: int) -> None:
        self._capture(pid)

    def verify(self, pin: ProcessPin, cleanup: bool = False) -> None:
        def action() -> None:
            if self._capture(pin.pid) != pin:
                raise ProbeError("process_identity_changed")

        self._run(action, cleanup)

    def inventory(
        self,
        init: ProcessPin,
        owner: ProcessPin,
        helper: ProcessPin | None = None,
        cleanup: bool = False,
    ) -> set[int]:
        def action() -> None:
            roles = (init, owner) if helper is None else (init, owner, helper)
            if init.pid != 1 or owner.parent != 1 or owner.pid == 1:
                raise ProbeError("init_owner_topology")
            if helper is not None and (helper.parent != owner.pid or helper.pid in (1, owner.pid)):
                raise ProbeError("helper_topology")
            for pin in roles:
                if self._capture(pin.pid) != pin:
                    raise ProbeError("process_identity_changed")
            members = self._pids()
            if any(pin.pid not in members for pin in roles):
                raise ProbeError("process_role_missing")
            self.staged = ObservationFacts(self.staged.pins, (), members)

        facts = self._run(action, cleanup)
        return set(facts.members) - {pin.pid for pin in facts.pins}

    def start_identity(self, pid: int, cleanup: bool = False) -> tuple[str, int]:
        def action() -> None:
            self.staged = ObservationFacts(stats=(self._stat(pid),))

        result = self._run(action, cleanup).stats[0]
        return result.state, result.start

    def group_members(self, group: int, cleanup: bool = False) -> list[int]:
        bounded_integer(group, PID_MAX, 1)

        def action() -> None:
            members: list[int] = []
            for pid in self._directory(None, "proc", 64):
                first = self._stat(pid)
                last = self._stat(pid)
                if (first.start, first.group) != (last.start, last.group):
                    raise ProbeError("process_inventory_unknown")
                if first.group == group:
                    members.append(pid)
                self.staged = ObservationFacts(stats=(*self.staged.stats, last))
            self.staged = ObservationFacts(stats=self.staged.stats, members=tuple(members))

        return sorted(self._run(action, cleanup).members)

    def single_thread(self, pid: int) -> None:
        def action() -> None:
            if self._directory(pid, "task", 1) != (pid,):
                raise ProbeError("observation_threads")

        self._run(action)

    def native_descriptors(self, pid: int) -> None:
        def action() -> None:
            facts = self._stat(pid)
            if facts.tty != 0 or set(self._directory(pid, "fd", 3)) != {0, 1, 2}:
                raise ProbeError("native_controlling_terminal_or_extra_descriptor")
            self.staged = ObservationFacts(stats=(facts,))

        self._run(action)

    def child_identity(self, pid: int, start: int, parent: int) -> None:
        def action() -> None:
            pin = self._capture(pid)
            facts = self._stat(pid)
            if pin.start != start or pin.parent != parent or facts.group != pid:
                raise ProbeError("native_child_identity")
            self.staged = ObservationFacts(self.staged.pins, (facts,))

        self._run(action)

    def stopped_identity(
        self,
        pid: int,
        start: int,
        executable: str,
        init: ProcessPin,
        owner: ProcessPin,
        runner: ProcessPin,
    ) -> None:
        self._run(lambda: self._stopped_identity(pid, start, executable, init, owner, runner))

    def _stopped_identity(
        self,
        pid: int,
        start: int,
        executable: str,
        init: ProcessPin,
        owner: ProcessPin,
        runner: ProcessPin,
    ) -> None:
        self.staged = replace(self.staged, pins=(), stats=(), members=())
        if init.pid != 1 or owner.parent != 1 or runner.parent != owner.pid:
            raise ProbeError("native_role_topology")
        roles = (init, owner, runner)
        if len({pin.pid for pin in roles}) != 3 or pid in {pin.pid for pin in roles}:
            raise ProbeError("native_role_topology")
        for pin in roles:
            if self._capture(pin.pid) != pin:
                raise ProbeError("process_identity_changed")
        native = self._capture(pid)
        stopped = self._stat(pid)
        if (
            native.start != start
            or native.parent != runner.pid
            or native.executable != executable
            or stopped.state != "T"
            or stopped.group != pid
            or stopped.start != start
            or self._directory(pid, "task", 1) != (pid,)
            or set(self._pids()) != {init.pid, owner.pid, runner.pid, pid}
        ):
            raise ProbeError("native_stopped_identity")
        members: list[int] = []
        for member in self._directory(None, "proc", 64):
            first = self._stat(member)
            last = self._stat(member)
            if (first.start, first.group) != (last.start, last.group):
                raise ProbeError("process_inventory_unknown")
            if first.group == pid:
                members.append(member)
        if members != [pid]:
            raise ProbeError("native_group_members")
        self.staged = replace(self.staged, stats=(stopped,), members=(pid,))

    def stopped_native(
        self,
        pid: int,
        start: int,
        executable: str,
        init: ProcessPin,
        owner: ProcessPin,
        runner: ProcessPin,
        deadline: float,
    ) -> tuple[NativeLimits, NativeMaps]:
        def action() -> None:
            self._stopped_identity(pid, start, executable, init, owner, runner)
            identity = self.staged.pins[-1]
            self._limits(pid)
            self._maps(pid)
            # A complete second bracket covers identity, security, threads and
            # namespace membership after evidence reads, under the same budget.
            self._stopped_identity(pid, start, executable, init, owner, runner)
            if self.staged.pins[-1] != identity:
                raise ProbeError("process_identity_changed")

        result = self._run(action, phase_end=deadline)
        assert result.limits is not None and result.maps is not None
        return result.limits, result.maps
