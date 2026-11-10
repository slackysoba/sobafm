"""Observe one explicitly selected L3 case inside a separately approved candidate."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from sobafm.decode import DecodeInput, DecodeProducer, DecoderCommand
from sobafm.pcm import BANK_BYTES, FRAME_BYTES, GLOBAL_PCM_BANKS

CASES = ("truncated", "corrupt", "oversized", "feed", "reader", "stderr")
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


def observe(case: str, workdir: Path) -> None:
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
        until = started + 15
        decode.start()
        finished = owner.submit(decode.finish)
        witness = None
        witness_seconds = None
        while not finished.done():
            report = decode.report()
            if case == "feed" and report.input_bytes == 1:
                witness = "one_byte_waiting_for_more"
            if case == "reader" and bank.facts().buffered_bytes == BANK_BYTES:
                witness = "full_unread_bank"
            if witness:
                witness_seconds = time.monotonic() - started
            if witness or time.monotonic() >= until:
                decode.cancel()
                break
            time.sleep(0.001)
        if feed_fd is not None:
            os.close(feed_fd)  # let the original read reach EOF after cancellation
        report = finished.result()
        print(
            json.dumps(
                {
                    "case": case,
                    "witness": witness,
                    "witness_seconds": witness_seconds,
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
        while not decode.release(0.05):
            pass  # the bounded outer run owns a stuck case; never issue another decode
        while not bank.release():
            time.sleep(0.01)
        released_at = time.monotonic()
        print(
            json.dumps(
                {
                    "settled": decode.report().settled,
                    "released_at": released_at,
                    "old_frames_preserved": before and after,
                    "diagnostic_frame": diagnostic_frame,
                    "end_of_stream": end,
                    "audible_drain": drained,
                    "old_refunded": current.release(),
                    "leases_left": pool.leased,
                }
            ),
            flush=True,
        )


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
        observe(sys.argv[1], Path(directory))
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
