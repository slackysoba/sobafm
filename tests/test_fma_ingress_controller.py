"""Selected management data and held ownership; synthetic Python children only."""

from __future__ import annotations

import gc
import hashlib
import json
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import cast

import pytest

from evaluations.fma_validation import controller as module
from evaluations.fma_validation.controller import Command, Controller, Journal
from evaluations.fma_validation.ingress import decode
from evaluations.fma_validation.limits import CaseClock
from evaluations.fma_validation.owner import INTERPRETER_FACTS
from evaluations.fma_validation.packet import Packet, load, load_qualification
from evaluations.linux_resource.probe import ProbeError
from tests.test_fma_validation_packet import NOW, ROOT, qualification_packet
from tests.test_fma_validation_packet import packet_path as packet_path


def controller(packet: Packet, path: Path) -> Controller:
    result = Controller(packet, Journal(path, packet), sys.executable)
    result.clock = CaseClock(packet.whole_end, packet.expires, lambda: NOW + 1, time.monotonic)
    return result


def config(packet: Packet) -> dict[str, object]:
    return {
        "id": "a" * 64,
        "name": "/" + packet.text("case_id"),
        "image": packet.text("image"),
        "labels": {
            "org.sobafm.project": "sobafm",
            "org.sobafm.issue": "44",
            "org.sobafm.packet": packet.fingerprint,
        },
        "state": {
            "Running": False,
            "Pid": 0,
            "Status": "exited",
            "ExitCode": 0,
            "OOMKilled": False,
        },
        "user": "1000:1000",
        "workdir": "/source",
        "path": packet.text("python_path"),
        "args": ["-B", "-m", packet.owner_module, "--execute-reviewed-packet"],
        "host": {
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
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges=true"],
            "RestartPolicy": {"Name": "no"},
            "LogConfig": {"Type": "none"},
        },
        "mounts": [
            {"Type": "bind", "Source": packet.text(key), "Destination": destination, "RW": False}
            for key, destination in (
                ("checkout_path", "/source"),
                ("source_path", "/input/original"),
                ("packet_path", "/case/packet.json"),
            )
        ]
        + [
            {"Type": "volume", "Name": packet.text("volume"), "Destination": "/pending", "RW": True}
        ],
    }


@pytest.mark.parametrize(
    "change",
    [
        None,
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
        "RestartPolicy",
        "LogConfig",
        "user",
        "workdir",
        "path",
        "args",
        "source",
        "destination",
        "rw",
        "volume",
        "mount_count",
    ],
)
def test_selected_initial_configuration_keeps_every_existing_gate(
    packet_path: Path, tmp_path: Path, change: str | None
) -> None:
    packet = load(packet_path, ROOT, now=NOW)
    owner = controller(packet, tmp_path / "ledger")
    record = config(packet)
    if change in cast(dict[str, object], record["host"]):
        cast(dict[str, object], record["host"])[change] = "wrong"
    elif change in ("user", "workdir", "path", "args"):
        record[change] = "wrong"
    elif change is not None:
        mounts = cast(list[dict[str, object]], record["mounts"])
        if change == "source":
            mounts[0]["Source"] = "/different"
        if change == "destination":
            mounts[0]["Destination"] = "/other"
        if change == "rw":
            mounts[0]["RW"] = True
        if change == "volume":
            mounts[-1]["Name"] = "different"
        if change == "mount_count":
            mounts.pop()
    try:
        if change is None:
            parsed = decode(json.dumps(record, ensure_ascii=False).encode(), "config")
            owner.verify_configuration(parsed)
        else:
            with pytest.raises(ProbeError):
                owner.verify_configuration(record)
    finally:
        owner.journal.close()


def test_projection_templates_omit_unrelated_docker_graphs() -> None:
    assert "{{json .HostConfig}}" not in module.INSPECT
    assert "{{json .State}}" not in module.INSPECT
    assert "{{json .Config.Labels}}" not in module.INSPECT
    assert "{{json .Mounts}}" not in module.INSPECT
    assert all(
        key not in module.CLEANUP_INSPECT for key in ("HostConfig", "Mounts", "WorkingDir", ".Args")
    )
    assert "{{json .}}" not in module.VOLUME and "Options" not in module.VOLUME
    assert "org.sobafm.issue" in module.VOLUME


@pytest.mark.parametrize(
    "case_name", ["night-owl", "tagged-full", "metadata-rejection", "first-write-cancel"]
)
@pytest.mark.parametrize("phase", ["inert", "metadata", "decoder", "released", "rejected"])
def test_actual_owner_null_partial_and_settled_producer_states(
    packet_path: Path, tmp_path: Path, case_name: str, phase: str
) -> None:
    from datetime import UTC, datetime

    from evaluations.fma_validation.consumer import ValidationConsumer
    from evaluations.fma_validation.limits import encode_report
    from evaluations.fma_validation.process import OwnedHelper, ProcessPin
    from sobafm.fma_intake import LocalIntake

    if case_name != "night-owl":
        qualification_packet(packet_path, case_name)
    packet = (load if case_name == "night-owl" else load_qualification)(packet_path, ROOT, now=NOW)
    clock = CaseClock(packet.whole_end, packet.expires, lambda: NOW + 1, lambda: 10.0)
    init, pin = (
        ProcessPin(1, 1, 0, "/init", 1, 1, 1000, "/"),
        ProcessPin(2, 1, 1, "/python", 1, 2, 1000, "/"),
    )
    intake = LocalIntake(
        tmp_path,
        None,
        packet.request,
        utc_now=lambda: datetime.fromtimestamp(NOW, UTC),
        monotonic=lambda: 10.0,
    )
    settled = phase in ("released", "rejected")
    if settled:
        # A real completed thread lets report() observe release itself; do not
        # assign released_at or substitute a hand-written intake report.
        intake.phase = phase
        intake.thread = threading.Thread()
        intake.thread.start()
        intake.thread.join(3)
        assert not intake.thread.is_alive()
        clock.cancel()
    consumer = ValidationConsumer(intake, init, pin, clock, packet.case)
    if phase in ("metadata", "decoder"):
        consumer.phase = phase
        consumer.active = OwnedHelper(clock, phase)
        if phase == "decoder":
            consumer.metadata = {
                "schema": packet.case.schema("metadata-rejection"),
                "reason": "unsupported_id3_text_encoding",
                "credits_complete": False,
            }
    record: dict[str, object] = {
        "schema": packet.case.schema("owner"),
        "case_binding": packet.case.record(),
        "packet_sha256": packet.fingerprint,
        "passed": False,
        "failure": "validation_owner_failed",
        "init": init.record(),
        "owner": pin.record(),
        "interpreter": dict(INTERPRETER_FACTS),
        "intake": intake.report(),
        "reader": None,
        "validation": consumer.receipt(),
        "memory_event_delta": {},
        "cleanup": clock.receipt(),
        "catalog_admitted": False,
        "container_exit_observed": False,
    }
    if packet.case.synthetic:
        record["qualification_passed"] = False
    wire = encode_report(record, newline=True)
    assert decode(wire, "owner") == record
    if settled:
        assert intake.released_at == 10.0

        class NoLaunch(Command):
            def _launch(self) -> None:
                pass

        command = NoLaunch(["synthetic-owner-output"])
        command.launch()
        command.worker.join(3)
        assert not command.worker.is_alive()
        command.owner_output = True
        command.ingress.feed(wire)

        class CaptureJournal(Journal):
            def __init__(self) -> None:
                # This boundary only records capture errors. Real journal write,
                # fsync and retained-worker schedules have dedicated tests.
                self.data = {}

        outer = Controller(packet, CaptureJournal())
        outer.clock = CaseClock(packet.whole_end, packet.expires, lambda: NOW + 1, lambda: 10.0)
        outer.capture_owner(command, final=True)
        assert outer.owner_report == record
        assert not outer.clock.timing_unknown and not outer.clock.missed
        assert outer.clock.cleanup_start_utc == NOW + 1


@pytest.mark.parametrize("created", ["x" * 4096, "\u6f22" * 100, "\U00010000" * 4096])
def test_volume_created_preserves_packet_bound_unicode_domain(
    packet_path: Path, tmp_path: Path, created: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = load(packet_path, ROOT, now=NOW)
    mountpoint = "/tmp/\U00010000"
    packet.raw["volume_created"] = created
    packet.raw["volume_mountpoint_sha256"] = hashlib.sha256(mountpoint.encode()).hexdigest()
    owner = controller(packet, tmp_path / "ledger")
    record = {
        "Name": packet.text("volume"),
        "CreatedAt": created,
        "Mountpoint": mountpoint,
        "Labels": {"org.sobafm.issue": "44"},
    }

    def run(
        _self: Controller,
        args: list[str],
        seconds: float = 5,
        *,
        cleanup: bool = False,
        attach: bool = False,
        family: str | None = None,
    ) -> bytes | dict[str, object]:
        assert family == "volume" and args[3] == module.VOLUME
        return decode(json.dumps(record, ensure_ascii=False).encode(), "volume")

    monkeypatch.setattr(Controller, "run", run)
    try:
        owner.verify_volume()
    finally:
        owner.journal.close()


def test_management_receipt_hashes_original_utf8_and_spacing(
    packet_path: Path, tmp_path: Path
) -> None:
    packet = load(packet_path, ROOT, now=NOW)
    owner = controller(packet, tmp_path / "ledger")
    raw = b'  { "architecture" : "x86_64", "seccomp": true, "os": "linux" }\n'
    try:
        result = owner.run(
            ["-I", "-B", "-c", "import os; os.write(1," + repr(raw) + ")"], family="info"
        )
        assert result == {"architecture": "x86_64", "seccomp": True, "os": "linux"}
        assert not owner.commands
        assert owner.command_receipts[-1]["output_bytes"] == len(raw)
        assert owner.command_receipts[-1]["output_sha256"] == hashlib.sha256(raw).hexdigest()
    finally:
        owner.journal.close()


def test_retained_attach_and_cleanup_command_share_two_slots(
    packet_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = load(packet_path, ROOT, now=NOW)
    owner = controller(packet, tmp_path / "ledger")
    entered, release = threading.Event(), threading.Event()

    class HeldLaunch(Command):
        def _launch(self) -> None:
            entered.set()
            assert release.wait(5)

    held = HeldLaunch(["synthetic-held-attach"])
    held.owner_output = True
    owner.commands.append(held)
    held.launch()
    assert entered.wait(2)
    actual = module.Command
    observed: list[int] = []

    def create(
        argv: list[str],
        *,
        clock: CaseClock | None = None,
        cleanup: bool = False,
        deadline: float | None = None,
    ) -> Command:
        observed.append(len(owner.commands))
        return actual(argv, clock=clock, cleanup=cleanup, deadline=deadline)

    monkeypatch.setattr(module, "Command", create)
    try:
        assert owner.run(["-I", "-B", "-c", "pass"], cleanup=True) == b""
        assert observed == [1] and owner.commands == [held] and held.worker.is_alive()
        with pytest.raises(ProbeError, match="management_retained_capacity"):
            owner.commands.append(held)
            owner.run(["unreachable"], cleanup=True)
        owner.commands.pop()
    finally:
        release.set()
        held.worker.join(3)
        assert not held.worker.is_alive()
        owner.commands.clear()
        owner.journal.close()


def test_completed_setup_exception_released_before_cleanup_with_journal_debt(
    packet_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = load(packet_path, ROOT, now=NOW)
    owner = controller(packet, tmp_path / "ledger")
    owner.container = "a" * 64
    release = threading.Event()
    journal_record = b"retained original journal record"
    owner.journal.record = journal_record

    class Payload:
        pass

    refs: list[weakref.ReferenceType[Payload]] = []
    cleanup_calls: list[float | None] = []

    def fail(_self: Controller) -> None:
        payload = Payload()
        refs.append(weakref.ref(payload))
        error = ValueError(payload)
        owner.journal.error = error
        try:
            owner.persist()  # The same stored error crosses Journal.save/persist.
        finally:
            owner.journal.worker = threading.Thread(target=lambda: release.wait(5))
            owner.journal.worker.start()

    def inspect(_self: Controller, *, cleanup: bool = False) -> dict[str, object]:
        assert cleanup and sys.exception() is None
        assert refs and refs[0]() is None
        assert owner.journal.record is journal_record and owner.journal.worker is not None
        assert owner.journal.worker.is_alive() and owner.journal.error is not None
        assert owner.journal.error.args == ()
        assert owner.journal.error.__traceback__ is None
        cleanup_calls.append(owner.clock.cleanup_start_utc)
        return {"state": {"Running": False, "Pid": 0, "Status": "exited"}}

    monkeypatch.setattr(Controller, "prepare", fail)
    monkeypatch.setattr(Controller, "inspect", inspect)
    enabled = gc.isenabled()
    gc.disable()
    try:
        with pytest.raises(ProbeError, match="journal_operation_retained"):
            owner.execute()
        assert len(cleanup_calls) == 2 and cleanup_calls == [NOW + 1, NOW + 1]
        assert owner.journal.record is journal_record
    finally:
        release.set()
        assert owner.journal.worker is not None
        owner.journal.worker.join(3)
        owner.journal.close()
        if enabled:
            gc.enable()


@pytest.mark.parametrize("stage", ["prepare", "complete"])
@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit])
def test_base_exception_drains_retained_command_before_propagation(
    packet_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    failure_type: type[BaseException],
) -> None:
    failure = failure_type("synthetic")
    packet = load(packet_path, ROOT, now=NOW)
    owner = controller(packet, tmp_path / "ledger")
    owner.clock.adopt_cleanup(NOW)
    original_clock = owner.clock.cleanup_monotonic
    entered, stopped, release, finished = (threading.Event() for _ in range(4))

    class HeldCommand(Command):
        def _launch(self) -> None:
            entered.set()
            assert release.wait(5)

        def stop(self) -> None:
            stopped.set()
            super().stop()

    command = HeldCommand(["synthetic"])
    command.launch()
    command.owner_output = True
    command.ingress.feed(b"{}\n")  # Rejection during drain cannot sanitize the pending verdict.
    owner.commands.append(command)
    errors: list[BaseException] = []

    def prepare(_self: Controller) -> None:
        if stage == "prepare":
            raise failure

    def complete(_self: Controller) -> bool:
        raise failure

    def execute() -> None:
        try:
            owner.execute()
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(Controller, "prepare", prepare)
    monkeypatch.setattr(Controller, "complete", complete)
    worker = threading.Thread(target=execute)
    assert entered.wait(2)
    worker.start()
    try:
        assert stopped.wait(1), "BaseException bypassed stop/drain"
        assert not finished.is_set() and owner.commands == [command]
        assert command.worker.is_alive() and owner.clock.cleanup_monotonic == original_clock
    finally:
        release.set()
        command.worker.join(3)
        worker.join(3)
        owner.journal.close()
    assert finished.is_set() and errors == [failure]
    assert failure.args == ("synthetic",)
    assert not owner.commands and command.settled()


def test_cleanup_persist_drops_repeated_stored_error_tracebacks(
    packet_path: Path, tmp_path: Path
) -> None:
    packet = load(packet_path, ROOT, now=NOW)
    owner = controller(packet, tmp_path / "ledger")
    owner.journal.record = b"owned failed record"
    error = ValueError("synthetic journal failure")
    owner.journal.error = error
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(4):
            owner.persist(cleanup=True)
            assert owner.journal.error is error and error.__traceback__ is None
            assert owner.journal.record == b"owned failed record" and owner.clock.missed
    finally:
        owner.journal.close()
        if enabled:
            gc.enable()


def test_successful_setup_graphs_and_create_arguments_end_before_attach(
    packet_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = load(packet_path, ROOT, now=NOW)
    mountpoint = "/synthetic/volume"
    packet.raw["volume_mountpoint_sha256"] = hashlib.sha256(mountpoint.encode()).hexdigest()
    owner = controller(packet, tmp_path / "ledger")

    class Tracked(dict[str, object]):
        pass

    class Arguments(list[str]):
        pass

    refs: list[weakref.ReferenceType[Tracked]] = []
    argument_refs: list[weakref.ReferenceType[Arguments]] = []
    actual_creation = module.creation_args

    def creation(selected: Packet) -> list[str]:
        args = Arguments(actual_creation(selected))
        argument_refs.append(weakref.ref(args))
        return args

    def run(
        _self: Controller,
        args: list[str],
        seconds: float = 5,
        *,
        cleanup: bool = False,
        attach: bool = False,
        family: str | None = None,
    ) -> bytes | dict[str, object]:
        if args[0] == "create":
            return b"a" * 64 + b"\n"
        value: dict[str, object]
        if family == "info":
            value = {"architecture": "x86_64", "os": "linux", "seccomp": True}
        elif family == "image":
            value = {"id": packet.text("image"), "architecture": "amd64", "os": "linux"}
        elif family == "volume":
            value = {
                "Name": packet.text("volume"),
                "CreatedAt": packet.text("volume_created"),
                "Mountpoint": mountpoint,
                "Labels": {"org.sobafm.issue": "44"},
            }
        else:
            assert family == "config"
            value = config(packet)
        record = Tracked(decode(json.dumps(value).encode(), cast(str, family)))
        refs.append(weakref.ref(record))
        return record

    observed: list[bool] = []

    def complete(_self: Controller) -> bool:
        assert len(refs) == 4 and all(ref() is None for ref in refs)
        assert len(argument_refs) == 1 and argument_refs[0]() is None
        observed.append(True)
        return False  # This test supplies no execution or exit qualification.

    monkeypatch.setattr(module, "creation_args", creation)
    monkeypatch.setattr(Controller, "run", run)
    monkeypatch.setattr(Controller, "complete", complete)
    enabled = gc.isenabled()
    gc.disable()
    try:
        owner.execute()
        assert observed == [True]
    finally:
        owner.journal.close()
        if enabled:
            gc.enable()
