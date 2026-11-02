"""PID>1 intake owner. Only the external controller can certify container exit."""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from evaluations.linux_resource.probe import ProbeError, counters, emit, observe, read_text
from sobafm.fma_intake import LocalIntake, OwnedRead

from .consumer import ValidationConsumer
from .limits import CaseClock, encode_report
from .packet import Packet, integer, load
from .process import ProcessPin, inventory

ROOT = Path("/source")
SLOT = Path("/pending/slot")
ORIGINAL = Path("/input/original")
CONFIG = Path("/case/packet.json")


def preflight(packet: Packet) -> tuple[ProcessPin, ProcessPin]:
    observe()
    if os.getpid() == 1 or os.uname().machine != "x86_64":
        raise ProbeError("separate_native_amd64_owner_required")
    init, owner = ProcessPin.capture(1), ProcessPin.capture(os.getpid())
    if inventory(init, owner):
        raise ProbeError("unexpected_initial_members")
    for pin, key in ((init, "init_sha256"), (owner, "python_sha256")):
        digest = hashlib.sha256()
        count = 0
        with Path(f"/proc/{pin.pid}/exe").open("rb", buffering=0) as executable:
            while chunk := executable.read(65_536):
                count += len(chunk)
                if count > 20_000_000:
                    raise ProbeError("role_executable_bound")
                digest.update(chunk)
        if digest.hexdigest() != packet.text(key):
            raise ProbeError("role_executable_changed")
    info = SLOT.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != 1000
        or info.st_gid != 1000
        or (info.st_dev, info.st_ino)
        != (integer(packet.raw["slot_device"]), integer(packet.raw["slot_inode"]))
        or os.statvfs(SLOT).f_flag & os.ST_RDONLY
    ):
        raise ProbeError("persistent_private_slot_binding")
    # Before LocalIntake opens the original, require separately bound read-only
    # source/config/original mounts and no unexpected mount below the volume.
    mounts = read_text(Path("/proc/self/mountinfo")).splitlines()
    destinations: dict[str, set[str]] = {}
    for line in mounts:
        parts = line.split()
        if len(parts) < 10:
            raise ProbeError("mount_inventory_unknown")
        destinations[parts[4]] = set(parts[5].split(","))
    if any(
        "ro" not in destinations.get(path, set())
        for path in ("/source", str(ORIGINAL), str(CONFIG))
    ):
        raise ProbeError("readonly_bind_gate")
    if "rw" not in destinations.get("/pending", set()) or any(
        name.startswith("/pending/") for name in destinations
    ):
        raise ProbeError("persistent_volume_mount_gate")
    return init, owner


def execute(packet: Packet) -> dict[str, object]:
    clock = CaseClock(packet.whole_end, packet.expires)
    init, owner_pin = preflight(packet)
    before = counters(read_text(Path("/sys/fs/cgroup/memory.events")))
    owner = LocalIntake(SLOT, ORIGINAL, packet.request)
    consumer = ValidationConsumer(owner, init, owner_pin, clock)
    reader: OwnedRead | None = None
    failure: str | None = None

    def cancel(reason: str) -> None:
        clock.cancel()
        assert clock.cleanup_start_utc is not None
        owner.cancel(reason, started_at=datetime.fromtimestamp(clock.cleanup_start_utc, UTC))

    def interrupted(_signal: int, _frame: FrameType | None) -> None:
        # Host-origin signal timing is not carried by a trusted IPC record.
        # Preserve unknown timing; outer controller keeps its earlier clock too.
        clock.timing_unknown = True
        cancel("controller_signal")

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        owner.start(execute=True)
        while not owner.pending.wait(0.01):
            if owner.join(0) or clock.remaining() <= 0:
                raise ProbeError("intake_did_not_become_pending")
        if clock.remaining() <= 0:
            raise ProbeError("case_deadline_before_handoff")
        reader = owner.read_into(consumer)
        while not reader.join(0.01):
            clock.observe()
            if clock.cleanup_start_utc is None:
                if clock.remaining() <= 0:
                    raise ProbeError("whole_case_deadline")
            elif consumer.phase not in ("finishing", "settled") or clock.missed:
                raise ProbeError("reader_cleanup_deadline")
        if not consumer.finished or reader.report().get("completed") is not True:
            raise ProbeError("reader_incomplete")
    except Exception:
        failure = "validation_owner_failed"
    finally:
        cancel("evaluation_complete" if failure is None else "evaluation_failed")
        # Non-daemon retained ownership outlives the verdict deadline. The outer
        # finite controller may stop this exact container, retaining its volume.
        while not owner.join(0.01):
            clock.observe()
    after = counters(read_text(Path("/sys/fs/cgroup/memory.events")))
    memory_ok = set(before) == set(after) and all(after[key] >= before[key] for key in before)
    delta = {key: after[key] - before[key] for key in before} if memory_ok else {}
    report = owner.report()
    reader_report = None if reader is None else reader.report()
    passed = (
        failure is None
        and consumer.finished
        and not inventory(init, owner_pin)
        and report["released"] is True
        and report["cleanup_deadline_met"] is True
        and not clock.missed
        and not clock.case_missed
        and not clock.timing_unknown
        and delta.get("oom") == 0
        and delta.get("oom_kill") == 0
        and read_text(Path("/sys/fs/cgroup/memory.swap.current")).strip() == "0"
    )
    result: dict[str, object] = {
        "schema": "sobafm-fma-owner-1",
        "packet_sha256": packet.fingerprint,
        "passed": passed,
        "failure": failure,
        "init": init.record(),
        "owner": owner_pin.record(),
        "intake": report,
        "reader": reader_report,
        "validation": consumer.receipt(),
        "memory_event_delta": delta,
        "cleanup": clock.receipt(),
        "catalog_admitted": False,
        "container_exit_observed": False,
    }
    encode_report(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-reviewed-packet", action="store_true")
    args = parser.parse_args()
    if not args.execute_reviewed_packet:
        parser.error("reviewed exact packet required")
    packet = load(CONFIG, ROOT)
    result = execute(packet)
    emit(result)
    return 0 if result["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
