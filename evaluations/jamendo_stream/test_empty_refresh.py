"""Offline regressions for the fixed-ID, metadata-only comparison."""

from __future__ import annotations

import json
import signal
import socket
import ssl
from dataclasses import asdict
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from evaluations.jamendo_stream import probe as subject
from evaluations.jamendo_stream.test_probe import FAKE_ID, Receiver, listing, peer, wire


def synthetic_id(_path: str) -> str:
    return FAKE_ID


@pytest.fixture(autouse=True)
def no_source_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("No external address lookup is permitted by these offline tests")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.mark.parametrize(
    ("counts", "diagnosis"),
    [([1], "mp32_found"), ([0, 1], "default_only_found"), ([0, 0], "both_empty")],
)
def test_exact_metadata_comparison_stops_without_audio(
    counts: list[int], diagnosis: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries: list[dict[str, list[str]]] = []

    def exchange(target: str, transfer: subject.Transfer, *, metadata: bool) -> bytes:
        assert metadata
        parts = urlsplit(target)
        assert parts.path == "/v3.0/tracks/"
        query = parse_qs(parts.query, strict_parsing=True)
        expected = {
            "client_id": [FAKE_ID],
            "format": ["json"],
            "type": ["single albumtrack"],
            "id": [subject.DIAGNOSTIC_TRACK_ID],
            "limit": ["1"],
        }
        if not queries:
            expected["audioformat"] = ["mp32"]
        assert query == expected
        count = counts[len(queries)]
        queries.append(query)
        transfer.connection_attempts = transfer.requests_attempted = 1
        # No admission fields needed: these observations must never grant playback.
        body = listing([{"id": subject.DIAGNOSTIC_TRACK_ID}] if count else [])
        return subject.receive(
            cast(ssl.SSLSocket, Receiver(wire(body, metadata=True))), transfer, metadata=True
        )

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.Report()
    subject.diagnose_empty_refresh(FAKE_ID, report)
    assert len(queries) == len(counts)
    assert report.outcome == "metadata_diagnosed"
    assert report.refresh_diagnosis == diagnosis
    assert not report.metadata_validated
    assert report.selected_track_id is None
    assert report.search == report.metadata == report.stream == subject.Transfer()
    assert [step.results.list_count for step in report.diagnostic_steps] == counts
    assert sum(step.transfer.requests_attempted for step in report.diagnostic_steps) == len(counts)
    if len(queries) == 2:
        assert {key: value for key, value in queries[0].items() if key != "audioformat"} == queries[
            1
        ]


@pytest.mark.parametrize("stage", [0, 1])
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (listing([], code=5), "application_rejected"),
        (listing([], warnings="private-warning"), "application_status_or_warning"),
        (listing([], results_count=True), "declared_count_type"),
        (listing([], results_count=1), "results_count_mismatch"),
        (listing([{"id": "168"}]), "candidate_identity"),
        (listing([{"id": int(subject.DIAGNOSTIC_TRACK_ID)}]), "candidate_identity"),
        (listing([None]), "invalid_metadata"),
        (listing([{}, {}]), "declared_count_value"),
    ],
)
def test_invalid_response_never_continues_or_admits(
    stage: int, body: bytes, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        assert metadata
        calls += 1
        return body if calls == stage + 1 else listing([])

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.Report()
    with pytest.raises(subject.Stopped, match="^" + reason + "$"):
        subject.diagnose_empty_refresh(FAKE_ID, report)
    assert calls == stage + 1
    assert report.refresh_diagnosis is None
    assert not report.metadata_validated
    assert report.stream.requests_attempted == 0


@pytest.mark.parametrize("stage", [0, 1])
def test_metadata_cap_stops_without_next_call(stage: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "METADATA_BYTES", 1024)
    calls = 0

    def exchange(_target: str, transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        assert metadata
        calls += 1
        body = b"x" * 2048 if calls == stage + 1 else listing([])
        return subject.receive(
            cast(ssl.SSLSocket, Receiver(wire(body, metadata=True, headers=b""))),
            transfer,
            metadata=True,
        )

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.Report()
    with pytest.raises(subject.Stopped, match="^response_limit$"):
        subject.diagnose_empty_refresh(FAKE_ID, report)
    assert calls == stage + 1
    assert report.diagnostic_steps[-1].transfer.response_bytes == 1024
    assert report.stream.requests_attempted == 0


@pytest.mark.parametrize("stage", [1, 2])
def test_diagnostic_child_keeps_one_alarm_and_safe_report(
    stage: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    timers: list[float] = []
    original = signal.setitimer
    calls = 0

    def timer(which: int, seconds: float) -> tuple[float, float]:
        timers.append(seconds)
        return original(which, seconds)

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        assert metadata
        calls += 1
        assert timers == [117]
        if calls == stage:
            signal.raise_signal(signal.SIGALRM)
        return listing([])

    monkeypatch.setattr(subject, "read_id", synthetic_id)
    monkeypatch.setattr(subject.signal, "setitimer", timer)
    monkeypatch.setattr(subject, "exchange", exchange)
    assert subject.run_child("synthetic-path", diagnostic=True) == 1
    assert calls == stage
    assert timers == [117, 0]
    output = capsys.readouterr().out
    assert FAKE_ID not in output
    assert json.loads(output)["reason"] == "timeout"


def test_report_discards_source_fields_and_does_not_parse_audio(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subject, "read_id", synthetic_id)

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        assert metadata
        return listing([{"id": subject.DIAGNOSTIC_TRACK_ID, "audio": FAKE_ID, "name": "private"}])

    monkeypatch.setattr(subject, "exchange", exchange)
    assert subject.run_child("synthetic-path", diagnostic=True) == 0
    output = capsys.readouterr().out
    assert FAKE_ID not in output and "private" not in output
    assert len(output) < subject.REPORT_BYTES
    assert json.loads(output)["refresh_diagnosis"] == "mp32_found"


def test_real_http_redirect_stops_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    with peer(monkeypatch, wire(b"", metadata=True, status=302)) as requests:
        report = subject.Report()
        with pytest.raises(subject.Stopped, match="^http_rejected$"):
            subject.diagnose_empty_refresh(FAKE_ID, report)
    assert len(requests) == len(report.diagnostic_steps) == 1
    assert b"Host: api.jamendo.com\r\n" in requests[0]
    assert report.stream == subject.Transfer()


def test_cli_selects_diagnostic_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bool]] = []

    def authorized(path: str, diagnostic: bool = False) -> int:
        calls.append((path, diagnostic))
        return 7

    monkeypatch.setattr(subject, "run_authorized", authorized)
    assert subject.main(["--diagnose-empty-refresh", "synthetic-path"]) == 7
    assert calls == [("synthetic-path", True)]


def test_real_diagnostic_supervisor_invalid_input_never_connects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_text("invalid!")
    assert subject.run_authorized(str(path), diagnostic=True) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "application_id_file"
    assert result["child_reaped"] and result["cleanup_met"]
    assert result["diagnostic_steps"] == []
    assert result["stream"] == asdict(subject.Transfer())
