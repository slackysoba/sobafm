"""One-shot Linux Docker supervisor with persistent, non-reusable ownership debt."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import select
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import FrameType
from typing import cast

from evaluations.linux_resource.probe import ProbeError

from .consumer import object_pairs, reports, validate_decode, validate_metadata
from .limits import FILE_BYTES, FILE_SHA256, REPORT_BYTES, CaseClock, encode_report
from .packet import Packet, load, read_bounded, string


class Command:
    """Retain launch, original pipes and actual child through timeout/close debt."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.process: subprocess.Popen[bytes] | None = None
        self.error: str | None = None
        self.output = bytearray()
        self.diagnostic = bytearray()
        self.retained: set[int] = set()
        self.uncertain: set[int] = set()
        self.stopping: float | None = None
        self.worker = threading.Thread(
            target=self._launch, name="owned-docker-command", daemon=False
        )
        self.worker.start()

    def _launch(self) -> None:
        try:
            self.process = subprocess.Popen(
                self.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
            for stream in (self.process.stdout, self.process.stderr):
                assert stream is not None
                self.retained.add(stream.fileno())
            for stream in (self.process.stdout, self.process.stderr):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
        except Exception:
            self.error = "management_launch_failed"

    def stop(self) -> None:
        if self.stopping is None:
            self.stopping = time.monotonic()

    def pump(self) -> None:
        if self.worker.is_alive():
            return
        self.worker.join(0)
        process = self.process
        if process is None:
            return
        if self.stopping is not None and process.poll() is None:
            # Direct unreaped Popen child only. No PID/name reuse targeting.
            if time.monotonic() - self.stopping >= 0.25:
                process.kill()
            else:
                process.terminate()
        for stream, target in ((process.stdout, self.output), (process.stderr, self.diagnostic)):
            assert stream is not None
            if stream.closed or stream.fileno() not in self.retained:
                continue
            fd = stream.fileno()
            if fd in self.uncertain:
                try:
                    os.fstat(fd)
                except OSError as error:
                    if error.errno == errno.EBADF:
                        self.uncertain.remove(fd)
                        self.retained.remove(fd)
                continue
            if not select.select([fd], [], [], 0)[0]:
                continue
            try:
                data = os.read(fd, min(4096, REPORT_BYTES - len(target) + 1))
            except BlockingIOError, InterruptedError:
                continue
            if len(target) + len(data) > REPORT_BYTES:
                self.error = "management_output_limit"
                self.stop()
            else:
                target.extend(data)
            if not data:
                try:
                    stream.close()
                    self.retained.remove(fd)
                except OSError:
                    self.uncertain.add(fd)
                    self.error = "management_close_debt"
        process.poll()

    def settled(self) -> bool:
        return (
            not self.worker.is_alive()
            and not self.retained
            and not self.uncertain
            and (self.process is None or self.process.poll() is not None)
        )

    def receipt(self) -> dict[str, object]:
        return {
            "pid": None if self.process is None else self.process.pid,
            "launch_alive": self.worker.is_alive(),
            "retained_fds": sorted(self.retained),
            "uncertain_closes": sorted(self.uncertain),
            "settled": self.settled(),
            "exit_code": None if self.process is None else self.process.poll(),
            "output_bytes": len(self.output),
            "diagnostic_bytes": len(self.diagnostic),
            "output_sha256": hashlib.sha256(self.output).hexdigest(),
            "diagnostic_sha256": hashlib.sha256(self.diagnostic).hexdigest(),
            "error": self.error,
        }


class Journal:
    """A new private inode per attempt. Existing/partial journals block new work."""

    def __init__(self, path: Path, packet: Packet) -> None:
        self.fd = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        info = os.fstat(self.fd)
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ProbeError("private_journal_required")
        self.identity = (info.st_dev, info.st_ino)
        self.path = path
        self.worker: threading.Thread | None = None
        self.error: Exception | None = None
        self.data: dict[str, object] = {
            "schema": "sobafm-fma-controller-ledger-1",
            "packet_sha256": packet.fingerprint,
            "case_id": packet.text("case_id"),
            "image": packet.text("image"),
            "volume": packet.text("volume"),
            "expires_at": packet.request.expires_at,
            "whole_end_utc": packet.whole_end,
            "container_id": None,
            "phase": "claimed",
            "cleanup_debt": True,
            "slot_reusable": False,
            "passed": False,
        }
        self.save()
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)

    def save(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            raise ProbeError("journal_operation_retained")
        if self.error is not None:
            raise self.error
        data = encode_report(self.data)

        def write() -> None:
            try:
                self._write(data)
            except Exception as error:
                self.error = error

        self.worker = threading.Thread(target=write, name="owned-journal-write", daemon=False)
        self.worker.start()
        self.worker.join(0.25)
        if self.worker.is_alive():
            raise ProbeError("journal_operation_retained")
        if self.error is not None:
            raise self.error

    def _write(self, data: bytes) -> None:
        info = self.path.lstat()
        if (info.st_dev, info.st_ino) != self.identity or info.st_nlink != 1:
            raise ProbeError("journal_identity_debt")
        offset = 0
        while offset < len(data):
            written = os.pwrite(self.fd, data[offset:], offset)
            if written <= 0:
                raise ProbeError("journal_write_failed")
            offset += written
        os.ftruncate(self.fd, len(data))
        os.fsync(self.fd)

    def close(self) -> None:
        # Keep the descriptor alive while a failed/timed-out original write is
        # still in flight. Closing or starting a replacement would lose debt.
        if self.worker is not None:
            self.worker.join()
        os.close(self.fd)


INSPECT = (
    '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
    '"labels":{{json .Config.Labels}},"state":{{json .State}},"mounts":{{json .Mounts}},'
    '"host":{{json .HostConfig}},"user":{{json .Config.User}},"path":{{json .Path}},'
    '"args":{{json .Args}},"workdir":{{json .Config.WorkingDir}}}'
)


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProbeError("management_object_required")
    return cast(dict[str, object], value)


def validate_owner(report: dict[str, object], packet: Packet) -> None:
    if set(report) != {
        "schema",
        "packet_sha256",
        "passed",
        "failure",
        "init",
        "owner",
        "intake",
        "reader",
        "validation",
        "memory_event_delta",
        "cleanup",
        "catalog_admitted",
        "container_exit_observed",
    } or (
        report["schema"] != "sobafm-fma-owner-1"
        or report["packet_sha256"] != packet.fingerprint
        or report["passed"] is not True
        or report["failure"] is not None
        or report["catalog_admitted"] is not False
        or report["container_exit_observed"] is not False
    ):
        raise ProbeError("owner_result_failed")
    intake = mapping(report["intake"])
    reader = mapping(report["reader"])
    validation = mapping(report["validation"])
    if (
        any(
            intake.get(key) is not True
            for key in ("released", "cleanup_deadline_met", "cleanup_deadline_known")
        )
        or any(
            intake.get(key) is not False
            for key in (
                "payload_retained",
                "worker_alive",
                "cleanup_debt",
                "unresolved_previous_slot",
            )
        )
        or intake.get("retained_handles") != []
        or intake.get("original_io_in_flight") is not None
        or intake.get("request_sha256") != packet.request.fingerprint
        or intake.get("copied_bytes") != FILE_BYTES
        or intake.get("copied_sha256") != FILE_SHA256
        or any(
            reader.get(key) is not True
            for key in ("stream_verified", "completed", "consumer_released", "settled")
        )
        or any(
            reader.get(key) is not False
            for key in ("descriptor_retained", "reader_alive", "cancel_alive")
        )
        or reader.get("failure") is not None
        or reader.get("in_flight") is not None
        or reader.get("read_bytes") != FILE_BYTES
        or reader.get("delivered_bytes") != FILE_BYTES
        or reader.get("read_sha256") != FILE_SHA256
        or validation.get("finished") is not True
        or validation.get("phase") != "settled"
        or validation.get("active") is not None
        or validation.get("delivered_bytes") != FILE_BYTES
    ):
        raise ProbeError("owner_release_evidence_failed")
    delta = mapping(report["memory_event_delta"])
    if (
        delta.get("oom") != 0
        or delta.get("oom_kill") != 0
        or any(type(value) is not int or value < 0 for value in delta.values())
    ):
        raise ProbeError("owner_memory_evidence_failed")
    validate_decode(mapping(validation.get("decode")))
    validate_metadata(mapping(validation.get("metadata")))
    helpers = validation.get("helpers")
    if not isinstance(helpers, list) or len(cast(list[object], helpers)) != 2:
        raise ProbeError("owner_helper_release_missing")
    for role, value in zip(("metadata", "decoder"), cast(list[object], helpers), strict=True):
        helper = mapping(value)
        if (
            helper.get("role") != role
            or helper.get("exit_code") != 0
            or helper.get("launching") is not False
            or helper.get("failure") is not None
            or any(
                helper.get(key) != []
                for key in ("retained_fds", "in_flight_fds", "uncertain_closes")
            )
            or helper.get("stdout_eof") is not True
            or helper.get("stderr_eof") is not True
        ):
            raise ProbeError("owner_helper_release_failed")


def creation_args(packet: Packet) -> list[str]:
    return [
        "create",
        "--pull=never",
        "--name",
        packet.text("case_id"),
        "--init",
        "--restart=no",
        "--label",
        "org.sobafm.project=sobafm",
        "--label",
        "org.sobafm.issue=44",
        "--label",
        f"org.sobafm.packet={packet.fingerprint}",
        "--user=1000:1000",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--network=none",
        "--read-only",
        "--cgroupns=private",
        "--memory=90177536",
        "--memory-swap=90177536",
        "--pids-limit=64",
        "--cpus=2",
        "--log-driver=none",
        "--workdir=/source",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=PYTHONPATH=/source/src:/source",
        "--env=LANG=C",
        "--env=LC_ALL=C",
        "--env=TERM=dumb",
        "--mount",
        f"type=bind,source={packet.text('checkout_path')},target=/source,readonly",
        "--mount",
        f"type=bind,source={packet.text('source_path')},target=/input/original,readonly",
        "--mount",
        f"type=bind,source={packet.text('packet_path')},target=/case/packet.json,readonly",
        "--mount",
        f"type=volume,source={packet.text('volume')},target=/pending,volume-nocopy",
        "--entrypoint",
        packet.text("python_path"),
        packet.text("image"),
        "-B",
        "-m",
        "evaluations.fma_validation.owner",
        "--execute-reviewed-packet",
    ]


class Controller:
    def __init__(self, packet: Packet, journal: Journal, docker: str = "docker") -> None:
        self.packet, self.journal, self.docker = packet, journal, docker
        self.clock = CaseClock(packet.whole_end, packet.expires)
        self.commands: list[Command] = []
        self.command_receipts: list[dict[str, object]] = []
        self.container: str | None = None
        self.owner_report: dict[str, object] | None = None

    def persist(self, *, cleanup: bool = False) -> None:
        try:
            self.journal.save()
        except Exception:
            if not cleanup:
                raise
            # The earlier durable intent already retains this exact case. A
            # stuck evidence write must not prevent stopping an owned container.
            self.clock.missed = True

    def run(
        self, args: list[str], seconds: float = 5, *, cleanup: bool = False, attach: bool = False
    ) -> bytes:
        if len(self.command_receipts) + len(self.commands) >= 20:
            raise ProbeError("management_operation_limit")
        if len(self.commands) >= 2:
            raise ProbeError("management_retained_capacity")
        if not cleanup and self.clock.remaining() <= 0:
            raise ProbeError("management_admission_expired")
        self.journal.data.update(
            phase="management_dispatch", operation=args[0], cleanup=self.clock.receipt()
        )
        self.persist(cleanup=cleanup)
        command = Command([self.docker, *args])
        self.commands.append(command)
        end = time.monotonic() + seconds
        if not cleanup:
            end = min(end, self.clock.end_monotonic)
        while True:
            command.pump()
            if (
                not cleanup
                and self.clock.cleanup_start_utc is not None
                and self.owner_report is None
            ):
                command.stop()
                raise ProbeError("management_cancelled")
            if attach and command.output.endswith(b"\n") and command.output:
                records = reports(bytes(command.output))
                if len(records) != 1:
                    raise ProbeError("owner_report_count")
                self.owner_report = records[0]
                timing = mapping(self.owner_report.get("cleanup"))
                started = timing.get("cleanup_start_utc")
                if type(started) not in (int, float):
                    self.clock.timing_unknown = True
                    raise ProbeError("owner_cleanup_clock_unknown")
                self.clock.adopt_cleanup(cast(float, started))
                if (
                    timing.get("cleanup_deadline_met") is not True
                    or timing.get("cleanup_timing_unknown") is not False
                ):
                    self.clock.missed = True
                if self.clock.cleanup_monotonic is not None:
                    end = min(end, self.clock.cleanup_monotonic)
            if command.settled():
                receipt = command.receipt()
                self.command_receipts.append(receipt)
                self.commands.remove(command)
                self.journal.data["commands"] = self.command_receipts
                self.persist(cleanup=cleanup)
                if (
                    command.error
                    or command.process is None
                    or command.process.returncode != 0
                    or command.diagnostic
                ):
                    raise ProbeError("management_command_failed")
                return bytes(command.output)
            if time.monotonic() >= end:
                self.clock.adopt_cleanup(
                    min(self.clock.wall(), self.clock.wall() + end - time.monotonic())
                )
                command.stop()
                self.journal.data.update(
                    phase="management_debt", retained_commands=[c.receipt() for c in self.commands]
                )
                self.persist(cleanup=cleanup)
                raise ProbeError("management_command_deadline")
            select.select([], [], [], 0.005)

    def inspect(self, *, cleanup: bool = False) -> dict[str, object]:
        if self.container is None:
            raise ProbeError("container_identity_unknown")
        record = mapping(
            json.loads(
                self.run(
                    ["container", "inspect", "--format", INSPECT, self.container],
                    1 if cleanup else 5,
                    cleanup=cleanup,
                ),
                object_pairs_hook=object_pairs,
            )
        )
        labels = mapping(record.get("labels"))
        if (
            record.get("id") != self.container
            or record.get("name") != "/" + self.packet.text("case_id")
            or record.get("image") != self.packet.text("image")
            or labels.get("org.sobafm.project") != "sobafm"
            or labels.get("org.sobafm.issue") != "44"
            or labels.get("org.sobafm.packet") != self.packet.fingerprint
        ):
            raise ProbeError("container_ownership_changed")
        return record

    def verify_configuration(self, record: dict[str, object]) -> None:
        host = mapping(record.get("host"))
        expected: dict[str, object] = {
            "Init": True,
            "ReadonlyRootfs": True,
            "Privileged": False,
            "NetworkMode": "none",
            "CgroupnsMode": "private",
            "Memory": 90_177_536,
            "MemorySwap": 90_177_536,
            "PidsLimit": 64,
            "NanoCpus": 2_000_000_000,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges=true"],
        }
        if any(host.get(key) != value for key, value in expected.items()) or host.get("CapAdd"):
            raise ProbeError("container_limits_changed")
        if (
            mapping(host.get("RestartPolicy")).get("Name") != "no"
            or mapping(host.get("LogConfig")).get("Type") != "none"
        ):
            raise ProbeError("container_persistence_policy")
        if (
            record.get("user") != "1000:1000"
            or record.get("workdir") != "/source"
            or record.get("path") != self.packet.text("python_path")
            or record.get("args")
            != ["-B", "-m", "evaluations.fma_validation.owner", "--execute-reviewed-packet"]
        ):
            raise ProbeError("container_entrypoint_changed")
        mounts = record.get("mounts")
        if not isinstance(mounts, list) or len(cast(list[object], mounts)) != 4:
            raise ProbeError("container_mounts_changed")
        observed = {
            string(mapping(row).get("Destination")): mapping(row)
            for row in cast(list[object], mounts)
        }
        for destination, key in (
            ("/source", "checkout_path"),
            ("/input/original", "source_path"),
            ("/case/packet.json", "packet_path"),
        ):
            item = observed.get(destination, {})
            if (
                item.get("Type") != "bind"
                or item.get("Source") != self.packet.text(key)
                or item.get("RW") is not False
            ):
                raise ProbeError("readonly_source_mount_changed")
        volume = observed.get("/pending", {})
        if (
            volume.get("Type") != "volume"
            or volume.get("Name") != self.packet.text("volume")
            or volume.get("RW") is not True
        ):
            raise ProbeError("volume_mount_changed")

    def execute(self) -> dict[str, object]:
        failed: str | None = None
        stopped = False
        try:
            info = mapping(
                json.loads(
                    self.run(
                        [
                            "info",
                            "--format",
                            '{"architecture":{{json .Architecture}},"os":{{json .OSType}},'
                            '"security":{{json .SecurityOptions}}}',
                        ]
                    )
                )
            )
            if (
                info.get("architecture") != "x86_64"
                or info.get("os") != "linux"
                or "name=seccomp,profile=builtin"
                not in cast(list[object], info.get("security", []))
            ):
                raise ProbeError("native_default_seccomp_engine_required")
            image = mapping(
                json.loads(
                    self.run(
                        [
                            "image",
                            "inspect",
                            "--format",
                            '{"id":{{json .Id}},"architecture":{{json .Architecture}},'
                            '"os":{{json .Os}}}',
                            self.packet.text("image"),
                        ]
                    )
                )
            )
            if image != {"id": self.packet.text("image"), "architecture": "amd64", "os": "linux"}:
                raise ProbeError("native_image_target_changed")
            volume = mapping(
                json.loads(
                    self.run(
                        ["volume", "inspect", "--format", "{{json .}}", self.packet.text("volume")]
                    )
                )
            )
            if (
                volume.get("Name") != self.packet.text("volume")
                or volume.get("CreatedAt") != self.packet.text("volume_created")
                or hashlib.sha256(string(volume.get("Mountpoint")).encode()).hexdigest()
                != self.packet.text("volume_mountpoint_sha256")
                or mapping(volume.get("Labels")).get("org.sobafm.issue") != "44"
            ):
                raise ProbeError("persistent_volume_binding")
            self.journal.data["phase"] = "creating_container"
            self.journal.save()
            identifier = self.run(creation_args(self.packet)).decode("ascii").strip()
            if re.fullmatch("[0-9a-f]{64}", identifier) is None:
                raise ProbeError("container_identifier_unknown")
            self.container = identifier
            self.journal.data["container_id"] = identifier
            self.journal.save()
            self.verify_configuration(self.inspect())
            self.run(["start", "--attach", identifier], max(0, self.clock.remaining()), attach=True)
            final = self.inspect(cleanup=True)
            state = mapping(final.get("state"))
            stopped = (
                state.get("Running") is False
                and state.get("Pid") == 0
                and state.get("Status") == "exited"
            )
            if (
                not stopped
                or state.get("ExitCode") != 0
                or state.get("OOMKilled") is not False
                or self.owner_report is None
            ):
                raise ProbeError("container_exit_not_qualified")
            validate_owner(self.owner_report, self.packet)
        except Exception:
            failed = "case_failed_or_incomplete"
            self.clock.cancel()
            if self.container is not None:
                try:
                    state = mapping(self.inspect(cleanup=True).get("state"))
                    if state.get("Running") is True:
                        self.run(["stop", "--time=1", self.container], 1.5, cleanup=True)
                    state = mapping(self.inspect(cleanup=True).get("state"))
                    stopped = (
                        state.get("Running") is False
                        and state.get("Pid") == 0
                        and state.get("Status") == "exited"
                    )
                except Exception:
                    stopped = False
        finally:
            for command in self.commands:
                command.stop()
            while self.commands:
                for command in tuple(self.commands):
                    command.pump()
                    if command.settled():
                        self.command_receipts.append(command.receipt())
                        self.commands.remove(command)
                self.clock.observe()
                if self.commands:
                    select.select([], [], [], 0.01)
            self.clock.observe()
        passed = (
            failed is None
            and stopped
            and not self.clock.missed
            and not self.clock.case_missed
            and not self.clock.timing_unknown
        )
        self.journal.data.update(
            phase="settled" if passed else "retained_for_reconciliation",
            passed=passed,
            failure=failed,
            container_exit_observed=stopped,
            cleanup_debt=not passed,
            cleanup=self.clock.receipt(),
            commands=self.command_receipts,
            owner_report=self.owner_report,
            slot_reusable=False,
        )
        self.journal.save()
        return self.journal.data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--execute-reviewed-packet", action="store_true")
    parser.add_argument("--inspect-retained", action="store_true")
    args = parser.parse_args()
    if args.execute_reviewed_packet == args.inspect_retained or os.uname().sysname != "Linux":
        parser.error("choose one explicit Linux execution or read-only retained inspection")
    root = Path(__file__).resolve().parents[2]
    packet = load(args.packet, root, recovery=args.inspect_retained)
    if args.inspect_retained:
        # Deliberately cannot clear an unknown original clock, restart a case,
        # infer ownership by disappearance, or unlink a payload by pathname.
        ledger = mapping(json.loads(read_bounded(args.journal), object_pairs_hook=object_pairs))
        if ledger.get("packet_sha256") != packet.fingerprint:
            raise ProbeError("retained_packet_mismatch")
        print(
            encode_report(
                {
                    "packet_sha256": packet.fingerprint,
                    "ledger": ledger,
                    "reconciliation_required": True,
                    "slot_reusable": False,
                }
            ).decode()
        )
        return 1
    if packet.text("checkout_path") != str(root) or packet.text("packet_path") != str(
        args.packet.resolve()
    ):
        raise ProbeError("host_packet_mount_binding")
    journal = Journal(args.journal, packet)
    controller = Controller(packet, journal)

    def interrupted(_signal: int, _frame: FrameType | None) -> None:
        controller.clock.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    try:
        result = controller.execute()
    finally:
        journal.close()
    print(encode_report(result).decode())
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
