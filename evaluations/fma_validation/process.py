"""Exact local helper ownership; bounded pipes, no pathname-based media access."""

from __future__ import annotations

import errno
import os
import select
import signal
import struct
import subprocess
import threading
import time

from evaluations.linux_resource.probe import ProbeError

from .ingress import Ingress, Opaque
from .limits import CHUNK_BYTES, CaseClock
from .observation import ObservationWorkspace
from .observation import ProcessPin as ProcessPin


def inventory(
    init: ProcessPin,
    owner: ProcessPin,
    helper: ProcessPin | None = None,
    *,
    workspace: ObservationWorkspace,
    cleanup: bool = False,
) -> set[int]:
    """Unknown observations retain debt; only a complete transaction excludes roles."""
    return workspace.inventory(init, owner, helper, cleanup)


class OwnedHelper:
    """A single inert helper slot. Creation, I/O and uncertain closes remain owned.

    The caller registers this object before launch and retains it through join.
    Only the owning inspect/write/finish thread pumps output; cancel only sets
    intent and writes a bounded original-clock record/signals the direct helper.
    """

    def __init__(
        self, clock: CaseClock, role: str, workspace: ObservationWorkspace | None = None
    ) -> None:
        self.clock, self.role = clock, role
        self.observation = ObservationWorkspace(clock) if workspace is None else workspace
        if self.observation.clock is not clock:
            raise ProbeError("observation_clock_owner")
        self.lock = threading.RLock()
        self.process: subprocess.Popen[bytes] | None = None
        self.pin: ProcessPin | None = None
        self.launching = False
        self.busy: set[int] = set()
        self.fds: set[int] = set()
        self.uncertain: set[int] = set()
        self.ingress = Ingress(role, deferred=True)
        self.errors = Opaque()
        self.output_eof = self.error_eof = False
        self.cancel_read: int | None = None
        self.cancel_write: int | None = None
        self.admit_read: int | None = None
        self.admit_write: int | None = None
        self.cancel_sent = False
        self.kill_sent = False
        self.failure: str | None = None
        self.receipt_published = False

    def launch(self, argv: list[str], inherited: tuple[int, ...] = ()) -> None:
        with self.lock:
            if self.launching or self.process is not None or self.clock.remaining() <= 0:
                raise ProbeError("helper_launch_not_admitted")
            self.launching = True
        try:
            try:
                self.ingress.allocate()
            finally:
                # Successful and exceptional late allocation both observe the
                # original bound. The registered inert helper retains backing.
                self.clock.remaining()
                self.clock.observe()
            if self.clock.remaining() <= 0:
                raise ProbeError("helper_allocation_deadline")
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
            self.pin = ProcessPin.capture(process.pid, self.observation)
        finally:
            with self.lock:
                self.launching = False
            if self.clock.cleanup_start_utc is not None:
                self.cancel()

    def admit(self) -> None:
        if self.clock.remaining() <= 0 or self.admit_write is None or self.pin is None:
            raise ProbeError("helper_admission_revoked")
        self.pin.verify(self.observation)
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
        streams = [(process.stdout, self.ingress, "output"), (process.stderr, self.errors, "error")]
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
                data = os.read(fd, target.read_size())
                try:
                    target.feed(data)
                    eof = not data
                except BaseException:
                    self.failure = "helper_receive_failed"
                    self.cancel()
                    raise
                finally:
                    # End this immutable read before the next os.read result.
                    del data
                if target.error:
                    self.failure = "helper_output_overrun"
                    self.cancel()
                if eof:
                    if name == "output":
                        self.output_eof = True
                    else:
                        self.error_eof = True
            except BlockingIOError:
                continue
            finally:
                with self.lock:
                    self.busy.remove(fd)
            if eof:
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
                if self.launching or self.busy or self.observation.debt:
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
        self.ingress.check_prefix()
        return {
            "role": self.role,
            "stdout_bytes": self.ingress.count,
            "stderr_bytes": self.errors.count,
            "process": None if self.pin is None else self.pin.record(),
            "exit_code": None if self.process is None else self.process.poll(),
            "launching": self.launching,
            "retained_fds": sorted(self.fds),
            "in_flight_fds": sorted(self.busy),
            "uncertain_closes": sorted(self.uncertain),
            "stdout_eof": self.output_eof,
            "stderr_eof": self.error_eof,
            "stdout_sha256": self.ingress.digest.hexdigest(),
            "stderr_sha256": self.errors.digest.hexdigest(),
            "failure": self.failure,
        }
