"""Synthetic metadata comparisons; no external lookup, audio or retained rows."""

from __future__ import annotations

import hashlib
import json
import signal
import socket
import ssl
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from evaluations.jamendo_stream import probe as subject
from evaluations.jamendo_stream.test_empty_refresh import synthetic_id
from evaluations.jamendo_stream.test_probe import FAKE_ID, Receiver, document, listing, row, wire


@pytest.fixture(autouse=True)
def no_source_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("No external address lookup is permitted")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.mark.parametrize(
    ("changes", "category"),
    [
        ({"name": "new title"}, "identity_credits"),
        ({"album_name": "new album"}, "album_type"),
        ({"duration": 179}, "duration"),
        ({"notices": ["new notice"]}, "rights_notices"),
        ({"details": {"nested-private-key": "new detail"}}, "details"),
        (
            {"audio": f"https://{subject.STREAM_HOST}/?trackid=168&format=mp32&from=new"},
            "stream_route",
        ),
        ({"audiodownload_allowed": True}, "download"),
        ({"unrecognized-private-key": {"private-key": "new value"}}, "other"),
    ],
)
def test_each_fixed_partition_preserves_whole_row_refusal(
    changes: dict[str, object], category: str
) -> None:
    report = subject.Report()
    selected = subject.select_candidate(document(), report)
    with pytest.raises(subject.Stopped, match="^refresh_evidence_changed$"):
        subject.validate_refresh(document(row(**changes)), selected, report)
    assert report.evidence_changes is not None
    assert set(report.evidence_changes) == set(subject.EvidenceCategory)
    assert {key for key, value in report.evidence_changes.items() if value == "changed"} == {
        category
    }
    assert not report.metadata_validated
    assert len(selected.category_evidence) == 8
    assert all(len(value) == 64 for value in selected.category_evidence)


@pytest.mark.parametrize("operation", ["add", "remove", "change"])
def test_all_documented_fields_and_catch_all_are_bound(operation: str) -> None:
    fields = [key for group in subject.EVIDENCE_FIELDS.values() for key in group]
    assert len(fields) == len(set(fields))  # No overlap or dropped partition.
    for key in [*fields, "unknown-private-key"]:
        category = next(
            (category for category, keys in subject.EVIDENCE_FIELDS.items() if key in keys),
            subject.EvidenceCategory.OTHER,
        )
        before: dict[str, object] = {} if operation == "add" else {key: None}
        after: dict[str, object] = {} if operation == "remove" else {key: "private-value"}
        left, right = subject.category_fingerprints(before), subject.category_fingerprints(after)
        assert [
            c for c, a, b in zip(subject.EvidenceCategory, left, right, strict=True) if a != b
        ] == [category]


def test_multiple_changes_and_nested_key_reordering() -> None:
    report = subject.Report()
    selected = subject.select_candidate(document(row(details={"b": 2, "a": 1})), report)
    with pytest.raises(subject.Stopped, match="^refresh_evidence_changed$"):
        subject.validate_refresh(
            document(row(details={"a": 1, "b": 2}, notices=["notice"], surprise=None)),
            selected,
            report,
        )
    assert report.evidence_changes is not None
    assert {k for k, v in report.evidence_changes.items() if v == "changed"} == {
        "rights_notices",
        "other",
    }


def test_normalization_and_whole_row_digest_are_unchanged() -> None:
    original = row(details={"b": 2, "a": 1})
    selected = subject.validate_candidate(original)
    normalized = {
        **original,
        "audio": f"/?format=mp32&from=app-{FAKE_ID}&trackid=168",
        "duration": 180,
    }
    expected = hashlib.sha256(
        json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    ).hexdigest()
    assert selected.evidence == expected
    report = subject.Report()
    subject.validate_refresh(
        document(
            row(
                duration="180",
                details={"a": 1, "b": 2},
                audio=f"https://{subject.STREAM_HOST}/?from=app-{FAKE_ID}&format=mp32&trackid=168",
            )
        ),
        selected,
        report,
    )
    assert report.evidence_changes == dict.fromkeys(subject.EvidenceCategory, "unchanged")
    assert report.metadata_validated


@pytest.mark.parametrize("changed", [False, True])
def test_metadata_mode_exact_two_requests_and_no_audio_even_when_equal(
    changed: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, list[str]]] = []
    timers: list[float] = []
    original_timer = signal.setitimer

    def timer(which: int, seconds: float) -> tuple[float, float]:
        timers.append(seconds)
        return original_timer(which, seconds)

    def exchange(target: str, transfer: subject.Transfer, *, metadata: bool) -> bytes:
        assert metadata
        assert timers == [117]
        parts = urlsplit(target)
        assert parts.path == "/v3.0/tracks/"
        query = parse_qs(parts.query, strict_parsing=True)
        expected = {
            "client_id": [FAKE_ID],
            "format": ["json"],
            "type": ["single albumtrack"],
            "audioformat": ["mp32"],
        }
        expected.update(
            {"limit": ["10"], "order": ["id_desc"], "ccnc": ["true"], "durationbetween": ["1_480"]}
            if not calls
            else {"id": ["168"], "limit": ["1"]}
        )
        assert query == expected
        calls.append(query)
        assert len(calls) <= 2
        transfer.connection_attempts = transfer.requests_attempted = 1
        body = (
            listing([None, row(), row(id="169")])
            if len(calls) == 1
            else document(
                row(**({"private-provider-key": "private-provider-value"} if changed else {}))
            )
        )
        return subject.receive(
            cast(ssl.SSLSocket, Receiver(wire(body, metadata=True))), transfer, metadata=True
        )

    monkeypatch.setattr(subject, "read_id", synthetic_id)
    monkeypatch.setattr(subject.signal, "setitimer", timer)
    monkeypatch.setattr(subject, "exchange", exchange)
    assert subject.run_child("synthetic", metadata_only=True) == (1 if changed else 0)
    output = capsys.readouterr().out
    result = json.loads(output)
    assert len(calls) == 2 and timers == [117, 0]
    assert result["stream"] == asdict(subject.Transfer())
    assert result["selected_track_id"] is None
    assert result["rejected_candidates"] == 1
    assert result["metadata_validated"] == (not changed)
    assert result["outcome"] == ("inconclusive" if changed else "metadata_compared")
    assert result["reason"] == ("refresh_evidence_changed" if changed else None)
    assert result["evidence_changes"]["other"] == ("changed" if changed else "unchanged")
    assert result["search"]["response_bytes"] + result["metadata"]["response_bytes"] < 512_000
    assert len(output) < subject.REPORT_BYTES
    for forbidden in (FAKE_ID, "private-provider", "Synthetic", "https://", "app-"):
        assert forbidden not in output
    for fingerprint in subject.validate_candidate(row()).category_evidence:
        assert fingerprint not in output


@pytest.mark.parametrize("stage", [1, 2])
@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (b"not json", "invalid_response"),
        (document(row(duration=False)), "candidate_duration_type"),
        (listing([], warnings="private-warning"), "application_status_or_warning"),
        (listing([None]), "invalid_metadata"),
        (listing([]), "refresh_empty"),
    ],
)
def test_refusals_do_not_compare_or_continue(
    stage: int,
    bad: bytes,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        assert metadata
        calls += 1
        assert calls <= stage
        return bad if calls == stage else document()

    monkeypatch.setattr(subject, "read_id", synthetic_id)
    monkeypatch.setattr(subject, "exchange", exchange)
    assert subject.run_child("synthetic", metadata_only=True) == 1
    result = json.loads(capsys.readouterr().out)
    if stage == 1 and reason in ("candidate_duration_type", "invalid_metadata", "refresh_empty"):
        reason = "no_acceptable_candidate"
    assert result["reason"] == reason
    assert calls == stage and result["evidence_changes"] is None
    assert result["stream"] == asdict(subject.Transfer())


def test_cli_and_real_supervisor_keep_metadata_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_text("invalid!")
    assert subject.main(["--diagnose-metadata-changes", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "application_id_file"
    assert result["child_reaped"] and result["cleanup_met"]
    assert result["evidence_changes"] is None
    assert result["stream"] == asdict(subject.Transfer())


def test_cli_propagates_mode_to_supervised_child(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def launch(command: list[str], **_kwargs: object) -> subprocess.Popen[bytes]:
        commands.append(command)
        raise OSError("synthetic launch failure")

    monkeypatch.setattr(subject.subprocess, "Popen", launch)
    assert subject.main(["--diagnose-metadata-changes", "synthetic-path"]) == 1
    assert len(commands) == 1
    assert commands[0][-1] == "metadata"
    assert "metadata_only=sys.argv[3] == 'metadata'" in commands[0][4]


def test_interruption_after_selection_withholds_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    select = subject.select_candidate

    def interrupted(data: bytes, report: subject.Report) -> subject.Candidate:
        select(data, report)
        raise TimeoutError

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        assert metadata
        return document()

    monkeypatch.setattr(subject, "read_id", synthetic_id)
    monkeypatch.setattr(subject, "select_candidate", interrupted)
    monkeypatch.setattr(subject, "exchange", exchange)
    assert subject.run_child("synthetic", metadata_only=True) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "timeout"
    assert result["selected_track_id"] is None
    assert result["evidence_changes"] is None
    assert result["stream"] == asdict(subject.Transfer())


@pytest.mark.parametrize("stage", [1, 2])
def test_metadata_cap_stops_without_audio(stage: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "METADATA_BYTES", 2048)
    calls = 0

    def exchange(_target: str, transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        assert metadata
        calls += 1
        body = b"x" * 4096 if calls == stage else document()
        return subject.receive(
            cast(ssl.SSLSocket, Receiver(wire(body, metadata=True, headers=b""))),
            transfer,
            metadata=True,
        )

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.Report()
    with pytest.raises(subject.Stopped, match="^response_limit$"):
        subject.probe(FAKE_ID, report, metadata_only=True)
    assert calls == stage
    assert (report.search if stage == 1 else report.metadata).response_bytes == 2048
    assert report.stream == subject.Transfer()
    assert report.evidence_changes is None
