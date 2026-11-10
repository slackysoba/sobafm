"""Observe one explicitly selected L3 case inside a separately approved candidate."""

from __future__ import annotations

import hashlib
import json
import os
import select
import signal
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from sobafm.decode import DecodeInput, DecodeProducer, DecoderCommand
from sobafm.pcm import BANK_BYTES, FRAME_BYTES, GLOBAL_PCM_BANKS, PcmBank

CASES = ("truncated", "corrupt", "oversized", "feed", "reader", "stderr")
STIMULUS_SECONDS = 15
# The owned CBR fixture in the 2026-10-28 Q3 confirmation. Verify before mounting it.
# pragma: allowlist nextline secret - published synthetic fixture identity
CBR_SHA256 = "1a431c03098d8be79a522a3b1f64a8182445a8bf6c009f54c1b3bb1f6dad044d"

# A no-media witness for the playback stderr route. All commands are shell builtins.
# A frame is emitted only after fd 2 is identified and every diagnostic write succeeds.
FLOOD = (
    "test /proc/self/fd/2 -ef /dev/null || exit 1; "
    "while read -r line; do :; done; "
    'i=0; while [ "$i" -lt 64 ]; do printf "%04096d" 0 >&2 || exit 2; i=$((i+1)); done; '
    f'printf "%0{FRAME_BYTES}d" 0'
)


def _in_fifo_read(decode: DecodeProducer, writer: int) -> bool:
    """On the approved amd64 target, witness read(2) on this FIFO in the owned feeder."""
    if os.uname().machine != "x86_64":
        return False
    for thread in decode._threads:  # pyright: ignore[reportPrivateUsage]
        if thread.name == "decode-feed" and thread.native_id is not None:
            try:
                fields = Path(f"/proc/self/task/{thread.native_id}/syscall").read_text().split()
                # __NR_read is 0 on x86_64. A running thread has no syscall arguments.
                return (
                    len(fields) > 1
                    and fields[0] == "0"
                    and os.path.samestat(os.fstat(int(fields[1], 0)), os.fstat(writer))
                )
            except OSError:
                return False  # an inaccessible or finished read supplies no witness
    return False


def _live_leader(decode: DecodeProducer) -> bool:
    pidfd = decode._pidfd  # pyright: ignore[reportPrivateUsage]
    if pidfd is None:
        return False
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return not poller.poll(0)  # an exited leader is readable even before reaping


@dataclass
class CaseOwners:
    decode: DecodeProducer
    candidate: PcmBank
    current: PcmBank
    released_at: float | None = None

    def release_by(self, deadline: float) -> bool:
        """Try once even after expiry. Keep every outstanding owner for late recovery."""
        while True:
            if self.released_at is None and self.decode.release() and self.candidate.release():
                self.released_at = time.monotonic()
            if self.current.release() and self.released_at is not None:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))


def observe(case: str, workdir: Path) -> CaseOwners | None:
    path = workdir / "input"
    payload = bytes(32)
    source = DecodeInput(path, len(payload), hashlib.sha256(payload).hexdigest(), FRAME_BYTES)
    command = None
    feed_fd = None
    if case == "reader":
        source = DecodeInput(Path("/fixture/owned-cbr.mp3"), 1_920_768, CBR_SHA256, 23_040_000)
    elif case == "feed":
        os.mkfifo(path)
        # Linux permits opening the FIFO read/write. Keep its write end open after one byte.
        feed_fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        os.write(feed_fd, payload[:1])
    else:
        length = {"truncated": 31, "corrupt": 32, "oversized": 33, "stderr": 32}[case]
        path.write_bytes(bytes(length))
        if case == "stderr":
            command = DecoderCommand(argv=("/bin/sh", "-c", FLOOD))

    # Two verified short frames stand for useful, already prepared PCM in the other lease.
    pool = GLOBAL_PCM_BANKS
    current = pool.acquire()
    marker = b"U" * FRAME_BYTES
    current.write(marker)
    current.write(marker)
    current.end_pcm()
    before = current.read_frame() == marker
    bank = pool.acquire()
    decode = DecodeProducer(source, command=command, bank=bank)
    with ThreadPoolExecutor(1) as owner:
        started = time.monotonic()
        until = started + STIMULUS_SECONDS
        decode.start()
        finished = owner.submit(decode.finish)
        witness = None
        witness_seconds = None
        while not finished.done():
            report = decode.report()
            full = case == "reader" and bank.facts().buffered_bytes == BANK_BYTES
            if feed_fd is not None and report.input_bytes == 1 and _in_fifo_read(decode, feed_fd):
                witness = "second_read_on_input_fifo"
            if full and _live_leader(decode):
                witness = "live_decoder_full_unread_bank"
            if witness:
                witness_seconds = time.monotonic() - started
            if witness or full or time.monotonic() >= until:
                decode.cancel()
                break
            time.sleep(0.001)
        if feed_fd is not None:
            os.close(feed_fd)  # let the original read reach EOF after cancellation
        report = finished.result()
        cancel_exit_expected = None
        if witness is not None:
            sent = decode._signalled  # pyright: ignore[reportPrivateUsage]
            cancel_exit_expected = report.returncode in (0, -signal.SIGTERM) or (
                sent == signal.SIGKILL and report.returncode == -signal.SIGKILL
            )
        print(
            json.dumps(
                {
                    "case": case,
                    "witness": witness,
                    "witness_seconds": witness_seconds,
                    "cancel_exit_expected": cancel_exit_expected,
                    "started_at": started,
                    "finish_observed_at": time.monotonic(),
                    **asdict(report),
                }
            ),
            flush=True,
        )

        # Only the successful fd-2 witness should have a readable frame at this point.
        frame = bank.read_frame()
        diagnostic_frame = case == "stderr" and frame == b"0" * FRAME_BYTES
        end = bank.read_frame() == b"" if diagnostic_frame else frame == b""
        del frame
        drained = bank.facts().audible_drain
        after = current.read_frame() == marker and current.facts().audible_drain
        owners = CaseOwners(decode, bank, current)
        assert report.cleanup_deadline is not None
        released = owners.release_by(report.cleanup_deadline)
        print(
            json.dumps(
                {
                    "settled": decode.report().settled,
                    "released_at": owners.released_at,
                    "release_outstanding": not released,
                    "old_frames_preserved": before and after,
                    "diagnostic_frame": diagnostic_frame,
                    "end_of_stream": end,
                    "audible_drain": drained,
                    "old_refunded": current.facts().released,
                    "leases_left": pool.leased,
                }
            ),
            flush=True,
        )
        return None if released else owners


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CASES:
        sys.exit("select exactly one L3 case: " + ", ".join(CASES))
    from sobafm.crash_dumps import disable_crash_dumps

    disable_crash_dumps()
    from sobafm.__main__ import check_limits

    check_limits()
    cgroup = Path("/sys/fs/cgroup")
    quota, period = map(int, (cgroup / "cpu.max").read_text().split())
    if quota != 2 * period:
        sys.exit("two CPUs required")
    print(json.dumps({"initial_tasks": int((cgroup / "pids.current").read_text())}), flush=True)
    with tempfile.TemporaryDirectory() as directory:
        debt = observe(sys.argv[1], Path(directory))
        if debt is not None:
            # The failed row is already flushed. The target supervisor owns this container.
            while not debt.release_by(time.monotonic() + 0.1):
                pass
            print(
                json.dumps(
                    {"all_released_at": time.monotonic(), "leases_left": GLOBAL_PCM_BANKS.leased}
                )
            )
    counters = (
        "memory.peak",
        "memory.events",
        "memory.swap.current",
        "pids.current",
        "pids.events",
    )
    if (cgroup / "pids.peak").exists():
        counters += ("pids.peak",)
    print(json.dumps({key: (cgroup / key).read_text().strip() for key in counters}), flush=True)
    if debt is not None:
        sys.exit(1)
