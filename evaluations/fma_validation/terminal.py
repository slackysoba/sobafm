"""Bounded decoder receipts are evidence even when decoding did not succeed."""

from __future__ import annotations

import math
import re
from typing import cast

from evaluations.linux_resource.probe import ProbeError

from .cases import CaseBinding
from .limits import CHUNK_BYTES, PCM_LIMIT, REPORT_BYTES, CaseClock, json_size
from .process import ProcessPin

DECODE_FIELDS = {
    "event",
    "schema",
    "case_binding",
    "runner",
    "passed",
    "failure",
    "input_bytes",
    "input_sha256",
    "input_complete",
    "stdout_eof",
    "stderr_eof",
    "exit_code",
    "pcm_bytes",
    "pcm_sha256",
    "measured_duration_seconds",
    "web_duration_seconds",
    "ready_seconds",
    "cleanup",
    "cleanup_debt",
    "memory_event_delta",
    "swap_current",
    "diagnostics",
    "catalog_admitted",
    "listening",
}


def validate_envelope(
    result: dict[str, object], case: CaseBinding, runner: ProcessPin, clock: CaseClock
) -> dict[str, object]:
    if (
        result.get("event") != "full_stream_result"
        or result.get("schema") != case.schema("decode")
        or result.get("case_binding") != case.record()
        or ProcessPin.from_record(result.get("runner")) != runner
    ):
        raise ProbeError("decoder_terminal_binding")
    return validate_clock(result.get("cleanup"), clock)


def validate_clock(value: object, clock: CaseClock) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProbeError("decoder_terminal_clock_binding")
    values = cast(dict[str, object], value)
    if (
        set(values)
        != {
            "whole_end_utc",
            "expires_utc",
            "cleanup_start_utc",
            "cleanup_deadline_met",
            "cleanup_timing_unknown",
            "case_deadline_met",
        }
        or values["whole_end_utc"] != clock.whole_end_utc
        or values["expires_utc"] != clock.expires_utc
        or any(
            type(values[key]) is not bool
            for key in ("cleanup_deadline_met", "cleanup_timing_unknown", "case_deadline_met")
        )
    ):
        raise ProbeError("decoder_terminal_clock_binding")
    started = values["cleanup_start_utc"]
    if started is None and values["cleanup_timing_unknown"] is True:
        return values
    if (
        type(started) not in (int, float)
        or not math.isfinite(cast(float, started))
        or not 0 <= cast(float, started) <= clock.wall()
    ):
        raise ProbeError("decoder_terminal_clock")
    return values


def validate_terminal(
    result: dict[str, object],
    case: CaseBinding,
    runner: ProcessPin,
    clock: CaseClock,
) -> dict[str, object]:
    json_size(result)
    if (
        set(result) != DECODE_FIELDS
        or result["event"] != "full_stream_result"
        or result["schema"] != case.schema("decode")
        or result["case_binding"] != case.record()
        or ProcessPin.from_record(result["runner"]) != runner
    ):
        raise ProbeError("decoder_terminal_binding")
    for key in ("passed", "input_complete", "stdout_eof", "stderr_eof", "cleanup_debt"):
        if type(result[key]) is not bool:
            raise ProbeError("decoder_terminal_boolean")
    failure = result["failure"]
    if failure is not None and (
        not isinstance(failure, str) or re.fullmatch("[a-z_]{1,80}", failure) is None
    ):
        raise ProbeError("decoder_terminal_failure")
    if result["passed"] is True and failure is not None:
        raise ProbeError("decoder_terminal_verdict")
    for key, limit in (
        ("input_bytes", case.bytes + CHUNK_BYTES),
        ("pcm_bytes", PCM_LIMIT + CHUNK_BYTES),
    ):
        value = result[key]
        if type(value) is not int or not 0 <= value <= limit:
            raise ProbeError("decoder_terminal_count")
    for key in ("input_sha256", "pcm_sha256"):
        value = result[key]
        if not isinstance(value, str) or re.fullmatch("[a-f0-9]{64}", value) is None:
            raise ProbeError("decoder_terminal_digest")
    for key in ("ready_seconds", "measured_duration_seconds"):
        value = result[key]
        if value is None and key == "ready_seconds":
            continue
        if (
            type(value) not in (float, int)
            or not math.isfinite(cast(float, value))
            or cast(float, value) < 0
        ):
            raise ProbeError("decoder_terminal_duration")
    exit_code = result["exit_code"]
    if exit_code is not None and (type(exit_code) is not int or not -128 <= exit_code <= 255):
        raise ProbeError("decoder_terminal_exit")
    counts, diagnostics, timing = (
        result[key] for key in ("memory_event_delta", "diagnostics", "cleanup")
    )
    if not all(isinstance(value, dict) for value in (counts, diagnostics, timing)):
        raise ProbeError("decoder_terminal_evidence")
    if any(
        type(value) is not int or not 0 <= value <= 2**63
        for value in cast(dict[str, object], counts).values()
    ):
        raise ProbeError("decoder_terminal_memory")
    diagnostic = cast(dict[str, object], diagnostics)
    if result["passed"] is False and (
        set(diagnostic) != {"bytes", "sha256"}
        or type(diagnostic.get("bytes")) is not int
        or not 0 <= cast(int, diagnostic["bytes"]) <= REPORT_BYTES
        or not isinstance(diagnostic.get("sha256"), str)
        or re.fullmatch("[a-f0-9]{64}", str(diagnostic["sha256"])) is None
    ):
        raise ProbeError("decoder_terminal_diagnostics")
    if result["catalog_admitted"] is not False or result["listening"] is not False:
        raise ProbeError("decoder_terminal_clock_binding")
    return validate_clock(timing, clock)
