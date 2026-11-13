"""Synthetic single-listing diagnosis of the unmodified production admission rules."""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from evaluations.jamendo_stream import probe as subject
from evaluations.jamendo_stream.test_admission import responses
from evaluations.jamendo_stream.test_probe import FAKE_ID, Child, document, listing, row
from sobafm import jamendo_admission, jamendo_catalog


@pytest.fixture(autouse=True)
def forbid_later_work(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Admission diagnosis must not select, refresh or contact a source")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(jamendo_admission, "Candidate", forbidden)
    for name in ("refresh", "refresh_request"):
        monkeypatch.setattr(jamendo_catalog, name, forbidden)
    for name in ("metadata_rows", "admission_categories", "select_candidate", "probe"):
        monkeypatch.setattr(subject, name, forbidden)


def test_all_boundaries_complete_rows_once_and_safe_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        row(content_id_free={"private-value": "https://private.invalid/secret"}),
        row(**{"content_id_free": False, "private-key": "private-value"}),
        row(**{"private-key": "private-value"}),
        row(musicinfo={"private-key": "private-value"}),
        row(musicinfo={"tags": {"private-key": ["private-value"]}}),
        row(licenses={"private-key": "private-value"}),
        row(details=["x" * 161]),
        row(type="unrecognized"),
        row(),  # Even a passing row cannot select, fingerprint, refresh or open audio.
        row(type="preview"),
    ]
    calls = responses(monkeypatch, [listing(rows)])
    parsed: list[object] = []
    original = jamendo_admission.parse_listing

    def parse(
        value: object,
        expected_id: str | None = None,
        *,
        diagnostic: jamendo_admission.AdmissionDiagnostic | None = None,
    ) -> jamendo_admission.Listing:
        parsed.append(value)
        return original(value, expected_id, diagnostic=diagnostic)

    monkeypatch.setattr(jamendo_admission, "parse_listing", parse)
    assert subject.run_child("synthetic", admission_diagnostic=True) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert parsed == rows and len(calls) == 1
    assert calls[0] == (
        "/v3.0/tracks/?client_id="
        + FAKE_ID
        + "&format=json&type=single+albumtrack&audioformat=mp32&include=musicinfo+licenses"
        + "&limit=10&order=id_desc&ccnc=true&durationbetween=1_480"
    )
    expected = [
        ("unsupported_evidence", "row_fields", "documented_only"),
        ("unsupported_evidence", "row_fields", "documented_and_other"),
        ("unsupported_evidence", "row_fields", "other_only"),
        ("unsupported_evidence", "musicinfo_fields", None),
        ("unsupported_evidence", "tag_groups", None),
        ("unsupported_evidence", "license_fields", None),
        ("unsupported_evidence", "details_bound", None),
        ("unsupported_evidence", "recording_type", None),
        ("provisional_listing", None, None),
        ("excluded_recording_form", None, None),
    ]
    assert report["admission_diagnostics"] == [
        {"ordinal": n, "reason": reason, "boundary": boundary, "unknown_row_keys": keys}
        for n, (reason, boundary, keys) in enumerate(expected, start=1)
    ]
    assert report["outcome"] == "admission_diagnosed" and report["reason"] is None
    assert report["metadata"] == report["stream"] == asdict(subject.Transfer())
    assert report["search_results"] == report["refresh_results"] == asdict(subject.Results())
    assert report["selected_track_id"] is None and not report["metadata_validated"]
    assert report["evidence_changes"] is report["previous_decision_matches"] is None
    assert report["admission_refusals"] == report["candidate_refusals"] == []
    for forbidden in (FAKE_ID, "private", "content_id_free", "Synthetic", "https://", "161"):
        assert forbidden not in output
    assert len(output) < 4_000 < subject.REPORT_BYTES


@pytest.mark.parametrize(
    ("body", "condition", "reason"),
    [
        (listing([]), None, None),
        (listing([row()] * 11), "results_count", "malformed_response"),
        (b"invalid-private-source", "json", "malformed_response"),
        (document(warnings="private-warning"), "warning", "provider_warning"),
    ],
)
def test_empty_or_bad_envelope_never_enters_row_parser(
    body: bytes,
    condition: str | None,
    reason: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Empty or refused envelope must not enter admission")

    monkeypatch.setattr(jamendo_admission, "parse_listing", forbidden)
    calls = responses(monkeypatch, [body])
    assert subject.run_child("synthetic", admission_diagnostic=True) == (0 if reason is None else 1)
    output = capsys.readouterr().out
    report = json.loads(output)
    assert len(calls) == 1 and report["admission_diagnostics"] == []
    assert report["envelope_condition"] == condition and report["reason"] == reason
    assert report["metadata"] == report["stream"] == asdict(subject.Transfer())
    assert "private" not in output


def test_exact_byte_cap_and_original_alarm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = responses(monkeypatch, [b"x" * subject.METADATA_BYTES])
    timers: list[float] = []
    original = signal.setitimer

    def timer(which: int, seconds: float) -> tuple[float, float]:
        timers.append(seconds)
        return original(which, seconds)

    monkeypatch.setattr(subject.signal, "setitimer", timer)
    assert subject.run_child("synthetic", admission_diagnostic=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert len(calls) == 1 and timers == [117, 0]
    assert report["reason"] == "response_limit"
    assert report["search"]["response_bytes"] == 256_000
    assert report["admission_diagnostics"] == []
    assert report["metadata"] == report["stream"] == asdict(subject.Transfer())


@pytest.mark.parametrize(
    ("error", "reason"),
    [(KeyboardInterrupt(), "interrupted"), (RuntimeError("private"), "internal")],
)
def test_interrupt_and_exception_text_are_not_reported(
    error: BaseException,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failed(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(jamendo_admission, "parse_listing", failed)
    calls = responses(monkeypatch, [document()])
    assert subject.run_child("synthetic", admission_diagnostic=True) == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert len(calls) == 1 and report["reason"] == reason
    assert "private" not in output
    assert report["metadata"] == report["stream"] == asdict(subject.Transfer())


def test_cli_keeps_supervision_and_reaps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_text("invalid!")
    assert subject.main(["--diagnose-admission", str(path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == "application_id_file"
    assert report["child_reaped"] and report["cleanup_met"]
    assert report["admission_diagnostics"] == []


def test_mode_keeps_original_late_cleanup_failure(
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
    assert subject.main(["--diagnose-admission", "synthetic-path"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert commands[0][-1] == "admission_diagnostic"
    assert "admission_diagnostic=sys.argv[3] == 'admission_diagnostic'" in commands[0][4]
    assert child.returncode == -9 and child.killed == 3
    assert report["child_reaped"] and not report["cleanup_met"]
    assert report["reason"] == "interrupted"


def test_isolated_checkout_import_binding() -> None:
    program = """
import pathlib, runpy, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('unexpected work')
socket.getaddrinfo = forbidden
module = runpy.run_path(sys.argv[1])
qualify = module['qualify_metadata']
body = sys.stdin.buffer.read()
calls = []
def exchange(target, transfer, *, metadata):
    assert metadata and not calls
    calls.append(True)
    return body
qualify.__globals__['exchange'] = exchange
report = module['Report']()
qualify('synthetic-client-id', report, admission_diagnostic=True)
root = pathlib.Path(sys.argv[1]).resolve().parents[2] / 'src'
names = {name for name in sys.modules if name == 'sobafm' or name.startswith('sobafm.')}
assert names == {'sobafm', 'sobafm.catalog', 'sobafm.credits',
                 'sobafm.jamendo_admission', 'sobafm.jamendo_catalog'}
for name in names:
    actual = pathlib.Path(sys.modules[name].__file__).resolve()
    relative = pathlib.Path(*name.split('.'))
    suffix = relative / '__init__.py' if name == 'sobafm' else relative.with_suffix('.py')
    assert actual == root / suffix
assert report.outcome == 'admission_diagnosed' and len(calls) == 1
assert report.admission_diagnostics[0].reason == 'provisional_listing'
assert report.metadata.requests_attempted == report.stream.requests_attempted == 0
print('checkout admission diagnosis passed')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", program, str(Path(subject.__file__).resolve())],
        input=document(),
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"checkout admission diagnosis passed\n"
