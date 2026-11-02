"""Exact local helper ownership; bounded pipes, no pathname-based media access."""

from __future__ import annotations

import errno
import hashlib
import os
import select
import signal
import struct
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from evaluations.linux_resource.probe import ProbeError, proc_fields, read_text, unified_path

from .limits import CHUNK_BYTES, REPORT_BYTES, CaseClock


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
        integers = ("pid", "start", "parent", "executable_device", "executable_inode", "uid")
        if set(fields) != {*integers, "executable", "group"}:
            raise ProbeError("invalid_process_record")
        if any(type(fields[key]) is not int or cast(int, fields[key]) < 0 for key in integers):
            raise ProbeError("invalid_process_record")
        if not isinstance(fields["executable"], str) or not isinstance(fields["group"], str):
            raise ProbeError("invalid_process_record")
        return cls(
            cast(int, fields["pid"]),
            cast(int, fields["start"]),
            cast(int, fields["parent"]),
            fields["executable"],
            cast(int, fields["executable_device"]),
            cast(int, fields["executable_inode"]),
            cast(int, fields["uid"]),
            fields["group"],
        )

    @classmethod
    def capture(cls, pid: int) -> ProcessPin:
        root = Path(f"/proc/{pid}")
        fields = read_text(root / "stat").rsplit(")", 1)[1].split()
        status = proc_fields(read_text(root / "status"))
        if (
            len(fields) < 20
            or status["Uid"].split() != ["1000"] * 4
            or status["Gid"].split() != ["1000"] * 4
            or status["NoNewPrivs"].strip() != "1"
            or status["Seccomp"].strip() != "2"
            or int(status["CapEff"], 16) != 0
        ):
            raise ProbeError("process_security_identity")
        info = (root / "exe").stat()
        return cls(
            pid,
            int(fields[19]),
            int(fields[1]),
            os.readlink(root / "exe"),
            info.st_dev,
            info.st_ino,
            1000,
            unified_path(read_text(root / "cgroup")),
        )

    def verify(self) -> None:
        if self.group != "/" or ProcessPin.capture(self.pid) != self:
            raise ProbeError("process_identity_changed")

    def record(self) -> dict[str, object]:
        return asdict(self)


def inventory(init: ProcessPin, owner: ProcessPin, helper: ProcessPin | None = None) -> set[int]:
    """Exclude only pinned roles; all other PIDs remain explicit descendants/debt."""
    init.verify()
    owner.verify()
    if init.pid != 1 or owner.parent != 1 or owner.pid == 1:
        raise ProbeError("init_owner_topology")
    roles = {init.pid, owner.pid}
    if helper is not None:
        helper.verify()
        if helper.parent != owner.pid or helper.pid in roles:
            raise ProbeError("helper_topology")
        roles.add(helper.pid)
    rows = read_text(Path("/sys/fs/cgroup/cgroup.procs")).splitlines()
    if not rows or len(rows) > 64 or any(not row.isdecimal() for row in rows):
        raise ProbeError("process_inventory_unknown")
    members = {int(row) for row in rows}
    if not roles <= members:
        raise ProbeError("process_role_missing")
    return members - roles


class OwnedHelper:
    """A single inert helper slot. Creation, I/O and uncertain closes remain owned.

    The caller registers this object before launch and retains it through join.
    Only the owning inspect/write/finish thread pumps output; cancel only sets
    intent and writes a bounded original-clock record/signals the direct helper.
    """

    def __init__(self, clock: CaseClock, role: str) -> None:
        self.clock, self.role = clock, role
        self.lock = threading.RLock()
        self.process: subprocess.Popen[bytes] | None = None
        self.pin: ProcessPin | None = None
        self.launching = False
        self.busy: set[int] = set()
        self.fds: set[int] = set()
        self.uncertain: set[int] = set()
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.output_eof = self.error_eof = False
        self.cancel_read: int | None = None
        self.cancel_write: int | None = None
        self.admit_read: int | None = None
        self.admit_write: int | None = None
        self.cancel_sent = False
        self.kill_sent = False
        self.failure: str | None = None

    def launch(self, argv: list[str], inherited: tuple[int, ...] = ()) -> None:
        with self.lock:
            if self.launching or self.process is not None or self.clock.remaining() <= 0:
                raise ProbeError("helper_launch_not_admitted")
            self.launching = True
        try:
            self.cancel_read, self.cancel_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
            self.fds.update((self.cancel_read, self.cancel_write))
            self.admit_read, self.admit_write = os.pipe2(os.O_CLOEXEC)
            self.fds.update((self.admit_read, self.admit_write))
            # No preexec_fn or Python fork from the threaded intake owner.
            process = subprocess.Popen(
                [*argv, "--cancel-fd", str(self.cancel_read), "--admit-fd", str(self.admit_read)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                pass_fds=(*inherited, self.cancel_read, self.admit_read),
                start_new_session=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "HOME": "/nonexistent",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            # Register before any observation which can fail. Popen retains the
            # unreaped direct child even if its /proc identity cannot be read.
            with self.lock:
                self.process = process
                for stream in (process.stdin, process.stdout, process.stderr):
                    assert stream is not None
                    self.fds.add(stream.fileno())
                for stream in (process.stdin, process.stdout, process.stderr):
                    assert stream is not None
                    os.set_blocking(stream.fileno(), False)
            self.close(self.cancel_read)
            self.close(self.admit_read)
            self.pin = ProcessPin.capture(process.pid)
        finally:
            with self.lock:
                self.launching = False
            if self.clock.cleanup_start_utc is not None:
                self.cancel()

    def admit(self) -> None:
        if self.clock.remaining() <= 0 or self.admit_write is None or self.pin is None:
            raise ProbeError("helper_admission_revoked")
        self.pin.verify()
        if os.write(self.admit_write, b"x") != 1:
            raise ProbeError("helper_admission_write")
        self.close(self.admit_write)

    def close(self, fd: int) -> None:
        with self.lock:
            if fd not in self.fds or fd in self.busy or fd in self.uncertain:
                return
            self.busy.add(fd)
        try:
            # A blocked close remains owned but never monopolizes the lock
            # needed by timed join and cancellation observation.
            streams = (
                ()
                if self.process is None
                else (self.process.stdin, self.process.stdout, self.process.stderr)
            )
            stream = next(
                (
                    item
                    for item in streams
                    if item is not None and not item.closed and item.fileno() == fd
                ),
                None,
            )
            if stream is None:
                os.close(fd)
            else:
                stream.close()
            with self.lock:
                self.fds.remove(fd)
        except OSError:
            with self.lock:
                self.uncertain.add(fd)
                self.failure = self.failure or "helper_close_uncertain"
        finally:
            with self.lock:
                self.busy.remove(fd)

    def reconcile(self) -> None:
        with self.lock:
            for fd in tuple(self.uncertain):
                try:
                    os.fstat(fd)
                except OSError as error:
                    if error.errno == errno.EBADF:
                        self.fds.remove(fd)
                        self.uncertain.remove(fd)

    def cancel(self) -> None:
        self.clock.cancel()
        with self.lock:
            process = self.process
            if process is None or self.launching:
                return
            if not self.cancel_sent:
                assert self.clock.cleanup_start_utc is not None
                if self.cancel_write in self.fds:
                    try:
                        assert self.cancel_write is not None
                        if (
                            os.write(
                                self.cancel_write, struct.pack("!d", self.clock.cleanup_start_utc)
                            )
                            != 8
                        ):
                            self.clock.timing_unknown = True
                    except OSError:
                        self.clock.timing_unknown = True
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                self.cancel_sent = True
            assert self.clock.cleanup_start_utc is not None
            if self.clock.wall() >= self.clock.cleanup_start_utc + 1 and not self.kill_sent:
                if process.poll() is None:
                    process.kill()
                self.kill_sent = True

    def pump(self, timeout: float = 0.01) -> None:
        process = self.process
        if process is None:
            return
        streams = [(process.stdout, self.stdout, "output"), (process.stderr, self.stderr, "error")]
        active = [
            (stream, target, name)
            for stream, target, name in streams
            if stream is not None and not stream.closed and stream.fileno() in self.fds
        ]
        ready, _, _ = select.select([stream.fileno() for stream, _, _ in active], [], [], timeout)
        for stream, target, name in active:
            fd = stream.fileno()
            if fd not in ready:
                continue
            with self.lock:
                self.busy.add(fd)
            try:
                data = os.read(fd, min(4096, REPORT_BYTES - len(target) + 1))
                if len(target) + len(data) > REPORT_BYTES:
                    self.failure = "helper_output_overrun"
                    self.cancel()
                    continue
                target.extend(data)
                if not data:
                    if name == "output":
                        self.output_eof = True
                    else:
                        self.error_eof = True
            except BlockingIOError:
                continue
            finally:
                with self.lock:
                    self.busy.remove(fd)
            if not data:
                self.close(fd)
        process.poll()
        if self.clock.remaining() <= 0:
            self.cancel()

    def write(self, chunk: bytes) -> None:
        if len(chunk) > CHUNK_BYTES or self.process is None or self.process.stdin is None:
            raise ProbeError("helper_input_not_admitted")
        fd = self.process.stdin.fileno()
        offset = 0
        while offset < len(chunk):
            if self.clock.remaining() <= 0 or self.failure is not None:
                raise ProbeError("helper_input_revoked")
            self.pump()
            if self.process.poll() is not None:
                raise ProbeError("helper_early_exit")
            if not select.select([], [fd], [], 0)[1]:
                continue
            with self.lock:
                self.busy.add(fd)
            try:
                amount = os.write(fd, memoryview(chunk)[offset:])
                if amount <= 0:
                    raise ProbeError("helper_input_no_progress")
                offset += amount
            except BlockingIOError:
                continue
            finally:
                with self.lock:
                    self.busy.remove(fd)

    def finish_input(self) -> None:
        if (
            self.process is not None
            and self.process.stdin is not None
            and not self.process.stdin.closed
        ):
            self.close(self.process.stdin.fileno())

    def join(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if not self.lock.acquire(timeout=max(0, deadline - time.monotonic())):
                return False
            try:
                if self.launching or self.busy:
                    return False
            finally:
                self.lock.release()
            self.pump(0)
            self.reconcile()
            process = self.process
            if process is None or process.poll() is not None:
                self.finish_input()
                for fd in (self.cancel_read, self.cancel_write, self.admit_read, self.admit_write):
                    if fd is not None:
                        self.close(fd)
                if not self.fds and not self.uncertain:
                    return True
            if time.monotonic() >= deadline:
                return False
            self.pump(min(0.01, max(0, deadline - time.monotonic())))

    def receipt(self) -> dict[str, object]:
        return {
            "role": self.role,
            "process": None if self.pin is None else self.pin.record(),
            "exit_code": None if self.process is None else self.process.poll(),
            "launching": self.launching,
            "retained_fds": sorted(self.fds),
            "in_flight_fds": sorted(self.busy),
            "uncertain_closes": sorted(self.uncertain),
            "stdout_eof": self.output_eof,
            "stderr_eof": self.error_eof,
            "stdout_sha256": hashlib.sha256(self.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(self.stderr).hexdigest(),
            "failure": self.failure,
        }
