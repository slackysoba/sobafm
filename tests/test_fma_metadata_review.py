"""Synthetic counterexamples from PR45 metadata/copy review; no native activity."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import cast

import pytest

from evaluations.fma_validation import limits, metadata
from evaluations.fma_validation.limits import encode_report, json_size
from evaluations.linux_resource.probe import ProbeError


def safe_size(size: int) -> bytes:
    return bytes((size >> 21 & 127, size >> 14 & 127, size >> 7 & 127, size & 127))


def frame(name: bytes, payload: bytes, flags: bytes = b"\0\0") -> bytes:
    return name + len(payload).to_bytes(4, "big") + flags + payload


def tag(data: bytes, version: int = 3) -> bytes:
    return b"ID3" + bytes((version, 0, 0)) + safe_size(len(data)) + data


def inspect_owned(tmp_path: Path, data: bytes) -> dict[str, object]:
    path = tmp_path / "owned-synthetic-tag"
    path.write_bytes(data)
    fd = os.open(path, os.O_RDONLY)
    try:
        return metadata.inspect(fd, len(data))
    finally:
        os.close(fd)


@pytest.fixture
def forbid_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> object:
        pytest.fail("unsupported source reached the Mutagen parser")

    monkeypatch.setattr(importlib.import_module("mutagen.id3"), "ID3", unexpected)


@pytest.mark.parametrize("name", [b"CHAP", b"CTOC"])
@pytest.mark.parametrize("child_case", ["compressed", "count", "depth"])
def test_nested_entry_points_never_reach_parser(
    tmp_path: Path,
    forbid_parser: None,
    name: bytes,
    child_case: str,
) -> None:
    if child_case == "compressed":
        # Compression flag is sufficient: neither payload nor decoder is invoked.
        child = frame(b"TIT2", b"\0\0\x10\0synthetic-compressed-bytes", b"\0\x80")
    elif child_case == "count":
        child = frame(b"TIT2", b"\x03text") * 513
    else:
        child = frame(b"TIT2", b"\x03text")
        for _ in range(16):
            child = frame(b"CHAP", b"child\0" + b"\0" * 16 + child)
    prefix = b"chapter\0" + b"\0" * 16 if name == b"CHAP" else b"toc\0\0\0"
    with pytest.raises(metadata.MetadataRejected, match="unsupported_nested"):
        inspect_owned(tmp_path, tag(frame(name, prefix + child)))


def test_alternate_v24_size_interpretation_is_rejected_before_parser(
    tmp_path: Path,
    forbid_parser: None,
) -> None:
    data = bytearray(320)
    data[:10] = b"ZZZZ" + safe_size(128) + b"\0\0"
    data[138:148] = b"YYYY" + safe_size(172) + b"\0\0"
    data[266:276] = b"TIT2" + safe_size(44) + b"\0\x08"
    data[276:320] = b"\0" * 44
    # Synchsafe path: two opaque frames. Ordinary-size path: unchecked TIT2.
    assert int.from_bytes(data[4:8], "big") == 256
    with pytest.raises(metadata.MetadataRejected, match="unsupported_id3_version"):
        inspect_owned(tmp_path, tag(bytes(data), 4))


@pytest.mark.parametrize(
    ("name", "first", "second"),
    [
        (b"USER", b"\x00engFirst license", b"\x00engDifferent license"),
        (b"WCOP", b"https://example.invalid/one", b"https://example.invalid/two"),
    ],
)
def test_duplicate_notices_are_unresolved_before_hashkey_consolidation(
    tmp_path: Path,
    forbid_parser: None,
    name: bytes,
    first: bytes,
    second: bytes,
) -> None:
    with pytest.raises(metadata.MetadataRejected, match="unresolved_duplicate_frame_id"):
        inspect_owned(tmp_path, tag(frame(name, first) + frame(name, second)))


@pytest.mark.parametrize(("name", "payload"), [(b"USER", b"\x00"), (b"COMM", b"\x00")])
def test_mutagen_silent_known_frame_drop_becomes_explicit_failure(
    tmp_path: Path,
    name: bytes,
    payload: bytes,
) -> None:
    with pytest.raises(metadata.MetadataRejected, match="unresolved_dropped_or_changed_notice"):
        inspect_owned(tmp_path, tag(frame(name, payload)))


def test_commercial_notice_with_logo_fails_without_exporting_binary_key(
    tmp_path: Path,
    forbid_parser: None,
) -> None:
    logo = b"\x89PNG\r\nDO_NOT_EXPORT_SYNTHETIC_LOGO"
    payload = (
        b"\x03USD1.00\0"
        + b"20261201"
        + b"https://example.invalid/contact\0"
        + b"\0Seller\0Terms\0image/png\0"
        + logo
    )
    with pytest.raises(metadata.MetadataRejected) as error:
        inspect_owned(tmp_path, tag(frame(b"COMR", payload)))
    assert str(error.value) == "unsupported_commercial_notice"
    assert "LOGO" not in str(error.value)


@pytest.mark.parametrize("kind", ["escaped_values", "wide_unicode", "aggregate"])
def test_decoded_payload_limits_apply_before_any_parser_allocation(
    tmp_path: Path,
    forbid_parser: None,
    kind: str,
) -> None:
    if kind == "escaped_values":
        data = frame(b"TIT2", b"\x03" + b"\0".join([b"\x01" * 4096] * 64))
    elif kind == "wide_unicode":
        data = frame(b"TIT2", b"\x03" + b"a" * 500_000 + "\U0001f600".encode())
    else:
        data = b"".join(frame(name, b"\x00" + b"a" * 3000) for name in (b"TIT2", b"TPE1", b"TALB"))
    assert len(tag(data)) <= metadata.PARSER_TAG_BYTES
    with pytest.raises(metadata.MetadataRejected, match="metadata_decoded_payload_reservation"):
        inspect_owned(tmp_path, tag(data))


def test_maximum_raw_envelope_keeps_opaque_artwork_out_of_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_artwork(*_args: object, **_kwargs: object) -> object:
        pytest.fail("opaque artwork reached frame-specific decoding")

    artwork = importlib.import_module("mutagen.id3").APIC
    monkeypatch.setattr(artwork, "_fromData", forbidden_artwork)
    text = b"\x00" + b"a" * 4094 + b"\xe9"
    assert len(text) == metadata.FRAME_SOURCE_BYTES
    notices = frame(b"TIT2", text) + frame(b"TPE1", text)
    opaque_size = metadata.PARSER_TAG_BYTES - 10 - len(notices) - 10
    data = tag(notices + frame(b"APIC", b"\xff" * opaque_size))
    assert len(data) == metadata.PARSER_TAG_BYTES
    result = inspect_owned(tmp_path, data)
    assert result["omitted_frame_ids"] == ["APIC"]
    unknown = cast(list[dict[str, object]], result["unknown_frames"])
    assert len(unknown) == 1 and unknown[0]["id"] == "APIC"
    frames = cast(list[dict[str, object]], result["id3v2"])
    assert [item["key"] for item in frames] == ["TIT2", "TPE1"]
    serialized = encode_report(result)
    assert b"\\u00ff" not in serialized and b"\\u00e9" in serialized


@pytest.mark.parametrize("frame_count", [1, 2])
def test_missing_bom_multivalue_repair_is_rejected_before_parser(
    tmp_path: Path,
    forbid_parser: None,
    frame_count: int,
) -> None:
    # Reviewer's exact source-level counterexample: each successful UTF-16
    # repair can retain an error traceback and its shrinking input suffix.
    payload = b"\x01" + (b"a\x00\x00\x00" * 1023)
    assert len(payload) == 4093 <= metadata.FRAME_SOURCE_BYTES
    assert frame_count * len(payload) <= metadata.DECODED_SOURCE_BYTES
    data = b"".join(frame(name, payload) for name in (b"TIT2", b"TPE1")[:frame_count])
    with pytest.raises(metadata.MetadataRejected, match="unsupported_id3_text_encoding"):
        inspect_owned(tmp_path, tag(data))


@pytest.mark.parametrize("name", [b"TIT2", b"WXXX", b"COMM", b"USER", b"IPLS", b"OWNE"])
@pytest.mark.parametrize("encoding", [1, 2, 3, 255])
def test_non_latin1_encoded_frames_never_reach_parser(
    tmp_path: Path,
    forbid_parser: None,
    name: bytes,
    encoding: int,
) -> None:
    with pytest.raises(metadata.MetadataRejected, match="unsupported_id3_text_encoding"):
        inspect_owned(tmp_path, tag(frame(name, bytes((encoding,)) + b"owned text")))


@pytest.mark.parametrize("values", [128, 129])
def test_supported_latin1_multivalue_success_and_projection_rejection(
    tmp_path: Path,
    values: int,
) -> None:
    data = tag(frame(b"TIT2", b"\x00" + b"\x00".join([b"caf\xe9"] * values)))
    if values == 129:
        with pytest.raises(ProbeError, match="metadata_notice_exceeds_supported_bound"):
            inspect_owned(tmp_path, data)
    else:
        result = inspect_owned(tmp_path, data)
        frames = cast(list[dict[str, object]], result["id3v2"])
        fields = cast(dict[str, object], frames[0]["fields"])
        assert fields["text"] == ["caf\xe9"] * values


@pytest.mark.parametrize(
    "value",
    [
        '\x00\x01\b\t\n\f\r"\\\x7f',
        "\u20ac\U0001f600",
        "\ud800",
        {"b": [1, -2, 0.1, True, None], "a": ("x",)},
    ],
)
def test_precomputed_json_size_matches_exact_escaping(value: object) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    assert json_size(value, len(encoded)) == len(encoded)
    assert encode_report(value, len(encoded)) == encoded
    with pytest.raises(ProbeError, match="report_capacity_exceeded"):
        encode_report(value, len(encoded) - 1)


def test_rejected_escaped_output_never_allocates_work_backing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> str:
        pytest.fail("oversized output reached work backing allocation")

    monkeypatch.setattr(limits, "bytearray", forbidden, raising=False)
    with pytest.raises(ProbeError, match="report_capacity_exceeded"):
        encode_report(["\x01" * 4096] * 64, 32_768)


def test_report_node_budget_applies_even_when_output_bytes_would_fit() -> None:
    with pytest.raises(ProbeError, match="report_structure_limit"):
        encode_report([None] * 8192)


@pytest.mark.parametrize("kind", ["depth", "cycle", "integer", "nonfinite", "custom"])
def test_report_preflight_rejects_unbounded_structure_and_scalars(kind: str) -> None:
    value: object
    if kind == "depth":
        value = 1
        for _ in range(14):
            value = [value]
    elif kind == "cycle":
        cycle: list[object] = []
        cycle.append(cycle)
        value = cycle
    elif kind == "integer":
        value = 1 << 129
    elif kind == "nonfinite":
        value = float("nan")
    else:
        value = object()
    with pytest.raises(ProbeError):
        encode_report(value)
