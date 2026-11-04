"""No-media volume, slot, input-identity and PTY preflight, then packet freeze (#63).

Operator order: ``run`` this preflight once, ``freeze`` one packet from its
receipt at a chosen end time, then start the owned cases with the controller.
Nothing here reads media, uses the network or retries; ordinary CI fakes Docker.

* ``run`` (host, Linux): one finite Docker operation under one original
  ``CaseClock``. It creates one new labeled volume, then two containers with the
  owner container's limits: a root setup container restricted to CAP_CHOWN that
  creates the UID/GID 1000, mode 0700 slot, and a UID/GID 1000 probe container.
  Both stat the slot and the read-only input without opening it; the probe also
  hashes init/interpreter executables and checks PTY raw readback, backpressure
  and held-slave EOF using synthetic bytes. The private ledger it writes is the
  receipt; its SHA-256 is the packet's ``synthetic_preflight_sha256``.
* ``container`` (inside the reviewed image only): one probe mode.
* ``freeze`` (host): write a new packet from the receipt and verify ``load()``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import select
import signal
import stat
import sys
import time
import traceback
from pathlib import Path
from types import FrameType
from typing import cast

from evaluations.linux_resource.probe import ProbeError

from .consumer import object_pairs
from .controller import (
    CLEANUP_INSPECT,
    EXPECTED_HOST,
    INSPECT,
    LIMIT_ARGS,
    Journal,
    Management,
    mapping,
)
from .ingress import discard_exception
from .limits import CaseClock, encode_report, json_size
from .owner import executable_sha256
from .packet import SOURCE_FILES, Packet, load, load_qualification, read_bounded, string
from .pty_channel import open_diagnostics, read_diagnostics

RECEIPT_SCHEMA = "sobafm-44-preflight-receipt-1"
REPORT_SCHEMA = "sobafm-44-preflight-probe-1"
PREFLIGHT_SECONDS = 90.0
CONTAINER_SECONDS = 20.0
PTY_SECONDS = 5.0
PTY_PATTERN = bytes(range(256))
PTY_CHUNK = PTY_PATTERN * 16
PTY_BACKPRESSURE_CAP = 1_048_576
MODES = ("setup", "probe")
SLOT = Path("/pending/slot")
INPUT = Path("/input/original")
PYTHON_PATHS = ("/usr/local/bin/python", "/usr/local/bin/python3.14", "/usr/bin/python3.14")
SLOT_OWNER = 1000
# The probe container uses the exact owner identity. Only the setup container
# runs as root, with CAP_CHOWN alone, to hand the new slot to UID/GID 1000.
CONTAINER_IDENTITY = {
    "setup": ("0:0", ("--cap-add=CHOWN",)),
    "probe": ("1000:1000", ()),
}
INPUT_IDENTITY = ("device", "inode", "bytes", "mode", "mtime_ns", "ctime_ns")
PTY_FACTS = (
    "raw_readback",
    "backpressure_reached",
    "drained",
    "held_slave_open",
    "eof_after_last_close",
)


# ---------------------------------------------------------------------------
# Container side. These functions run inside the reviewed image only.


def slot_facts(path: Path) -> dict[str, object]:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ProbeError("preflight_slot_not_directory")
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "writable": not os.statvfs(path).f_flag & os.ST_RDONLY,
    }


def create_slot(path: Path) -> None:
    """Create the one new slot; an existing slot means the volume was not new."""
    os.mkdir(path, 0o700)
    os.chmod(path, 0o700)  # Independent of umask; before ownership leaves root.
    os.chown(path, SLOT_OWNER, SLOT_OWNER)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def input_facts(path: Path) -> dict[str, object]:
    """Identity from metadata only. The input is never opened or read."""
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ProbeError("preflight_input_not_regular")
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "bytes": info.st_size,
        "mode": stat.S_IMODE(info.st_mode),
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _drain(master: int, count: int, deadline: float) -> bytes | None:
    """Read exactly ``count`` synthetic bytes, or None at the bounded deadline."""
    received = bytearray()
    while len(received) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
            return None
        chunk = read_diagnostics(master, min(65_536, count - len(received)))
        if chunk is None:
            continue
        if not chunk:
            return None
        received += chunk
    return bytes(received)


def pty_facts(deadline: float) -> dict[str, object]:
    """Raw readback, write backpressure and held-slave EOF; synthetic bytes only."""
    facts: dict[str, object] = dict.fromkeys(PTY_FACTS, False)
    facts["backpressure_bytes"] = 0
    owned: list[int] = []
    try:
        master, slave = open_diagnostics(owned)
        held = os.dup(slave)
        owned.append(held)
        if os.write(slave, PTY_PATTERN) != len(PTY_PATTERN):
            raise ProbeError("preflight_pty_short_write")
        facts["raw_readback"] = _drain(master, len(PTY_PATTERN), deadline) == PTY_PATTERN
        os.set_blocking(slave, False)
        written = hashlib.sha256()
        count = 0
        while count < PTY_BACKPRESSURE_CAP and time.monotonic() < deadline:
            try:
                amount = os.write(slave, PTY_CHUNK)
            except BlockingIOError:
                facts["backpressure_reached"] = True
                break
            written.update(PTY_CHUNK[:amount])
            count += amount
        facts["backpressure_bytes"] = count
        received = _drain(master, count, deadline)
        facts["drained"] = (
            received is not None
            and hashlib.sha256(received).digest() == written.digest()
            and read_diagnostics(master, 1) is None
        )
        del received
        os.close(slave)
        owned.remove(slave)
        # Another open slave keeps the channel alive: no EOF, no data.
        select.select([master], [], [], 0.05)
        facts["held_slave_open"] = read_diagnostics(master, 1) is None
        os.close(held)
        owned.remove(held)
        remaining = max(0, deadline - time.monotonic())
        facts["eof_after_last_close"] = bool(
            select.select([master], [], [], remaining)[0] and read_diagnostics(master, 1) == b""
        )
    finally:
        for fd in owned:
            os.close(fd)
    return facts


def blank_report(mode: str) -> dict[str, object]:
    pty: dict[str, object] = dict.fromkeys(PTY_FACTS, False)
    pty["backpressure_bytes"] = 0
    return {
        "schema": REPORT_SCHEMA,
        "mode": mode,
        "failure": None,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "slot": {"device": 0, "inode": 0, "mode": 0, "uid": 0, "gid": 0, "writable": False},
        "input": dict.fromkeys(INPUT_IDENTITY, 0),
        "init_sha256": None,
        "python_sha256": None,
        "pty": pty,
    }


def container_report(mode: str, slot: Path = SLOT, source: Path = INPUT) -> dict[str, object]:
    if mode not in MODES:
        raise ProbeError("preflight_mode")
    report = blank_report(mode)
    try:
        if mode == "setup":
            create_slot(slot)
        report["slot"] = slot_facts(slot)
        report["input"] = input_facts(source)
        if mode == "probe":
            report["init_sha256"] = executable_sha256(1)
            report["python_sha256"] = executable_sha256(os.getpid())
            report["pty"] = pty_facts(time.monotonic() + PTY_SECONDS)
    except ProbeError as error:
        report["failure"] = str(error)
    except OSError:
        report["failure"] = f"preflight_{mode}_os_error"
    json_size(report)
    return report


# ---------------------------------------------------------------------------
# Host side: one finite Docker management operation.


def validate_target(
    image: str, python_path: str, volume: str, checkout_path: str, source_path: str
) -> None:
    if re.fullmatch("sha256:[0-9a-f]{64}", image) is None:
        raise ProbeError("preflight_image")
    if python_path not in PYTHON_PATHS:
        raise ProbeError("preflight_python_path")
    # The longest container name, "/" + volume + "-setup", stays within the
    # existing 43-character cleanup name domain.
    if re.fullmatch("sobafm-44-[a-z0-9-]{8,26}", volume) is None:
        raise ProbeError("preflight_volume_name")
    for value in (checkout_path, source_path):
        string(value)
        if not value.startswith("/") or "," in value or ".." in Path(value).parts:
            raise ProbeError("preflight_absolute_linux_mount")


def preflight_sources(root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256(
            read_bounded(root / "evaluations/fma_validation" / name, 262_144)
        ).hexdigest()
        for name in ("preflight.py", "pty_channel.py", "controller.py")
    }


class Preflight(Management):
    def __init__(
        self,
        journal: Journal,
        *,
        image: str,
        python_path: str,
        volume: str,
        checkout_path: str,
        source_path: str,
        identity: str,
        clock: CaseClock | None = None,
        docker: str = "docker",
    ) -> None:
        if clock is None:
            end = time.time() + PREFLIGHT_SECONDS
            clock = CaseClock(end, end)
        super().__init__(journal, clock, docker)
        self.image, self.python_path, self.volume = image, python_path, volume
        self.checkout_path, self.source_path = checkout_path, source_path
        self.identity = identity
        self.container: str | None = None
        self.failure: str | None = None

    @staticmethod
    def ledger(
        *,
        image: str,
        python_path: str,
        volume: str,
        checkout_path: str,
        source_path: str,
        identity: str,
        root: Path,
    ) -> dict[str, object]:
        return {
            "schema": RECEIPT_SCHEMA,
            "identity": identity,
            "image": image,
            "python_path": python_path,
            "volume": volume,
            "checkout_path": checkout_path,
            "source_path": source_path,
            "preflight_source_sha256": preflight_sources(root),
            "phase": "claimed",
            "containers": [],
            "volume_created": None,
            "volume_mountpoint_sha256": None,
            "slot_device": None,
            "slot_inode": None,
            "input": None,
            "init_sha256": None,
            "python_sha256": None,
            "pty": None,
            "failure": None,
            "cleanup_debt": True,
            "passed": False,
        }

    def labels(self) -> list[str]:
        return [
            "--label",
            "org.sobafm.project=sobafm",
            "--label",
            "org.sobafm.issue=44",
        ]

    def create_volume(self) -> None:
        self.journal.data["phase"] = "creating_volume"
        self.journal.save()
        created = cast(
            bytes,
            self.run(
                [
                    "volume",
                    "create",
                    *self.labels(),
                    "--label",
                    f"org.sobafm.preflight={self.identity}",
                    self.volume,
                ]
            ),
        )
        if created.decode("ascii").strip() != self.volume:
            raise ProbeError("preflight_volume_create")
        volume = mapping(
            self.run(
                [
                    "volume",
                    "inspect",
                    "--format",
                    '{"Name":{{json .Name}},"CreatedAt":{{json .CreatedAt}},'
                    '"Mountpoint":{{json .Mountpoint}},"Labels":{'
                    '"org.sobafm.issue":{{json (index .Labels "org.sobafm.issue")}},'
                    '"org.sobafm.preflight":{{json (index .Labels "org.sobafm.preflight")}}}}',
                    self.volume,
                ],
                family="preflight_volume",
            )
        )
        labels = mapping(volume.get("Labels"))
        # Docker returns an existing volume of the same name without error; only
        # this run's identity label shows that the volume is new.
        if (
            volume.get("Name") != self.volume
            or labels.get("org.sobafm.issue") != "44"
            or labels.get("org.sobafm.preflight") != self.identity
        ):
            raise ProbeError("preflight_volume_not_new")
        self.journal.data.update(
            volume_created=string(volume.get("CreatedAt")),
            volume_mountpoint_sha256=hashlib.sha256(
                string(volume.get("Mountpoint")).encode()
            ).hexdigest(),
        )
        self.journal.save()

    def creation_args(self, mode: str) -> list[str]:
        user, capabilities = CONTAINER_IDENTITY[mode]
        return [
            "create",
            *LIMIT_ARGS,
            "--name",
            f"{self.volume}-{mode}",
            *self.labels(),
            "--label",
            f"org.sobafm.packet={self.identity}",
            f"--user={user}",
            *capabilities,
            "--mount",
            f"type=bind,source={self.checkout_path},target=/source,readonly",
            "--mount",
            f"type=bind,source={self.source_path},target=/input/original,readonly",
            "--mount",
            f"type=volume,source={self.volume},target=/pending,volume-nocopy",
            "--entrypoint",
            self.python_path,
            self.image,
            *self.entry_args(mode),
        ]

    @staticmethod
    def entry_args(mode: str) -> list[str]:
        return ["-B", "-m", "evaluations.fma_validation.preflight", "container", mode]

    def inspect(self, mode: str, *, cleanup: bool = False) -> dict[str, object]:
        if self.container is None:
            raise ProbeError("container_identity_unknown")
        record = mapping(
            self.run(
                [
                    "container",
                    "inspect",
                    "--format",
                    CLEANUP_INSPECT if cleanup else INSPECT,
                    self.container,
                ],
                1 if cleanup else 5,
                cleanup=cleanup,
                family="cleanup" if cleanup else "config",
            )
        )
        labels = mapping(record.get("labels"))
        if (
            record.get("id") != self.container
            or record.get("name") != f"/{self.volume}-{mode}"
            or record.get("image") != self.image
            or labels.get("org.sobafm.project") != "sobafm"
            or labels.get("org.sobafm.issue") != "44"
            or labels.get("org.sobafm.packet") != self.identity
        ):
            raise ProbeError("container_ownership_changed")
        return record

    def verify_configuration(self, mode: str, record: dict[str, object]) -> None:
        host = mapping(record.get("host"))
        user, capabilities = CONTAINER_IDENTITY[mode]
        added = host.get("CapAdd")
        if any(host.get(key) != value for key, value in EXPECTED_HOST.items()) or (
            added not in (["CHOWN"], ["CAP_CHOWN"]) if capabilities else added
        ):
            raise ProbeError("container_limits_changed")
        if (
            mapping(host.get("RestartPolicy")).get("Name") != "no"
            or mapping(host.get("LogConfig")).get("Type") != "none"
        ):
            raise ProbeError("container_persistence_policy")
        if (
            record.get("user") != user
            or record.get("workdir") != "/source"
            or record.get("path") != self.python_path
            or record.get("args") != self.entry_args(mode)
        ):
            raise ProbeError("container_entrypoint_changed")
        mounts = record.get("mounts")
        if not isinstance(mounts, list) or len(cast(list[object], mounts)) != 3:
            raise ProbeError("container_mounts_changed")
        observed = {
            string(mapping(row).get("Destination")): mapping(row)
            for row in cast(list[object], mounts)
        }
        for destination, source in (
            ("/source", self.checkout_path),
            ("/input/original", self.source_path),
        ):
            item = observed.get(destination, {})
            if (
                item.get("Type") != "bind"
                or item.get("Source") != source
                or item.get("RW") is not False
            ):
                raise ProbeError("readonly_source_mount_changed")
        volume = observed.get("/pending", {})
        if (
            volume.get("Type") != "volume"
            or volume.get("Name") != self.volume
            or volume.get("RW") is not True
        ):
            raise ProbeError("volume_mount_changed")

    def exited(self, mode: str) -> bool:
        state = mapping(self.inspect(mode, cleanup=True).get("state"))
        return (
            state.get("Running") is False
            and state.get("Pid") == 0
            and (state.get("Status") == "exited")
        )

    def container_run(self, mode: str) -> dict[str, object]:
        self.journal.data["phase"] = f"creating_{mode}_container"
        self.journal.save()
        identifier = cast(bytes, self.run(self.creation_args(mode))).decode("ascii").strip()
        if re.fullmatch("[0-9a-f]{64}", identifier) is None:
            raise ProbeError("container_identifier_unknown")
        self.container = identifier
        cast(list[object], self.journal.data["containers"]).append(
            {"mode": mode, "id": identifier, "exited": False}
        )
        self.journal.save()
        self.verify_configuration(mode, self.inspect(mode))
        report = mapping(
            self.run(["start", "--attach", identifier], CONTAINER_SECONDS, family="preflight")
        )
        state = mapping(self.inspect(mode, cleanup=True).get("state"))
        if (
            state.get("Running") is not False
            or state.get("Pid") != 0
            or state.get("Status") != "exited"
            or state.get("ExitCode") != 0
            or state.get("OOMKilled") is not False
        ):
            raise ProbeError("preflight_container_exit")
        cast(list[dict[str, object]], self.journal.data["containers"])[-1]["exited"] = True
        self.container = None
        self.journal.save()
        self.verify_report(mode, report)
        return report

    def verify_report(self, mode: str, report: dict[str, object]) -> None:
        if report.get("schema") != REPORT_SCHEMA or report.get("mode") != mode:
            raise ProbeError("preflight_report_binding")
        if report.get("failure") is not None:
            raise ProbeError("preflight_container_failed")
        uid = 0 if mode == "setup" else SLOT_OWNER
        if report.get("uid") != uid or report.get("gid") != uid:
            raise ProbeError("preflight_container_identity")
        slot = mapping(report.get("slot"))
        if (
            slot.get("mode") != 0o700
            or slot.get("uid") != SLOT_OWNER
            or slot.get("gid") != SLOT_OWNER
            or slot.get("writable") is not True
            or slot.get("inode") == 0
        ):
            raise ProbeError("persistent_private_slot_binding")
        if mapping(report.get("input")).get("inode") == 0:
            raise ProbeError("preflight_input_identity")
        if mode == "probe":
            pty = mapping(report.get("pty"))
            for key in PTY_FACTS:
                if pty.get(key) is not True:
                    raise ProbeError(f"preflight_pty_{key}")
            for key in ("init_sha256", "python_sha256"):
                if report.get(key) is None:
                    raise ProbeError("preflight_executable_identity")

    def compare(self, setup: dict[str, object], probe: dict[str, object]) -> None:
        slots = [mapping(item.get("slot")) for item in (setup, probe)]
        if any(slots[0].get(key) != slots[1].get(key) for key in ("device", "inode")):
            raise ProbeError("preflight_slot_identity_unstable")
        inputs = [mapping(item.get("input")) for item in (setup, probe)]
        if inputs[0] != inputs[1]:
            raise ProbeError("preflight_input_identity_unstable")
        self.journal.data.update(
            slot_device=slots[1]["device"],
            slot_inode=slots[1]["inode"],
            input=inputs[1],
            init_sha256=probe["init_sha256"],
            python_sha256=probe["python_sha256"],
            pty=probe["pty"],
        )

    def evaluate(self) -> tuple[str | None, bool]:
        failed: str | None = None
        stopped = True
        current = "setup"
        try:
            self.verify_engine()
            self.verify_image(self.image)
            self.create_volume()
            reports: list[dict[str, object]] = []
            for mode in MODES:
                current = mode
                reports.append(self.container_run(mode))
            self.compare(*reports)
        except Exception as error:
            failed = str(error) if isinstance(error, ProbeError) else "preflight_failed"
            self.clock.cancel()
            discard_exception(error)
        if failed is not None and self.container is not None:
            # One stop under the original cleanup clock; no retry, no removal.
            try:
                state = mapping(self.inspect(current, cleanup=True).get("state"))
                if state.get("Running") is True:
                    self.run(["stop", "--time=1", self.container], 1.5, cleanup=True)
                stopped = self.exited(current)
                if stopped:
                    containers = cast(list[dict[str, object]], self.journal.data["containers"])
                    if containers and containers[-1]["id"] == self.container:
                        containers[-1]["exited"] = True
            except Exception as error:
                stopped = False
                discard_exception(error)
        return failed, stopped

    def execute(self) -> dict[str, object]:
        failed: str | None = None
        stopped = False
        interrupted: BaseException | None = None
        try:
            try:
                failed, stopped = self.evaluate()
            except BaseException as error:
                interrupted = error
                if error.__traceback__ is not None:
                    traceback.clear_frames(error.__traceback__)
                error.__traceback__ = None
        finally:
            self.drain()
        if interrupted is not None:
            raise interrupted
        accepted = (
            failed is None
            and stopped
            and not self.clock.missed
            and not self.clock.case_missed
            and not self.clock.timing_unknown
        )
        self.journal.data.update(
            phase="settled" if accepted else "retained_for_reconciliation",
            passed=accepted,
            failure=failed,
            cleanup_debt=not accepted,
            cleanup=self.clock.receipt(),
            commands=self.command_receipts,
        )
        self.journal.save()
        return self.journal.data


# ---------------------------------------------------------------------------
# Freeze: one packet from one passed receipt and a chosen end time.


def freeze(
    receipt_path: Path,
    request_path: Path,
    packet_path: Path,
    root: Path,
    *,
    whole_end_utc: str,
    case_id: str,
    review: str,
    source_commit: str,
    source_tree: str,
    qualification: bool,
    now: float | None = None,
) -> Packet:
    data = read_bounded(receipt_path)
    receipt = mapping(json.loads(data, object_pairs_hook=object_pairs))
    if (
        receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("phase") != "settled"
        or receipt.get("passed") is not True
        or receipt.get("cleanup_debt") is not False
        or receipt.get("failure") is not None
    ):
        raise ProbeError("preflight_receipt_not_passed")
    if receipt.get("checkout_path") != str(root):
        raise ProbeError("preflight_checkout_changed")
    if receipt.get("preflight_source_sha256") != preflight_sources(root):
        raise ProbeError("preflight_source_changed")
    request: object = json.loads(read_bounded(request_path), object_pairs_hook=object_pairs)
    target = packet_path.parent.resolve() / packet_path.name
    raw: dict[str, object] = {
        "schema": "sobafm-owned-qualification-case-1" if qualification else "sobafm-fma-case-1",
        "case_id": case_id,
        "review": review,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "source_hashes": {
            name: hashlib.sha256(read_bounded(root / name, 262_144)).hexdigest()
            for name in SOURCE_FILES
        },
        "image": receipt.get("image"),
        "target": "linux/amd64",
        "init_sha256": receipt.get("init_sha256"),
        "python_sha256": receipt.get("python_sha256"),
        "python_path": receipt.get("python_path"),
        "whole_end_utc": whole_end_utc,
        "request": request,
        "volume": receipt.get("volume"),
        "volume_created": receipt.get("volume_created"),
        "volume_mountpoint_sha256": receipt.get("volume_mountpoint_sha256"),
        "slot_device": receipt.get("slot_device"),
        "slot_inode": receipt.get("slot_inode"),
        "source_path": receipt.get("source_path"),
        "checkout_path": str(root),
        "packet_path": str(target),
        "synthetic_preflight_sha256": hashlib.sha256(data).hexdigest(),
    }
    encoded = encode_report(raw)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            with memoryview(encoded) as view:
                offset = 0
                while offset < len(view):
                    offset += os.write(fd, view[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        packet = (load_qualification if qualification else load)(target, root, now=now)
        if mapping(receipt.get("input")).get("bytes") != packet.case.bytes:
            raise ProbeError("preflight_input_size_changed")
    except BaseException:
        # This call created the inode exclusively; a rejected packet never remains.
        target.unlink(missing_ok=True)
        raise
    return packet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    container = commands.add_parser("container")
    container.add_argument("mode", choices=MODES)
    run = commands.add_parser("run")
    run.add_argument("--image", required=True)
    run.add_argument("--python-path", required=True)
    run.add_argument("--volume", required=True)
    run.add_argument("--source", type=Path, required=True)
    run.add_argument("--receipt", type=Path, required=True)
    run.add_argument("--execute-preflight", action="store_true")
    frozen = commands.add_parser("freeze")
    frozen.add_argument("--receipt", type=Path, required=True)
    frozen.add_argument("--request", type=Path, required=True)
    frozen.add_argument("--packet", type=Path, required=True)
    frozen.add_argument("--whole-end-utc", required=True)
    frozen.add_argument("--case-id", required=True)
    frozen.add_argument("--review", required=True)
    frozen.add_argument("--source-commit", required=True)
    frozen.add_argument("--source-tree", required=True)
    frozen.add_argument("--qualification", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.command == "container":
        report = container_report(args.mode)
        sys.stdout.write(encode_report(report, newline=True).decode())
        return 0
    if args.command == "freeze":
        packet = freeze(
            args.receipt,
            args.request,
            args.packet,
            root,
            whole_end_utc=args.whole_end_utc,
            case_id=args.case_id,
            review=args.review,
            source_commit=args.source_commit,
            source_tree=args.source_tree,
            qualification=args.qualification,
        )
        print(packet.fingerprint)
        return 0
    if not args.execute_preflight or os.uname().sysname != "Linux":
        parser.error("explicit Linux --execute-preflight required")
    source = str(args.source.resolve())
    validate_target(args.image, args.python_path, args.volume, str(root), source)
    identity = secrets.token_hex(32)
    journal = Journal(
        args.receipt,
        Preflight.ledger(
            image=args.image,
            python_path=args.python_path,
            volume=args.volume,
            checkout_path=str(root),
            source_path=source,
            identity=identity,
            root=root,
        ),
    )
    preflight = Preflight(
        journal,
        image=args.image,
        python_path=args.python_path,
        volume=args.volume,
        checkout_path=str(root),
        source_path=source,
        identity=identity,
    )

    def interrupted(_signal: int, _frame: FrameType | None) -> None:
        preflight.clock.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    try:
        result = preflight.execute()
    finally:
        journal.close()
    print(encode_report({k: result[k] for k in ("phase", "passed", "failure")}).decode())
    return 0 if result["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
