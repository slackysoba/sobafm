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

from evaluations.linux_resource.probe import ProbeError
from sobafm.fma_intake import LocalIntake, OwnedRead

from .consumer import ValidationConsumer, expected_outcome
from .limits import CaseClock, json_size
from .observation import ObservationWorkspace
from .packet import Packet, integer, load, load_qualification
from .process import ProcessPin, inventory
from .report_io import emit

ROOT = Path("/source")
SLOT = Path("/pending/slot")
ORIGINAL = Path("/input/original")
CONFIG = Path("/case/packet.json")


def executable_sha256(pid: int) -> str:
    """Bounded hash of a role executable (init/interpreter), never media."""
    digest = hashlib.sha256()
    count = 0
    with Path(f"/proc/{pid}/exe").open("rb", buffering=0) as executable:
        while chunk := executable.read(65_536):
            count += len(chunk)
            if count > 20_000_000:
                raise ProbeError("role_executable_bound")
            digest.update(chunk)
    return digest.hexdigest()


def preflight(packet: Packet, workspace: ObservationWorkspace) -> tuple[ProcessPin, ProcessPin]:
    workspace.environment(intake=True)
    if os.getpid() == 1 or os.uname().machine != "x86_64":
        raise ProbeError("separate_native_amd64_owner_required")
    init, owner = ProcessPin.capture(1, workspace), ProcessPin.capture(os.getpid(), workspace)
    if inventory(init, owner, workspace=workspace):
        raise ProbeError("unexpected_initial_members")
    for pin, key in ((init, "init_sha256"), (owner, "python_sha256")):
        if executable_sha256(pin.pid) != packet.text(key):
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
    return init, owner


def execute(packet: Packet) -> dict[str, object]:
    clock = CaseClock(packet.whole_end, packet.expires)
    workspace = ObservationWorkspace(clock)
    init, owner_pin = preflight(packet, workspace)
    before = workspace.resources()
    owner = LocalIntake(SLOT, ORIGINAL, packet.request)
    consumer = ValidationConsumer(owner, init, owner_pin, clock, packet.case, workspace=workspace)
    reader: OwnedRead | None = None
    failure: str | None = None

    def cancel(reason: str) -> None:
        clock.cancel()
        assert clock.cleanup_start_utc is not None
        owner.cancel(
            reason,
            started_at=datetime.fromtimestamp(clock.cleanup_start_utc, UTC),
            timing_unknown=clock.timing_unknown,
            deadline_missed=clock.missed,
        )

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
    delta: dict[str, int] = {}
    swap: int | None = None
    try:
        after = workspace.resources(cleanup=True)
        delta, swap = after.delta(before), after.swap
    except ProbeError:
        failure = failure or "validation_resources_failed"
    # All observations precede the debt/clock verdict. A failed final read is
    # never replaced by a fresh snapshot or a cached zero-swap observation.
    report = owner.report()
    reader_report = None if reader is None else reader.report()
    settled = (
        not workspace.debt
        and not inventory(init, owner_pin, workspace=workspace, cleanup=True)
        and report["released"] is True
        and report["cleanup_deadline_met"] is True
        and not clock.missed
        and not clock.case_missed
        and not clock.timing_unknown
        and delta.get("oom") == 0
        and delta.get("oom_kill") == 0
        and swap == 0
    )
    passed = settled and failure is None and consumer.finished
    result: dict[str, object] = {
        "schema": packet.case.schema("owner"),
        "case_binding": packet.case.record(),
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
    if packet.case.synthetic:
        result["qualification_passed"] = settled and expected_outcome(
            consumer.receipt(), packet.case
        )
    json_size(result)
    return result


def main(*, qualification: bool = False) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-reviewed-packet", action="store_true")
    args = parser.parse_args()
    if not args.execute_reviewed_packet:
        parser.error("reviewed exact packet required")
    packet = (load_qualification if qualification else load)(CONFIG, ROOT)
    result = execute(packet)
    emit(result)
    return 0 if result["qualification_passed" if qualification else "passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
