"""Real-process scenarios for ``test_crash_dumps.py``, each run as its own program.

The controls can't be undone, so they never run inside pytest. A scenario that needs
a non-root process gives up root itself when the checks run as root, and imports
everything it needs first, because a non-root process may not be able to read the
environment it was started from. No scenario changes a host setting.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from sobafm import crash_dumps
from sobafm.crash_dumps import CrashDumpError, disable_crash_dumps
from sobafm.decode import (
    MPG123_LIMITS,
    DecodeInput,
    DecodeProducer,
    DecoderCommand,
    launch_argv,
)
from sobafm.pcm import FRAME_BYTES, PcmBankPool

NOBODY = 65534
SELF_FILTER = Path("/proc/self/coredump_filter")
CGROUP_CONTROLLERS = Path("/sys/fs/cgroup/cgroup.controllers")
# Shell builtins only, because an external tool may not fit the decoder's address-space
# limit. So these are the facts of the decoder's own process. It records them only after
# end of input, when the parent's limits are already applied. A non-dumpable process's
# /proc files would be root-owned, so owning its own shows it is dumpable.
DECODER = """\
while read -r _; do :; done
read -r value < /proc/$$/coredump_filter
echo "$value" > "$FACTS/filter"
while read -r line; do echo "$line"; done < /proc/$$/limits > "$FACTS/limits"
if [ -O "/proc/$$/coredump_filter" ]; then echo yes > "$FACTS/dumpable"; fi
i=0
while [ "$i" -lt {bytes} ]; do printf '\\000'; i=$((i + 1)); done
"""
# The width of the name column in /proc/pid/limits.
LIMIT_NAME_WIDTH = 25


def report(**facts: object) -> None:
    print(json.dumps(facts), flush=True)


def limit_rows(path: Path) -> dict[str, list[str]]:
    return {
        line[:LIMIT_NAME_WIDTH].strip(): line[LIMIT_NAME_WIDTH:].split()[:2]
        for line in path.read_text().splitlines()
    }


def give_up_root() -> None:
    """Become non-root and dumpable, as the service starts, if this run is root."""
    if os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(NOBODY)
    os.setuid(NOBODY)
    # Changing user resets the attribute, which the service never does.
    assert crash_dumps.prctl(crash_dumps.PR_SET_DUMPABLE, 1) == 0


def filter_value() -> int:
    return int(SELF_FILTER.read_text(), 16)


def readable_cgroup() -> str:
    return CGROUP_CONTROLLERS.read_text() if CGROUP_CONTROLLERS.exists() else ""


def controls() -> None:
    give_up_root()
    before = (Path("/proc/self/cgroup").read_text(), readable_cgroup())
    disable_crash_dumps()
    report(
        uid=os.geteuid(),
        dumpable=crash_dumps.prctl(crash_dumps.PR_GET_DUMPABLE),
        filter=filter_value(),
        filter_owner=SELF_FILTER.stat().st_uid,
        open_descriptors=len(os.listdir("/proc/self/fd")),
        maps_bytes=len(Path("/proc/self/maps").read_text()),
        limits_bytes=len(Path("/proc/self/limits").read_text()),
        status_bytes=len(Path("/proc/self/status").read_text()),
        cgroup_unchanged=(Path("/proc/self/cgroup").read_text(), readable_cgroup()) == before,
        environ_readable=os.access("/proc/self/environ", os.R_OK),
    )


def wrong_order() -> None:
    """Already non-dumpable, a non-root process can't write the filter, and is refused."""
    give_up_root()
    assert crash_dumps.prctl(crash_dumps.PR_SET_DUMPABLE, 0) == 0
    try:
        disable_crash_dumps()
    except CrashDumpError as exc:
        report(uid=os.geteuid(), refused=str(exc))
    else:
        report(uid=os.geteuid(), refused=None)


def crash(directory: str) -> None:
    """Crash with every permission to dump, reporting the core limit first."""
    os.chdir(directory)
    _, hard = resource.getrlimit(resource.RLIMIT_CORE)
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    except ValueError, OSError:
        resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))
    report(core_limit=resource.getrlimit(resource.RLIMIT_CORE)[0])
    disable_crash_dumps()
    os.kill(os.getpid(), signal.SIGSEGV)


def decoder() -> None:
    """Launch a stand-in under the real limits, the way the service does, once protected."""
    give_up_root()
    disable_crash_dumps()
    facts = Path(tempfile.mkdtemp())
    try:
        payload = bytes(range(256)) * 8
        source = facts / "input"
        source.write_bytes(payload)
        pcm_bytes = 2 * FRAME_BYTES
        producer = DecodeProducer(
            DecodeInput(source, len(payload), hashlib.sha256(payload).hexdigest(), pcm_bytes),
            command=DecoderCommand(
                argv=("/bin/sh", "-c", DECODER.format(bytes=pcm_bytes)),
                limits=MPG123_LIMITS,
                env={"FACTS": str(facts)},
            ),
            pool=PcmBankPool(),
        )
        producer.start()
        result = producer.finish()
        report(
            uid=os.geteuid(),
            failure=result.failure,
            cleanup=result.cleanup,
            settled=result.settled,
            returncode=result.returncode,
            decoder_complete=result.decoder_complete,
            released=producer.release(),
            decoder_filter=int((facts / "filter").read_text(), 16),
            decoder_dumpable=(facts / "dumpable").exists(),
            decoder_limits=limit_rows(facts / "limits"),
            parent_dumpable=crash_dumps.prctl(crash_dumps.PR_GET_DUMPABLE),
        )
    finally:
        shutil.rmtree(facts)


def stack_limit_refused() -> None:
    """Ask for more stack than a non-root process may grant, so no route can set it."""
    resource.setrlimit(resource.RLIMIT_STACK, (1 << 20, 1 << 20))
    give_up_root()
    facts = Path(tempfile.mkdtemp())
    try:
        payload = b"x\n"
        source = facts / "input"
        source.write_bytes(payload)
        command = DecoderCommand(
            argv=("/bin/sh", "-c", "echo reached"),
            limits=((resource.RLIMIT_STACK, 2 << 20),),
            env={},
        )
        alone = subprocess.run(launch_argv(command), env={}, capture_output=True, text=True)
        producer = DecodeProducer(
            DecodeInput(source, len(payload), hashlib.sha256(payload).hexdigest(), FRAME_BYTES),
            command=command,
            pool=PcmBankPool(),
        )
        producer.start()
        result = producer.finish()
        report(
            uid=os.geteuid(),
            failure=result.failure,
            cleanup=result.cleanup,
            settled=result.settled,
            released=producer.release(),
            launch_status=alone.returncode,
            launch_output=alone.stdout,
        )
    finally:
        shutil.rmtree(facts)


def entry(filter_path: str = "") -> None:
    """Run the real ``python -m sobafm`` path, with the filter's location optionally moved."""
    if filter_path:
        crash_dumps.COREDUMP_FILTER = Path(filter_path)
    try:
        runpy.run_module("sobafm", run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        status = exc.code
    else:
        status = None
    report(
        status=status,
        native_loaded="disnake" in sys.modules,
        dumpable=crash_dumps.prctl(crash_dumps.PR_GET_DUMPABLE),
        filter=filter_value(),
    )


SCENARIOS: dict[str, Callable[..., None]] = {
    "controls": controls,
    "wrong_order": wrong_order,
    "crash": crash,
    "decoder": decoder,
    "stack_limit_refused": stack_limit_refused,
    "entry": entry,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]](*sys.argv[2:])
