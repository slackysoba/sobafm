"""Closed synthetic wire records; no provider, media or Docker execution."""

from __future__ import annotations

import gc
import hashlib
import json
from pathlib import Path
from typing import NoReturn, cast

import pytest

from evaluations.fma_validation import ingress as module
from evaluations.fma_validation.cases import CASE_IDS, NIGHT_OWL, CaseBinding, CaseId
from evaluations.fma_validation.consumer import ValidationConsumer
from evaluations.fma_validation.ingress import Ingress, decode
from evaluations.fma_validation.limits import REPORT_BYTES, encode_report
from evaluations.fma_validation.process import OwnedHelper, ProcessPin
from evaluations.linux_resource.probe import ProbeError
from tests.test_fma_failure_receipts import consumer as consumer
from tests.test_fma_failure_receipts import terminal

PIN = ProcessPin(2, 1, 1, "/python", 1, 2, 1000, "/")


def forbidden_decode(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("unsupported wire reached JSON materialization")


def metadata() -> dict[str, object]:
    return {
        "schema": NIGHT_OWL.schema("metadata"),
        "parser": "mutagen-1.48.1",
        "framing": {"v2_present": False, "frame_ids": [], "duplicate_frame_ids": []},
        "id3v1": [],
        "id3v2": [],
        "id3v1_version": None,
        "id3v1_sha256": None,
        "conflicting_frame_ids": [],
        "omitted_frame_ids": [],
        "unknown_frames": [],
        "read_bytes": 128,
        "seek_count": 2,
        "credits_complete": False,
        "recording_correspondence": "requires_trusted_review",
        "links_followed": False,
    }


def metadata_wire(value: dict[str, object] | None = None) -> bytes:
    value = metadata() if value is None else value
    return encode_report(
        {**value, "case_binding": NIGHT_OWL.record(), "helper_pid": 2}, newline=True
    )


@pytest.mark.parametrize(
    "value",
    [
        '\x00\b\f\n\r\t/\\"\xff',
        ["one", "\xff"],
        [["role", "name"]],
        ["one", ["role", "name"]],
        "\xff" * 4096,
        ["x"] * 128,
        [["x"] * 128],
    ],
)
def test_metadata_text_list_and_nested_notice_domains(value: object) -> None:
    record = metadata()
    record["id3v2"] = [{"id": "COMM", "key": "COMM", "fields": {"text": value}}]
    assert decode(encode_report(record), "metadata") == record


@pytest.mark.parametrize(
    "field",
    [
        "text",
        "desc",
        "lang",
        "url",
        "people",
        "owner",
        "email",
        "price",
        "date",
        "contact",
        "seller",
    ],
)
def test_every_selected_notice_field_survives(field: str) -> None:
    record = metadata()
    record["id3v2"] = [{"id": "TXXX", "key": "TXXX", "fields": {field: "\xff notice"}}]
    assert decode(encode_report(record), "metadata") == record


@pytest.mark.parametrize(
    "value",
    [
        "\u0100",
        "\U00010000",
        "\ud800",
        "x" * 4097,
        ["x"] * 129,
        [["x"] * 129],
        [[["x"]]],
        {"unexpected": "x"},
    ],
)
def test_rejected_notice_never_reaches_json_decoder(
    value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = metadata()
    record["id3v2"] = [{"id": "COMM", "key": "COMM", "fields": {"text": value}}]
    raw = json.dumps(record).encode("ascii")
    monkeypatch.setattr(module.json, "loads", forbidden_decode)
    with pytest.raises(ProbeError):
        decode(raw, "metadata")


@pytest.mark.parametrize(
    "raw",
    [
        b'{"' + b"x" * 9000 + b'":0}',
        b'{"\\ud800\\udc00":0}',
        b'{"schema":"x","unknown":"' + b"\\u0100" * 9000 + b'"}',
        b'{"unknown":' + b"[" * 13 + b"0" + b"]" * 13 + b"}",
        b'{"unknown":[' + b"0," * 8192 + b"0]}",
        b'{"read_bytes":' + b"1" * 129 + b"}",
        b'{"read_bytes":NaN}',
        b'{"read_bytes":Infinity}',
        b'{"read_bytes":1e999}',
        b'{"read_bytes":01}',
        b'{"read_bytes":1,}',
        b'{"read_bytes":1} false',
    ],
)
def test_unknown_large_deep_or_invalid_values_rejected_before_loads(
    raw: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module.json, "loads", forbidden_decode)
    with pytest.raises(ProbeError):
        decode(raw, "metadata")


@pytest.mark.parametrize("family", ["metadata", "info"])
@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-32", "utf-32-be", "utf-8-sig"])
def test_no_bom_or_utf16_autodetection(family: str, encoding: str) -> None:
    record: dict[str, object] = (
        metadata()
        if family == "metadata"
        else {"architecture": "x86_64", "os": "linux", "seccomp": True}
    )
    with pytest.raises(ProbeError):
        decode(json.dumps(record).encode(encoding), family)


def test_escaped_equivalent_keys_rejected_before_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = encode_report(metadata())[:-1] + b',"\\u0073chema":"duplicate"}'
    monkeypatch.setattr(module.json, "loads", forbidden_decode)
    with pytest.raises(ProbeError, match="duplicate_report_field"):
        decode(raw, "metadata")


def test_noncanonical_valid_json_preserves_semantics() -> None:
    record = metadata()
    raw = (
        json.dumps(dict(reversed(list(record.items()))), indent=2)
        .replace("review", "re\\u0076iew")
        .encode()
    )
    assert decode(raw, "metadata") == record


@pytest.mark.parametrize(
    "token",
    [
        b"1." + b"0" * 2000,
        b"0." + b"0" * 2000 + b"1",
        b"-0." + b"0" * 2000,
        b"1e-" + b"0" * 2000 + b"2",
        b"1e100",
    ],
)
def test_long_finite_number_spellings_keep_maintained_semantics(token: bytes) -> None:
    record = terminal(PIN)
    raw = encode_report(record).replace(
        b'"measured_duration_seconds":0', b'"measured_duration_seconds":' + token
    )
    expected: object = json.loads(raw)
    assert decode(raw, "decode") == expected


@pytest.mark.parametrize("separator", ["T", "\U00010000", "\ud800", "\x00"])
def test_qualification_timestamp_domain_is_not_normal_intake_regex(separator: str) -> None:
    timestamp = "2026-11-02" + separator + "00:00:00." + "0" * 500 + "Z"
    from evaluations.fma_validation.limits import utc_seconds

    assert utc_seconds(timestamp) == utc_seconds("2026-11-02T00:00:00Z")
    with memoryview(encode_report(timestamp)) as view:
        check = module.Scan(view, module.TIMESTAMP)
        check.run()
        assert check.valid


@pytest.mark.parametrize("size", [0, 1, 32])
def test_dynamic_resource_map_supports_documented_keys(size: int) -> None:
    record = terminal(PIN)
    record["memory_event_delta"] = {("k" + str(i)).ljust(32, "_"): 2**63 for i in range(size)}
    assert decode(encode_report(record), "decode") == record


@pytest.mark.parametrize(
    "counts",
    [
        {"x" * 33: 0},
        {"bad-key": 0},
        {"\u0100": 0},
        {str(i): 0 for i in range(33)},
        {"x": 2**63 + 1},
        {"x": -1},
        {"x": True},
        {"x": 1.0},
    ],
)
def test_resource_map_rejects_unsupported_input_before_loads(
    counts: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    record = terminal(PIN)
    record["memory_event_delta"] = counts
    raw = encode_report(record)
    monkeypatch.setattr(module.json, "loads", forbidden_decode)
    with pytest.raises(ProbeError):
        decode(raw, "decode")


@pytest.mark.parametrize("chunk", [1, 7, 4096, REPORT_BYTES])
def test_parse_once_original_prefix_hash_and_retirement(
    chunk: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = b"\n\n" + metadata_wire() + b"\n"
    owner = Ingress("metadata")
    actual = module.materialize
    calls: list[int] = []

    def tracked(data: memoryview, *, utf8: bool = False) -> dict[str, object]:
        calls.append(len(data))
        return actual(data, utf8=utf8)

    monkeypatch.setattr(module, "materialize", tracked)
    for start in range(0, len(raw), chunk):
        owner.feed(raw[start : start + chunk])
        owner.scan()
        owner.scan()
    owner.scan(final=True)
    assert owner.error is None and len(calls) == 1 and len(owner.records) == 1
    assert owner.count == len(raw) and owner.digest.hexdigest() == hashlib.sha256(raw).hexdigest()
    owner.retire()
    assert not owner.raw and not owner.records and owner.count == len(raw)
    assert owner.digest.hexdigest() == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("extra", [False, True])
def test_aggregate_cap_is_original_stream_not_each_record(extra: bool) -> None:
    raw = metadata_wire()
    raw = b"\n" * (REPORT_BYTES - len(raw)) + raw
    owner = Ingress("metadata")
    owner.feed(raw[:30000])
    owner.scan()
    owner.feed(raw[30000:] + (b"x" if extra else b""))
    owner.scan(final=True)
    assert (owner.error is not None) is extra
    assert (
        owner.count == REPORT_BYTES and owner.digest.hexdigest() == hashlib.sha256(raw).hexdigest()
    )


@pytest.mark.parametrize("suffix", [b"{}\n", metadata_wire(), b"{partial", b" "])
def test_cached_metadata_does_not_hide_suffix(suffix: bytes) -> None:
    owner = Ingress("metadata")
    owner.feed(metadata_wire())
    owner.scan()
    retained = owner.records[0].value
    owner.feed(suffix)
    owner.scan(final=True)
    assert owner.error and len(owner.records) == 1 and owner.records[0].value is retained


def test_metadata_final_record_without_lf_but_decoder_requires_lf() -> None:
    owner = Ingress("metadata")
    owner.feed(metadata_wire().removesuffix(b"\n"))
    owner.scan()
    assert not owner.records
    owner.scan(final=True)
    assert owner.error is None and owner.records[0].value is not None
    decoder = Ingress("decoder")
    decoder.feed(encode_report(terminal(PIN)))
    decoder.scan(final=True)
    assert decoder.error == "ingress_partial_record" and not decoder.records


def test_changed_original_prefix_cannot_reuse_cached_record() -> None:
    owner = Ingress("metadata")
    owner.feed(metadata_wire())
    owner.scan()
    owner.raw[10] ^= 1
    owner.scan(final=True)
    assert owner.error == "ingress_prefix_changed"


@pytest.mark.parametrize(
    "field",
    ["diagnostics", "memory_event_delta", "pcm_bytes", "passed", "catalog_admitted", "init"],
)
def test_bad_terminal_detail_keeps_authenticated_earliest_clock(
    consumer: ValidationConsumer, field: str
) -> None:
    helper = OwnedHelper(consumer.clock, "decoder")
    helper.pin = consumer.owner_pin
    row = terminal(helper.pin)
    row[field] = "\U00010000" * 500
    helper.ingress.feed(encode_report(row, newline=True))
    consumer.active = helper
    assert consumer.join(0)
    assert consumer.decode is None and consumer.terminal_error
    assert consumer.clock.cleanup_start_utc == 100 and consumer.clock.timing_unknown
    consumer.cancel()
    assert consumer.owner.cleanup_started_utc == "1970-01-01T00:01:40Z"


@pytest.mark.parametrize(
    "suffix", [b"{}\n", b"{partial", b"\n" + encode_report(terminal(PIN), newline=True)]
)
def test_terminal_suffix_keeps_prior_trusted_clock(
    consumer: ValidationConsumer, suffix: bytes
) -> None:
    helper = OwnedHelper(consumer.clock, "decoder")
    helper.pin = consumer.owner_pin
    helper.ingress.feed(encode_report(terminal(helper.pin), newline=True))
    consumer.active = helper
    consumer.cancel()
    helper.ingress.feed(suffix)
    assert consumer.join(0)
    assert consumer.clock.cleanup_start_utc == 100 and consumer.clock.timing_unknown


@pytest.mark.parametrize(
    "path", ["/tmp/caf\xe9", "/tmp/\u6f22", "/tmp/\U00010000", "/" + "\U0010ffff" * 4095]
)
@pytest.mark.parametrize("escaped", [False, True])
def test_docker_strict_utf8_decoded_paths_preserve_exact_codepoints(
    path: str, escaped: bool
) -> None:
    record = {
        "Name": "volume",
        "CreatedAt": "2026-11-02T00:00:00Z",
        "Mountpoint": path,
        "Labels": {"org.sobafm.issue": "44"},
    }
    raw = json.dumps(record, ensure_ascii=escaped).encode("utf-8")
    assert decode(raw, "volume") == record
    assert (
        hashlib.sha256(cast(str, decode(raw, "volume")["Mountpoint"]).encode()).digest()
        == hashlib.sha256(path.encode()).digest()
    )


@pytest.mark.parametrize(
    "bad", [b"\xc0\x80", b"\xed\xa0\x80", b"\xf4\x90\x80\x80", b"\x80", b"\xf0\x90", b"\\ud800"]
)
def test_docker_rejects_non_utf8_or_unpaired_surrogate_before_decode(
    bad: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = (
        b'{"Name":"v","CreatedAt":"x","Mountpoint":"/'
        + bad
        + b'","Labels":{"org.sobafm.issue":"44"}}'
    )
    monkeypatch.setattr(module.json, "loads", forbidden_decode)
    with pytest.raises(ProbeError):
        decode(raw, "volume")


@pytest.mark.parametrize("case_name", CASE_IDS)
def test_supported_case_bindings_and_metadata_rejection(case_name: CaseId) -> None:
    case = CaseBinding(case_name)
    owner = Ingress("metadata")
    record = {
        "schema": case.schema("metadata-rejection"),
        "reason": "unsupported_id3_version",
        "credits_complete": False,
        "case_binding": case.record(),
        "helper_pid": 2,
    }
    owner.feed(encode_report(record, newline=True))
    owner.scan(final=True)
    assert owner.error is None and owner.records[0].value == record


def test_completed_exception_graphs_release_payload_with_gc_disabled() -> None:
    class Payload:
        pass

    import weakref

    refs: list[weakref.ReferenceType[Payload]] = []

    def fail() -> None:
        payload = Payload()
        refs.append(weakref.ref(payload))
        raise ValueError(payload)

    retained: list[BaseException] = []
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(4):
            try:
                fail()
            except ValueError as error:
                error.__cause__ = error
                retained.append(error)
                module.discard_exception(error)
            assert all(ref() is None for ref in refs)
    finally:
        if enabled:
            gc.enable()


@pytest.mark.parametrize("already_captured", [False, True])
def test_two_terminal_records_preserve_first_envelope(
    consumer: ValidationConsumer, already_captured: bool
) -> None:
    helper = OwnedHelper(consumer.clock, "decoder")
    helper.pin = consumer.owner_pin
    consumer.active = helper
    raw = encode_report(terminal(helper.pin), newline=True)
    helper.ingress.feed(raw)
    if already_captured:
        consumer.cancel()
    helper.ingress.feed(raw)
    assert consumer.join(0)
    assert consumer.clock.cleanup_start_utc == 100 and consumer.clock.timing_unknown


def test_helper_retirement_failure_publishes_one_receipt(
    consumer: ValidationConsumer, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = OwnedHelper(consumer.clock, "decoder")
    helper.pin = consumer.owner_pin
    helper.ingress.feed(encode_report(terminal(helper.pin), newline=True))
    consumer.active = helper
    actual = Ingress.retire
    attempts = 0

    def fail_once(owner: Ingress) -> None:
        nonlocal attempts
        if owner is helper.errors:
            attempts += 1
            if attempts == 1:
                raise ProbeError("synthetic_retirement_failure")
        actual(owner)

    monkeypatch.setattr(Ingress, "retire", fail_once)
    with pytest.raises(ProbeError, match="synthetic_retirement_failure"):
        consumer.join(0)
    assert consumer.active is helper and len(consumer.history) == 1
    receipt = consumer.history[0]
    assert consumer.join(0) and len(consumer.history) == 1
    assert consumer.history[0] is receipt and consumer.active is None
    assert helper.ingress.count == receipt["stdout_bytes"]
    assert helper.ingress.digest.hexdigest() == receipt["stdout_sha256"]


def test_actual_metadata_producer_notices_survive_wire(tmp_path: Path) -> None:
    import os

    from evaluations.fma_validation.metadata import inspect
    from tests.test_fma_validation import frame, id3

    content = id3(frame(b"TIT2", b"\x00Latin\xff") + frame(b"ZZZZ", b"opaque"))
    content += b"owned synthetic" * 20
    path = tmp_path / "owned-nonaudio"
    path.write_bytes(content)
    fd = os.open(path, os.O_RDONLY)
    try:
        result = inspect(fd, len(content))
    finally:
        os.close(fd)
    assert decode(encode_report(result), "metadata") == result


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("case_name", CASE_IDS)
def test_actual_terminal_producer_variants_pass_ingress(
    monkeypatch: pytest.MonkeyPatch, failed: bool, case_name: CaseId
) -> None:
    from tests.test_fma_environment_observation import environment, settled_decoder

    env = environment(monkeypatch)
    run = settled_decoder(monkeypatch, env)
    run.case = CaseBinding(case_name)
    assert run.release_if_done()
    if failed:
        run.fail("synthetic_terminal_failure")
        run.memory_delta = {}
    result = run.finish()
    assert result["passed"] is (not failed)
    assert decode(encode_report(result), "decode") == result
