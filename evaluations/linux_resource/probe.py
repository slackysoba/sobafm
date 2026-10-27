"""Opt-in Q1/Q2 process proof; ordinary CI imports only for offline tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import select
import signal
import sys
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

GROUP_BYTES = 90_177_536
PCM_BYTES = 4_608_000
COPY_BYTES = 4_194_304
PID_LIMIT = 64
READ_LIMIT = 65_536
ALLOCATION_PER_PROCESS = 60_000_000
CLEANUP_SECONDS = 3.0


class ProbeError(RuntimeError):
    """The observation cannot support the required claim."""


def read_text(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read(READ_LIMIT + 1)
    if len(raw) > READ_LIMIT:
        raise ProbeError("observation exceeds its read bound")
    return raw.decode("ascii")


def counters(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        key, value = line.split()
        if key in result or not value.isdecimal():
            raise ProbeError("invalid event counter")
        result[key] = int(value)
    return result


def unified_path(text: str) -> str:
    entries = [line[3:] for line in text.splitlines() if line.startswith("0::")]
    if len(entries) != 1 or not entries[0].startswith("/"):
        raise ProbeError("expected exactly one cgroup v2 membership")
    return entries[0]


def proc_fields(text: str) -> dict[str, str]:
    return dict(line.split(":", 1) for line in text.splitlines() if ":" in line)


@dataclass(frozen=True)
class Identity:
    pid: int
    parent_pid: int
    uid: int
    gid: int
    group: str
    seccomp: int
    effective_capabilities: int


def identity() -> Identity:
    status = proc_fields(read_text(Path("/proc/self/status")))
    return Identity(
        os.getpid(),
        os.getppid(),
        os.geteuid(),
        os.getegid(),
        unified_path(read_text(Path("/proc/self/cgroup"))),
        int(status["Seccomp"]),
        int(status["CapEff"], 16),
    )


def validate_identity(item: Identity, expected_group: str) -> None:
    if item.uid == 0 or item.gid == 0 or item.seccomp != 2 or item.effective_capabilities != 0:
        raise ProbeError("non-root, filtered, capability-free process required")
    if item.group != expected_group:
        raise ProbeError("descendant escaped the expected cgroup")


def cgroup_root() -> Path:
    # The reviewed invocation deliberately selects a private cgroup namespace.
    # Supporting unrelated mount/delegation configurations is out of scope.
    if unified_path(read_text(Path("/proc/self/cgroup"))) != "/":
        raise ProbeError("expected the reviewed private cgroup namespace root")
    for line in read_text(Path("/proc/self/mountinfo")).splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        if separator and after.split()[0] == "cgroup2" and fields[4] == "/sys/fs/cgroup":
            if "ro" not in fields[5].split(","):
                raise ProbeError("cgroup mount is writable")
            return Path("/sys/fs/cgroup")
    raise ProbeError("reviewed read-only cgroup mount is absent")


def verify_limits(values: dict[str, str]) -> None:
    expected = {"memory.max": GROUP_BYTES, "memory.swap.max": 0, "pids.max": PID_LIMIT}
    for name, limit in expected.items():
        if values.get(name, "").strip() != str(limit):
            raise ProbeError(f"effective {name} differs from the reviewed limit")
    quota, period = values["cpu.max"].split()
    if (
        not quota.isdecimal()
        or not period.isdecimal()
        or int(period) == 0
        or int(quota) != 2 * int(period)
    ):
        raise ProbeError("effective CPU limit differs from two CPUs")


def observe() -> dict[str, object]:
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise ProbeError("the proof requires a supported Linux target")
    own = identity()
    validate_identity(own, "/")
    root = cgroup_root()
    values = {
        name: read_text(root / name).strip()
        for name in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
    }
    verify_limits(values)
    if any(os.access(root / name, os.W_OK) for name in values):
        raise ProbeError("application can change a resource limit")
    return {
        "identity": asdict(own),
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "limits": values,
        "memory_events": counters(read_text(root / "memory.events")),
        "memory_current": int(read_text(root / "memory.current")),
        "swap_current": int(read_text(root / "memory.swap.current")),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "controlled_capacity": GROUP_BYTES + PCM_BYTES + COPY_BYTES,
        "pcm_parent_and_copy_reservation": PCM_BYTES + COPY_BYTES,
        "native_pcm_bound": "unresolved; Q3 is not qualified",
    }


def emit(event: dict[str, object]) -> None:
    line = json.dumps(event, sort_keys=True)
    if len(line) > READ_LIMIT:
        raise ProbeError("result exceeds its output bound")
    print(line, flush=True)


def receive_identities(fd: int, deadline: float) -> list[Identity]:
    data = bytearray()
    while data.count(b"\n") < 2:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise ProbeError("descendant identity handshake timed out")
        part = os.read(fd, 1024)
        if not part or len(data) + len(part) > 4096:
            raise ProbeError("invalid descendant identity handshake")
        data.extend(part)
    result: list[Identity] = []
    for line in data.decode("ascii").splitlines():
        pid, parent, uid, gid, group, seccomp, capabilities = line.split()
        result.append(
            Identity(
                int(pid), int(parent), int(uid), int(gid), group, int(seccomp), int(capabilities)
            )
        )
    if len(result) != 2:
        raise ProbeError("expected a child and grandchild")
    return result


def report_identity(fd: int) -> None:
    own = identity()
    line = (
        f"{own.pid} {own.parent_pid} {own.uid} {own.gid} {own.group} "
        f"{own.seccomp} {own.effective_capabilities}\n"
    ).encode("ascii")
    if os.write(fd, line) != len(line):
        raise ProbeError("short identity write")


def allocate() -> None:
    # Q2 allocates non-audio bytes. Its deliberate pressure is not useful fit.
    blocks: list[bytearray] = []
    remaining = ALLOCATION_PER_PROCESS
    while remaining:
        block = bytearray(min(1_048_576, remaining))
        for offset in range(0, len(block), 4096):
            block[offset] = 1
        blocks.append(block)
        remaining -= len(block)
    # Retain allocations until the owner terminates this process group.
    signal.pause()


def child_tree(ready: int, release: int, pressure: bool) -> None:
    os.setsid()
    report_identity(ready)
    grandchild = os.fork()
    if grandchild == 0:
        report_identity(ready)
    if os.read(release, 1) != b"x":
        os._exit(2)
    if pressure:
        try:
            allocate()
        except MemoryError:
            signal.pause()
    if grandchild:
        os.waitpid(grandchild, 0)
    os._exit(0)


def wait_child(pid: int, deadline: float) -> bool:
    with os.fdopen(os.pidfd_open(pid), "rb", buffering=0) as handle:
        ready = select.select([handle], [], [], max(0.0, deadline - time.monotonic()))[0]
        if not ready:
            return False
    os.waitpid(pid, 0)
    return True


def run_tree(pressure: bool) -> dict[str, object]:
    root = cgroup_root()
    before = counters(read_text(root / "memory.events"))
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    leader = os.fork()
    if leader == 0:
        os.close(ready_read)
        os.close(release_write)
        try:
            child_tree(ready_write, release_read, pressure)
        finally:
            os._exit(2)
    os.close(ready_write)
    os.close(release_read)
    reaped = False
    cleanup_deadline: float | None = None
    members: list[Identity] = []
    try:
        members = receive_identities(ready_read, time.monotonic() + 3)
        for member in members:
            validate_identity(member, "/")
        if members[0].pid != leader or members[1].parent_pid != leader:
            raise ProbeError("unexpected process tree")
        emit(
            {
                "event": "input_gate",
                "case": "Q2" if pressure else "Q1",
                "descendants": [asdict(member) for member in members],
            }
        )
        os.write(release_write, b"xx")
        if pressure:
            deadline = time.monotonic() + 10
            current = before
            while time.monotonic() < deadline:
                current = counters(read_text(root / "memory.events"))
                if current.get("oom_kill", 0) > before.get("oom_kill", 0):
                    break
                # Bounded event polling, not a sleep-based race assertion.
                select.select([], [], [], min(0.05, max(0.0, deadline - time.monotonic())))
            else:
                raise ProbeError("no cgroup OOM kill observed within the pressure bound")
            result: dict[str, object] = {
                "classification": "expected_enforcement_oom",
                "events_before": before,
                "events_after": current,
                "requested_bytes_maximum": 2 * ALLOCATION_PER_PROCESS,
                "station_cleanup_claim": False,
            }
            if int(read_text(root / "memory.swap.current")) != 0:
                raise ProbeError("swap was used despite the reviewed zero allowance")
        else:
            cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
            reaped = wait_child(leader, cleanup_deadline)
            if not reaped:
                raise ProbeError("identity helpers did not exit and reap normally")
            result = {"classification": "runtime_placement_observed"}
        return result
    finally:
        os.close(ready_read)
        os.close(release_write)
        deadline = cleanup_deadline or time.monotonic() + CLEANUP_SECONDS
        if not reaped:
            try:
                os.killpg(leader, signal.SIGKILL)
            except ProcessLookupError:
                # A failure before the ready handshake may precede setsid().
                with suppress(ProcessLookupError):
                    os.kill(leader, signal.SIGKILL)
            reaped = wait_child(leader, deadline)
        # The container init owns orphan reaping after Q2. Never equate a signal
        # or the direct child's exit with the release of all descendants.
        retained = [member.pid for member in members if Path(f"/proc/{member.pid}").exists()]
        while retained and time.monotonic() < deadline:
            select.select([], [], [], min(0.01, max(0.0, deadline - time.monotonic())))
            retained = [pid for pid in retained if Path(f"/proc/{pid}").exists()]
        emit(
            {
                "event": "release_observation",
                "leader_reaped": reaped,
                "retained_pids": retained,
                "cleanup_debt": not reaped or bool(retained),
            }
        )
        if not reaped or retained:
            raise ProbeError("probe cleanup deadline expired with retained process debt")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("q1", "q2"))
    parser.add_argument("--execute-owned-probe", action="store_true", required=True)
    args = parser.parse_args()
    try:
        emit({"event": "preflight", "observation": observe()})
        result = run_tree(args.case == "q2")
        emit(
            {
                "event": "result",
                "case": args.case.upper(),
                "observation": result,
                "q3": "not_run: native PCM bound unresolved",
                "q4": "not_run: Q3 gate precedes decoder cancellation proof",
            }
        )
        return 0
    except (ProbeError, OSError, ValueError) as error:
        # No traceback, arbitrary environment, payload or credentials in evidence.
        emit(
            {
                "event": "failure",
                "category": type(error).__name__,
                "message": str(error)[:512],
                "remaining_cases": "not_run",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
