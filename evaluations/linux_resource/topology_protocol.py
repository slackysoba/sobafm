"""Fixed non-media launcher protocol for the two opt-in Q4 topology fixtures."""

from __future__ import annotations

import os
import resource
import select
import signal
import struct
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal, cast

from .decoder_startup import ADDRESS_SPACE_BYTES, CHILD_ENV, DATA_BYTES, STACK_BYTES, Child
from .owned_mp3_decode import ARGV, read_start_identity
from .owned_mp3_overlap import Pipes, make_pipes
from .probe import ProbeError

TopologyCase = Literal["ignoring_launcher", "descendant_after_leader_exit"]
RECORD = struct.Struct("!cIQi")
STATUS_LIMIT = 85
TERM_BIT = 1 << (signal.SIGTERM - 1)


@dataclass(frozen=True)
class TopologyPipes:
    media: Pipes
    command_read: int
    command_write: int
    status_read: int
    status_write: int

    def parent_ends(self) -> tuple[int, ...]:
        p = self.media
        return (
            p.input_write,
            p.output_read,
            p.error_read,
            p.ready_read,
            p.gate_write,
            self.command_write,
            self.status_read,
        )


def topology_pipes(owned: list[int]) -> TopologyPipes:
    media = make_pipes(owned)
    command = os.pipe()
    owned.extend(command)
    status = os.pipe()
    owned.extend(status)
    return TopologyPipes(media, *command, *status)


@dataclass(frozen=True)
class Record:
    tag: str
    pid: int
    start: int
    status: int

    def pack(self) -> bytes:
        return RECORD.pack(self.tag.encode("ascii"), self.pid, self.start, self.status)


class StatusReader:
    def __init__(self) -> None:
        self.partial = bytearray()
        self.total = 0
        self.seen: set[str] = set()
        self.pid = self.start = 0
        self.records: list[Record] = []
        self.commands: set[str] = set()
        self.decoder_status: int | None = None

    def accept(self, data: bytes) -> list[Record]:
        if self.total + len(data) > STATUS_LIMIT:
            raise ProbeError("launcher status lifetime bound exceeded")
        self.total += len(data)
        self.partial.extend(data)
        received: list[Record] = []
        while len(self.partial) >= RECORD.size:
            raw = bytes(self.partial[: RECORD.size])
            del self.partial[: RECORD.size]
            tag_bytes, pid, start, status = cast(tuple[bytes, int, int, int], RECORD.unpack(raw))
            tag = tag_bytes.decode("ascii")
            if tag not in {"H", "S", "P", "X", "E"} or tag in self.seen:
                raise ProbeError("unknown or repeated launcher status")
            if tag == "E":
                if not 1 <= status <= 10 or ((pid == 0) != (start == 0)):
                    raise ProbeError("invalid launcher error record")
                raise ProbeError(f"launcher failed with fixed error code {status}")
            if pid <= 1 or start <= 0:
                raise ProbeError("invalid decoder identity in launcher status")
            if tag == "H":
                if self.seen or status != 0:
                    raise ProbeError("invalid launcher HELLO ordering")
                self.pid, self.start = pid, start
            elif (pid, start) != (self.pid, self.start) or "H" not in self.seen:
                raise ProbeError("launcher decoder identity changed")
            if tag == "S" and (
                "S" not in self.commands
                or not 0 <= status <= 65535
                or not os.WIFSTOPPED(status)
                or os.WSTOPSIG(status) != signal.SIGSTOP
            ):
                raise ProbeError("launcher lacks commanded actual SIGSTOP wait evidence")
            if tag == "P" and ("P" not in self.commands or "S" not in self.seen or status != 0):
                raise ProbeError("unsolicited launcher PONG")
            if tag == "X":
                if not 0 <= status <= 65535 or not (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
                    raise ProbeError("launcher decoder status is not a terminal wait result")
                self.decoder_status = os.waitstatus_to_exitcode(status)
            record = Record(tag, pid, start, status)
            self.seen.add(tag)
            self.records.append(record)
            received.append(record)
        return received

    def eof(self) -> None:
        if self.partial:
            raise ProbeError("launcher status ended in a partial record")


def topology_decoder(pipes: TopologyPipes, owned: list[int]) -> None:
    """Only this child stays in the launcher's session; historical child code is unchanged."""
    p = pipes.media
    try:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        for source, target in ((p.input_read, 0), (p.output_write, 1), (p.error_write, 2)):
            os.dup2(source, target)
        for fd in owned:
            if fd not in {p.ready_write, p.gate_read}:
                os.close(fd)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_STACK, (STACK_BYTES, STACK_BYTES))
        resource.setrlimit(resource.RLIMIT_DATA, (DATA_BYTES, DATA_BYTES))
        resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
        os.write(p.ready_write, b"R")
        if os.read(p.gate_read, 1) != b"x":
            os._exit(125)
        os.close(p.gate_read)
        os.execve(ARGV[0], ARGV, CHILD_ENV)
    except BaseException:
        with suppress(OSError):
            os.write(p.ready_write, b"E")
        os._exit(125)


class Launcher:
    """Non-media helper: one child, two controls, finite metadata, no replacement work."""

    def __init__(self, case: TopologyCase, pipes: TopologyPipes, owned: list[int]) -> None:
        self.case, self.pipes, self.owned = case, pipes, owned
        self.decoder = Child(0)
        self.start = 0
        self.commands: list[str] = []
        self.sent: set[str] = set()
        self.pending: Record | None = None
        self.hello = False
        self.stop_status: int | None = None
        self.exit_status: int | None = None
        self.pong = False
        self.failure = 0
        self.controls = True
        self.wait_uncertain = False

    def setup(self) -> None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        try:
            os.setsid()
        except OSError:
            self.fault(1)
            raise
        try:
            pid = os.fork()
        except OSError:
            self.fault(2)
            raise
        if pid == 0:
            topology_decoder(self.pipes, self.owned)
            os._exit(125)
        self.decoder.pid = pid
        try:
            _, self.start = read_start_identity(pid)
        except OSError, ValueError, ProbeError:
            self.fault(3)
            raise
        for fd in tuple(self.owned):
            if fd not in {self.pipes.command_read, self.pipes.status_write}:
                os.close(fd)
                self.owned.remove(fd)
        null = os.open("/dev/null", os.O_RDWR)
        try:
            for fd in (0, 1, 2):
                os.dup2(null, fd)
        finally:
            os.close(null)
        for fd in (self.pipes.command_read, self.pipes.status_write):
            os.set_blocking(fd, False)
        if self.case == "ignoring_launcher":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.hello = True

    def close_controls(self) -> None:
        if not self.controls:
            return
        self.controls = False
        for fd in (self.pipes.command_read, self.pipes.status_write):
            with suppress(OSError):
                os.close(fd)
        # Any ambiguous close remains owned by this live process until A kills/reaps it.

    def fault(self, code: int) -> None:
        self.failure = self.failure or code

    def poll_decoder(self) -> None:
        if self.decoder.pid <= 0 or self.exit_status is not None or self.wait_uncertain:
            return
        flags = os.WNOHANG | (os.WUNTRACED if "S" in self.commands else 0)
        try:
            pid, status = os.waitpid(self.decoder.pid, flags)
        except OSError:
            self.wait_uncertain = True
            self.fault(8)
            return
        if not pid:
            return
        if os.WIFSTOPPED(status):
            if self.stop_status is not None or os.WSTOPSIG(status) != signal.SIGSTOP:
                self.fault(5)
            else:
                self.stop_status = status
        elif os.WIFEXITED(status) or os.WIFSIGNALED(status):
            self.exit_status = status
        else:
            self.fault(8)

    def command(self, value: bytes) -> bool:
        expected = ["S", "P" if self.case == "ignoring_launcher" else "Q"]
        if (
            not value
            or len(value) != 1
            or len(self.commands) >= 2
            or value != expected[len(self.commands)].encode("ascii")
        ):
            self.fault(6)
            return False
        tag = value.decode("ascii")
        if tag != "S" and "S" not in self.sent:
            self.fault(6)
            return False
        self.commands.append(tag)
        if tag == "S":
            if self.exit_status is not None:
                self.fault(4)
            else:
                try:
                    os.kill(self.decoder.pid, signal.SIGSTOP)
                except OSError:
                    self.fault(4)
        elif tag == "P":
            self.pong = True
        elif self.exit_status is None:
            return True
        else:
            self.fault(6)
        return False

    def next_record(self) -> None:
        if self.pending is not None:
            return
        candidates = (
            ("H", 0, self.hello),
            ("S", self.stop_status, self.stop_status is not None),
            ("P", 0, self.pong),
            ("X", self.exit_status, self.exit_status is not None),
            ("E", self.failure, self.failure != 0),
        )
        for tag, status, available in candidates:
            if available and tag not in self.sent:
                assert status is not None
                pid, start = (self.decoder.pid, self.start) if self.start else (0, 0)
                self.pending = Record(tag, pid, start, status)
                return

    def step(self) -> bool:
        self.poll_decoder()
        if not self.controls:
            select.select([], [], [], 0.01)
            return False
        self.next_record()
        readers = [self.pipes.command_read] if not self.failure else []
        writers = [self.pipes.status_write] if self.pending is not None else []
        readable, writable, _ = select.select(readers, writers, [], 0.01)
        if readable:
            try:
                if self.command(os.read(self.pipes.command_read, 1)):
                    return True
            except BlockingIOError, InterruptedError:
                pass
            except OSError:
                self.fault(6)
        if writable and self.pending is not None:
            try:
                if os.write(self.pipes.status_write, self.pending.pack()) != RECORD.size:
                    raise ProbeError("short status write")
                tag = self.pending.tag
                self.sent.add(tag)
                self.pending = None
                if tag == "E":
                    self.close_controls()
            except BlockingIOError, InterruptedError:
                pass
            except OSError, ProbeError:
                self.fault(7)
                self.close_controls()
        return False


def launcher_main(case: TopologyCase, pipes: TopologyPipes, owned: list[int]) -> None:
    launcher = Launcher(case, pipes, owned)
    try:
        launcher.setup()
    except BaseException:
        launcher.fault(10)
        # Setup never admits media. Keep every acquired resource in this process
        # until the original controller terminates/reaps it, even if reporting fails.
        for fd in (pipes.command_read, pipes.status_write):
            with suppress(OSError):
                os.set_blocking(fd, False)
    while True:
        try:
            if launcher.step():
                launcher.close_controls()
                os._exit(0)
        except BaseException:
            launcher.fault(10)
            launcher.close_controls()
