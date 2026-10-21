"""Owned, fixed Windows Job controls for WJ1; no audio, URL or credentials."""

import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"ordering", "memory", "descendant", "hold"}:
        return 30
    case = sys.argv[1]
    if case == "hold":
        sys.stdout.buffer.write(b"H")
        sys.stdout.buffer.flush()
        time.sleep(60)
        return 0
    if sys.stdin.buffer.read(1) != b"G":
        return 31
    if case == "ordering":
        sys.stdout.buffer.write(b"O")
        sys.stdout.buffer.flush()
        return 0
    if case == "memory":
        try:
            allocation = bytearray(144 * 1024**2)
        except MemoryError:
            sys.stdout.buffer.write(b"M")
            sys.stdout.buffer.flush()
            return 23
        return 24 if len(allocation) else 25
    # The only descendant is this same isolated interpreter and fixed helper.
    # It inherits this Job and both output handles, never breakaway flags.
    subprocess.Popen(  # noqa: S603 - fixed owned executable/fixture, no external input
        [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "hold"],
        stdin=subprocess.DEVNULL,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    sys.stdout.buffer.write(b"D")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
