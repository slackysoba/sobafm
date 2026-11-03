"""Fixed receiver backing and opaque facts; owned synthetic Python processes only."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, NoReturn, cast

import pytest

from evaluations.fma_validation import ingress as module
from evaluations.fma_validation import process as process_module
from evaluations.fma_validation.consumer import ValidationConsumer
from evaluations.fma_validation.controller import Command, Controller
from evaluations.fma_validation.ingress import Ingress, Opaque, decode
from evaluations.fma_validation.limits import REPORT_BYTES, CaseClock, encode_report
from evaluations.fma_validation.packet import load
from evaluations.fma_validation.process import OwnedHelper
from evaluations.linux_resource.probe import ProbeError
from tests.test_fma_failure_receipts import consumer as consumer
from tests.test_fma_failure_receipts import terminal
from tests.test_fma_ingress import metadata_wire
from tests.test_fma_ingress_controller import config, controller
from tests.test_fma_validation_packet import NOW, ROOT
from tests.test_fma_validation_packet import packet_path as packet_path


def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("unadmitted conversion or process launch")


def test_fixed_capacity_never_becomes_received_payload() -> None:
    owner = Ingress("metadata")
    backing = owner.raw
    assert len(backing) == REPORT_BYTES and not owner and owner.used == 0
    backing[-8:] = b"junk\n{}\n"
    owner.scan()
    assert owner.count == 0 and not owner.records and owner.error is None
    assert owner.digest.hexdigest() == hashlib.sha256(b"").hexdigest()
    raw = metadata_wire()
    for chunk in (raw[:5], raw[5:]):
        owner.feed(chunk)
        assert owner.raw is backing and len(backing) == REPORT_BYTES
        owner.scan()
    with owner.view() as view:
        assert view.obj is backing and len(view) == len(raw)
    assert owner.used == owner.count == len(raw) and owner
    assert len(owner.records) == 1 and owner.error is None
    assert owner.digest.hexdigest() == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("chunk", [1, 7, 4096, REPORT_BYTES - 3, REPORT_BYTES + 1])
@pytest.mark.parametrize("extra", [0, 1, 4097])
def test_opaque_prefix_parity_across_overflow_chunks(chunk: int, extra: int) -> None:
    data = (bytes(range(256)) * ((REPORT_BYTES + extra + 255) // 256))[: REPORT_BYTES + extra]
    owner = Opaque()
    assert not owner
    for start in range(0, len(data), chunk):
        assert 1 <= owner.read_size() <= 4096
        owner.feed(data[start : start + chunk])
    owner.feed(b"")
    assert owner.count == REPORT_BYTES and owner and owner.eof
    assert owner.digest.hexdigest() == hashlib.sha256(data[:REPORT_BYTES]).hexdigest()
    assert (owner.error is not None) is bool(extra)
    assert not any(
        isinstance(value, bytes | bytearray | memoryview) for value in vars(owner).values()
    )
    owner.retire()
    assert (
        owner.count == REPORT_BYTES
        and owner.digest.hexdigest() == hashlib.sha256(data[:REPORT_BYTES]).hexdigest()
    )


def test_borrowed_fixed_backing_survives_failed_retirement() -> None:
    owner = Ingress("metadata")
    owner.feed(metadata_wire())
    owner.scan()
    backing, record, used = owner.raw, owner.records[0], owner.used
    parent = owner.view()
    child = parent[1:]
    parent.release()
    try:
        for _ in range(2):
            with pytest.raises(BufferError):
                owner.retire()
            assert owner.raw is backing and len(backing) == REPORT_BYTES
            assert owner.used == used and owner.records[0] is record and not owner.retired
    finally:
        child.release()
    owner.retire()
    assert owner.retired and not owner.raw and not owner and not owner.records
    assert owner.count == used


def test_helper_borrow_blocks_retirement_without_duplicate_receipt(
    consumer: ValidationConsumer,
) -> None:
    helper = OwnedHelper(consumer.clock, "decoder")
    helper.pin = consumer.owner_pin
    helper.ingress.feed(encode_report(terminal(helper.pin), newline=True))
    consumer.active = helper
    with helper.ingress.view():
        for _ in range(2):
            with pytest.raises(BufferError):
                consumer.join(0)
            assert consumer.active is helper and len(consumer.history) == 1
            assert len(helper.ingress.raw) == REPORT_BYTES
    assert consumer.join(0) and consumer.active is None and len(consumer.history) == 1


def test_management_borrow_retains_command_and_receipt_once(
    packet_path: Path, tmp_path: Path
) -> None:
    packet = load(packet_path, ROOT, now=NOW)
    outer = controller(packet, tmp_path / "ledger")

    class NoLaunch(Command):
        def _launch(self) -> None:
            self.ingress.allocate()

    command = NoLaunch(["synthetic"])
    command.launch()
    command.worker.join(3)
    assert command.settled()
    outer.commands.append(command)
    try:
        with command.ingress.view():
            for _ in range(2):
                with pytest.raises(BufferError):
                    outer.drain()
                assert outer.commands == [command] and len(outer.command_receipts) == 1
                assert len(command.ingress.raw) == REPORT_BYTES
        outer.drain()
        assert not outer.commands and len(outer.command_receipts) == 1
    finally:
        outer.journal.close()


@pytest.mark.parametrize("name", ["\U00010000", "/" + "x" * 43, "x" * 4096])
def test_cleanup_name_rejects_before_materialization(
    packet_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = config(load(packet_path, ROOT, now=NOW))
    record = {key: value for key, value in record.items() if key in module.DOCKER_CLEANUP.fields}
    record["name"] = name
    raw = json.dumps(record, ensure_ascii=False).encode()
    monkeypatch.setattr(module.json, "loads", forbidden)
    with pytest.raises(ProbeError, match="ingress_schema"):
        decode(raw, "cleanup")


def test_maximum_escaped_name_and_unicode_paths_keep_exact_binding(
    packet_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = load(packet_path, ROOT, now=NOW)
    packet.raw["case_id"] = "sobafm-44-" + "a" * 32
    packet.raw["checkout_path"] = "/\U00010000" + "\u6f22" * 500
    record = config(packet)
    raw = (
        json.dumps(record, ensure_ascii=False, indent=1)
        .replace("/sobafm", "\\u002fsobafm")
        .encode()
    )

    def numeric_token(data: memoryview) -> bytes:
        assert len(data) <= 40  # Existing scalar conversion, never the whole document.
        return bytes(data)

    monkeypatch.setattr(module, "bytes", numeric_token, raising=False)
    parsed = decode(memoryview(raw), "config")
    assert parsed == record and len(str(record["name"])) == 43
    outer = controller(packet, tmp_path / "ledger")
    outer.container = "a" * 64

    def selected(
        _self: Controller,
        _args: list[str],
        _seconds: float = 5,
        *,
        cleanup: bool = False,
        attach: bool = False,
        family: str | None = None,
    ) -> dict[str, object]:
        return parsed

    monkeypatch.setattr(Controller, "run", selected)
    try:
        outer.verify_configuration(outer.inspect())
        parsed["name"] = "/sobafm-44-" + "b" * 32
        with pytest.raises(ProbeError):
            outer.inspect()
    finally:
        outer.journal.close()


def test_materialize_and_rejected_detail_envelope_borrow_original_views(
    consumer: ValidationConsumer, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = OwnedHelper(consumer.clock, "decoder")
    helper.pin = consumer.owner_pin
    record = terminal(helper.pin)
    record["diagnostics"] = {"unsupported": "detail"}
    helper.ingress.feed(encode_report(record, newline=True))
    actual = str
    originals: list[object] = []

    def text(data: memoryview, encoding: str, errors: str) -> str:
        assert isinstance(data, memoryview) and errors == "strict"
        originals.append(data.obj)
        return actual(data, encoding, errors)

    monkeypatch.setattr(module, "str", text, raising=False)
    consumer.active = helper
    consumer.cancel()
    assert originals and all(backing is helper.ingress.raw for backing in originals)
    assert helper.ingress.records[0].value is None and helper.ingress.records[0].envelope
    assert consumer.clock.cleanup_start_utc == 100 and consumer.clock.timing_unknown


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("earlier", [False, True])
def test_helper_late_allocation_retains_registered_owner_and_clock(
    consumer: ValidationConsumer, monkeypatch: pytest.MonkeyPatch, failed: bool, earlier: bool
) -> None:
    now = [105.0]
    consumer.clock.wall = lambda: now[0]
    consumer.clock.monotonic = lambda: now[0] - 95
    consumer.owner.utc_now = lambda: datetime.fromtimestamp(now[0], UTC)
    consumer.owner.monotonic = consumer.clock.monotonic

    def allocate(size: int = 0) -> bytearray:
        if not size:
            return bytearray()
        assert size == REPORT_BYTES and consumer.active is not None
        if earlier:
            consumer.clock.adopt_cleanup(100)
        now[0] = 205
        if failed:
            raise MemoryError("synthetic fixed receive allocation")
        return bytearray(size)

    monkeypatch.setattr(module, "bytearray", allocate, raising=False)
    monkeypatch.setattr(process_module.subprocess, "Popen", forbidden)
    with pytest.raises(MemoryError if failed else ProbeError):
        consumer._start("metadata", [])  # pyright: ignore[reportPrivateUsage]
    helper = consumer.active
    assert helper is not None and helper.process is None and not helper.launching
    assert len(helper.ingress.raw) == (0 if failed else REPORT_BYTES)
    assert consumer.clock.cleanup_start_utc == (100 if earlier else 200)
    assert consumer.clock.missed and consumer.clock.case_missed
    assert consumer.join(0) and consumer.active is None


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("earlier", [False, True])
@pytest.mark.parametrize("bound", ["case", "command"])
def test_management_held_allocation_keeps_partial_backing_and_original_clock(
    monkeypatch: pytest.MonkeyPatch, failed: bool, earlier: bool, bound: str
) -> None:
    now = [100.0]
    clock = CaseClock(115 if bound == "case" else 200, 300, lambda: now[0], lambda: now[0])
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    actual = Ingress.allocate

    def allocate(owner: Ingress) -> None:
        actual(owner)
        entered.set()
        assert release.wait(5)
        if earlier:
            now[0] = 101
            clock.adopt_cleanup(101)
        now[0] = 119
        if failed:
            raise MemoryError("synthetic failure after allocation")

    monkeypatch.setattr(Ingress, "allocate", allocate)
    monkeypatch.setattr(process_module.subprocess, "Popen", forbidden)
    command = Command(["never-launch"], clock=clock, deadline=115 if bound == "command" else None)
    command.launch()
    try:
        assert entered.wait(3) and not command.settled()
        assert len(command.ingress.raw) == REPORT_BYTES and not command.ingress
    finally:
        release.set()
        command.worker.join(3)
    assert command.settled() and command.process is None and command.error
    assert clock.cleanup_start_utc == (101 if earlier else 115) and clock.missed
    assert len(command.ingress.raw) == REPORT_BYTES
    command.ingress.retire()


@pytest.mark.parametrize("fd", [1, 2])
@pytest.mark.parametrize("extra", [0, 1, 4097])
def test_actual_command_prefix_facts_and_bounded_drain(fd: int, extra: int) -> None:
    command = Command(
        [sys.executable, "-I", "-B", "-c", f"import os; os.write({fd},b'x'*{REPORT_BYTES + extra})"]
    )
    command.launch()
    deadline = time.monotonic() + 5
    try:
        while not command.settled() and time.monotonic() < deadline:
            command.pump()
        assert command.settled(), command.receipt()
        receipt = command.receipt()
        prefix = "output" if fd == 1 else "diagnostic"
        assert receipt[prefix + "_bytes"] == REPORT_BYTES
        assert receipt[prefix + "_sha256"] == hashlib.sha256(b"x" * REPORT_BYTES).hexdigest()
        assert (command.error is not None) is bool(extra)
        assert command.ingress.eof and command.errors.eof
        assert len(command.ingress.raw) == REPORT_BYTES
    finally:
        command.stop()
        end = time.monotonic() + 3
        while not command.settled() and time.monotonic() < end:
            command.pump()
        assert command.settled()


@pytest.mark.parametrize("stage", ["read", "hash", "close"])
def test_opaque_pipe_operation_retains_helper_through_cancellation(
    consumer: ValidationConsumer, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    helper = OwnedHelper(consumer.clock, "metadata")
    consumer.active = helper
    helper.launch([sys.executable, "-I", "-B", "-c", "import os; os.write(2,b'opaque')"])
    process = helper.process
    assert process is not None and process.wait(timeout=3) == 0 and process.stderr is not None
    stream = process.stderr
    fd = stream.fileno()
    entered, release = threading.Event(), threading.Event()
    actual_read = os.read
    actual_digest = helper.errors.digest

    def hold() -> None:
        entered.set()
        assert release.wait(5)

    def read(number: int, size: int) -> bytes:
        assert size <= 4096
        if number == fd and stage == "read":
            hold()
        return actual_read(number, size)

    class Digest:
        def update(self, data: memoryview) -> None:
            if stage == "hash" and data:
                hold()
            actual_digest.update(data)

        def hexdigest(self) -> str:
            return actual_digest.hexdigest()

    class Stream:
        @property
        def closed(self) -> bool:
            return stream.closed

        def fileno(self) -> int:
            return stream.fileno()

        def close(self) -> None:
            if stage == "close":
                hold()
            stream.close()

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(helper.errors, "digest", Digest())
    process.stderr = cast(IO[bytes], Stream())
    failures: list[BaseException] = []

    def pump() -> None:
        try:
            while fd in helper.fds:
                helper.pump(0)
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=pump)
    worker.start()
    try:
        assert entered.wait(3)
        helper.cancel()
        assert consumer.clock.cleanup_start_utc == 105
        assert fd in helper.busy and fd in helper.fds and not helper.join(0)
        assert consumer.active is helper and len(helper.ingress.raw) == REPORT_BYTES
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not failures
    assert helper.join(1) and not helper.fds
    assert helper.errors.count == 6 and helper.errors.eof
    assert helper.errors.digest.hexdigest() == hashlib.sha256(b"opaque").hexdigest()
    assert consumer.clock.cleanup_start_utc == 105
    assert consumer.join(0)


def test_failed_opaque_hash_cannot_be_retried_as_a_new_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = Opaque()
    calls = 0

    class Digest:
        def update(self, _data: memoryview) -> None:
            nonlocal calls
            calls += 1
            raise OSError("synthetic hash failure")

    monkeypatch.setattr(owner, "digest", Digest())
    with pytest.raises(OSError):
        owner.feed(b"first")
    owner.feed(b"later")
    owner.feed(b"")
    assert owner.error == "opaque_hash_failed" and owner.count == 0 and owner.eof and calls == 1


def test_os_read_results_end_before_next_read(monkeypatch: pytest.MonkeyPatch) -> None:
    command = Command([sys.executable, "-I", "-B", "-c", "import os; os.write(2,b'x'*9000)"])
    command.launch()
    command.worker.join(3)
    assert not command.worker.is_alive()
    actual = os.read
    live = [0]
    observations: list[int] = []

    class Chunk(bytes):
        def __del__(self) -> None:
            live[0] -= 1

    def read(fd: int, size: int) -> bytes:
        observations.append(live[0])
        assert live[0] == 0
        data = actual(fd, size)
        result = Chunk(data)
        live[0] += 1
        return result

    monkeypatch.setattr(os, "read", read)
    try:
        end = time.monotonic() + 5
        while not command.settled() and time.monotonic() < end:
            command.pump()
        assert command.settled() and len(observations) >= 5 and live[0] == 0
        assert command.receipt()["diagnostic_bytes"] == 9000
    finally:
        command.stop()
        end = time.monotonic() + 3
        while not command.settled() and time.monotonic() < end:
            command.pump()
        assert command.settled()


def test_interrupted_command_start_is_registered_before_fixed_allocation(
    packet_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = controller(load(packet_path, ROOT, now=NOW), tmp_path / "ledger")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    commands: list[Command] = []
    failures: list[BaseException] = []
    actual_start = threading.Thread.start

    def launch(command: Command) -> None:
        commands.append(command)
        command.ingress.allocate()
        entered.set()
        assert release.wait(5)

    def start(worker: threading.Thread) -> None:
        actual_start(worker)
        if worker.name == "owned-docker-command":
            assert entered.wait(3)
            raise KeyboardInterrupt("synthetic uncertain start return")

    def prepare(owner: Controller) -> None:
        owner.run(["synthetic"])

    def execute() -> None:
        try:
            outer.execute()
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(Command, "_launch", launch)
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(Controller, "prepare", prepare)
    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert entered.wait(3) and len(commands) == 1
        command = commands[0]
        assert outer.commands == [command]
        assert not finished.is_set() and command.worker.is_alive()
        assert len(command.ingress.raw) == REPORT_BYTES
    finally:
        release.set()
        worker.join(3)
        for command in commands:
            command.worker.join(3)
        outer.journal.close()
    assert finished.is_set() and len(failures) == 1
    assert isinstance(failures[0], KeyboardInterrupt)
    assert failures[0].args == ("synthetic uncertain start return",)
    assert not outer.commands and commands[0].ingress.retired
    assert len(outer.command_receipts) == 1


def test_unobserved_attempted_thread_start_retains_uncertain_debt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = Command(["never-launch"])
    assert command.settled() and not command.ingress.raw

    def fail(_worker: threading.Thread) -> None:
        raise MemoryError("synthetic start without an observed identity")

    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(MemoryError):
        command.launch()
    command.pump()
    assert command.launch_pending and not command.settled()
    assert command.receipt()["launch_alive"] is True and not command.ingress.raw
    with pytest.raises(ProbeError, match="management_launch_not_admitted"):
        command.launch()


def test_interrupted_bootstrap_identity_does_not_establish_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = Command(["never-launch"])
    joins: list[float] = []

    class Bootstrap:
        # Controlled public-state schedule: ident exists before the target has
        # run or Thread's started/alive state can establish actual completion.
        ident: int | None = None

        def start(self) -> None:
            self.ident = 123
            raise KeyboardInterrupt("synthetic intermediate bootstrap")

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float) -> None:
            joins.append(timeout)
            raise RuntimeError("cannot join thread before it is started")

    monkeypatch.setattr(command, "worker", Bootstrap())
    with pytest.raises(KeyboardInterrupt):
        command.launch()
    assert command.launch_pending and not command.settled()
    command.pump()
    assert not joins and command.receipt()["launch_alive"] is True


def test_launch_target_signal_still_requires_actual_thread_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    actual = Command._run_launch  # pyright: ignore[reportPrivateUsage]

    class NoLaunch(Command):
        def _launch(self) -> None:
            self.ingress.allocate()

    def held_exit(command: Command) -> None:
        actual(command)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(Command, "_run_launch", held_exit)
    command = NoLaunch(["synthetic"])
    command.launch()
    try:
        assert entered.wait(3) and command.launch_finished.is_set()
        assert command.worker.is_alive() and command.launch_pending and not command.settled()
        command.pump()
        assert len(command.ingress.raw) == REPORT_BYTES
    finally:
        release.set()
        command.worker.join(3)
    assert command.settled() and not command.launch_pending
    command.ingress.retire()
