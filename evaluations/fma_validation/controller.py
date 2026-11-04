"""One-shot Linux Docker supervisor with persistent, non-reusable ownership debt."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import re
import select
import signal
import stat
import subprocess
import threading
import time
import traceback
from pathlib import Path
from types import FrameType
from typing import cast

from evaluations.linux_resource.probe import ProbeError

from .consumer import (
    carried_rejection,
    expected_outcome,
    object_pairs,
    validate_decode,
    validate_metadata,
)
from .ingress import Ingress, Opaque, decode, discard_exception
from .limits import CLEANUP_SECONDS, CaseClock, encode_report
from .packet import Packet, load, load_qualification, read_bounded, string
from .process import ProcessPin
from .terminal import validate_clock, validate_terminal

# Re-check granularity only. The original clock, not this, bounds journal waits.
JOURNAL_CLOCK_POLL_SECONDS = 0.05
# Unchanged pre-#76 cleanup bound; preserves the stop budget.
CLEANUP_JOURNAL_WAIT_SECONDS = 0.25


class Command:
    """Retain launch, original pipes and actual child through timeout/close debt."""

    def __init__(
        self,
        argv: list[str],
        *,
        clock: CaseClock | None = None,
        cleanup: bool = False,
        deadline: float | None = None,
    ) -> None:
        self.argv = argv
        self.clock, self.cleanup = clock, cleanup
        self.deadline = deadline
        self.owner_output = False
        self.process: subprocess.Popen[bytes] | None = None
        self.error: str | None = None
        self.ingress = Ingress("owner", deferred=True)
        self.errors = Opaque()
        self.receipt_published = False
        self.retained: set[int] = set()
        self.uncertain: set[int] = set()
        self.descriptors: dict[str, int] = {}
        self.stopping: float | None = None
        self.launch_attempted = False
        self.launch_finished = threading.Event()
        self.worker = threading.Thread(
            target=self._run_launch, name="owned-docker-command", daemon=False
        )

    def _run_launch(self) -> None:
        try:
            self._launch()
        finally:
            self.launch_finished.set()

    def launch(self) -> None:
        """The controller must register this inert owner before attempting start."""
        if self.launch_attempted or self.stopping is not None:
            raise ProbeError("management_launch_not_admitted")
        self.launch_attempted = True
        try:
            self.worker.start()
        except BaseException:
            self.error = "management_start_uncertain"
            self.stop()
            if self.clock is not None:
                self.clock.cancel()
            raise

    @property
    def launch_pending(self) -> bool:
        # An early ident and a false is_alive() do not prove startup completed.
        # Require the target's own completion signal and actual thread exit.
        return self.launch_attempted and (
            not self.launch_finished.is_set() or self.worker.is_alive()
        )

    def launch_admitted(self) -> bool:
        if self.clock is not None:
            if not self.cleanup and self.clock.remaining() <= 0:
                self.stop()
            self.clock.observe()
        now = time.monotonic()
        if self.deadline is not None and now >= self.deadline:
            if self.clock is not None:
                self.clock.adopt_cleanup(
                    min(self.clock.wall(), self.clock.wall() + self.deadline - now)
                )
            self.stop()
        if self.stopping is not None:
            self.error = "management_allocation_revoked"
            return False
        return True

    def _launch(self) -> None:
        try:
            if not self.launch_admitted():
                return
            try:
                self.ingress.allocate()
            finally:
                admitted = self.launch_admitted()
            if not admitted:
                return
            self.process = subprocess.Popen(
                self.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
            for name, stream in (("stdout", self.process.stdout), ("stderr", self.process.stderr)):
                assert stream is not None
                fd = stream.fileno()
                self.descriptors[name] = fd
                self.retained.add(fd)
            for stream in (self.process.stdout, self.process.stderr):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
        except Exception as error:
            discard_exception(error)
            self.error = "management_launch_failed"
            if self.clock is not None:
                self.clock.cancel()

    def stop(self) -> None:
        if self.stopping is None:
            self.stopping = time.monotonic()

    def pump(self) -> None:
        if not self.launch_attempted or self.launch_pending:
            return
        self.worker.join(0)
        # Launch is complete; commands and Popen do not need the copied bind
        # arguments for direct-child signals, polling or descriptor settlement.
        self.argv = []
        process = self.process
        if process is None:
            return
        process.args = []
        if self.stopping is not None and process.poll() is None:
            # Direct unreaped Popen child only. No PID/name reuse targeting.
            if time.monotonic() - self.stopping >= 0.25:
                process.kill()
            else:
                process.terminate()
        for name, stream, target in (
            ("stdout", process.stdout, self.ingress),
            ("stderr", process.stderr, self.errors),
        ):
            assert stream is not None
            fd = self.descriptors.get(name)
            if fd is None or fd not in self.retained:
                continue
            # close() can release its fd, mark FileIO closed, and still raise.
            # Reconcile the original number without consulting closed/fileno().
            # A live/reused descriptor is never closed or credited by inference.
            if fd in self.uncertain:
                try:
                    os.fstat(fd)
                except OSError as error:
                    if error.errno == errno.EBADF:
                        self.uncertain.remove(fd)
                        self.retained.remove(fd)
                continue
            if stream.closed:
                self.uncertain.add(fd)
                self.error = "management_close_debt"
                continue
            if not select.select([fd], [], [], 0)[0]:
                continue
            try:
                data = os.read(fd, target.read_size())
            except BlockingIOError, InterruptedError:
                continue
            try:
                target.feed(data)
                eof = not data
            except BaseException:
                self.error = "management_receive_failed"
                self.stop()
                raise
            finally:
                del data
            if target.error:
                self.error = "management_output_limit"
                self.stop()
            if eof:
                try:
                    stream.close()
                    self.retained.remove(fd)
                except OSError:
                    self.uncertain.add(fd)
                    self.error = "management_close_debt"
        process.poll()

    def settled(self) -> bool:
        return (
            not self.launch_pending
            and not self.retained
            and not self.uncertain
            and (self.process is None or self.process.poll() is not None)
        )

    def receipt(self) -> dict[str, object]:
        self.ingress.check_prefix()
        return {
            "pid": None if self.process is None else self.process.pid,
            "launch_alive": self.launch_pending,
            "retained_fds": sorted(self.retained),
            "uncertain_closes": sorted(self.uncertain),
            "settled": self.settled(),
            "exit_code": None if self.process is None else self.process.poll(),
            "output_bytes": self.ingress.count,
            "diagnostic_bytes": self.errors.count,
            "output_sha256": self.ingress.digest.hexdigest(),
            "diagnostic_sha256": self.errors.digest.hexdigest(),
            "error": self.error,
        }


class Journal:
    """A new private inode per attempt. Existing/partial journals block new work.

    A case-phase write is waited for under the case's original clock; a cleanup
    write gets at most the unchanged short bound inside the cleanup window.
    """

    def __init__(self, path: Path, record: Packet | dict[str, object], clock: CaseClock) -> None:
        self.clock = clock
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
        self.record: bytes | None = None
        self.data: dict[str, object] = (
            dict(record) if isinstance(record, dict) else self.case_ledger(record)
        )
        self.save()
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)

    @staticmethod
    def case_ledger(packet: Packet) -> dict[str, object]:
        return {
            "schema": packet.case.schema("controller-ledger"),
            "case_binding": packet.case.record(),
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

    def save(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            raise ProbeError("journal_operation_retained")
        if self.error is not None:
            raise self.error
        self.record = None  # Previous worker and fsync actually settled.
        data = encode_report(self.data)
        self.record = data

        def write() -> None:
            try:
                self._write(data)
            except Exception as error:
                self.error = error

        # A save begun in the case waits while the original case clock allows it.
        # One begun in cleanup keeps the old short bound so the stop budget stays.
        cleanup = self._cleanup_started()
        self.worker = threading.Thread(target=write, name="owned-journal-write", daemon=False)
        self.worker.start()
        if cleanup:
            self.worker.join(max(0.0, min(CLEANUP_JOURNAL_WAIT_SECONDS, self._cleanup_remaining())))
        while self.worker.is_alive():
            remaining = 0 if cleanup else self.clock.remaining()
            if remaining <= 0:
                raise ProbeError("journal_operation_retained")
            # Re-read the clock: cancellation or an adopted start can tighten it.
            self.worker.join(min(remaining, JOURNAL_CLOCK_POLL_SECONDS))
        if self.error is not None:
            raise self.error
        self.record = None

    def _cleanup_started(self) -> bool:
        self.clock.remaining()  # Case expiry adopts cleanup; nothing renews it.
        return self.clock.cleanup_start_utc is not None

    def _cleanup_remaining(self) -> float:
        clock = self.clock
        with clock.lock:
            start, end = clock.cleanup_start_utc, clock.cleanup_monotonic
            if start is None or end is None:
                return 0
            return min(end - clock.monotonic(), start + CLEANUP_SECONDS - clock.wall())

    def _write(self, data: bytes) -> None:
        info = self.path.lstat()
        if (info.st_dev, info.st_ino) != self.identity or info.st_nlink != 1:
            raise ProbeError("journal_identity_debt")
        offset = 0
        with memoryview(data) as view:
            while offset < len(view):
                with view[offset:] as remaining:
                    try:
                        written = os.pwrite(self.fd, remaining, offset)
                    except InterruptedError:
                        continue
                    if type(written) is not int or not 0 < written <= len(remaining):
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


OWNERSHIP = (
    '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
    '"labels":{"org.sobafm.project":{{json (index .Config.Labels "org.sobafm.project")}},'
    '"org.sobafm.issue":{{json (index .Config.Labels "org.sobafm.issue")}},'
    '"org.sobafm.packet":{{json (index .Config.Labels "org.sobafm.packet")}}},'
    '"state":{"Running":{{json .State.Running}},"Pid":{{json .State.Pid}},'
    '"Status":{{json .State.Status}},"ExitCode":{{json .State.ExitCode}},'
    '"OOMKilled":{{json .State.OOMKilled}}}'
)
CLEANUP_INSPECT = OWNERSHIP + "}"
INSPECT = (
    OWNERSHIP + ',"mounts":[{{range $i,$m := .Mounts}}{{if $i}},{{end}}'
    '{"Type":{{json $m.Type}},"Destination":{{json $m.Destination}},"RW":{{json $m.RW}}'
    '{{if eq $m.Type "volume"}},"Name":{{json $m.Name}}{{else}},"Source":{{json $m.Source}}{{end}}}'
    '{{end}}],"host":{'
    + ",".join(
        '"' + key + '":{{json .HostConfig.' + key + "}}"
        for key in (
            "Init",
            "ReadonlyRootfs",
            "Privileged",
            "NetworkMode",
            "CgroupnsMode",
            "Memory",
            "MemorySwap",
            "PidsLimit",
            "NanoCpus",
            "CapDrop",
            "CapAdd",
            "SecurityOpt",
        )
    )
    + ',"RestartPolicy":{"Name":{{json .HostConfig.RestartPolicy.Name}}},'
    '"LogConfig":{"Type":{{json .HostConfig.LogConfig.Type}}}},'
    '"user":{{json .Config.User}},"path":{{json .Path}},'
    '"args":{{json .Args}},"workdir":{{json .Config.WorkingDir}}}'
)
VOLUME = (
    '{"Name":{{json .Name}},"CreatedAt":{{json .CreatedAt}},'
    '"Mountpoint":{{json .Mountpoint}},"Labels":{'
    '"org.sobafm.issue":{{json (index .Labels "org.sobafm.issue")}}}}'
)


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProbeError("management_object_required")
    return cast(dict[str, object], value)


OWNER_FIELDS = {
    "schema",
    "case_binding",
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
}


def validate_owner(report: dict[str, object], packet: Packet) -> None:
    case = packet.case
    rejection = case.name in ("metadata-rejection", "first-write-cancel")
    if set(report) != OWNER_FIELDS | ({"qualification_passed"} if case.synthetic else set()) or (
        report["schema"] != case.schema("owner")
        or report["case_binding"] != case.record()
        or report["packet_sha256"] != packet.fingerprint
        or report["passed"] is not (not rejection)
        or report["failure"] != ("validation_owner_failed" if rejection else None)
        or (case.synthetic and report.get("qualification_passed") is not True)
        or report["catalog_admitted"] is not False
        or report["container_exit_observed"] is not False
    ):
        raise ProbeError("owner_result_failed")
    intake = mapping(report["intake"])
    reader = mapping(report["reader"])
    validation = mapping(report["validation"])
    for value in (report["cleanup"], validation.get("cleanup")):
        timing = mapping(value)
        started = timing.get("cleanup_start_utc")
        if (
            timing.get("whole_end_utc") != packet.whole_end
            or timing.get("expires_utc") != packet.expires
            or timing.get("cleanup_deadline_met") is not True
            or timing.get("cleanup_timing_unknown") is not False
            or timing.get("case_deadline_met") is not True
            or type(started) not in (int, float)
            or not math.isfinite(cast(float, started))
            or not 0 <= cast(float, started) <= time.time()
        ):
            raise ProbeError("owner_cleanup_evidence_failed")
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
        or intake.get("copied_bytes") != case.bytes
        or intake.get("copied_sha256") != case.sha256
        or any(reader.get(key) is not True for key in ("consumer_released", "settled"))
        or any(
            reader.get(key) is not False
            for key in ("descriptor_retained", "reader_alive", "cancel_alive")
        )
        or reader.get("in_flight") is not None
        or validation.get("active") is not None
        or validation.get("case_binding") != case.record()
    ):
        raise ProbeError("owner_release_evidence_failed")
    if case.synthetic and (
        intake.get("schema") != case.schema("pending-intake")
        or intake.get("case_binding") != case.record()
        or reader.get("case_binding") != case.record()
    ):
        raise ProbeError("owner_qualification_binding")
    if rejection:
        if (
            not expected_outcome(validation, case)
            or reader.get("completed") is not False
            or not isinstance(reader.get("failure"), str)
        ):
            raise ProbeError("qualification_expected_failure_missing")
    elif (
        reader.get("failure") is not None
        or reader.get("stream_verified") is not True
        or reader.get("completed") is not True
        or reader.get("read_bytes") != case.bytes
        or reader.get("delivered_bytes") != case.bytes
        or reader.get("read_sha256") != case.sha256
        or validation.get("finished") is not True
        or validation.get("phase") != "settled"
        or validation.get("delivered_bytes") != case.bytes
    ):
        raise ProbeError("owner_stream_evidence_failed")
    delta = mapping(report["memory_event_delta"])
    if (
        delta.get("oom") != 0
        or delta.get("oom_kill") != 0
        or any(type(value) is not int or value < 0 for value in delta.values())
    ):
        raise ProbeError("owner_memory_evidence_failed")
    # A retained parser-scope rejection is a separate metadata verdict (#62).
    metadata_rejected = not rejection and carried_rejection(validation.get("metadata"), case)
    if not rejection:
        if not expected_outcome(validation, case):
            raise ProbeError("owner_verdict_evidence_failed")
        validate_decode(mapping(validation.get("decode")), case)
        if not metadata_rejected:
            validate_metadata(mapping(validation.get("metadata")), case)
    helpers = validation.get("helpers")
    roles = ("metadata",) if case.name == "metadata-rejection" else ("metadata", "decoder")
    if not isinstance(helpers, list) or len(cast(list[object], helpers)) != len(roles):
        raise ProbeError("owner_helper_release_missing")
    for role, value in zip(roles, cast(list[object], helpers), strict=True):
        helper = mapping(value)
        if (
            helper.get("role") != role
            or helper.get("exit_code")
            != (
                1
                if (rejection and role == roles[-1]) or (metadata_rejected and role == "metadata")
                else 0
            )
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
        if role == "decoder":
            timing = validate_terminal(
                mapping(validation.get("decode")),
                case,
                ProcessPin.from_record(helper.get("process")),
                CaseClock(packet.whole_end, packet.expires),
            )
            if (
                timing["cleanup_deadline_met"] is not True
                or timing["cleanup_timing_unknown"] is not False
                or timing["case_deadline_met"] is not True
            ):
                raise ProbeError("owner_decoder_cleanup_failed")


# Shared by the owner container and the #63 preflight containers. Identity,
# user, mounts and entrypoint are chosen by each caller.
LIMIT_ARGS = (
    "--pull=never",
    "--init",
    "--restart=no",
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
)
EXPECTED_HOST: dict[str, object] = {
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


def creation_args(packet: Packet) -> list[str]:
    return [
        "create",
        *LIMIT_ARGS,
        "--name",
        packet.text("case_id"),
        "--label",
        "org.sobafm.project=sobafm",
        "--label",
        "org.sobafm.issue=44",
        "--label",
        f"org.sobafm.packet={packet.fingerprint}",
        "--user=1000:1000",
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
        packet.owner_module,
        "--execute-reviewed-packet",
    ]


class Management:
    """Finite Docker management dispatch under one original clock and journal.

    At most two live command owners and 20 operations; timeouts retain debt.
    """

    def __init__(self, journal: Journal, docker: str = "docker") -> None:
        self.journal, self.clock, self.docker = journal, journal.clock, docker
        self.commands: list[Command] = []
        self.command_receipts: list[dict[str, object]] = []
        self.owner_report: dict[str, object] | None = None

    def capture_owner(self, command: Command, *, final: bool = False) -> None:
        """Only the case controller attaches to an owner terminal record."""

    def persist(self, *, cleanup: bool = False) -> None:
        try:
            self.journal.save()
        except Exception as error:
            if not cleanup:
                raise
            # The earlier durable intent already retains this exact case. A
            # stuck evidence write must not prevent stopping an owned container.
            self.clock.missed = True
            discard_exception(error)

    def run(
        self,
        args: list[str],
        seconds: float = 5,
        *,
        cleanup: bool = False,
        attach: bool = False,
        family: str | None = None,
    ) -> bytes | dict[str, object]:
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
        end = time.monotonic() + seconds
        if not cleanup:
            end = min(end, self.clock.end_monotonic)
        command = Command([self.docker, *args], clock=self.clock, cleanup=cleanup, deadline=end)
        args = []
        command.owner_output = attach
        self.commands.append(command)
        command.launch()
        while True:
            command.pump()
            self.capture_owner(command, final=command.settled())
            if (
                not cleanup
                and self.clock.cleanup_start_utc is not None
                and self.owner_report is None
            ):
                command.stop()
                raise ProbeError("management_cancelled")
            if attach and self.clock.cleanup_monotonic is not None:
                end = min(end, self.clock.cleanup_monotonic)
            if command.settled():
                if attach and self.owner_report is None:
                    self.clock.timing_unknown = self.clock.missed = True
                self.publish_command(command)
                self.journal.data["commands"] = self.command_receipts
                self.persist(cleanup=cleanup)
                if (
                    command.error
                    or command.process is None
                    or command.process.returncode != 0
                    or command.errors
                ):
                    raise ProbeError("management_command_failed")
                with command.ingress.view() as output:
                    result = (
                        decode(output, family)
                        if family is not None
                        else b""
                        if attach
                        else bytes(output)
                    )
                command.ingress.retire()
                command.errors.retire()
                self.commands.remove(command)
                return result
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

    def publish_command(self, command: Command) -> None:
        if not command.receipt_published:
            self.command_receipts.append(command.receipt())
            command.receipt_published = True

    def drain(self) -> None:
        try:
            for command in self.commands:
                command.stop()
            while self.commands:
                for command in tuple(self.commands):
                    command.pump()
                    self.capture_owner(command, final=command.settled())
                    if command.settled():
                        self.publish_command(command)
                        command.ingress.retire()
                        command.errors.retire()
                        self.commands.remove(command)
                self.clock.observe()
                if self.commands:
                    select.select([], [], [], 0.01)
            self.clock.observe()
        finally:
            # Pending owners are never erased by an exceptional drain.
            self.clock.observe()

    def verify_engine(self) -> None:
        info = mapping(
            self.run(
                [
                    "info",
                    "--format",
                    "{{$seccomp := false}}{{range .SecurityOptions}}"
                    '{{if eq . "name=seccomp,profile=builtin"}}{{$seccomp = true}}{{end}}{{end}}'
                    '{"architecture":{{json .Architecture}},"os":{{json .OSType}},'
                    '"seccomp":{{$seccomp}}}',
                ],
                family="info",
            )
        )
        if (
            info.get("architecture") != "x86_64"
            or info.get("os") != "linux"
            or info.get("seccomp") is not True
        ):
            raise ProbeError("native_default_seccomp_engine_required")

    def verify_image(self, reference: str) -> None:
        image = mapping(
            self.run(
                [
                    "image",
                    "inspect",
                    "--format",
                    '{"id":{{json .Id}},"architecture":{{json .Architecture}},"os":{{json .Os}}}',
                    reference,
                ],
                family="image",
            )
        )
        if image != {"id": reference, "architecture": "amd64", "os": "linux"}:
            raise ProbeError("native_image_target_changed")


class Controller(Management):
    def __init__(self, packet: Packet, journal: Journal, docker: str = "docker") -> None:
        super().__init__(journal, docker)
        self.packet = packet
        # Computed once: the fingerprint re-encodes the packet on every access.
        self.packet_sha256 = packet.fingerprint
        self.container: str | None = None

    def capture_owner(self, command: Command, *, final: bool = False) -> None:
        """Adopt failure timing before command acceptance or final retirement."""
        if not command.owner_output or command.ingress.retired:
            return
        command.ingress.scan(final=final)
        records = command.ingress.records
        if not records and not final and command.ingress.error is None:
            return
        try:
            if len(records) != 1:
                raise ProbeError("owner_report_count")
            report = records[0].envelope
            if report is None:
                raise ProbeError("owner_terminal_missing_envelope")
            if (
                report.get("packet_sha256") != self.packet_sha256
                or report.get("schema") != self.packet.case.schema("owner")
                or report.get("case_binding") != self.packet.case.record()
                or ProcessPin.from_record(report.get("init")).pid != 1
                or ProcessPin.from_record(report.get("owner")).pid <= 1
            ):
                raise ProbeError("owner_terminal_binding")
            timing = validate_clock(report.get("cleanup"), self.clock)
            started = timing["cleanup_start_utc"]
            if started is not None:
                if (
                    type(started) not in (int, float)
                    or not math.isfinite(cast(float, started))
                    or not 0 <= cast(float, started) <= self.clock.wall()
                ):
                    raise ProbeError("owner_terminal_clock")
                self.clock.adopt_cleanup(cast(float, started))
            elif timing["cleanup_timing_unknown"] is not True:
                raise ProbeError("owner_terminal_clock_missing")
            self.clock.timing_unknown |= timing["cleanup_timing_unknown"] is True
            self.clock.missed |= timing["cleanup_deadline_met"] is not True
            self.clock.case_missed |= timing["case_deadline_met"] is not True
            if records[0].value is not None:
                if self.owner_report is not None and self.owner_report is not records[0].value:
                    raise ProbeError("owner_terminal_changed")
                self.owner_report = records[0].value
            if command.ingress.error or records[0].value is None:
                raise ProbeError("owner_terminal_invalid_detail_or_suffix")
        except (ValueError, KeyError, TypeError, RecursionError, ProbeError) as error:
            discard_exception(error)
            self.clock.timing_unknown = self.clock.missed = True
            self.journal.data["owner_terminal_error"] = "invalid_or_missing_owner_terminal"

    def inspect(self, *, cleanup: bool = False) -> dict[str, object]:
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
            or record.get("name") != "/" + self.packet.text("case_id")
            or record.get("image") != self.packet.text("image")
            or labels.get("org.sobafm.project") != "sobafm"
            or labels.get("org.sobafm.issue") != "44"
            or labels.get("org.sobafm.packet") != self.packet_sha256
        ):
            raise ProbeError("container_ownership_changed")
        return record

    def verify_configuration(self, record: dict[str, object]) -> None:
        host = mapping(record.get("host"))
        if any(host.get(key) != value for key, value in EXPECTED_HOST.items()) or host.get(
            "CapAdd"
        ):
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
            != ["-B", "-m", self.packet.owner_module, "--execute-reviewed-packet"]
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

    def prepare(self) -> None:
        """Setup responses have no lifetime across attach or cleanup."""
        self.verify_engine()
        self.verify_image(self.packet.text("image"))
        self.verify_volume()
        self.journal.data["phase"] = "creating_container"
        self.journal.save()
        identifier = cast(bytes, self.run(creation_args(self.packet))).decode("ascii").strip()
        if re.fullmatch("[0-9a-f]{64}", identifier) is None:
            raise ProbeError("container_identifier_unknown")
        self.container = identifier
        self.journal.data["container_id"] = identifier
        self.journal.save()
        self.verify_configuration(self.inspect())

    def verify_volume(self) -> None:
        volume = mapping(
            self.run(
                ["volume", "inspect", "--format", VOLUME, self.packet.text("volume")],
                family="volume",
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

    def evaluate(self) -> tuple[str | None, bool]:
        failed: str | None = None
        stopped = False
        try:
            self.prepare()
            stopped = self.complete()
        except Exception as error:
            failed = "case_failed_or_incomplete"
            self.clock.cancel()
            # Clear completed setup/decoder frames before entering a new cleanup
            # operation. Journal.record and pending command/FD owners stay held.
            discard_exception(error)
        if failed is not None and self.container is not None:
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
                # Preserve the original exceptional verdict, including SystemExit
                # arguments, but end completed setup frames before actual drain.
                interrupted = error
                if error.__traceback__ is not None:
                    traceback.clear_frames(error.__traceback__)
                error.__traceback__ = None
        finally:
            # KeyboardInterrupt/SystemExit still propagate, but cannot abandon
            # a launched command, held pipe or incomplete launch operation. The
            # catch scope has ended: a malformed drained record cannot acquire
            # and sanitize the propagating verdict as its exception context.
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
            passed=accepted
            and self.owner_report is not None
            and self.owner_report.get("passed") is True,
            failure=failed,
            container_exit_observed=stopped,
            cleanup_debt=not accepted,
            cleanup=self.clock.receipt(),
            commands=self.command_receipts,
            owner_report=self.owner_report,
            slot_reusable=False,
        )
        if self.packet.case.synthetic:
            self.journal.data["qualification_passed"] = accepted
        self.journal.save()
        return self.journal.data

    def complete(self) -> bool:
        assert self.container is not None
        self.run(["start", "--attach", self.container], max(0, self.clock.remaining()), attach=True)
        state = mapping(self.inspect(cleanup=True).get("state"))
        if (
            state.get("Running") is not False
            or state.get("Pid") != 0
            or state.get("Status") != "exited"
            or state.get("ExitCode") != 0
            or state.get("OOMKilled") is not False
            or self.owner_report is None
        ):
            raise ProbeError("container_exit_not_qualified")
        validate_owner(self.owner_report, self.packet)
        return True


def main(*, qualification: bool = False) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--execute-reviewed-packet", action="store_true")
    parser.add_argument("--inspect-retained", action="store_true")
    args = parser.parse_args()
    if args.execute_reviewed_packet == args.inspect_retained or os.uname().sysname != "Linux":
        parser.error("choose one explicit Linux execution or read-only retained inspection")
    root = Path(__file__).resolve().parents[2]
    packet = (load_qualification if qualification else load)(
        args.packet, root, recovery=args.inspect_retained
    )
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
    journal = Journal(args.journal, packet, CaseClock(packet.whole_end, packet.expires))
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
    return 0 if result["qualification_passed" if qualification else "passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
