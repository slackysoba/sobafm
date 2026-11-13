"""Offline single-listing envelope diagnosis: no source or credential access."""

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
from evaluations.jamendo_stream.test_probe import FAKE_ID, Child, document, listing
from sobafm import jamendo_admission, jamendo_catalog
from tests.test_jamendo_catalog import nested, response


@pytest.fixture(autouse=True)
def forbid_later_work(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Envelope diagnosis must not enter admission or external lookup")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for name in ("parse_listing", "evaluate"):
        monkeypatch.setattr(jamendo_admission, name, forbidden)
    for name in ("refresh", "refresh_request"):
        monkeypatch.setattr(jamendo_catalog, name, forbidden)
    monkeypatch.setattr(subject, "metadata_rows", forbidden)
    monkeypatch.setattr(subject, "admission_categories", forbidden)


@pytest.mark.parametrize(
    ("body", "condition", "reason"),
    [
        (document(), None, None),
        (listing([]), None, None),
        (response([{"private-key": nested(6)}]), "depth", "malformed_response"),
        (response([{str(n): None for n in range(65)}]), "object_members", "malformed_response"),
        (response([{"k" * 65: None}]), "key_length", "malformed_response"),
        (response([{"private-key": [None] * 101}]), "list_length", "malformed_response"),
        (response([{"private-key": "v" * 32_769}]), "string_length", "malformed_response"),
        (
            b'{"headers":{},"results":[],"private-key":"private-value"}',
            "unexpected_root_keys",
            "malformed_response",
        ),
        (
            response([{}], **{"private-header": "private-value"}),
            "unexpected_header_keys",
            "malformed_response",
        ),
        (response([{}], results_fullcount=True), "total_type", "malformed_response"),
        (response([{}], results_fullcount=0), "total_value", "malformed_response"),
        (b'{"private-key":1,"private-key":2}', "json", "malformed_response"),
        (b"\xff", "json", "malformed_response"),
        (b"{}", "root_shape", "malformed_response"),
        (response([{}], status="succeed"), "provider", "provider_failure"),
        (response([{}], warnings="private-warning"), "warning", "provider_warning"),
    ],
)
def test_single_request_stops_before_admission_and_redacts(
    body: bytes,
    condition: str | None,
    reason: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = responses(monkeypatch, [body])
    assert subject.run_child("synthetic", envelope_only=True) == (0 if reason is None else 1)
    output = capsys.readouterr().out
    report = json.loads(output)
    assert len(calls) == 1
    assert calls[0] == (
        "/v3.0/tracks/?client_id="
        + FAKE_ID
        + "&format=json&type=single+albumtrack&audioformat=mp32&include=musicinfo+licenses"
        + "&limit=10&order=id_desc&ccnc=true&durationbetween=1_480"
    )
    assert report["envelope_condition"] == condition and report["reason"] == reason
    assert report["outcome"] == ("envelope_passed" if reason is None else "inconclusive")
    assert report["metadata"] == report["stream"] == asdict(subject.Transfer())
    assert report["search_results"] == asdict(subject.Results())
    assert report["selected_track_id"] is None and not report["metadata_validated"]
    assert report["admission_refusals"] == report["candidate_refusals"] == []
    assert report["rejected_candidates"] == 0
    assert report["evidence_changes"] is report["previous_decision_matches"] is None
    for forbidden in (FAKE_ID, "private-", "Synthetic", "https://", "32769"):
        assert forbidden not in output
    assert len(output) < 4_000 < subject.REPORT_BYTES


def test_exact_cap_and_alarm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = responses(monkeypatch, [b"x" * subject.METADATA_BYTES])
    timers: list[float] = []
    original = signal.setitimer

    def timer(which: int, seconds: float) -> tuple[float, float]:
        timers.append(seconds)
        return original(which, seconds)

    monkeypatch.setattr(subject.signal, "setitimer", timer)
    assert subject.run_child("synthetic", envelope_only=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert len(calls) == 1 and timers == [117, 0]
    assert report["search"]["response_bytes"] <= 256_000
    assert report["envelope_condition"] is None
    assert report["stream"] == asdict(subject.Transfer())


def test_cli_keeps_supervision_and_reaps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_text("invalid!")
    assert subject.main(["--diagnose-envelope", str(path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == "application_id_file"
    assert report["child_reaped"] and report["cleanup_met"]
    assert report["envelope_condition"] is None


def test_mode_keeps_original_late_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    child = Child(waits=2, interrupted=True)
    commands: list[list[str]] = []

    def spawn(command: list[str], **_kwargs: object) -> Child:
        commands.append(command)
        return child

    ticks = iter([0.0, 0.1, 0.2, 1.0, 4.0, 5.0, 6.0])
    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    monkeypatch.setattr(subject.time, "monotonic", lambda: next(ticks))
    assert subject.main(["--diagnose-envelope", "synthetic-path"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert commands[0][-1] == "envelope"
    assert "envelope_only=sys.argv[3] == 'envelope'" in commands[0][4]
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
qualify('synthetic-client-id', report, envelope_only=True)
root = pathlib.Path(sys.argv[1]).resolve().parents[2] / 'src'
names = {name for name in sys.modules if name == 'sobafm' or name.startswith('sobafm.')}
assert names == {'sobafm', 'sobafm.catalog', 'sobafm.credits',
                 'sobafm.jamendo_admission', 'sobafm.jamendo_catalog'}
for name in names:
    actual = pathlib.Path(sys.modules[name].__file__).resolve()
    relative = pathlib.Path(*name.split('.'))
    suffix = relative / '__init__.py' if name == 'sobafm' else relative.with_suffix('.py')
    expected = root / suffix
    assert actual == expected
assert report.outcome == 'envelope_passed' and len(calls) == 1
assert report.metadata.requests_attempted == report.stream.requests_attempted == 0
print('checkout envelope passed')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", program, str(Path(subject.__file__).resolve())],
        input=document(),
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"checkout envelope passed\n"
