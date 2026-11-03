"""One initially inert owner for sequential contained metadata and streaming decode."""

from __future__ import annotations

import math
import re
import sys
import threading
import time
from datetime import UTC, datetime
from typing import cast

from evaluations.linux_resource.probe import ProbeError
from sobafm.fma_intake import IntakeRequest, LocalIntake

from .cases import NIGHT_OWL, PCM_SHA256, CaseBinding, OwnedQualificationRequest
from .diagnostics import FORMAT
from .ingress import Ingress, Record, discard_exception, pairs
from .limits import (
    MAX_FRAMES,
    MAX_SEEKS,
    METADATA_SECONDS,
    PCM_LIMIT,
    READY_BYTES,
    CaseClock,
    encode_report,
    json_size,
)
from .observation import ObservationWorkspace
from .process import OwnedHelper, ProcessPin, inventory
from .terminal import DECODE_FIELDS, validate_envelope, validate_terminal

object_pairs = pairs


def reports(raw: bytes, role: str = "metadata") -> list[dict[str, object]]:
    ingress = Ingress(role)
    ingress.feed(raw)
    ingress.scan(final=True)
    if ingress.error:
        raise ProbeError(ingress.error)
    return [record.value for record in ingress.records if record.value is not None]


def validate_metadata(result: dict[str, object], case: CaseBinding = NIGHT_OWL) -> None:
    if set(result) != {
        "schema",
        "parser",
        "framing",
        "id3v2",
        "id3v1",
        "id3v1_version",
        "id3v1_sha256",
        "conflicting_frame_ids",
        "omitted_frame_ids",
        "unknown_frames",
        "read_bytes",
        "seek_count",
        "credits_complete",
        "recording_correspondence",
        "links_followed",
    } or (
        result["schema"] != case.schema("metadata")
        or result["parser"] != "mutagen-1.48.1"
        or result["credits_complete"] is not False
        or result["links_followed"] is not False
        or result["recording_correspondence"] != "requires_trusted_review"
        or result["id3v1_version"] not in (None, "1.0", "1.1")
        or not isinstance(result["framing"], dict)
    ):
        raise ProbeError("metadata_report_fields")
    for key, limit in (("read_bytes", case.bytes * 3), ("seek_count", MAX_SEEKS)):
        value = result[key]
        if type(value) is not int or not 0 <= value <= limit:
            raise ProbeError("metadata_report_limits")
    for key in ("id3v1", "id3v2", "conflicting_frame_ids", "omitted_frame_ids", "unknown_frames"):
        value = result[key]
        if not isinstance(value, list) or len(cast(list[object], value)) > MAX_FRAMES:
            raise ProbeError("metadata_report_collection")
    for key in ("id3v1", "id3v2"):
        for item in cast(list[object], result[key]):
            if not isinstance(item, dict):
                raise ProbeError("metadata_report_frame")
            frame = cast(dict[str, object], item)
            if (
                set(frame) != {"id", "key", "fields"}
                or not isinstance(frame["id"], str)
                or not isinstance(frame["key"], str)
                or not isinstance(frame["fields"], dict)
            ):
                raise ProbeError("metadata_report_frame")
    json_size(result)


def validate_decode(result: dict[str, object], case: CaseBinding = NIGHT_OWL) -> None:
    if set(result) != DECODE_FIELDS:
        raise ProbeError("decode_report_fields")
    pcm = result["pcm_bytes"]
    if (
        result["event"] != "full_stream_result"
        or result["schema"] != case.schema("decode")
        or result["case_binding"] != case.record()
        or result["passed"] is not True
        or result["failure"] is not None
        or result["input_bytes"] != case.bytes
        or result["input_sha256"] != case.sha256
        or any(result[key] is not True for key in ("input_complete", "stdout_eof", "stderr_eof"))
        or type(result["exit_code"]) is not int
        or result["exit_code"] != 0
        or type(pcm) is not int
        or not READY_BYTES <= pcm <= PCM_LIMIT
        or pcm % 4
        or result["cleanup_debt"] is not False
        or result["catalog_admitted"] is not False
        or result["listening"] is not False
        or result["swap_current"] != "0"
    ):
        raise ProbeError("incomplete_full_stream_report")
    ready = result["ready_seconds"]
    duration = result["measured_duration_seconds"]
    if (
        type(ready) not in (int, float)
        or not math.isfinite(cast(float, ready))
        or not 0 <= cast(float, ready) <= 15
        or type(duration) not in (int, float)
        or duration != pcm / 192_000
        or result["web_duration_seconds"] != (None if case.synthetic else 194)
        or (case.synthetic and (pcm != 23_040_000 or result["pcm_sha256"] != PCM_SHA256))
        or not isinstance(result["pcm_sha256"], str)
        or re.fullmatch("[0-9a-f]{64}", result["pcm_sha256"]) is None
    ):
        raise ProbeError("invalid_pcm_measurement")
    delta = result["memory_event_delta"]
    cleanup = result["cleanup"]
    diagnostics = result["diagnostics"]
    if not all(isinstance(value, dict) for value in (delta, cleanup, diagnostics)):
        raise ProbeError("decode_evidence_missing")
    counts = cast(dict[str, object], delta)
    timing = cast(dict[str, object], cleanup)
    diagnostic = cast(dict[str, object], diagnostics)
    if (
        any(type(value) is not int or value < 0 for value in counts.values())
        or counts.get("oom") != 0
        or counts.get("oom_kill") != 0
        or timing.get("cleanup_deadline_met") is not True
        or timing.get("cleanup_timing_unknown") is not False
        or timing.get("case_deadline_met") is not True
        or type(timing.get("cleanup_start_utc")) not in (int, float)
        or diagnostic.get("terminal_source_filter_required") is not True
        or not isinstance(diagnostic.get("format"), str)
    ):
        raise ProbeError("decode_evidence_failed")
    if FORMAT.fullmatch(str(diagnostic["format"]).encode("ascii")) is None:
        raise ProbeError("decode_format_failed")


class ValidationConsumer:
    def __init__(
        self,
        owner: LocalIntake,
        init: ProcessPin,
        owner_pin: ProcessPin,
        clock: CaseClock,
        case: CaseBinding = NIGHT_OWL,
        *,
        workspace: ObservationWorkspace | None = None,
    ) -> None:
        self.owner, self.init_pin, self.owner_pin, self.clock = owner, init, owner_pin, clock
        self.observation = ObservationWorkspace(clock) if workspace is None else workspace
        if self.observation.clock is not clock:
            raise ProbeError("observation_clock_owner")
        request = owner.request
        if (
            request.file.bytes != case.bytes
            or request.file.sha256 != case.sha256
            or (isinstance(request, OwnedQualificationRequest) and request.case != case)
            or (isinstance(request, IntakeRequest) and case.synthetic)
        ):
            raise ProbeError("consumer_request_case_binding")
        self._case = case
        self.lock = threading.RLock()
        self.active: OwnedHelper | None = None
        self.operation = False
        self.phase = "inert"
        self.metadata: dict[str, object] | None = None
        self.decode: dict[str, object] | None = None
        self.history: list[dict[str, object]] = []
        self.delivered = 0
        self.finished = False
        self.terminal_error: str | None = None
        self.first_write_cancelled = False
        self.terminal_record: Record | None = None

    @property
    def case(self) -> CaseBinding:
        return self._case

    def _start(self, role: str, args: list[str], inherited: tuple[int, ...] = ()) -> OwnedHelper:
        with self.lock:
            if self.active is not None or len(self.history) >= 2 or self.clock.remaining() <= 0:
                raise ProbeError("sequential_helper_not_admitted")
            helper = OwnedHelper(self.clock, role, self.observation)
            self.active = helper
        if inventory(
            self.init_pin,
            self.owner_pin,
            workspace=self.observation,
            cleanup=self.clock.cleanup_start_utc is not None,
        ):
            raise ProbeError("unsettled_previous_phase")
        helper.launch(
            [
                sys.executable,
                "-m",
                f"evaluations.fma_validation.{role}",
                "--execute",
                "--case",
                self.case.name,
                *args,
            ],
            inherited,
        )
        assert helper.pin is not None
        if inventory(self.init_pin, self.owner_pin, helper.pin, workspace=self.observation):
            raise ProbeError("unexpected_helper_phase_members")
        helper.admit()
        return helper

    def _capture(self, helper: OwnedHelper, *, final: bool = False) -> None:
        if helper.role != "decoder" or helper.ingress.retired:
            return
        helper.ingress.scan(final=final)
        try:
            rows = helper.ingress.records
            terminals = [row for row in rows if row.family == "decode"]
            if not terminals:
                if final or helper.ingress.error:
                    raise ProbeError("decoder_terminal_missing")
                return
            if helper.pin is None:
                raise ProbeError("decoder_terminal_pin")
            terminal = terminals[0]
            if self.terminal_record is not None and self.terminal_record is not terminal:
                raise ProbeError("decoder_terminal_changed")
            self.terminal_record = terminal
            if terminal.envelope is None:
                raise ProbeError("decoder_terminal_missing_envelope")
            values = validate_envelope(terminal.envelope, self.case, helper.pin, self.clock)
            started = values["cleanup_start_utc"]
            if started is not None:
                self.clock.adopt_cleanup(cast(float, started))
            self.clock.timing_unknown |= values["cleanup_timing_unknown"] is True
            self.clock.missed |= values["cleanup_deadline_met"] is not True
            self.clock.case_missed |= values["case_deadline_met"] is not True
            if len(terminals) != 1 or rows[-1] is not terminals[0]:
                raise ProbeError("decoder_terminal_sequence")
            if len(rows) == 2 and (
                rows[0].envelope is None
                or rows[0].envelope.get("event") != "decoder_admitted"
                or rows[0].envelope.get("case_binding") != self.case.record()
                or ProcessPin.from_record(rows[0].envelope.get("runner")) != helper.pin
            ):
                raise ProbeError("decoder_terminal_admission")
            if terminal.value is not None and self.decode is None:
                validate_terminal(terminal.value, self.case, helper.pin, self.clock)
                self.decode = terminal.value
            if helper.ingress.error or terminal.value is None:
                raise ProbeError("decoder_terminal_invalid_detail_or_suffix")
        except (ValueError, TypeError, KeyError, RecursionError, ProbeError) as error:
            discard_exception(error)
            self.terminal_error = "invalid_or_missing_decoder_terminal"
            self.clock.timing_unknown = self.clock.missed = True
        # Do not revoke the owned reader inside its finish callback. The outer
        # owner conveys this earlier clock after observing actual reader exit.

    def _propagate(self) -> None:
        self.clock.cancel()
        assert self.clock.cleanup_start_utc is not None
        self.owner.cancel(
            "evaluation_cancelled",
            started_at=datetime.fromtimestamp(self.clock.cleanup_start_utc, UTC),
            timing_unknown=self.clock.timing_unknown,
            deadline_missed=self.clock.missed,
        )

    def _retire(self, helper: OwnedHelper) -> None:
        if not helper.join(0):
            raise ProbeError("helper_still_owned")
        self._capture(helper, final=True)
        if inventory(
            self.init_pin,
            self.owner_pin,
            workspace=self.observation,
            cleanup=self.clock.cleanup_start_utc is not None,
        ):
            raise ProbeError("helper_descendant_debt")
        with self.lock:
            if not helper.receipt_published:
                self.history.append(helper.receipt())
                helper.receipt_published = True
        helper.ingress.retire()
        helper.errors.retire()
        self.terminal_record = None
        with self.lock:
            self.active = None

    def inspect(self, fd: int) -> None:
        self.operation = True
        try:
            self.phase = "metadata"
            helper = self._start("metadata", ["--owned-fd", str(fd)], (fd,))
            helper.finish_input()
            end = time.monotonic() + METADATA_SECONDS
            while not helper.join(0.01):
                if self.clock.remaining(end) <= 0:
                    self.cancel()
                    raise ProbeError("metadata_deadline")
            helper.ingress.scan(final=True)
            rows = [record.value for record in helper.ingress.records if record.value is not None]
            if (
                helper.ingress.error
                or len(rows) != 1
                or rows[0].pop("case_binding", None) != self.case.record()
                or helper.pin is None
                or rows[0].pop("helper_pid", None) != helper.pin.pid
            ):
                raise ProbeError("metadata_case_binding")
            if len(rows) == 1 and rows[0].get("schema") == self.case.schema("metadata-rejection"):
                rejection = rows[0]
                reason = rejection.get("reason")
                if (
                    set(rejection) == {"schema", "reason", "credits_complete"}
                    and rejection["credits_complete"] is False
                    and isinstance(reason, str)
                    and re.fullmatch("[a-z0-9_]{1,80}", reason)
                ):
                    self.metadata = rejection
                raise ProbeError("metadata_helper_rejected")
            if (
                helper.failure
                or helper.errors
                or helper.process is None
                or helper.process.returncode != 0
                or len(rows) != 1
                or rows[0].get("schema") != self.case.schema("metadata")
                or rows[0].get("credits_complete") is not False
            ):
                raise ProbeError("metadata_helper_failed")
            self.metadata = rows[0]
            validate_metadata(self.metadata, self.case)
            self._retire(helper)
            self.phase = "decoder_preparation"
            helper = self._start(
                "decoder",
                [
                    "--init",
                    encode_report(self.init_pin.record()).decode(),
                    "--owner",
                    encode_report(self.owner_pin.record()).decode(),
                    "--whole-end",
                    str(self.clock.whole_end_utc),
                    "--expires",
                    str(self.clock.expires_utc),
                ],
            )
            helper.ingress.scan()
            while not helper.ingress.records:
                helper.pump()
                helper.ingress.scan()
                if (
                    self.clock.remaining() <= 0
                    or helper.process is None
                    or helper.process.poll() is not None
                    or helper.failure
                ):
                    raise ProbeError("decoder_admission_failed")
            ready = helper.ingress.records
            if (
                helper.ingress.error
                or len(ready) != 1
                or ready[0].value is None
                or ready[0].value.get("event") != "decoder_admitted"
            ):
                raise ProbeError("decoder_admission_record")
            record = ready[0].value
            if set(record) != {
                "event",
                "case_binding",
                "native_gate",
                "init",
                "owner",
                "runner",
            } or (
                record["case_binding"] != self.case.record()
                or ProcessPin.from_record(record["init"]) != self.init_pin
                or ProcessPin.from_record(record["owner"]) != self.owner_pin
                or ProcessPin.from_record(record["runner"]) != helper.pin
            ):
                raise ProbeError("decoder_admission_identity")
            gate = record["native_gate"]
            if not isinstance(gate, dict):
                raise ProbeError("native_admission_gate")
            observation = cast(dict[str, object], gate)
            native_pid = observation.get("pid")
            if type(native_pid) is not int or helper.pin is None:
                raise ProbeError("native_admission_pid")
            native = ProcessPin.capture(native_pid, self.observation)
            if (
                native.parent != helper.pin.pid
                or native.start != observation.get("start_ticks")
                or native.executable != "/usr/bin/mpg123"
                or inventory(self.init_pin, self.owner_pin, helper.pin, workspace=self.observation)
                != {native_pid}
            ):
                raise ProbeError("native_phase_inventory")
            # The complete native gate was checked before stream handoff. Only
            # case/runner/order facts are needed to authenticate a later terminal.
            ready[0].envelope = {key: record[key] for key in ("event", "case_binding", "runner")}
            ready[0].value = None
            self.phase = "streaming"
        finally:
            self.operation = False

    def write(self, chunk: bytes) -> None:
        self.operation = True
        try:
            if self.phase != "streaming" or self.active is None or self.clock.remaining() <= 0:
                raise ProbeError("consumer_stream_revoked")
            self.active.write(chunk)
            self.delivered += len(chunk)
            if self.delivered > self.case.bytes:
                raise ProbeError("consumer_input_overrun")
            if self.case.name == "first-write-cancel" and not self.first_write_cancelled:
                self.first_write_cancelled = True
                self.cancel()
                raise ProbeError("qualification_first_write_cancel")
        except Exception:
            if self.active is not None:
                self._capture(self.active)
            raise
        finally:
            self.operation = False

    def finish(self) -> None:
        self.operation = True
        try:
            helper = self.active
            if helper is None or self.delivered != self.case.bytes:
                raise ProbeError("consumer_incomplete_input")
            helper.finish_input()
            while not helper.join(0.01):
                if self.clock.remaining() <= 0:
                    self.cancel()
                    raise ProbeError("decoder_case_deadline")
            self._capture(helper, final=True)
            if (
                self.decode is None
                or self.terminal_error is not None
                or helper.errors
                or helper.failure
                or helper.process is None
                or helper.process.returncode != 0
            ):
                raise ProbeError("decoder_report_sequence")
            self.phase = "finishing"
            validate_decode(self.decode, self.case)
            self._retire(helper)
            self.finished = True
            self.phase = "settled"
        finally:
            self.operation = False

    def cancel(self) -> None:
        helper = self.active
        if helper is not None:
            self._capture(helper)
        if self.owner.cleanup_started_utc is not None:
            self.clock.adopt_cleanup(
                datetime.fromisoformat(self.owner.cleanup_started_utc).timestamp()
            )
        self._propagate()
        if helper is not None:
            helper.cancel()

    def join(self, timeout: float) -> bool:
        if self.operation or self.observation.debt:
            return False
        helper = self.active
        if helper is not None:
            settled = helper.join(timeout)
            self._capture(helper, final=settled)
            if self.owner.stop.is_set() or not self.finished:
                self._propagate()
            if not settled:
                return False
            self._retire(helper)
        return not inventory(
            self.init_pin,
            self.owner_pin,
            workspace=self.observation,
            cleanup=self.clock.cleanup_start_utc is not None,
        )

    def receipt(self) -> dict[str, object]:
        return {
            "case_binding": self.case.record(),
            "phase": self.phase,
            "finished": self.finished,
            "delivered_bytes": self.delivered,
            "metadata": self.metadata,
            "decode": self.decode,
            "terminal_error": self.terminal_error,
            "first_write_cancelled": self.first_write_cancelled,
            "helpers": self.history,
            "active": None if self.active is None else self.active.receipt(),
            "cleanup": self.clock.receipt(),
        }


def expected_outcome(validation: dict[str, object], case: CaseBinding) -> bool:
    """Only an observation predicate; resource release must be checked separately."""
    if (
        validation.get("case_binding") != case.record()
        or validation.get("terminal_error") is not None
    ):
        return False
    if case.name == "metadata-rejection":
        return (
            validation.get("metadata")
            == {
                "schema": case.schema("metadata-rejection"),
                "reason": "unsupported_id3_text_encoding",
                "credits_complete": False,
            }
            and validation.get("phase") == "metadata"
            and validation.get("decode") is None
            and validation.get("delivered_bytes") == 0
            and validation.get("finished") is False
            and validation.get("first_write_cancelled") is False
        )
    if case.name == "first-write-cancel":
        result = validation.get("decode")
        return (
            validation.get("first_write_cancelled") is True
            and validation.get("finished") is False
            and isinstance(result, dict)
            and cast(dict[str, object], result).get("passed") is False
            and type(validation.get("delivered_bytes")) is int
            and 0 < cast(int, validation["delivered_bytes"]) <= 65_536
        )
    return validation.get("finished") is True
