"""Explicit offline evaluation tests. All bytes/IDs are synthetic; no live calls."""

from __future__ import annotations

import hashlib
import json
import signal
import socket
import ssl
import subprocess
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from evaluations.jamendo_stream import probe as subject

FAKE_ID = "synthetic-client-id"
AUDIO = b"synthetic bytes, not an audio file"


def row(**changes: object) -> dict[str, object]:
    track_id = changes.get("id", "168")
    return {
        "id": track_id,
        "name": "Synthetic title",
        "artist_id": "9",
        "artist_name": "Synthetic",
        "album_id": "8",
        "album_name": "Synthetic album",
        "duration": 180,
        "license_ccurl": "http://creativecommons.org/licenses/by-nc-sa/3.0/",
        "shareurl": f"http://www.jamendo.com/track/{track_id}",
        "audiodownload_allowed": False,
        "audio": f"https://{subject.STREAM_HOST}/?trackid={track_id}&format=mp32&from=app-{FAKE_ID}",
        **changes,
    }


def document(record: dict[str, object] | None = None, /, **changes: object) -> bytes:
    return listing([row() if record is None else record], **changes)


def listing(records: list[object], **changes: object) -> bytes:
    return json.dumps(
        {
            "headers": {
                "status": "success",
                "code": 0,
                "error_message": "",
                "warnings": "",
                "results_count": len(records),
                **changes,
            },
            "results": records,
        }
    ).encode()


def wire(
    body: bytes,
    *,
    metadata: bool = False,
    headers: bytes | None = None,
    status: int = 200,
    version: str = "1.0",
) -> bytes:
    if headers is None:
        headers = f"Content-Length: {len(body)}\r\n".encode()
    media = "application/json" if metadata else "audio/mpeg"
    return f"HTTP/{version} {status} Synthetic\r\nContent-Type: {media}\r\n".encode() + (
        headers + b"\r\n" + body
    )


class Receiver:
    def __init__(self, data: bytes, chunk: int = 999_999) -> None:
        self.data = data
        self.offset = 0
        self.chunk = chunk
        self.read_sizes: list[int] = []

    def recv(self, size: int) -> bytes:
        self.read_sizes.append(size)
        part = self.data[self.offset : self.offset + min(size, self.chunk)]
        self.offset += len(part)
        return part


def receive(data: bytes, *, metadata: bool = False, chunk: int = 999_999) -> subject.Transfer:
    transfer = subject.Transfer()
    result = subject.receive(
        cast(ssl.SSLSocket, Receiver(data, chunk)), transfer, metadata=metadata
    )
    assert result == (document() if metadata else b"")
    return transfer


def test_default_invalid_arguments_do_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unexpected credential/network/process operation")

    for name in ("read_id", "probe", "run_authorized"):
        monkeypatch.setattr(subject, name, forbidden)
    assert subject.main([]) == 0
    for args in ([FAKE_ID], ["--send-to-jamendo"], ["--bad", FAKE_ID]):
        assert subject.main(args) == 2
    output = capsys.readouterr()
    assert FAKE_ID not in output.out
    assert output.err == ""


@pytest.mark.parametrize("value", [b"", b"x" * 161, b"bad=id", b"\xff", b"709fa152"])
def test_invalid_id_file_never_connects(
    value: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_bytes(value)

    def forbidden(*_args: object) -> None:
        pytest.fail("invalid ID reached probe")

    monkeypatch.setattr(subject, "probe", forbidden)
    assert subject.run_child(str(path)) == 1
    output = capsys.readouterr()
    assert str(path) not in output.out
    assert output.err == ""


def test_only_regular_bounded_id_file(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_text(FAKE_ID + "\n")
    assert subject.read_id(str(path)) == FAKE_ID
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        subject.read_id(str(link))
    with pytest.raises(OSError):
        subject.read_id(str(tmp_path))


@pytest.mark.parametrize("version", ["1.0", "1.1"])
@pytest.mark.parametrize("chunk", [1, 7, 8192])
@pytest.mark.parametrize("length", [True, False])
def test_real_parser_valid_stream_controls(version: str, chunk: int, length: bool) -> None:
    transfer = receive(wire(AUDIO, version=version, headers=None if length else b""), chunk=chunk)
    assert transfer.body_bytes == len(AUDIO)
    assert transfer.sha256 == hashlib.sha256(AUDIO).hexdigest()
    assert transfer.eof
    assert transfer.framing == ("content_length" if length else "connection_eof")


@pytest.mark.parametrize(
    "headers",
    [
        b"Content-Length: 2\r\nContent-Length: 3\r\n",
        b"Content-Length: 2\r\nContent-Length: 2\r\n",
        b"Content-Length: -1\r\n",
        b"Content-Length: +2\r\n",
        b"Content-Length: 2,2\r\n",
        b"Content-Length: 9999999999999\r\n",
        b"Content-Length: 0\r\n",
        b"Content-Length: 999\r\n",
        b"Content-Length: 1\r\n",
        b"Transfer-Encoding: chunked\r\n",
        b"Transfer-Encoding: identity\r\n",
        b"Content-Encoding: gzip\r\n",
        b"Content-Encoding: identity\r\nContent-Encoding: identity\r\n",
        b"Content-Type: audio/mpeg\r\n",
        b"Content-Range: bytes 0-1/20\r\n",
        b"Bad header\r\n",
        b"Folded: value\r\n continuation\r\n",
    ],
)
def test_real_parser_rejects_ambiguous_or_unsupported_headers(headers: bytes) -> None:
    with pytest.raises(subject.Stopped):
        receive(wire(AUDIO, headers=headers))


@pytest.mark.parametrize("body", [b"1\r\nxXX0\r\n\r\n", b"1\r\nx\r\n0\r\n", b"1\r\nx\r\n0\r\n\r\n"])
def test_prior_review_chunk_cases_fail_closed(body: bytes) -> None:
    with pytest.raises(subject.Stopped, match="unsupported_transfer_encoding"):
        receive(wire(body, headers=b"Transfer-Encoding: chunked\r\n"))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"HTTP/1.0 200 OK\r\n",
        b"HTTP/1.0 200 OK\n\n",
        wire(b""),
        wire(AUDIO).replace(b"1.0", b"2.0", 1),
    ],
)
def test_incomplete_empty_or_bad_protocol(data: bytes) -> None:
    with pytest.raises(subject.Stopped):
        receive(data)


@pytest.mark.parametrize("status", [100, 206, 301, 302, 307, 308, 401, 403, 429, 500])
def test_status_never_followed(status: int) -> None:
    with pytest.raises(subject.Stopped):
        receive(wire(AUDIO, status=status))


def test_total_bytes_and_header_cap_no_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "STREAM_BYTES", 200)
    peer = Receiver(wire(b"x" * 500, headers=b""))
    transfer = subject.Transfer()
    with pytest.raises(subject.Stopped, match="response_limit"):
        subject.receive(cast(ssl.SSLSocket, peer), transfer, metadata=False)
    assert peer.offset == transfer.response_bytes == 200
    assert max(peer.read_sizes) <= subject.BLOCK_BYTES
    peer = Receiver(b"HTTP/1.0 200 OK\r\nX: " + b"x" * subject.HEADER_BYTES)
    monkeypatch.setattr(subject, "STREAM_BYTES", subject.STREAM_BYTES + subject.HEADER_BYTES * 2)
    with pytest.raises(subject.Stopped, match="header_limit"):
        subject.receive(cast(ssl.SSLSocket, peer), subject.Transfer(), metadata=False)
    assert peer.offset == subject.HEADER_BYTES


def test_exact_cap_is_inconclusive_and_declared_oversize_stops_before_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = wire(AUDIO)
    monkeypatch.setattr(subject, "STREAM_BYTES", len(data))
    with pytest.raises(subject.Stopped, match="response_limit"):
        receive(data, chunk=1)
    peer = Receiver(wire(b"x" * 500), chunk=1)
    with pytest.raises(subject.Stopped, match="response_limit"):
        subject.receive(cast(ssl.SSLSocket, peer), subject.Transfer(), metadata=False)
    assert peer.offset == peer.data.index(b"\r\n\r\n") + 4


def test_metadata_response_cap_is_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    assert receive(wire(document(), metadata=True), metadata=True).body_bytes == len(document())
    monkeypatch.setattr(subject, "METADATA_BYTES", 300)
    with pytest.raises(subject.Stopped, match="response_limit"):
        receive(wire(document(), metadata=True), metadata=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not-numeric"},
        {"id": "0"},
        {"id": "1" * 21},
        {"id": 168},
        {"name": ""},
        {"artist_name": None},
        {"artist_id": "x"},
        {"album_id": "", "album_name": "unexpected"},
        {"type": "preview"},
        {"duration": 0},
        {"duration": 481},
        {"duration": True},
        {"duration": 1.5},
        {"duration": "inf"},
        {"license_ccurl": "https://example.test/license"},
        {"license_ccurl": "https://creativecommons.org/licenses/by/3.0/?secret=" + FAKE_ID},
        {"shareurl": "https://www.jamendo.com/track/169"},
        {"audiodownload_allowed": "true"},
    ],
)
def test_metadata_gate_rejects_invalid_candidate(changes: dict[str, object]) -> None:
    with pytest.raises(subject.Stopped):
        subject.validate_candidate(row(**changes), FAKE_ID)


@pytest.mark.parametrize(
    "changes",
    [
        {"code": 5},
        {"code": 6},
        {"code": 11},
        {"code": True},
        {"code": "0"},
        {"status": "failed"},
        {"error_message": FAKE_ID},
        {"warnings": FAKE_ID},
        {"results_count": 0},
        {"results_count": True},
        {"results_count": 2},
    ],
)
def test_application_gate(changes: dict[str, object]) -> None:
    with pytest.raises(subject.Stopped):
        subject.metadata_rows(document(**changes), subject.Results(), 1)


@pytest.mark.parametrize(
    "data", [b"{}", b"null", b"[]", b"{bad", b"\xff", b'{"headers":{},"headers":{},"results":[]}']
)
def test_bad_json(data: bytes) -> None:
    with pytest.raises((subject.Stopped, ValueError)):
        subject.metadata_rows(data, subject.Results(), 1)


@pytest.mark.parametrize(
    "changes", [{}, {"duration": "480"}, {"album_id": "", "album_name": "", "type": "single"}]
)
def test_candidate_controls_are_not_admission(changes: dict[str, object]) -> None:
    report = subject.Report()
    data = document(row(**changes))
    selected = subject.select_candidate(data, FAKE_ID, report)
    assert subject.validate_refresh(data, FAKE_ID, selected, report) == (
        "/?trackid=168&format=mp32&from=app-" + FAKE_ID
    )
    assert report.metadata_validated
    assert report.outcome == "inconclusive"
    assert "Synthetic" not in json.dumps(asdict(report))


@pytest.mark.parametrize("album_id_present", [False, True])
@pytest.mark.parametrize("album_name_present", [False, True])
@pytest.mark.parametrize("explicit_type", [False, True])
def test_single_optional_album_omissions_pass_listing_and_refresh(
    album_id_present: bool, album_name_present: bool, explicit_type: bool
) -> None:
    record = row(album_id="", album_name="")
    if not album_id_present:
        del record["album_id"]
    if not album_name_present:
        del record["album_name"]
    if explicit_type:
        record["type"] = "single"
    report = subject.Report()
    data = document(record)
    selected = subject.select_candidate(data, FAKE_ID, report)
    assert subject.validate_refresh(data, FAKE_ID, selected, report) == selected.target
    assert report.metadata_validated
    assert report.candidate_refusals == []


@pytest.mark.parametrize(
    "album",
    [
        {"album_id": "8"},
        {"album_name": "Synthetic album"},
        {"album_id": "", "album_name": "Synthetic album"},
        {"album_id": "8", "album_name": ""},
        {"album_id": "0", "album_name": "Synthetic album"},
        {"album_id": None},
        {"album_name": None},
        {"album_id": 8, "album_name": "Synthetic album"},
        {"album_id": "8", "album_name": []},
        {"type": "albumtrack"},
        {"type": None},
        {"type": "preview"},
        {"album_id": "8", "album_name": "Synthetic album", "type": "single"},
    ],
)
def test_optional_album_fields_do_not_accept_conflicting_evidence(
    album: dict[str, object],
) -> None:
    record = row()
    del record["album_id"], record["album_name"]
    record.update(album)
    with pytest.raises(subject.CandidateRefused) as refused:
        subject.validate_candidate(record, FAKE_ID)
    assert refused.value.gate == subject.Gate.ALBUM


@pytest.mark.parametrize("explicit_type", [False, True])
def test_coherent_album_track_still_passes(explicit_type: bool) -> None:
    record = row()
    if explicit_type:
        record["type"] = "albumtrack"
    assert subject.validate_candidate(record, FAKE_ID).track_id == "168"


@pytest.mark.parametrize(
    ("record", "gate", "condition"),
    [
        (None, "row", "type"),
        ([], "row", "type"),
        ({}, "identity", "missing"),
        (row(id=168), "identity", "type"),
        (row(id="0"), "identity", "shape"),
        (row(id="1" * 21), "identity", "bound"),
        (row(name=""), "title", "shape"),
        (row(name=None), "title", "type"),
        (row(name="x" * 513), "title", "bound"),
        (row(artist_name="\x00"), "artist", "shape"),
        (row(artist_id="invalid"), "artist", "shape"),
        (row(album_id="0"), "album", "shape"),
        (row(album_name=None), "album", "type"),
        (row(album_name="x" * 513), "album", "bound"),
        (row(duration=481), "duration", "bound"),
        (row(duration=True), "duration", "type"),
        (row(duration="inf"), "duration", "shape"),
        (row(license_ccurl="https://invalid.test/"), "license", "shape"),
        (row(license_ccurl=None), "license", "type"),
        (row(shareurl="https://invalid.test/"), "backlink", "shape"),
        (row(shareurl=None), "backlink", "type"),
        (row(audiodownload_allowed="true"), "download_flag", "type"),
        (row(audio=None), "stream_route", "type"),
        (row(audio="https://invalid.test/"), "stream_route", "shape"),
        (row(audio="x" * 1025), "stream_route", "bound"),
    ],
)
def test_fixed_first_refusal_diagnostics(record: object, gate: str, condition: str) -> None:
    report = subject.Report()
    with pytest.raises(subject.Stopped, match="^no_acceptable_candidate$"):
        subject.select_candidate(listing([record]), FAKE_ID, report)
    assert json.loads(json.dumps(asdict(report)))["candidate_refusals"] == [
        {"ordinal": 1, "gate": gate, "condition": condition}
    ]


@pytest.mark.parametrize(
    ("key", "gate"),
    [
        ("id", "identity"),
        ("name", "title"),
        ("artist_id", "artist"),
        ("artist_name", "artist"),
        ("duration", "duration"),
        ("license_ccurl", "license"),
        ("shareurl", "backlink"),
        ("audiodownload_allowed", "download_flag"),
        ("audio", "stream_route"),
    ],
)
def test_required_omissions_have_safe_missing_diagnostics(key: str, gate: str) -> None:
    record = row()
    del record[key]
    with pytest.raises(subject.CandidateRefused) as refused:
        subject.validate_candidate(record, FAKE_ID)
    assert refused.value.gate == gate
    assert refused.value.condition == subject.Condition.MISSING


def test_ten_first_refusals_only_and_no_refresh_or_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def exchange(_target: str, transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        calls += 1
        assert metadata and calls == 1
        transfer.requests_attempted = 1
        return listing([row(name="", duration=0)] * 10)

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.Report()
    with pytest.raises(subject.Stopped, match="^no_acceptable_candidate$"):
        subject.probe(FAKE_ID, report)
    assert report.rejected_candidates == 10
    assert [item.ordinal for item in report.candidate_refusals] == list(range(1, 11))
    assert all(item.gate == subject.Gate.TITLE for item in report.candidate_refusals)
    assert calls == report.search.requests_attempted == 1
    assert report.metadata == report.stream == subject.Transfer()
    assert report.selected_track_id is None
    assert len(json.dumps(asdict(report))) < subject.REPORT_BYTES
    # An invalid oversized listing never produces an eleventh entry.
    with pytest.raises(subject.Stopped, match="^declared_count_value$"):
        subject.select_candidate(listing([row(name="")] * 11), FAKE_ID, report)
    assert len(report.candidate_refusals) == 10


@pytest.mark.parametrize("injected", [False, True])
def test_refusal_report_never_retains_source_or_exception_text(
    injected: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile = "provider exception https://secret.test/?key=" + FAKE_ID
    record = row(id="987654321", name=hostile + "\x00", details=hostile)

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        assert metadata
        return listing([record] * 10)

    def refuse(_value: object, _key: str) -> subject.Candidate:
        raise subject.Stopped(hostile)

    def read_id(_path: str) -> str:
        return FAKE_ID

    monkeypatch.setattr(subject, "read_id", read_id)
    monkeypatch.setattr(subject, "exchange", exchange)
    if injected:
        monkeypatch.setattr(subject, "validate_candidate", refuse)
    assert subject.run_child("synthetic-path") == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert len(output.out) <= subject.REPORT_BYTES
    for forbidden in (
        hostile,
        FAKE_ID,
        "https://",
        "987654321",
        hashlib.sha256(hostile.encode()).hexdigest(),
    ):
        assert forbidden not in output.out
    report = json.loads(output.out)
    assert report["reason"] == "no_acceptable_candidate"
    assert report["candidate_refusals"] == [
        {"ordinal": ordinal, "gate": "row" if injected else "title", "condition": "shape"}
        for ordinal in range(1, 11)
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://prod-1.storage.jamendo.com/",
        "https://evil.test/",
        "https://127.0.0.1/",
        "https://prod-1.storage.jamendo.com:443/",
        "https://user@prod-1.storage.jamendo.com/",
        "https://prod-1.storage.jamendo.com.evil.test/",
        "https://prod-1.storage.jamendo.com/download/",
        " https://prod-1.storage.jamendo.com/",
        "https://prod-1.storage.jamendo.com/\r\nX: x",
        f"https://{subject.STREAM_HOST}/?trackid=169&format=mp32&from=app-{FAKE_ID}",
        f"https://{subject.STREAM_HOST}/?trackid=168&format=mp31&from=app-{FAKE_ID}",
        f"https://{subject.STREAM_HOST}/?trackid=168&format=mp32&from=app-{FAKE_ID}#",
        f"https://{subject.STREAM_HOST}/?trackid=168&format=mp32&from=app-{FAKE_ID}&extra=x",
        f"https://{subject.STREAM_HOST}/?trackid=168&format=mp32&from=app-wrong",
        f"https://{subject.STREAM_HOST}/?trackid=168&format=mp32&format=mp32",
    ],
)
def test_stream_url_is_fixed(url: str) -> None:
    with pytest.raises((subject.Stopped, ValueError)):
        subject.stream_target(url, FAKE_ID, "168")


def test_one_listing_refresh_stream_and_no_raw_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bool]] = []

    def exchange(target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        calls.append((target, metadata))
        return document() if metadata else b""

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.Report()
    subject.probe(FAKE_ID, report)
    assert report.outcome == "stream_received"
    assert [metadata for _, metadata in calls] == [True, True, False]
    assert parse_qs(urlsplit(calls[0][0]).query) == {
        "client_id": [FAKE_ID],
        "format": ["json"],
        "order": ["id_desc"],
        "limit": ["10"],
        "type": ["single albumtrack"],
        "audioformat": ["mp32"],
    }
    assert parse_qs(urlsplit(calls[1][0]).query) == {
        "client_id": [FAKE_ID],
        "format": ["json"],
        "id": ["168"],
        "limit": ["1"],
        "type": ["single albumtrack"],
        "audioformat": ["mp32"],
    }
    assert report.selected_track_id == "168"
    assert FAKE_ID not in json.dumps(asdict(report))


def test_bad_refresh_prevents_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        calls += 1
        assert metadata
        return document() if calls == 1 else document(code=6)

    monkeypatch.setattr(subject, "exchange", exchange)
    with pytest.raises(subject.Stopped, match="application_rejected"):
        subject.probe(FAKE_ID, subject.Report())
    assert calls == 2


def test_fresh_selection_skips_invalid_rows_in_returned_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    selected = row(id="702")
    responses = iter(
        [listing([row(id="703", duration=481), selected, row(id="701")]), document(selected), b""]
    )

    def exchange(target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        calls.append(target)
        assert metadata == (len(calls) < 3)
        return next(responses)

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.Report()
    subject.probe(FAKE_ID, report)
    assert report.selected_track_id == "702"
    assert report.rejected_candidates == 1
    assert report.candidate_refusals == [
        subject.Refusal(1, subject.Gate.DURATION, subject.Condition.BOUND)
    ]
    assert report.search_results.list_count == 3
    assert parse_qs(urlsplit(calls[1]).query)["id"] == ["702"]
    assert parse_qs(urlsplit(calls[2]).query)["trackid"] == ["702"]
    assert report.outcome == "stream_received"


@pytest.mark.parametrize(
    "records", [[], [None, row(duration=0), row(audio="https://unexpected.test/")]]
)
def test_no_acceptable_candidate_stops_after_listing(
    records: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        calls += 1
        assert metadata and calls == 1
        return listing(records)

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.Report()
    with pytest.raises(subject.Stopped, match="no_acceptable_candidate"):
        subject.probe(FAKE_ID, report)
    assert calls == 1
    assert report.selected_track_id is None
    assert report.rejected_candidates == len(records)
    assert report.metadata.requests_attempted == report.stream.requests_attempted == 0


@pytest.mark.parametrize(
    ("count", "kind", "value", "reason"),
    [
        (None, "null", "not_integer", "declared_count_type"),
        (True, "boolean", "not_integer", "declared_count_type"),
        (1.0, "number", "not_integer", "declared_count_type"),
        (FAKE_ID, "string", "not_integer", "declared_count_type"),
        ([], "list", "not_integer", "declared_count_type"),
        ({"secret": FAKE_ID}, "object", "not_integer", "declared_count_type"),
        (-1, "integer", "negative", "declared_count_value"),
        (999999999999999, "integer", "above_limit", "declared_count_value"),
        (0, "integer", 0, "results_count_mismatch"),
        (2, "integer", 2, "results_count_mismatch"),
    ],
)
def test_safe_declared_count_diagnostics(
    count: object, kind: str, value: int | str, reason: str
) -> None:
    diagnostics = subject.Results()
    with pytest.raises(subject.Stopped, match=f"^{reason}$"):
        subject.metadata_rows(document(results_count=count), diagnostics, 10)
    assert diagnostics.declared_count_type == kind
    assert diagnostics.declared_count_value == value
    assert diagnostics.container_type == "list" and diagnostics.list_count == 1
    assert FAKE_ID not in json.dumps(asdict(diagnostics))


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (None, "null"),
        (True, "boolean"),
        (1, "integer"),
        (1.5, "number"),
        (FAKE_ID, "string"),
        ({}, "object"),
    ],
)
def test_safe_results_container_diagnostics(value: object, kind: str) -> None:
    payload = json.loads(document())
    payload["results"] = value
    diagnostics = subject.Results()
    with pytest.raises(subject.Stopped, match="^results_container$"):
        subject.metadata_rows(json.dumps(payload).encode(), diagnostics, 10)
    assert diagnostics.container_type == kind
    assert diagnostics.list_count is None
    assert diagnostics.declared_count_value == 1
    assert FAKE_ID not in json.dumps(asdict(diagnostics))


@pytest.mark.parametrize("missing", ["count", "results"])
def test_missing_count_and_results_are_distinguished(missing: str) -> None:
    payload = json.loads(document())
    if missing == "count":
        del payload["headers"]["results_count"]
    else:
        del payload["results"]
    diagnostics = subject.Results()
    with pytest.raises(subject.Stopped):
        subject.metadata_rows(json.dumps(payload).encode(), diagnostics, 10)
    assert (
        diagnostics.declared_count_type if missing == "count" else diagnostics.container_type
    ) == "missing"


def test_listing_count_limit_is_not_silently_truncated() -> None:
    diagnostics = subject.Results()
    with pytest.raises(subject.Stopped, match="^results_limit$"):
        subject.metadata_rows(listing([row()] * 11, results_count=10), diagnostics, 10)
    assert diagnostics.list_count == 11
    assert diagnostics.declared_count_value == 10
    assert len(subject.metadata_rows(listing([row()] * 10), subject.Results(), 10)) == 10


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_json_numbers_stop_before_selection(literal: bytes) -> None:
    with pytest.raises(subject.Stopped, match="invalid_metadata"):
        subject.metadata_rows(
            document().replace(b'"duration": 180', b'"duration": ' + literal), subject.Results(), 10
        )


@pytest.mark.parametrize(
    ("refresh", "reason"),
    [
        (document(row(id="701")), "candidate_identity"),
        (document(row(name="Changed title")), "refresh_evidence_changed"),
        (document(row(artist_name="Changed creator")), "refresh_evidence_changed"),
        (document(row(duration=179)), "refresh_evidence_changed"),
        (document(row(details={"notice": "new notice"})), "refresh_evidence_changed"),
        (
            document(row(license_ccurl="https://creativecommons.org/licenses/by/4.0/")),
            "refresh_evidence_changed",
        ),
        (document(row(audio="https://unexpected.test/")), "candidate_stream_route_shape"),
        (listing([]), "refresh_empty"),
        (document(warnings=FAKE_ID), "application_status_or_warning"),
    ],
)
def test_refresh_refusal_cannot_choose_alternative(
    refresh: bytes, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        calls += 1
        assert metadata and calls <= 2
        return listing([row(), row(id="701")]) if calls == 1 else refresh

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.Report()
    with pytest.raises(subject.Stopped, match=f"^{reason}$"):
        subject.probe(FAKE_ID, report)
    assert calls == 2
    assert not report.metadata_validated
    assert report.selected_track_id == "168"
    assert report.stream.requests_attempted == 0


def test_refresh_normalizes_only_duration_and_checked_stream_query() -> None:
    report = subject.Report()
    selected = subject.select_candidate(document(), FAKE_ID, report)
    refreshed = document(
        row(
            duration="180",
            audio=f"https://{subject.STREAM_HOST}/?from=app-{FAKE_ID}&format=mp32&trackid=168",
        )
    )
    assert subject.validate_refresh(refreshed, FAKE_ID, selected, report) == selected.target


@pytest.mark.parametrize("stage", [0, 1, 2])
@pytest.mark.parametrize("failure", ["http_rejected", "response_limit", "timeout"])
def test_all_three_transfers_have_independent_caps_and_no_retry(
    stage: int, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subject, "METADATA_BYTES", 2_000)
    monkeypatch.setattr(subject, "STREAM_BYTES", 3_000)
    calls: list[subject.Transfer] = []

    def exchange(_target: str, transfer: subject.Transfer, *, metadata: bool) -> bytes:
        index = len(calls)
        calls.append(transfer)
        transfer.connection_attempts = transfer.requests_attempted = 1
        assert metadata == (index < 2)
        if index == stage and failure == "timeout":
            raise TimeoutError
        body = document() if metadata else AUDIO
        if index == stage and failure == "response_limit":
            body = b"x" * 4_000
        response = wire(
            body,
            metadata=metadata,
            headers=b"",
            status=403 if index == stage and failure == "http_rejected" else 200,
        )
        return subject.receive(cast(ssl.SSLSocket, Receiver(response)), transfer, metadata=metadata)

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.Report()
    with pytest.raises(TimeoutError if failure == "timeout" else subject.Stopped):
        subject.probe(FAKE_ID, report)
    assert calls == [report.search, report.metadata, report.stream][: stage + 1]
    assert sum(item.requests_attempted for item in calls) == stage + 1
    assert report.search.response_bytes <= 2_000
    assert report.metadata.response_bytes <= 2_000
    assert report.stream.response_bytes <= 3_000
    if failure == "response_limit":
        assert calls[-1].response_bytes == (2_000 if stage < 2 else 3_000)
    assert report.outcome == "inconclusive"


@pytest.mark.parametrize("expired_at", [0, 1, 2, 3])
def test_one_child_alarm_covers_id_read_and_all_requests(
    expired_at: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = signal.setitimer
    timers: list[float] = []
    calls = 0

    def timer(which: int, seconds: float) -> tuple[float, float]:
        timers.append(seconds)
        return original(which, seconds)

    def read_id(_path: str) -> str:
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
        if expired_at == 0:
            signal.raise_signal(signal.SIGALRM)
        return FAKE_ID

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        calls += 1
        assert timers == [117]
        if calls == expired_at:
            signal.raise_signal(signal.SIGALRM)
        return document() if metadata else b""

    monkeypatch.setattr(subject.signal, "setitimer", timer)
    monkeypatch.setattr(subject, "read_id", read_id)
    monkeypatch.setattr(subject, "exchange", exchange)
    assert subject.run_child("synthetic-path") == 1
    assert calls == expired_at
    assert timers == [117, 0]
    assert json.loads(capsys.readouterr().out)["reason"] == "timeout"


def test_safe_diagnostics_report_never_echoes_raw_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def read_id(_path: str) -> str:
        return FAKE_ID

    monkeypatch.setattr(subject, "read_id", read_id)

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        assert metadata
        return document(row(name=FAKE_ID), results_count="https://secret.test/" + FAKE_ID)

    monkeypatch.setattr(subject, "exchange", exchange)
    assert subject.run_child("synthetic-path") == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert len(output.out) <= subject.REPORT_BYTES
    for forbidden in (FAKE_ID, "secret.test", "Synthetic", "https://", "artist", "license"):
        assert forbidden not in output.out
    report = json.loads(output.out)
    assert report["reason"] == "declared_count_type"
    assert report["search_results"]["declared_count_type"] == "string"
    assert report["search_results"]["list_count"] == 1


class PlainTLS:
    def wrap_socket(
        self, connection: socket.socket, *, server_hostname: str, suppress_ragged_eofs: bool
    ) -> socket.socket:
        assert server_hostname in (subject.API_HOST, subject.STREAM_HOST)
        assert not suppress_ragged_eofs
        return connection


@contextmanager
def peer(monkeypatch: pytest.MonkeyPatch, response: bytes) -> Generator[list[bytes]]:
    requests: list[bytes] = []
    errors: list[BaseException] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(3)

        # Send FIN but still observe the client's close; no retry is accepted.
        def simple_serve() -> None:
            try:
                with listener.accept()[0] as connection:
                    connection.settimeout(3)
                    data = bytearray()
                    while not data.endswith(b"\r\n\r\n"):
                        data.extend(connection.recv(4096))
                    requests.append(bytes(data))
                    connection.sendall(response)
                    connection.shutdown(socket.SHUT_WR)
                    assert connection.recv(4096) == b""
            except BaseException as error:
                errors.append(error)

        def addresses(*_args: object, **_kwargs: object) -> list[tuple[int, int, int, str, object]]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", listener.getsockname())]

        monkeypatch.setattr(subject.socket, "getaddrinfo", addresses)
        monkeypatch.setattr(subject, "tls_context", PlainTLS)
        thread = threading.Thread(target=simple_serve, daemon=True)
        thread.start()
        try:
            yield requests
        finally:
            thread.join(4)
            assert not thread.is_alive()
            assert not errors


@pytest.mark.parametrize("response", [wire(AUDIO), wire(AUDIO, status=302), b""])
def test_real_transport_single_request_and_closes(
    monkeypatch: pytest.MonkeyPatch, response: bytes
) -> None:
    with peer(monkeypatch, response) as requests:
        transfer = subject.Transfer()
        with suppress(subject.Stopped):
            subject.exchange("/?synthetic=1", transfer, metadata=False)
    assert transfer.connection_attempts == transfer.requests_attempted == 1
    assert len(requests) == 1
    request = requests[0]
    assert request.startswith(b"GET /?synthetic=1 HTTP/1.0\r\n")
    assert b"Host: prod-1.storage.jamendo.com\r\n" in request
    assert b"User-Agent: sobafm-jamendo-stream/1\r\n" in request
    assert b"Connection: close\r\n" in request
    for forbidden in (b"Range:", b"Referer:", b"Authorization:", b"Cookie:"):
        assert forbidden not in request


def test_no_dns_address_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))  # Not listening: deterministic connect refusal.

        def addresses(*_args: object, **_kwargs: object) -> list[tuple[int, int, int, str, object]]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", listener.getsockname()),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("invalid", 0)),
            ]

        monkeypatch.setattr(subject.socket, "getaddrinfo", addresses)
        transfer = subject.Transfer()
        with pytest.raises(OSError):
            subject.exchange("/", transfer, metadata=False)
        assert transfer.connection_attempts == 1
        assert transfer.requests_attempted == 0


def test_real_tls_verifies_without_keylog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "must-not-exist"
    monkeypatch.setenv("SSLKEYLOGFILE", str(path))
    context = subject.tls_context()
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.keylog_filename is None
    assert not path.exists()


def test_alarm_restored_and_interrupts_work(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = signal.getsignal(signal.SIGALRM)
    with pytest.raises(TimeoutError), subject.deadline():
        signal.raise_signal(signal.SIGALRM)
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


@pytest.mark.parametrize("error", [RuntimeError(FAKE_ID), OSError(FAKE_ID), TimeoutError(FAKE_ID)])
def test_child_errors_and_raw_metadata_never_leak(
    error: Exception, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def read_id(_path: str) -> str:
        return FAKE_ID

    monkeypatch.setattr(subject, "read_id", read_id)

    def fail(_key: str, report: subject.Report) -> None:
        report.metadata_validated = True
        raise error

    monkeypatch.setattr(subject, "probe", fail)
    assert subject.run_child("synthetic-path") == 1
    output = capsys.readouterr()
    assert FAKE_ID not in output.out
    assert output.err == ""
    assert len(output.out) < subject.REPORT_BYTES


def test_accidental_digest_key_echo_is_suppressed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def read_id(_path: str) -> str:
        return FAKE_ID

    monkeypatch.setattr(subject, "read_id", read_id)

    def finish(_key: str, report: subject.Report) -> None:
        report.stream.sha256 = FAKE_ID
        report.outcome = "stream_received"

    monkeypatch.setattr(subject, "probe", finish)
    assert subject.run_child("synthetic-path") == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "report_redacted"


class Child:
    def __init__(self, *, interrupted: bool = False, waits: int = 0) -> None:
        self.returncode: int | None = None
        self.stdout = None
        self.killed = 0
        self.waits = waits
        self.interrupted = interrupted

    def communicate(self, timeout: float) -> tuple[bytes, None]:
        assert 0 < timeout <= subject.WORK_SECONDS
        if self.interrupted:
            raise KeyboardInterrupt
        raise subprocess.TimeoutExpired("synthetic", timeout)

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed += 1

    def wait(self, timeout: float) -> int:
        assert 0 < timeout <= 1
        if self.waits:
            self.waits -= 1
            raise subprocess.TimeoutExpired("synthetic", timeout)
        self.returncode = -9
        return -9


@pytest.mark.parametrize("interrupted", [False, True])
def test_supervisor_kills_reaps_and_keeps_original_failure(
    interrupted: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child = Child(interrupted=interrupted)

    def spawn(args: list[str], **kwargs: object) -> Child:
        assert FAKE_ID not in str(args)
        assert kwargs["env"] == {"LANG": "C.UTF-8"}
        assert kwargs["stderr"] == subprocess.DEVNULL
        return child

    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    assert subject.run_authorized("synthetic-path") == 1
    assert child.killed == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == ("interrupted" if interrupted else "timeout")
    assert result["cleanup_met"] and result["child_reaped"]


def test_late_reaping_preserves_failed_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    child = Child(waits=2)
    ticks = iter([0.0, 1.0, 4.0, 5.0, 6.0])
    monkeypatch.setattr(subject.time, "monotonic", lambda: next(ticks))
    assert not subject.settle(cast(subprocess.Popen[bytes], child))
    assert child.returncode == -9
    assert child.killed == 3


def test_real_supervisor_synthetic_invalid_file_is_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_text("invalid!")
    assert subject.run_authorized(str(path)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "application_id_file"
    assert result["search"]["requests_attempted"] == 0
    assert result["metadata"]["requests_attempted"] == 0
    assert result["stream"]["requests_attempted"] == 0


def test_real_stalled_child_is_killed_and_reaped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def spawn(_args: list[str], **_kwargs: object) -> subprocess.Popen[bytes]:
        child = original(
            [sys.executable, "-I", "-c", "import signal; signal.pause()"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        return child

    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    monkeypatch.setattr(subject, "WORK_SECONDS", 0.1)
    assert subject.run_authorized("unused-synthetic-path") == 1
    assert len(children) == 1 and children[0].returncode == -signal.SIGKILL
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "timeout"
    assert result["child_reaped"] and result["cleanup_met"]


def test_sigterm_cancels_supervisor_but_restores_handler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class TerminatedChild(Child):
        def communicate(self, timeout: float) -> tuple[bytes, None]:
            signal.raise_signal(signal.SIGTERM)
            pytest.fail("SIGTERM was not handled")

    child = TerminatedChild()

    def spawn(_args: list[str], **_kwargs: object) -> Child:
        return child

    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    assert subject.run_authorized("synthetic-path") == 1
    assert child.returncode == -9
    assert signal.getsignal(signal.SIGTERM) == previous
    assert json.loads(capsys.readouterr().out)["reason"] == "interrupted"


def test_signal_during_spawn_cannot_lose_child(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child = Child()

    def spawn(_args: list[str], **_kwargs: object) -> Child:
        signal.raise_signal(signal.SIGINT)  # Delivered only after child handle assignment.
        return child

    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    assert subject.run_authorized("synthetic-path") == 1
    assert child.returncode == -9 and child.killed == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "interrupted"


def test_partial_stream_failure_preserves_hash_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    peer = Receiver(wire(AUDIO, headers=b"Content-Length: 999\r\n"))
    transfer = subject.Transfer()
    with pytest.raises(subject.Stopped, match="invalid_framing"):
        subject.receive(cast(ssl.SSLSocket, peer), transfer, metadata=False)
    assert transfer.body_bytes == len(AUDIO)
    assert transfer.sha256 == hashlib.sha256(AUDIO).hexdigest()
    assert transfer.eof


def test_late_cleanup_does_not_replace_child_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailedChild(Child):
        def communicate(self, timeout: float) -> tuple[bytes, None]:
            self.returncode = 1
            return b'{"outcome":"inconclusive","reason":"http_rejected"}', None

    def spawn(_args: list[str], **_kwargs: object) -> Child:
        return FailedChild()

    def late_settle(_child: subprocess.Popen[bytes]) -> bool:
        return False

    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    monkeypatch.setattr(subject, "settle", late_settle)
    assert subject.run_authorized("synthetic-path") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "http_rejected"
    assert result["cleanup_met"] is False
