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
from evaluations.fma_validation.ingress import Ingress, Opaque, decode
from evaluations.fma_validation.limits import REPORT_BYTES, encode_report
from evaluations.fma_validation.process import OwnedHelper, ProcessPin
from evaluations.linux_resource.probe import ProbeError
from tests.test_fma_failure_receipts import consumer as consumer
from tests.test_fma_failure_receipts import terminal
from tests.test_fma_validation_packet import packet_path as packet_path

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
    actual = Opaque.retire
    attempts = 0

    def fail_once(owner: Opaque) -> None:
        nonlocal attempts
        if owner is helper.errors:
            attempts += 1
            if attempts == 1:
                raise ProbeError("synthetic_retirement_failure")
        actual(owner)

    monkeypatch.setattr(Opaque, "retire", fail_once)
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


# #66 receiver payload gate. Oracles below are computed from the decoded value,
# independently of the scanner's own accounting.


def oracle_width(value: str) -> int:
    top = max(map(ord, value), default=0)
    return 1 if top < 256 else 2 if top < 65_536 else 4


def oracle_strings(value: object) -> int:
    """Decoded non-key string backing (PEP 393 width x length)."""
    if isinstance(value, str):
        return oracle_width(value) * len(value)
    if isinstance(value, list):
        return sum(oracle_strings(item) for item in cast(list[object], value))
    if isinstance(value, dict):
        return sum(oracle_strings(item) for item in cast(dict[str, object], value).values())
    return 0


def oracle_peak(wire: bytes) -> int:
    text = wire.decode("utf-8")
    raw = max((ord(char) for char in text), default=0)
    conversion = (1 if raw < 128 else 2 if raw < 256 else 3 if raw < 65_536 else 6) * len(wire)
    return max(conversion, oracle_width(text) * len(text) + oracle_strings(json.loads(text)))


def forbid_materialization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module.json, "loads", forbidden_decode)
    monkeypatch.setattr(module, "str", forbidden_decode, raising=False)


def config_record(paths: list[str], sources: list[str] | None = None) -> dict[str, object]:
    """A complete selected `docker inspect` configuration with chosen path fields."""
    fields = iter(paths)
    host: dict[str, object] = {
        "Init": True,
        "ReadonlyRootfs": True,
        "Privileged": False,
        "NetworkMode": "none",
        "CgroupnsMode": "private",
        "Memory": 1,
        "MemorySwap": 1,
        "PidsLimit": 64,
        "NanoCpus": 1,
        "CapDrop": ["ALL"],
        "CapAdd": None,
        "SecurityOpt": ["no-new-privileges=true"],
        "RestartPolicy": {"Name": "no"},
        "LogConfig": {"Type": "none"},
    }
    mounts: list[dict[str, object]] = []
    for index in range(4):
        source = sources[index] if sources is not None and index < len(sources) else None
        row: dict[str, object] = {
            "Type": "bind",
            "Source": source if source is not None else next(fields, "/s"),
            "Destination": next(fields, "/d"),
            "RW": False,
        }
        mounts.append(row)
    return {
        "id": "a" * 64,
        "name": "/case",
        "image": "sha256:" + "b" * 64,
        "labels": {
            "org.sobafm.project": "sobafm",
            "org.sobafm.issue": "44",
            "org.sobafm.packet": "c" * 64,
        },
        "state": {
            "Running": True,
            "Pid": 2,
            "Status": "running",
            "ExitCode": 0,
            "OOMKilled": False,
        },
        "host": host,
        "user": "1000:1000",
        "path": next(fields, "/python"),
        "args": [next(fields, "-B") for _ in range(8)],
        "workdir": next(fields, "/"),
        "mounts": mounts,
    }


def volume_wire(created: str, padding: int = 0) -> bytes:
    record = {
        "Name": "volume",
        "CreatedAt": created,
        "Mountpoint": "/var/lib/docker/volumes/volume/_data",
        "Labels": {"org.sobafm.issue": "44"},
    }
    body = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
    return b" " * padding + body


def test_gate_is_twice_the_wire_bound_plus_margin() -> None:
    assert module.DECODED_BYTES == 2 * REPORT_BYTES + 1024 == 132_096
    assert module.PATH.count == 4096 and module.TIMESTAMP.count == REPORT_BYTES


def test_wide_raw_config_rejected_before_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 18 raw-astral paths: strict UTF-8 conversion alone reaches 6n.
    size = 3000
    wire = json.dumps(config_record(["/\U00010000" + "x" * size] * 18), ensure_ascii=False).encode()
    while len(wire) > REPORT_BYTES:
        size -= 10
        wire = json.dumps(
            config_record(["/\U00010000" + "x" * size] * 18), ensure_ascii=False
        ).encode()
    assert oracle_peak(wire) > module.DECODED_BYTES
    forbid_materialization(monkeypatch)
    with pytest.raises(ProbeError, match="ingress_schema"):
        decode(wire, "config")


def test_whitespace_padded_wide_volume_rejected_before_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = volume_wire("\xe9Ā\U00010000")
    wire = volume_wire("\xe9Ā\U00010000", REPORT_BYTES - len(body))
    assert len(wire) == REPORT_BYTES and oracle_peak(wire) == 6 * REPORT_BYTES
    forbid_materialization(monkeypatch)
    with pytest.raises(ProbeError, match="ingress_schema"):
        decode(wire, "volume")


def test_escaped_wide_path_rejected_by_decoded_width_not_wire_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The wire is pure ASCII, but each escaped astral widens a whole 4,096-point path.
    path = "/\U00010000" + "x" * 4094
    count = 18
    wire = json.dumps(config_record([path] * count)).encode("ascii")
    while len(wire) > REPORT_BYTES:
        count -= 1
        wire = json.dumps(config_record([path] * count)).encode("ascii")
    assert oracle_peak(wire) > module.DECODED_BYTES
    forbid_materialization(monkeypatch)
    with pytest.raises(ProbeError, match="ingress_schema"):
        decode(wire, "config")


def test_one_escaped_wide_path_is_still_admitted() -> None:
    record = config_record(["/\U00010000" + "x" * 4094])
    wire = json.dumps(record).encode("ascii")
    assert oracle_peak(wire) <= module.DECODED_BYTES
    assert decode(wire, "config") == record


@pytest.mark.parametrize("created", ["2026-11-02T00:00:00Z", "\xff" * 4096])
def test_ascii_and_latin1_documents_pass_at_full_wire(created: str) -> None:
    body = volume_wire(created)
    wire = volume_wire(created, REPORT_BYTES - len(body))
    assert len(wire) == REPORT_BYTES and oracle_peak(wire) <= module.DECODED_BYTES
    assert decode(wire, "volume")["CreatedAt"] == created
    with pytest.raises(ProbeError, match="helper_report_bound"):
        decode(b" " + wire, "volume")


def test_ascii_metadata_notices_pass_at_full_wire() -> None:
    def wire(count: int, padding: int = 0) -> bytes:
        frame = {"id": "COMM", "key": "COMM", "fields": {"text": ["x" * 4096] * count}}
        record = {**metadata(), "id3v2": [frame], "case_binding": NIGHT_OWL.record()}
        body = json.dumps({**record, "helper_pid": 2}, separators=(",", ":")).encode("ascii")
        return b"{" + b" " * padding + body[1:] + b"\n"

    count = 1
    while len(wire(count + 1)) <= REPORT_BYTES:
        count += 1
    full = wire(count, REPORT_BYTES - len(wire(count)))
    assert len(full) == REPORT_BYTES and oracle_peak(full[:-1]) <= module.DECODED_BYTES
    owner = Ingress("metadata")
    owner.feed(full)
    owner.scan(final=True)
    assert owner.error is None and owner.records[0].value is not None


def test_conversion_term_boundary_is_exact() -> None:
    # One raw U+0100 gives a 3n strict-conversion peak; 3 x 44,032 equals the gate.
    body = volume_wire("Ā")
    limit = module.DECODED_BYTES // 3
    assert 3 * limit == module.DECODED_BYTES
    accepted = volume_wire("Ā", limit - len(body))
    rejected = volume_wire("Ā", limit + 1 - len(body))
    assert oracle_peak(accepted) == module.DECODED_BYTES
    assert decode(accepted, "volume")["CreatedAt"] == "Ā"
    with pytest.raises(ProbeError, match="ingress_schema"):
        decode(rejected, "volume")


def test_three_maximal_astral_sources_are_admitted() -> None:
    source = "/\U00010000" + "x" * (4095 - 5)
    assert len(source.encode()) == 4095
    record = config_record([], [source] * 3)
    wire = json.dumps(record, ensure_ascii=False).encode()
    assert oracle_peak(wire) <= module.DECODED_BYTES
    assert decode(wire, "config") == record


def family_strings(rule: module.Wire, seen: set[int]) -> list[module.Wire]:
    """Every string rule reachable by `Scan.value`, including dynamic variants."""
    if id(rule) in seen:
        return []
    seen.add(id(rule))
    dynamic = {
        "pin_or_null": [module.PIN],
        "reader_or_null": [module.READER],
        "helper_or_null": [module.HELPER],
        "decode_or_null": [module.DECODE],
        "metadata_or_null": [module.METADATA_RESULT],
        "strings_or_null": [module.text(32)],
        "notice": [module.text(4096, "latin1"), module.Wire("notice_item")],
        "notice_item": [module.text(4096, "latin1")],
    }
    children = [*rule.fields.values(), *([rule.item] if rule.item is not None else [])]
    children += dynamic.get(rule.kind, [])
    found = [rule] if rule.kind == "string" else []
    for child in children:
        found += family_strings(child, seen)
    return found


@pytest.mark.parametrize("family", ["metadata", "rejection", "admission", "decode"])
def test_helper_families_have_no_path_or_timestamp_domain(family: str) -> None:
    strings = family_strings(module.FAMILIES[family], set())
    assert strings
    assert all(rule.domain not in ("path", "timestamp") for rule in strings)
    assert max(rule.count for rule in strings) <= 4096


def owner_wire(packet_path: Path, tmp_path: Path, created_at: str, padding: int = 0) -> bytes:
    """A settled, rejected night-owl owner terminal with a chosen intake timestamp."""
    import threading
    from datetime import UTC, datetime

    from evaluations.fma_validation.limits import CaseClock
    from evaluations.fma_validation.packet import load
    from sobafm.fma_intake import LocalIntake
    from tests.test_fma_validation_packet import NOW, ROOT

    packet = load(packet_path, ROOT, now=NOW)
    clock = CaseClock(packet.whole_end, packet.expires, lambda: NOW + 1, lambda: 10.0)
    init = ProcessPin(1, 1, 0, "/init", 1, 1, 1000, "/")
    intake = LocalIntake(
        tmp_path,
        None,
        packet.request,
        utc_now=lambda: datetime.fromtimestamp(NOW, UTC),
        monotonic=lambda: 10.0,
    )
    intake.phase = "rejected"
    intake.thread = threading.Thread()
    intake.thread.start()
    intake.thread.join(3)
    clock.cancel()
    validation = ValidationConsumer(intake, init, PIN, clock, packet.case)
    report = intake.report()
    report["created_at"] = created_at
    record: dict[str, object] = {
        "schema": packet.case.schema("owner"),
        "case_binding": packet.case.record(),
        "packet_sha256": packet.fingerprint,
        "passed": False,
        "failure": "validation_owner_failed",
        "init": init.record(),
        "owner": PIN.record(),
        "intake": report,
        "reader": None,
        "validation": validation.receipt(),
        "memory_event_delta": {},
        "cleanup": clock.receipt(),
        "catalog_admitted": False,
        "container_exit_observed": False,
    }
    wire = json.dumps(record, separators=(",", ":")).encode("ascii")
    return b"{" + b" " * padding + wire[1:] + b"\n"


def wide_stamp(size: int) -> str:
    # One escaped non-ordinary separator at position 11 widens the whole timestamp.
    return "2026-11-02Ā" + "0" * (size - 11)


def test_decoded_term_boundary_is_exact_for_wide_owner_timestamp(
    packet_path: Path, tmp_path: Path
) -> None:
    probe = owner_wire(packet_path, tmp_path, wide_stamp(40_000))
    stamp = wide_stamp(40_000 + (module.DECODED_BYTES - oracle_peak(probe[:-1])) // 3)
    base = owner_wire(packet_path, tmp_path, stamp)
    padding = module.DECODED_BYTES - oracle_peak(base[:-1])
    assert 0 <= padding < 3
    results: list[bool] = []
    for extra in (0, 1):
        wire = owner_wire(packet_path, tmp_path, stamp, padding + extra)
        assert oracle_peak(wire[:-1]) == module.DECODED_BYTES + extra
        assert len(wire) <= REPORT_BYTES
        owner = Ingress("owner")
        owner.feed(wire)
        owner.scan(final=True)
        results.append(owner.records[0].value is not None)
    assert results == [True, False]


def test_wide_owner_timestamp_keeps_envelope_and_adopts_clock(
    packet_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evaluations.fma_validation.controller import Command, Controller, Journal
    from evaluations.fma_validation.limits import CaseClock
    from evaluations.fma_validation.packet import load
    from tests.test_fma_validation_packet import NOW, ROOT

    probe = owner_wire(packet_path, tmp_path, wide_stamp(11))
    wire = owner_wire(packet_path, tmp_path, wide_stamp(REPORT_BYTES - len(probe) + 11))
    assert len(wire) == REPORT_BYTES and oracle_peak(wire[:-1]) > module.DECODED_BYTES

    class NoLaunch(Command):
        def _launch(self) -> None:
            pass

    class CaptureJournal(Journal):
        def __init__(self) -> None:
            self.data = {}

    packet = load(packet_path, ROOT, now=NOW)
    command = NoLaunch(["synthetic-owner-output"])
    command.launch()
    command.worker.join(3)
    command.owner_output = True
    command.ingress.feed(wire)
    outer = Controller(packet, CaptureJournal())
    outer.clock = CaseClock(packet.whole_end, packet.expires, lambda: NOW + 2, lambda: 10.0)
    monkeypatch.setattr(module, "materialize", forbidden_decode)
    outer.capture_owner(command, final=True)
    record = command.ingress.records[0]
    assert record.value is None and record.envelope is not None
    assert set(record.envelope) == module.OWNER_ENVELOPE
    assert command.ingress.error == "ingress_schema"
    assert outer.owner_report is None
    assert outer.clock.cleanup_start_utc == NOW + 1
    assert outer.clock.timing_unknown and outer.clock.missed
    assert outer.journal.data["owner_terminal_error"] == "invalid_or_missing_owner_terminal"
