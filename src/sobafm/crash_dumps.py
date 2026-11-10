"""Keep the main process's memory out of crash dumps.

The main process holds the Discord token, the Gemini key and PCM, and its initial
environment stays visible in its memory after the credentials are removed from
``os.environ``. A crash would otherwise copy all of that where the host's
``kernel.core_pattern`` says: into the working directory, the writable ``/data``
volume, or to a crash handler on the host. A container can't change that setting,
so these controls act on this process instead and need no host setting.

There are two controls, and their order matters. ``coredump_filter`` is written
first, because a non-dumpable process's ``/proc`` files become root-owned and a
non-root process can then no longer write it. ``PR_SET_DUMPABLE`` 0 then means the
kernel skips the dump before routing it: no file and no handler, on any host.

Exec resets the dumpable attribute but keeps the filter. So a decoder started from
this process is dumpable again, with the filter it inherited: on a host that pipes
dumps, its crash still starts the handler, which receives no memory mappings. Its
resource limits are ``decode.py``'s and are unchanged.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
COREDUMP_FILTER = Path("/proc/self/coredump_filter")
MAX_FILTER_BYTES = 64


class CrashDumpError(Exception):
    """A control did not take effect. The message names no path or value."""


def prctl(operation: int, value: int = 0) -> int:
    """Call prctl(2), which the standard library doesn't wrap. A failure is -1."""
    try:
        call = ctypes.CDLL(None, use_errno=True).prctl
    except OSError, AttributeError:
        return -1
    call.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
    call.restype = ctypes.c_int
    return call(operation, value, 0, 0, 0)


def _filter_is_zero() -> bool:
    try:
        with COREDUMP_FILTER.open("rb") as file:
            return int(file.read(MAX_FILTER_BYTES), 16) == 0
    except OSError, ValueError:
        return False


def disable_crash_dumps() -> None:
    """Apply both controls, then read both back. Raise unless both took effect."""
    try:
        COREDUMP_FILTER.write_text("0")
    except OSError as exc:
        raise CrashDumpError("coredump_filter could not be written") from exc
    if prctl(PR_SET_DUMPABLE, 0) != 0:
        raise CrashDumpError("the process could not be made non-dumpable")
    if not _filter_is_zero():
        raise CrashDumpError("coredump_filter did not take effect")
    if prctl(PR_GET_DUMPABLE) != 0:
        raise CrashDumpError("the process is still dumpable")
