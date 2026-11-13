"""Offline production-contract qualification, with synthetic bounded HTTP only."""

from __future__ import annotations

import json
import signal
import socket
import ssl
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import parse_qsl, urlsplit

import pytest

from evaluations.jamendo_stream import probe as subject
from evaluations.jamendo_stream.test_empty_refresh import synthetic_id
from evaluations.jamendo_stream.test_probe import (
    FAKE_ID,
    Child,
    Receiver,
    document,
    listing,
    row,
    wire,
)
from sobafm import jamendo_catalog
from sobafm.jamendo_admission import Candidate, Policy, Purpose


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("No external lookup permitted")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def responses(monkeypatch: pytest.MonkeyPatch, payloads: list[bytes]) -> list[str]:
    calls: list[str] = []

    def exchange(target: str, transfer: subject.Transfer, *, metadata: bool) -> bytes:
        assert metadata  # Any audio path fails, including after successful admission.
        assert len(calls) < len(payloads)
        body = payloads[len(calls)]
        calls.append(target)
        transfer.connection_attempts = transfer.requests_attempted = 1
        return subject.receive(
            cast(ssl.SSLSocket, Receiver(wire(body, metadata=True))), transfer, metadata=True
        )

    monkeypatch.setattr(subject, "read_id", synthetic_id)
    monkeypatch.setattr(subject, "exchange", exchange)
    return calls


@pytest.mark.parametrize(
    "changed",
    [
        {"notices": ["Additional attribution"]},
        {"credits": ["Additional attribution"]},
        {
            "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
            "licenses": {"ccnc": False, "ccsa": False},
        },
    ],
)
def test_fresh_decision_and_immutable_credits(
    changed: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    responses(monkeypatch, [document(), document(row(**changed))])
    refresh = jamendo_catalog.refresh
    observed: list[tuple[Candidate, jamendo_catalog.Refreshed]] = []

    def capture(
        request: jamendo_catalog.Request,
        payload: bytes | jamendo_catalog.Refusal,
        ranked: Candidate,
        policy: Policy,
        observed_at: datetime,
    ) -> jamendo_catalog.Refreshed | jamendo_catalog.Refusal:
        before = asdict(ranked)
        result = refresh(request, payload, ranked, policy, observed_at)
        assert isinstance(result, jamendo_catalog.Refreshed)
        assert asdict(ranked) == before
        observed.append((ranked, result))
        return result

    monkeypatch.setattr(jamendo_catalog, "refresh", capture)
    report = subject.Report()
    subject.qualify_metadata(FAKE_ID, report)
    old, fresh = observed[0]
    assert fresh.candidate is not old and fresh.candidate.decision is not old.decision
    assert old.decision.purpose == Purpose.RANKING
    assert fresh.candidate.decision.purpose == Purpose.REFRESH
    assert fresh.candidate.decision.refreshed_at >= old.decision.refreshed_at
    assert fresh.candidate.recording.credits != old.recording.credits
    assert report.previous_decision_matches is False
    assert report.metadata_validated and report.outcome == "metadata_admitted"
    assert report.evidence_changes is not None
    assert report.evidence_changes[subject.EvidenceCategory.RIGHTS_NOTICES] == "changed"
    assert report.stream == subject.Transfer()


@pytest.mark.parametrize(
    "changed",
    [
        {},
        {"audio": "https://prod-2.storage.jamendo.com/?format=mp32&trackid=168&from=new"},
        {"stats": {"arbitrary": [1, None]}, "image": "incidental", "audiodownload_allowed": True},
        {"license_ccurl": "https://creativecommons.org/licenses/by-nc-sa/3.0/"},
        {"id": 168, "artist_id": 9, "album_id": 8},
        {"audio": f"https://{subject.STREAM_HOST}/?from=app-{FAKE_ID}&format=mp32&trackid=168"},
    ],
)
def test_production_equivalence_is_independent_of_structural_categories(
    changed: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    responses(monkeypatch, [document(), document(row(**changed))])
    report = subject.Report()
    subject.qualify_metadata(FAKE_ID, report)
    assert report.previous_decision_matches is True
    assert report.metadata_validated
    assert set(report.evidence_changes or {}) == set(subject.EvidenceCategory)
    assert report.selected_track_id is None and report.stream == subject.Transfer()


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"id": "169"}, "identity_mismatch"),
        ({"name": "Changed title"}, "ranked_content_changed"),
        ({"duration": 181}, "ranked_content_changed"),
        ({"description": "Changed musical description"}, "ranked_content_changed"),
        ({"creators": ["Additional creator"]}, "ranked_content_changed"),
        ({"details": ["preview"]}, "excluded_recording_form"),
        ({"notices": ["permission required"]}, "unresolved_rights_or_notice"),
        (
            {"notices": ["https://creativecommons.org/licenses/by/4.0/"]},
            "unresolved_rights_or_notice",
        ),
        ({"licenses": {"ccnc": False}}, "unresolved_rights_or_notice"),
        ({"rights_warnings": ["pending"]}, "unresolved_rights_or_notice"),
        ({"notices": ["unreadable\u202e"]}, "unreadable_or_oversized_credits"),
        ({"credits": ["x" * 1025]}, "unreadable_or_oversized_credits"),
        ({"license_ccurl": "https://creativecommons.org/licenses/by/9.0/"}, "unsupported_license"),
        ({"audio": "https://unapproved.invalid/?trackid=168&format=mp32"}, "invalid_stream_route"),
        ({"private-unknown-field": {"private-value": "hidden"}}, "unsupported_evidence"),
        ({"musicinfo": {"unknown": "hidden"}}, "unsupported_evidence"),
        ({"duration": "180"}, "invalid_metadata"),
        ({"type": "unsupported"}, "unsupported_evidence"),
    ],
)
def test_production_refusal_never_tries_another_candidate(
    changed: dict[str, object],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = responses(monkeypatch, [listing([row(), row(id="170")]), document(row(**changed))])
    assert subject.run_child("synthetic", admission=True) == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert len(calls) == 2 and report["reason"] == reason
    assert not report["metadata_validated"]
    assert report["previous_decision_matches"] is None
    assert report["evidence_changes"] is None
    assert report["stream"] == asdict(subject.Transfer())
    for forbidden in ("private-", "https://", FAKE_ID, "Synthetic", "hidden"):
        assert forbidden not in output


def test_exact_requests_first_eligible_and_original_alarm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = responses(
        monkeypatch, [listing([row(type="preview"), row(), row(id="170")]), document()]
    )
    timers: list[float] = []
    setitimer = signal.setitimer

    def timer(which: int, seconds: float) -> tuple[float, float]:
        timers.append(seconds)
        return setitimer(which, seconds)

    monkeypatch.setattr(subject.signal, "setitimer", timer)
    assert subject.run_child("synthetic", admission=True) == 0
    report = json.loads(capsys.readouterr().out)
    common = [
        ("client_id", FAKE_ID),
        ("format", "json"),
        ("type", "single albumtrack"),
        ("audioformat", "mp32"),
        ("include", "musicinfo licenses"),
    ]
    assert [parse_qsl(urlsplit(call).query) for call in calls] == [
        common + [("search", "jazz"), ("limit", "10"), ("offset", "0")],
        common + [("id", "168"), ("limit", "1")],
    ]
    assert all(urlsplit(call).path == "/v3.0/tracks/" for call in calls)
    assert timers == [117, 0]
    assert report["admission_refusals"] == [[1, "excluded_recording_form"]]
    assert report["selected_track_id"] is None
    assert report["stream"] == asdict(subject.Transfer())
    assert report["search"]["response_bytes"] + report["metadata"]["response_bytes"] < 512_000


def test_listing_copies_first_production_request_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = responses(monkeypatch, [document(), document()])
    parameters = jamendo_catalog.Query.parameters
    request = jamendo_catalog.SearchWindow.request
    issued: list[jamendo_catalog.Request] = []

    def query_parameters(query: jamendo_catalog.Query) -> tuple[tuple[str, str], ...]:
        assert query == jamendo_catalog.Query(search="jazz")
        # A synthetic builder addition must survive: no parallel request builder.
        return parameters(query) + (("builder-marker", "synthetic"),)

    def first_request(window: jamendo_catalog.SearchWindow) -> jamendo_catalog.Request:
        assert not issued
        result = request(window)
        assert isinstance(result, jamendo_catalog.Request)
        assert dict(result.parameters)["limit"] == "100"
        assert dict(result.parameters)["offset"] == "0"
        issued.append(result)
        return result

    def no_receive(*_args: object, **_kwargs: object) -> None:
        pytest.fail("The bounded copy must not be supplied to SearchWindow.receive")

    monkeypatch.setattr(jamendo_catalog.Query, "parameters", query_parameters)
    monkeypatch.setattr(jamendo_catalog.SearchWindow, "request", first_request)
    monkeypatch.setattr(jamendo_catalog.SearchWindow, "receive", no_receive)
    subject.qualify_metadata(FAKE_ID, subject.Report())
    assert len(issued) == 1 and len(calls) == 2
    expected = replace(
        issued[0],
        parameters=tuple(
            (key, "10" if key == "limit" else value) for key, value in issued[0].parameters
        ),
    )
    assert parse_qsl(urlsplit(calls[0]).query) == [("client_id", FAKE_ID), *expected.parameters]
    assert urlsplit(calls[0]).path == urlsplit(expected.endpoint).path
    assert dict(issued[0].parameters)["limit"] == "100"


@pytest.mark.parametrize(
    "reason", [jamendo_catalog.Failure.DISABLED, jamendo_catalog.Failure.REQUEST]
)
def test_builder_refusal_stops_before_transport(
    reason: jamendo_catalog.Failure,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = responses(monkeypatch, [])

    def refuse(_window: jamendo_catalog.SearchWindow) -> jamendo_catalog.Refusal:
        return jamendo_catalog.Refusal(reason)

    monkeypatch.setattr(jamendo_catalog.SearchWindow, "request", refuse)
    assert subject.run_child("synthetic", admission=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == reason.value
    assert calls == [] and not report["metadata_validated"]
    assert report["search"] == report["metadata"] == report["stream"] == asdict(subject.Transfer())


@pytest.mark.parametrize("count", [10, 11])
def test_listing_keeps_ten_row_cap(count: int, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = responses(monkeypatch, [listing([row()] * count), document()])
    report = subject.Report()
    if count == 10:
        subject.qualify_metadata(FAKE_ID, report)
        assert report.metadata_validated and len(calls) == 2
    else:
        with pytest.raises(subject.Stopped):
            subject.qualify_metadata(FAKE_ID, report)
        assert not report.metadata_validated and len(calls) == 1
    assert report.stream == subject.Transfer()


@pytest.mark.parametrize("stage", [1, 2])
@pytest.mark.parametrize("payload", [listing([]), b"invalid", document(warnings="source-warning")])
def test_empty_or_invalid_response_stops(
    stage: int,
    payload: bytes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = responses(monkeypatch, [document()] * (stage - 1) + [payload])
    assert subject.run_child("synthetic", admission=True) == 1
    report = json.loads(capsys.readouterr().out)
    if payload == listing([]):
        assert report["reason"] == (
            "no_acceptable_candidate" if stage == 1 else "recording_missing"
        )
    assert len(calls) == stage and report["stream"] == asdict(subject.Transfer())


def test_unknown_envelope_evidence_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = json.loads(document())
    bad["private-extra"] = "private-evidence"
    responses(monkeypatch, [json.dumps(bad).encode()])
    with pytest.raises(subject.Stopped, match="^malformed_response$"):
        subject.qualify_metadata(FAKE_ID, subject.Report())


def test_listing_uses_production_gates_without_legacy_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    single = row(duration=600, shareurl="https://www.jamendo.com/track/168/a-single")
    for optional in ("album_id", "album_name", "audiodownload_allowed"):
        del single[optional]
    responses(monkeypatch, [document(single), document(single)])
    report = subject.Report()
    subject.qualify_metadata(FAKE_ID, report)
    assert report.metadata_validated and report.previous_decision_matches is True
    assert report.rejected_candidates == 0 and report.stream == subject.Transfer()


@pytest.mark.parametrize("stage", [1, 2])
def test_exact_cap_is_not_crossed(stage: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "METADATA_BYTES", 2048)
    calls = responses(monkeypatch, [document()] * (stage - 1) + [b"x" * 4096])
    report = subject.Report()
    with pytest.raises(subject.Stopped, match="^response_limit$"):
        subject.qualify_metadata(FAKE_ID, report)
    assert len(calls) == stage
    assert (report.search if stage == 1 else report.metadata).response_bytes == 2048
    assert report.stream == subject.Transfer()


def test_interrupted_refresh_has_no_identity_or_audio(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    responses(monkeypatch, [document()])

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(jamendo_catalog, "refresh_request", interrupted)
    assert subject.run_child("synthetic", admission=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == "interrupted"
    assert report["selected_track_id"] is None
    assert report["stream"] == asdict(subject.Transfer())


def test_real_cli_keeps_supervision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_text("invalid!")
    assert subject.main(["--qualify-metadata", str(path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == "application_id_file"
    assert report["child_reaped"] and report["cleanup_met"]
    assert report["stream"] == asdict(subject.Transfer())


def test_isolated_child_loads_checkout_without_installed_package() -> None:
    # -S removes site-packages too: the prospective evaluator's pure production
    # imports must work with the existing bare interpreter and reviewed source.
    program = """
import json, pathlib, runpy, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('external lookup')
socket.getaddrinfo = forbidden
module = runpy.run_path(sys.argv[1])
qualify = module['qualify_metadata']
body = sys.stdin.buffer.read()
calls = []
def exchange(target, transfer, *, metadata):
    assert metadata and len(calls) < 2
    calls.append(True)
    return body
qualify.__globals__['exchange'] = exchange
report = module['Report']()
qualify('synthetic-client-id', report)
import sobafm.jamendo_admission
expected = pathlib.Path(sys.argv[1]).resolve().parents[2] / 'src/sobafm/jamendo_admission.py'
assert pathlib.Path(sobafm.jamendo_admission.__file__).resolve() == expected
assert report.outcome == 'metadata_admitted' and len(calls) == 2
assert report.stream.requests_attempted == 0
print('checkout admission passed')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", program, str(Path(subject.__file__).resolve())],
        input=document(),
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"checkout admission passed\n"


def test_mode_retains_owner_through_late_reaping(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child = Child(waits=2, interrupted=True)
    commands: list[list[str]] = []

    def spawn(command: list[str], **_kwargs: object) -> Child:
        commands.append(command)
        return child

    ticks = iter([0.0, 0.1, 0.2, 1.0, 4.0, 5.0, 6.0])
    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    monkeypatch.setattr(subject.time, "monotonic", lambda: next(ticks))
    assert subject.main(["--qualify-metadata", "synthetic-path"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert commands[0][-1] == "admission"
    assert "admission=sys.argv[3] == 'admission'" in commands[0][4]
    assert child.returncode == -9 and child.killed == 3
    assert report["child_reaped"] and not report["cleanup_met"]
    assert report["reason"] == "interrupted"
