"""The first-slice decode producer: one stock mpg123 child feeding one PCM bank.

One ``DecodeProducer`` owns one decode: the verified local input, a fresh
decoder process in its own process group, its stdin/stdout pipes, a feeder and
a reader thread, and one ``pcm.py`` bank lease, or a bank its caller already
leased and keeps. The decoder starts before this
producer allocates PCM or starts threads. ``subprocess.Popen`` returns only
after the child has exec'd, so whatever parent memory a forked child image held
is gone before any input; any banks it could have inherited were already leased
and therefore charged. The reviewed resource limits are applied to the decoder
after exec and before the first input byte. Decoder stderr is discarded and no
PTY is used: completeness of a recording belongs to ingestion.

The feeder streams the input in fixed chunks, checking its exact size and
SHA-256, and closes the decoder's stdin only at exact verified EOF. On any input
failure stdin stays open until the decoder is reaped, so the decoder never sees
end of input for a bad file. The reader fills the bank through ``reserve_write``
and ``commit_write`` up to the caller's finite PCM limit.

Transfer completion, decoder completion and valid PCM are recorded separately.
The verified end of PCM is declared only when the transfer completed, the
decoder exited with status zero and no failure or overrun occurred. Any failure
stops the bank's stream.

Cleanup has one owner (``finish``) and one original deadline, three seconds
after the first of cancellation, a failure or the decoder's end of output.
Repeated cancellation never resets it. Expiry is a failed verdict that a later
release does not change; the lease, threads, descriptors and process slot stay
owned until ``release`` observes their actual settlement.

The fixed 16,384-byte input chunk is application audio/IPC copy payload that
belongs to the separate copy reservation; this module does not account for it.
Offline tests use small Python children in place of mpg123 and do not qualify
native behavior or fit.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import resource
import select
import signal
import subprocess
import threading
import time
from collections.abc import Buffer, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from sobafm.pcm import GLOBAL_PCM_BANKS, PcmBank, PcmBankPool, PcmError, PcmFacts, PcmStateError

CLEANUP_SECONDS = 3.0
INPUT_CHUNK_BYTES = 16_384

# The stock decoder invocation established by the #44 evaluation
# (evaluations/fma_validation): stereo signed 16-bit at 48 kHz on stdout,
# input on stdin, no resynchronisation past corrupt frames.
MPG123_ARGV = (
    "/usr/bin/mpg123",
    "--no-control",
    "--no-visual",
    "--buffer",
    "0",
    "--stdout",
    "--rate",
    "48000",
    "--stereo",
    "--encoding",
    "s16",
    "--resample",
    "ntom",
    "--no-resync",
    "--skip-id3v2",
    "--no-icy-meta",
    "--long-tag",
    "-",
)
# The reviewed per-decoder limits behind the conditional PCM bound
# 2 * 12,524,288 + 4,608,000 + 4,194,304 = 33,850,880 (docs/linux-feasibility.md).
MPG123_LIMITS = (
    (resource.RLIMIT_CORE, 0),
    (resource.RLIMIT_STACK, 524_288),
    (resource.RLIMIT_DATA, 12_000_000),
    (resource.RLIMIT_AS, 12_524_288),
)
DECODER_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/nonexistent"}

type DecodeFailure = Literal[
    "input_open",
    "input_read",
    "input_truncated",
    "input_oversized",
    "input_hash",
    "input_write",
    "decoder_spawn",
    "decoder_limits",
    "decoder_exit",
    "decoder_stalled",
    "output_read",
    "thread_start",
    "output_overrun",
    "pcm_capacity",
    "empty_pcm",
    "partial_final_frame",
]
type CleanupVerdict = Literal["pending", "clean", "expired"]


class RawInput(Protocol):
    """The unbuffered input file operations the feeder uses."""

    def readinto(self, buffer: Buffer, /) -> int | None: ...

    def close(self) -> None: ...


def _open_input(path: Path) -> RawInput:
    return open(path, "rb", buffering=0)  # noqa: SIM115 - the feeder owns closing


@dataclass(frozen=True, slots=True)
class DecodeInput:
    """One admitted recording's local file, its exact identity and a PCM bound.

    ``pcm_limit`` is a hard finite output bound derived by the caller, not a
    duration oracle: more decoder output than this is an overrun failure.
    """

    path: Path
    size: int
    sha256: str
    pcm_limit: int

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError("input size must be positive")
        if re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("sha256 must be 64 lowercase hex digits")
        if self.pcm_limit < 1:
            raise ValueError("pcm_limit must be positive")


@dataclass(frozen=True, slots=True)
class DecoderCommand:
    """The decoder process: argv, environment and (resource, limit) pairs."""

    argv: tuple[str, ...] = MPG123_ARGV
    limits: tuple[tuple[int, int], ...] = MPG123_LIMITS
    env: Mapping[str, str] = field(default_factory=lambda: dict(DECODER_ENV))


@dataclass(frozen=True, slots=True)
class DecodeReport:
    """Separately tracked facts of one decode and its cleanup."""

    transfer_complete: bool
    input_bytes: int
    decoder_complete: bool
    returncode: int | None
    pcm: PcmFacts | None
    failure: DecodeFailure | None
    cancelled_at: float | None
    cleanup_started_at: float | None
    cleanup_deadline: float | None
    cleanup: CleanupVerdict
    settled: bool


class DecodeProducer:
    """The single owner of one decode. Call ``start``, then ``finish`` once.

    ``finish`` blocks until the decode ends or is cancelled and cleanup is done
    or expired, so run it away from the event loop. ``cancel`` never blocks.
    ``release`` returns the bank lease once everything else has settled.
    """

    def __init__(
        self,
        source: DecodeInput,
        *,
        command: DecoderCommand | None = None,
        pool: PcmBankPool = GLOBAL_PCM_BANKS,
        open_input: Callable[[Path], RawInput] = _open_input,
        clock: Callable[[], float] = time.monotonic,
        cleanup_seconds: float = CLEANUP_SECONDS,
        bank: PcmBank | None = None,
    ) -> None:
        """``bank`` is a lease the caller already holds and keeps.

        Without it the producer leases one from ``pool`` after launch and returns
        it in ``release``. With it the producer fills that bank at the same point
        in the launch, never acquires, and leaves the lease to the caller;
        ``release`` then only reports settlement. The bank must share ``clock``.
        """
        self._source = source
        self._leased = bank
        self._command = command if command is not None else DecoderCommand()
        self._pool = pool
        self._open_input = open_input
        self._clock = clock
        self._window = cleanup_seconds
        self._lock = threading.Lock()
        self._cleanup_requested = threading.Event()
        self._started = False
        self._finished = False
        self._input: RawInput | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._pidfd: int | None = None
        self._bank: PcmBank | None = None
        self._threads: list[threading.Thread] = []
        self._signalled: int | None = None
        self._reaped = False
        self._returncode: int | None = None
        self._input_bytes = 0
        self._transfer_complete = False
        self._decoder_complete = False
        self._failure: DecodeFailure | None = None
        self._cancelled_at: float | None = None
        self._cleanup_started_at: float | None = None
        self._deadline: float | None = None
        self._verdict: CleanupVerdict = "pending"
        self._settled = False

    @property
    def bank(self) -> PcmBank | None:
        """The leased bank the voice consumer reads, once allocated.

        Consumers only read frames from it; to stop the decode, call
        ``DecodeProducer.cancel()``, never ``bank.cancel()`` directly.
        """
        return self._bank

    def start(self) -> None:
        """Open the input, launch the decoder, then lease the bank and start I/O.

        Failures are reported by ``finish``; this never raises for them.
        """
        with self._lock:
            if self._started:
                raise RuntimeError("the decode producer was already started")
            self._started = True
        if self._cleanup_requested.is_set():
            return  # cancelled before start: launch nothing
        try:
            self._input = self._open_input(self._source.path)
        except OSError:
            self._fail("input_open")
            return
        try:
            process = subprocess.Popen(
                self._command.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                close_fds=True,
                start_new_session=True,
                env=dict(self._command.env),
            )
        except OSError:
            self._fail("decoder_spawn")
            return
        with self._lock:
            self._process = process
        try:
            self._pidfd = os.pidfd_open(process.pid)
        except OSError:
            self._fail("decoder_limits")
            return
        if self._stopped_during_start():
            return
        try:
            for kind, limit in self._command.limits:
                resource.prlimit(process.pid, kind, (limit, limit))
        except OSError:
            self._fail("decoder_limits")
            return
        if self._leased is not None:
            self._bank = self._leased  # charged by its caller before this launch
        else:
            try:
                self._bank = self._pool.acquire()
            except PcmError, MemoryError:  # refused or failed allocation
                self._fail("pcm_capacity")
                return
        if self._stopped_during_start():
            return
        for thread in (
            threading.Thread(target=self._feed, name="decode-feed", daemon=True),
            threading.Thread(target=self._read, name="decode-read", daemon=True),
        ):
            try:
                thread.start()
            except RuntimeError:  # thread or pids limit: own only what started
                self._fail("thread_start")
                return
            self._threads.append(thread)

    def _stopped_during_start(self) -> bool:
        """Apply a cancellation that raced the launch to what now exists."""
        if not self._cleanup_requested.is_set():
            return False
        self._signal(signal.SIGTERM)
        if self._bank is not None:
            self._bank.cancel()
        return True

    def cancel(self) -> None:
        """Stop the decode. Only the first call sets the time and the deadline.

        A caller-held bank that its owner already cancelled keeps that earlier
        time as the original clock.
        """
        now = self._clock()
        if self._leased is not None:
            first = self._leased.facts().cancelled_at
            if first is not None:
                now = min(now, first)
        with self._lock:
            if self._cancelled_at is None:
                self._cancelled_at = now
        self._begin_cleanup(now, terminate=True)

    def finish(self) -> DecodeReport:
        """Wait for the decode to end, then settle it under the original deadline."""
        with self._lock:
            if not self._started:
                raise RuntimeError("the decode producer was not started")
            if self._finished:
                raise RuntimeError("finish may be called only once")
            self._finished = True
        self._cleanup_requested.wait()
        start, deadline = self._cleanup_started_at, self._deadline
        assert start is not None and deadline is not None
        if self._process is not None:
            if self._signalled is None and not self._wait_exit(start + self._window / 3):
                self._fail("decoder_stalled")
            if not self._wait_exit(start + 2 * self._window / 3):
                self._signal(signal.SIGKILL)
            if self._wait_exit(deadline):
                self._reap()
        for thread in self._threads:
            thread.join(max(0.0, deadline - self._clock()))
        settled = self._settle()
        within = self._clock() <= deadline
        self._judge()
        with self._lock:
            self._verdict = "clean" if settled and within else "expired"
        return self.report()

    def release(self, timeout: float = 0.0) -> bool:
        """Return the bank lease once process, threads and descriptors settle.

        Waits up to ``timeout`` for them; the cleanup verdict never changes.
        Returns False, keeping every retained resource, until all of it is free.
        """
        with self._lock:
            if not self._finished or self._verdict == "pending":
                return False
        end = self._clock() + timeout
        if self._process is not None and not self._reaped and self._wait_exit(end):
            self._reap()
        for thread in self._threads:
            thread.join(max(0.0, end - self._clock()))
        if not self._settle():
            return False
        if self._leased is not None:
            return True  # the caller keeps and releases its own lease
        return self._bank is None or self._bank.release()

    def report(self) -> DecodeReport:
        """Snapshot the decode's facts."""
        with self._lock:
            return DecodeReport(
                transfer_complete=self._transfer_complete,
                input_bytes=self._input_bytes,
                decoder_complete=self._decoder_complete,
                returncode=self._returncode,
                pcm=self._bank.facts() if self._bank is not None else None,
                failure=self._failure,
                cancelled_at=self._cancelled_at,
                cleanup_started_at=self._cleanup_started_at,
                cleanup_deadline=self._deadline,
                cleanup=self._verdict,
                settled=self._settled,
            )

    # Worker threads.

    def _feed(self) -> None:
        source, raw = self._source, self._input
        process = self._process
        assert raw is not None and process is not None
        stdin = process.stdin
        assert isinstance(stdin, io.FileIO)
        digest = hashlib.sha256()
        chunk = bytearray(INPUT_CHUNK_BYTES)
        try:
            with memoryview(chunk) as whole:
                while True:
                    # Read one byte past the expected size to detect growth.
                    want = min(INPUT_CHUNK_BYTES, source.size + 1 - self._input_bytes)
                    try:
                        count = raw.readinto(whole[:want]) or 0
                    except OSError:
                        self._fail("input_read")
                        return
                    if count == 0:
                        break
                    if self._input_bytes + count > source.size:
                        self._fail("input_oversized")
                        return
                    with whole[:count] as data:
                        digest.update(data)
                        if not self._write_all(stdin, data):
                            return
                    with self._lock:
                        self._input_bytes += count
            if self._input_bytes != source.size:
                self._fail("input_truncated")
            elif digest.hexdigest() != source.sha256:
                self._fail("input_hash")
            else:
                try:
                    stdin.close()  # exact verified EOF
                except OSError:
                    self._fail("input_write")
                    return
                with self._lock:
                    self._transfer_complete = True
                assert self._bank is not None
                self._bank.mark_transfer_complete()
        finally:
            raw.close()

    def _write_all(self, stdin: io.FileIO, data: memoryview) -> bool:
        offset = 0
        while offset < len(data):
            try:
                written = stdin.write(data[offset:])
            except OSError:
                self._fail("input_write")
                return False
            offset += written or 0
        return True

    def _read(self) -> None:
        process, bank = self._process, self._bank
        assert process is not None and bank is not None
        stdout = process.stdout
        assert isinstance(stdout, io.FileIO)
        remaining = self._source.pcm_limit
        try:
            while True:
                if remaining == 0:
                    if stdout.readinto(bytearray(1)):
                        self._fail("output_overrun")
                    break
                region = bank.reserve_write(remaining)
                if region is None:
                    bank.wait_writable()
                    continue
                count = 0
                try:
                    count = stdout.readinto(region) or 0
                finally:
                    region.release()
                    bank.commit_write(count)
                if count == 0:
                    break
                remaining -= count
        except PcmStateError:
            return  # the stream was stopped
        except OSError:
            self._fail("output_read")
            return
        self._begin_cleanup(self._clock(), terminate=False)

    # Ownership.

    def _fail(self, failure: DecodeFailure) -> None:
        now = self._clock()
        with self._lock:
            # Errors caused by our own cancellation are not decode failures.
            if self._failure is None and self._cancelled_at is None:
                self._failure = failure
        self._begin_cleanup(now, terminate=True)

    def _begin_cleanup(self, now: float, *, terminate: bool) -> None:
        with self._lock:
            if self._cleanup_started_at is None:
                self._cleanup_started_at = now
                self._deadline = now + self._window
        if terminate:
            self._signal(signal.SIGTERM)
            if self._bank is not None:
                self._bank.cancel()
        self._cleanup_requested.set()

    def _signal(self, signum: int) -> None:
        """Signal the decoder's group; never after the leader was reaped."""
        with self._lock:
            process = self._process
            if process is None or self._reaped:
                return
            if signum == signal.SIGTERM and self._signalled is not None:
                return
            self._signalled = signum
            # The unreaped leader keeps the group ID from being reused.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signum)

    def _wait_exit(self, until: float) -> bool:
        """Wait until the leader has exited (without reaping it) or ``until``."""
        if self._reaped:
            return True
        pidfd = self._pidfd
        if pidfd is None:  # pidfd_open failed: only a kill can end the child
            self._signal(signal.SIGKILL)
            process = self._process
            assert process is not None
            try:
                process.wait(max(0.0, until - self._clock()))
            except subprocess.TimeoutExpired:
                return False
            with self._lock:
                self._reaped = True
                self._returncode = process.returncode
            return True
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        return bool(poller.poll(max(0, int((until - self._clock()) * 1000) + 1)))

    def _reap(self) -> None:
        process = self._process
        assert process is not None
        if self._reaped:
            return
        # Kill any descendants left in the group while the zombie leader still
        # pins its ID, then reap the leader.
        with self._lock, contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        returncode = process.wait()
        with self._lock:
            self._reaped = True
            self._returncode = returncode

    def _settle(self) -> bool:
        """Close descriptors once nothing can use them; report full settlement."""
        if self._settled:
            return True
        if any(thread.is_alive() for thread in self._threads):
            return False
        process = self._process
        if process is not None and not self._reaped:
            return False
        if not self._threads and self._input is not None:
            self._input.close()  # the feeder never started to own it
        if process is not None:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
        if self._pidfd is not None:
            os.close(self._pidfd)
            self._pidfd = None
        with self._lock:
            self._settled = True
        return True

    def _judge(self) -> None:
        """Record decoder completion and the verified end of PCM, if earned."""
        bank = self._bank
        if bank is None or bank.facts().released:
            return
        with self._lock:
            returncode = self._returncode
            self._decoder_complete = returncode == 0
        if returncode == 0:
            bank.mark_decoder_complete()
        elif returncode is not None:
            self._fail("decoder_exit")
        with self._lock:
            earned = (
                self._failure is None and self._cancelled_at is None and self._transfer_complete
            )
        if not earned or returncode != 0:
            return
        try:
            bank.end_pcm()
        except PcmStateError:
            return  # the bank's owner stopped the stream first; nothing is declared
        failure = bank.facts().failure
        if failure is not None:
            self._fail(failure)
