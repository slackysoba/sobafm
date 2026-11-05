"""Decode producer regressions with small Python children standing in for mpg123.

Synchronisation uses pipes, FIFOs and events only; no test sleeps for a race.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import resource
import signal
import sys
import threading
import time
from collections.abc import Buffer, Iterator
from pathlib import Path

import pytest

from sobafm.decode import (
    MPG123_ARGV,
    DecodeInput,
    DecodeProducer,
    DecoderCommand,
    DecodeReport,
    RawInput,
)
from sobafm.pcm import FRAME_BYTES, PcmBankPool

PAYLOAD = bytes(range(256)) * 40


def child(script: str) -> DecoderCommand:
    return DecoderCommand(argv=(sys.executable, "-I", "-c", script), limits=(), env={})


def echo_frames(frames: int, *, extra: int = 0, status: int = 0) -> DecoderCommand:
    """Consume all input, then write whole frames plus ``extra`` bytes and exit."""
    return child(
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        f"sys.stdout.buffer.write(b'\\x01' * ({frames} * {FRAME_BYTES} + {extra}))\n"
        "sys.stdout.buffer.flush()\n"
        f"sys.exit({status})\n"
    )


@pytest.fixture
def source(tmp_path: Path) -> DecodeInput:
    path = tmp_path / "input.mp3"
    path.write_bytes(PAYLOAD)
    return DecodeInput(
        path=path,
        size=len(PAYLOAD),
        sha256=hashlib.sha256(PAYLOAD).hexdigest(),
        pcm_limit=4 * FRAME_BYTES,
    )


@pytest.fixture
def pool() -> Iterator[PcmBankPool]:
    pool = PcmBankPool()
    yield pool
    assert pool.leased == 0


def run(producer: DecodeProducer) -> None:
    producer.start()
    report = producer.finish()
    assert report.cleanup == "clean" and report.settled


# The argv the #44 evaluation established and its approved real run used. The
# evaluation was removed from main after that run; commit 81a7519 retains it.
ESTABLISHED_MPG123_ARGV = (
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


def test_mpg123_argv_matches_the_established_decoder() -> None:
    assert MPG123_ARGV == ESTABLISHED_MPG123_ARGV


def test_complete_decode_reports_every_fact(source: DecodeInput, pool: PcmBankPool) -> None:
    producer = DecodeProducer(source, command=echo_frames(3), pool=pool)
    run(producer)
    report = producer.report()
    assert report.failure is None
    assert report.transfer_complete and report.input_bytes == len(PAYLOAD)
    assert report.decoder_complete and report.returncode == 0
    assert report.pcm is not None
    assert report.pcm.transfer_complete and report.pcm.decoder_complete
    assert report.pcm.pcm_valid and report.pcm.ready
    assert report.pcm.written_bytes == 3 * FRAME_BYTES
    assert report.pcm.cancelled_at is None
    bank = producer.bank
    assert bank is not None and bank.read_frame() == b"\x01" * FRAME_BYTES
    assert pool.leased == 1
    assert producer.release()
    assert pool.leased == 0


def test_hash_mismatch_never_gives_the_decoder_eof(source: DecodeInput, pool: PcmBankPool) -> None:
    wrong = DecodeInput(source.path, source.size, "0" * 64, source.pcm_limit)
    # The child would emit valid PCM on EOF; it must be terminated instead.
    producer = DecodeProducer(wrong, command=echo_frames(1), pool=pool)
    run(producer)
    report = producer.report()
    assert report.failure == "input_hash"
    assert not report.transfer_complete and report.input_bytes == len(PAYLOAD)
    assert report.returncode == -signal.SIGTERM and not report.decoder_complete
    assert report.pcm is not None and report.pcm.written_bytes == 0
    assert not report.pcm.pcm_valid and report.pcm.cancelled_at is not None
    assert producer.release()


def test_truncated_input_fails(source: DecodeInput, pool: PcmBankPool) -> None:
    longer = DecodeInput(source.path, source.size + 1, source.sha256, source.pcm_limit)
    producer = DecodeProducer(longer, command=echo_frames(1), pool=pool)
    run(producer)
    report = producer.report()
    assert report.failure == "input_truncated" and not report.transfer_complete
    assert report.pcm is not None and not report.pcm.pcm_valid
    assert producer.release()


def test_oversized_input_fails(source: DecodeInput, pool: PcmBankPool) -> None:
    shorter = DecodeInput(source.path, source.size - 1, source.sha256, source.pcm_limit)
    producer = DecodeProducer(shorter, command=echo_frames(1), pool=pool)
    run(producer)
    assert producer.report().failure == "input_oversized"
    assert producer.release()


def test_nonzero_exit_after_full_output_fails(source: DecodeInput, pool: PcmBankPool) -> None:
    producer = DecodeProducer(source, command=echo_frames(2, status=3), pool=pool)
    run(producer)
    report = producer.report()
    assert report.failure == "decoder_exit" and report.returncode == 3
    assert report.transfer_complete and not report.decoder_complete
    assert report.pcm is not None and report.pcm.written_bytes == 2 * FRAME_BYTES
    assert not report.pcm.pcm_valid and report.pcm.cancelled_at is not None
    assert producer.release()


@pytest.mark.parametrize("extra", [1, 3, FRAME_BYTES // 2 + 1])
def test_partial_final_frame_fails(source: DecodeInput, pool: PcmBankPool, extra: int) -> None:
    """Output that is not a whole number of stereo samples is truncated."""
    producer = DecodeProducer(source, command=echo_frames(1, extra=extra), pool=pool)
    run(producer)
    report = producer.report()
    assert report.failure == "partial_final_frame"
    assert report.transfer_complete and report.decoder_complete
    assert report.pcm is not None and not report.pcm.pcm_valid
    assert report.pcm.failure == "partial_final_frame"
    assert producer.release()


def test_sample_aligned_output_that_is_not_frame_aligned_plays_to_its_end(
    source: DecodeInput, pool: PcmBankPool
) -> None:
    # Night Owl's real output ended 988 bytes past a whole frame.
    producer = DecodeProducer(source, command=echo_frames(3, extra=988), pool=pool)
    run(producer)
    report = producer.report()
    assert report.failure is None
    assert report.transfer_complete and report.decoder_complete
    assert report.pcm is not None and report.pcm.pcm_valid and report.pcm.failure is None
    assert report.pcm.cancelled_at is None and report.pcm.written_bytes == 3 * FRAME_BYTES + 988
    bank = producer.bank
    assert bank is not None
    frames = [bank.read_frame() for _ in range(3)]  # after finish returned
    assert frames == [b"\x01" * FRAME_BYTES] * 3
    assert bank.read_frame() == b""
    assert bank.facts().audible_drain and bank.facts().cancelled_at is None
    assert producer.release()


@pytest.mark.parametrize(("extra", "failure"), [(988, "empty_pcm"), (987, "partial_final_frame")])
def test_output_under_one_frame_fails(
    source: DecodeInput, pool: PcmBankPool, extra: int, failure: str
) -> None:
    producer = DecodeProducer(source, command=echo_frames(0, extra=extra), pool=pool)
    run(producer)
    report = producer.report()
    assert report.failure == failure
    assert report.pcm is not None and not report.pcm.pcm_valid
    assert report.pcm.cancelled_at is not None
    assert producer.release()


def test_output_overrun_fails(source: DecodeInput, pool: PcmBankPool) -> None:
    producer = DecodeProducer(source, command=echo_frames(5), pool=pool)
    run(producer)
    report = producer.report()
    assert report.failure == "output_overrun"
    assert report.pcm is not None and report.pcm.written_bytes == source.pcm_limit
    assert not report.pcm.pcm_valid
    assert producer.release()


def test_capacity_refusal_terminates_the_launched_decoder(source: DecodeInput) -> None:
    pool = PcmBankPool()
    held = [pool.acquire(), pool.acquire()]
    producer = DecodeProducer(source, command=echo_frames(1), pool=pool)
    run(producer)
    report = producer.report()
    assert report.failure == "pcm_capacity" and report.pcm is None
    assert report.returncode == -signal.SIGTERM
    assert producer.release()
    assert all(bank.release() for bank in held) and pool.leased == 0


def test_missing_decoder_is_a_spawn_failure(source: DecodeInput, pool: PcmBankPool) -> None:
    command = DecoderCommand(argv=("/nonexistent/decoder",), limits=(), env={})
    producer = DecodeProducer(source, command=command, pool=pool)
    run(producer)
    assert producer.report().failure == "decoder_spawn"
    assert producer.release()


STALL = "import signal\nsignal.pause()\n"


def test_stalled_child_is_killed_and_reaped_on_the_original_clock(
    source: DecodeInput, pool: PcmBankPool
) -> None:
    producer = DecodeProducer(source, command=child(STALL), pool=pool)
    producer.start()
    producer.cancel()
    report = producer.finish()
    assert report.cancelled_at is not None
    assert report.cleanup_started_at == report.cancelled_at
    assert report.cleanup_deadline == report.cancelled_at + 3.0
    assert report.cleanup == "clean" and report.settled
    assert report.returncode == -signal.SIGTERM
    assert report.failure is None and not report.decoder_complete
    assert report.pcm is not None and not report.pcm.pcm_valid
    assert producer.release()


def test_repeated_cancellation_keeps_the_first_clock(
    source: DecodeInput, pool: PcmBankPool
) -> None:
    times = iter([10.0, 11.0, 12.5])
    producer = DecodeProducer(
        source, command=child(STALL), pool=pool, clock=lambda: next(times, 12.5)
    )
    producer.start()
    producer.cancel()
    producer.cancel()
    producer.cancel()
    report = producer.report()
    assert report.cancelled_at == 10.0
    assert report.cleanup_started_at == 10.0 and report.cleanup_deadline == 13.0
    assert report.pcm is not None and report.pcm.cancelled_at is not None
    final = producer.finish()
    assert final.cancelled_at == 10.0 and final.cleanup_deadline == 13.0
    # The frozen clock passes the KILL point at once; either signal may win.
    assert final.cleanup == "clean" and final.returncode in (-signal.SIGTERM, -signal.SIGKILL)
    assert producer.release()


def test_a_caller_held_bank_keeps_its_first_cancellation_as_the_clock(
    source: DecodeInput,
) -> None:
    now = [0.0]
    pool = PcmBankPool(clock=lambda: now[0])
    bank = pool.acquire()
    bank.cancel()  # the owner cancels the bank first, at t=0
    now[0] = 1.0
    producer = DecodeProducer(
        source, command=child(STALL), pool=pool, clock=lambda: now[0], bank=bank
    )
    producer.start()
    producer.cancel()  # reaches the decode at t=1
    report = producer.finish()
    assert report.cancelled_at == 0.0
    assert report.cleanup_started_at == 0.0 and report.cleanup_deadline == 3.0
    assert report.cleanup == "clean" and report.settled
    assert producer.release() and pool.leased == 1  # the lease stays the caller's
    assert bank.release() and pool.leased == 0


def test_a_failure_after_the_owners_cancellation_keeps_the_original_clock(
    source: DecodeInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner cancels at t=0; the launched decode fails at t=1 before its cancel.

    The first thread start is refused, so no reader can adopt the owner's
    cancellation first and the failure is the decode's own, on the original clock.
    """
    now = [0.0]
    pool = PcmBankPool(clock=lambda: now[0])
    bank = pool.acquire()
    bank.cancel()
    now[0] = 1.0

    def refuse(_: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    producer = DecodeProducer(
        source, command=child(STALL), pool=pool, clock=lambda: now[0], bank=bank
    )
    producer.start()
    monkeypatch.undo()
    report = producer.finish()
    assert report.failure == "thread_start"
    assert report.cleanup_started_at == 0.0 and report.cleanup_deadline == 3.0
    assert report.returncode == -signal.SIGTERM
    assert report.cleanup == "clean" and report.settled
    assert producer.release() and bank.release() and pool.leased == 0


def test_an_owner_cancellation_before_judgement_declares_no_end(
    source: DecodeInput, pool: PcmBankPool
) -> None:
    bank = pool.acquire()
    producer = DecodeProducer(source, command=echo_frames(1), pool=pool, bank=bank)
    producer.start()
    deadline = time.monotonic() + 10
    while producer.report().cleanup_started_at is None:  # the reader reached EOF
        assert time.monotonic() < deadline
        time.sleep(0.001)
    bank.cancel()  # the owner stops the stream before finish judges it
    report = producer.finish()  # must not raise on the stopped stream
    assert report.failure is None and report.transfer_complete
    assert not bank.facts().pcm_valid
    assert producer.release() and pool.leased == 1
    assert bank.release()


def test_child_ignoring_term_is_killed_within_the_deadline(
    source: DecodeInput, pool: PcmBankPool, tmp_path: Path
) -> None:
    fifo = tmp_path / "ready"
    os.mkfifo(fifo)
    script = (
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(fifo)!r}, 'wb').write(b'r')\n"
        "while True:\n"
        "    signal.pause()\n"
    )
    producer = DecodeProducer(source, command=child(script), pool=pool, cleanup_seconds=1.5)
    producer.start()
    with open(fifo, "rb") as ready:
        assert ready.read() == b"r"  # TERM is now ignored
    producer.cancel()
    report = producer.finish()
    assert report.returncode == -signal.SIGKILL
    assert report.cleanup == "clean" and report.settled
    assert producer.release()


def test_cancel_unblocks_a_writer_that_defers_term_and_exit_zero_is_not_complete(
    source: DecodeInput, pool: PcmBankPool, tmp_path: Path
) -> None:
    """Like mpg123: TERM is only acted on once the blocked output write fails.

    The child reads its whole input, then writes forever and never checks the
    TERM flag between writes, so without anyone reading its stdout it stays
    blocked in a write until killed. Once the pipe closes it exits 0, as mpg123
    does on TERM; that exit must not count as decoder completion.
    """
    fifo = tmp_path / "ready"
    os.mkfifo(fifo)
    script = (
        "import os, signal, sys\n"
        "terminated = []\n"
        "signal.signal(signal.SIGTERM, lambda *_: terminated.append(1))\n"
        "sys.stdin.buffer.read()\n"
        f"open({str(fifo)!r}, 'wb').write(b'r')\n"
        f"chunk = b'\\x01' * {64 * FRAME_BYTES}\n"
        "try:\n"
        "    while True:\n"
        "        os.write(1, chunk)\n"
        "except BrokenPipeError:\n"
        "    os._exit(0 if terminated else 5)\n"
    )
    producer = DecodeProducer(
        dataclasses.replace(source, pcm_limit=1 << 40),
        command=child(script),
        pool=pool,
    )
    producer.start()
    with open(fifo, "rb") as ready:
        assert ready.read() == b"r"  # TERM is now deferred
    producer.cancel()
    report = producer.finish()
    assert report.cancelled_at is not None and report.cleanup_deadline is not None
    assert report.returncode == 0  # it exited on its own: no KILL was needed
    # Well inside the deadline: before the KILL point at two thirds of it.
    assert report.cleanup_deadline - report.cancelled_at == 3.0
    assert time.monotonic() < report.cancelled_at + 2.0
    assert report.transfer_complete and report.failure is None
    assert not report.decoder_complete
    assert report.pcm is not None and not report.pcm.decoder_complete
    assert not report.pcm.pcm_valid
    assert report.cleanup == "clean" and report.settled
    assert producer.release()


class SignalledInput:
    """Real file reads that report when the feeder has closed the input."""

    def __init__(self, path: Path) -> None:
        self.raw: RawInput = open(path, "rb", buffering=0)  # noqa: SIM115
        self.closed = threading.Event()

    def readinto(self, buffer: Buffer, /) -> int | None:
        return self.raw.readinto(buffer)

    def close(self) -> None:
        self.raw.close()
        self.closed.set()


def test_owner_cancelling_the_bank_first_keeps_its_clock_and_is_no_failure(
    tmp_path: Path, pool: PcmBankPool
) -> None:
    """The adapter cancels its leased bank before the producer hears of it.

    The child never reads its input, so the feeder stays blocked writing, and
    it writes output forever. Once its output fails it exits, which fails the
    feeder's write; that must count as the owner's cancellation, on its clock.
    """
    payload = bytes(1 << 20)  # far beyond a pipe buffer: the feeder blocks
    path = tmp_path / "input"
    path.write_bytes(payload)
    source = DecodeInput(path, len(payload), hashlib.sha256(payload).hexdigest(), 1 << 40)
    script = (
        "import os, signal\n"
        "signal.signal(signal.SIGTERM, lambda *_: None)\n"
        f"chunk = b'\\x01' * {64 * FRAME_BYTES}\n"
        "try:\n"
        "    while True:\n"
        "        os.write(1, chunk)\n"
        "except BrokenPipeError:\n"
        "    os._exit(0)\n"
    )
    held = SignalledInput(path)
    bank = pool.acquire()
    producer = DecodeProducer(
        source, command=child(script), pool=pool, open_input=lambda _: held, bank=bank
    )
    producer.start()
    bank.cancel()
    assert held.closed.wait(30)  # the feeder has ended: its write failed
    producer.cancel()
    report = producer.finish()
    first = bank.facts().cancelled_at
    assert first is not None
    assert report.failure is None
    assert report.cancelled_at == first and report.cleanup_started_at == first
    assert report.cleanup_deadline == first + 3.0
    assert not report.decoder_complete and not report.transfer_complete
    assert report.cleanup == "clean" and report.settled
    assert producer.release()
    assert bank.release()


class HeldInput:
    """Real file reads whose close blocks until the test allows it."""

    def __init__(self, path: Path) -> None:
        self.raw: RawInput = open(path, "rb", buffering=0)  # noqa: SIM115
        self.closing = threading.Event()
        self.allow = threading.Event()

    def readinto(self, buffer: Buffer, /) -> int | None:
        return self.raw.readinto(buffer)

    def close(self) -> None:
        self.closing.set()
        assert self.allow.wait(30)
        self.raw.close()


def test_held_close_keeps_owner_and_lease_and_expiry_stays_failed(
    source: DecodeInput, pool: PcmBankPool
) -> None:
    held = HeldInput(source.path)
    producer = DecodeProducer(
        source,
        command=echo_frames(1),
        pool=pool,
        open_input=lambda _: held,
        cleanup_seconds=0.2,
    )
    producer.start()
    assert held.closing.wait(30)
    producer.cancel()
    report = producer.finish()
    assert report.cleanup == "expired" and not report.settled
    assert pool.leased == 1
    assert not producer.release()
    assert pool.leased == 1 and producer.report().cleanup == "expired"
    held.allow.set()
    assert producer.release(timeout=30)
    late = producer.report()
    assert late.cleanup == "expired" and late.settled
    assert late.cleanup_started_at == report.cleanup_started_at
    assert late.pcm is not None and late.pcm.released
    assert pool.leased == 0


def test_cancel_before_start_launches_nothing(source: DecodeInput, pool: PcmBankPool) -> None:
    producer = DecodeProducer(source, command=echo_frames(1), pool=pool)
    producer.cancel()
    run(producer)
    report = producer.report()
    assert report.returncode is None and report.pcm is None
    assert producer.release()


def test_full_bank_waits_for_the_consumer(tmp_path: Path, pool: PcmBankPool) -> None:
    from sobafm.pcm import BANK_BYTES

    path = tmp_path / "input"
    path.write_bytes(b"x")
    total = BANK_BYTES + 2 * FRAME_BYTES
    source = DecodeInput(path, 1, hashlib.sha256(b"x").hexdigest(), total)
    producer = DecodeProducer(source, command=echo_frames(total // FRAME_BYTES), pool=pool)
    producer.start()
    bank = producer.bank
    assert bank is not None
    reports: list[DecodeReport] = []
    owner = threading.Thread(target=lambda: reports.append(producer.finish()))
    owner.start()
    # The producer can only finish after the consumer frees space, so the
    # outcome does not depend on timing; the short wait only yields the CPU.
    idle = threading.Event()
    frames = 0
    while (frame := bank.read_frame()) != b"":
        if frame is None:
            idle.wait(0.001)
        else:
            frames += 1
    owner.join(30)
    [report] = reports
    assert report.cleanup == "clean" and report.failure is None
    assert report.pcm is not None and report.pcm.pcm_valid
    # ``finish`` may snapshot before the consumer reads the last frames, so
    # drain is observed on the bank once the consumer has seen the end.
    assert bank.facts().audible_drain
    assert frames == total // FRAME_BYTES
    assert producer.release()


def test_failed_thread_start_is_owned_and_settled(
    source: DecodeInput, pool: PcmBankPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_start = threading.Thread.start
    calls = 0

    def start_once(thread: threading.Thread) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start_once)
    producer = DecodeProducer(source, command=echo_frames(1), pool=pool)
    producer.start()  # must not raise
    monkeypatch.undo()
    report = producer.finish()
    assert report.failure == "thread_start"
    assert report.cleanup == "clean" and report.settled
    assert report.returncode == -signal.SIGTERM
    assert producer.release()


def test_limits_are_applied_before_input(tmp_path: Path, pool: PcmBankPool) -> None:
    path = tmp_path / "input"
    path.write_bytes(b"x")
    source = DecodeInput(path, 1, hashlib.sha256(b"x").hexdigest(), FRAME_BYTES)
    # A distinctive value that cannot be inherited; the child reads its input
    # first, so it reports the limit applied before that input.
    assert resource.getrlimit(resource.RLIMIT_FSIZE) != (12_345, 12_345)
    script = (
        "import resource, sys\n"
        "sys.stdin.buffer.read()\n"
        "soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)\n"
        f"sys.stdout.buffer.write(b'\\x01' * {FRAME_BYTES} if soft == hard == 12_345 else b'')\n"
    )
    command = DecoderCommand(
        argv=(sys.executable, "-I", "-c", script), limits=((resource.RLIMIT_FSIZE, 12_345),), env={}
    )
    producer = DecodeProducer(source, command=command, pool=pool)
    run(producer)
    report = producer.report()
    assert report.failure is None
    assert report.pcm is not None and report.pcm.pcm_valid
    assert report.pcm.written_bytes == FRAME_BYTES
    assert producer.release()
