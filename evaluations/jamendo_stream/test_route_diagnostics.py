"""Synthetic route refusal evidence; no source traffic or real credentials."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from evaluations.jamendo_stream import probe as subject
from evaluations.jamendo_stream.test_probe import FAKE_ID, document, listing, row

BASE = f"https://{subject.STREAM_HOST}/"
QUERY = f"trackid=168&format=mp32&from=app-{FAKE_ID}"
VALID = BASE + "?" + QUERY


def refused(url: str) -> dict[str, object]:
    with pytest.raises(subject.CandidateRefused, match="^candidate_stream_route_shape$") as error:
        subject.stream_target(url, FAKE_ID, "168")
    assert error.value.route_diagnostic is not None
    return asdict(error.value.route_diagnostic)


@pytest.mark.parametrize(
    ("url", "first", "facts"),
    [
        ("é" + VALID, "text_shape", {"non_ascii": "present", "scheme": "empty"}),
        (" " + VALID, "text_shape", {"whitespace": "present", "control": "absent"}),
        ("\t" + VALID, "text_shape", {"whitespace": "present", "control": "present"}),
        ("\x00" + VALID, "text_shape", {"whitespace": "absent", "control": "present"}),
        (VALID + "\x7f", "text_shape", {"control": "present", "application_binding": "mismatch"}),
        (VALID + "\\", "application_binding", {"backslash": "present"}),
        ("https://[broken/?" + QUERY + "#", "url_parser", {
            "url_parser": "failure", "query_parser": "not_evaluated", "scheme": "not_evaluated",
            "authority": "not_evaluated", "path": "not_evaluated", "fragment": "present",
        }),
        (VALID.replace("https:", "http:"), "scheme", {"scheme": "http"}),
        (VALID.replace("https:", "ftp:"), "scheme", {"scheme": "other"}),
        (VALID.removeprefix("https:"), "scheme", {"scheme": "empty"}),
        (VALID.replace(subject.STREAM_HOST, "invalid.test"), "authority", {"authority": "other"}),
        (VALID.replace(subject.STREAM_HOST, "user@" + subject.STREAM_HOST), "authority", {
            "authority": "userinfo",
        }),
        (VALID.replace(subject.STREAM_HOST, subject.STREAM_HOST + ":443"), "authority", {
            "authority": "port",
        }),
        (VALID.replace(subject.STREAM_HOST, "user@" + subject.STREAM_HOST + ":bad"), "authority", {
            "authority": "userinfo_and_port",
        }),
        (VALID.replace(subject.STREAM_HOST, "[::1]"), "authority", {"authority": "other"}),
        (VALID.replace(subject.STREAM_HOST, "[::1]:443"), "authority", {"authority": "port"}),
        (VALID.replace("/?", "?"), "path", {"path": "empty"}),
        (VALID.replace("/?", "/other?"), "path", {"path": "other"}),
        (VALID + "#", "fragment", {"fragment": "present"}),
        (VALID + "#private", "fragment", {"fragment": "present"}),
        (VALID + "&extra=x", "query_parser", {"query_parser": "field_limit"}),
        (BASE + "?broken&x=y&z=w&fourth", "query_parser", {"query_parser": "field_limit"}),
        (BASE + "?broken", "query_parser", {"query_parser": "syntax"}),
        (BASE + "?trackid=168&&format=mp32", "query_parser", {"query_parser": "syntax"}),
        (BASE, "query_keys", {"query_parser": "success", "missing_keys": "present"}),
        (BASE + "?format=mp32", "query_keys", {"format": "match", "track_id": "not_evaluated"}),
        (BASE + "?trackid=wrong&format=mp31&extra=x", "query_keys", {
            "missing_keys": "present", "extra_keys": "present", "duplicate_keys": "absent",
            "track_id": "mismatch", "format": "mismatch", "application_binding": "not_evaluated",
        }),
        (BASE + "?format=mp32&format=mp31&extra=x", "query_keys", {
            "missing_keys": "present", "extra_keys": "present", "duplicate_keys": "present",
            "format": "not_evaluated",
        }),
        (VALID.replace("168", "169"), "track_id", {"track_id": "mismatch"}),
        (VALID.replace("mp32", "mp31"), "format", {"format": "mismatch"}),
        (VALID.replace(FAKE_ID, "wrong"), "application_binding", {"application_binding": "mismatch"}),
    ],
)
def test_each_dimension(url: str, first: str, facts: dict[str, str]) -> None:
    diagnostic = refused(url)
    assert diagnostic["first_component"] == first
    for name, expected in facts.items():
        assert diagnostic[name] == expected
    if diagnostic["query_parser"] != "success":
        for name in ("query_keys", "missing_keys", "extra_keys", "duplicate_keys", "track_id",
                     "format", "application_binding"):
            assert diagnostic[name] == "not_evaluated"


def test_mixed_failures_keep_original_component_precedence() -> None:
    mixed = "http://user@invalid.test:443/other?trackid=wrong&format=mp31&from=wrong#"
    assert refused(mixed) == {
        "first_component": "scheme", "non_ascii": "absent", "whitespace": "absent",
        "control": "absent", "backslash": "absent", "url_parser": "success", "scheme": "http",
        "authority": "userinfo_and_port", "path": "other", "fragment": "present",
        "query_parser": "success", "query_keys": "exact", "missing_keys": "absent",
        "extra_keys": "absent", "duplicate_keys": "absent", "track_id": "mismatch",
        "format": "mismatch", "application_binding": "mismatch",
    }
    # Original query parsing precedes scheme/authority checks, even with both wrong.
    diagnostic = refused(mixed.replace("trackid=wrong", "broken"))
    assert diagnostic["first_component"] == "query_parser"
    assert diagnostic["scheme"] == "http" and diagnostic["path"] == "other"
    diagnostic = refused("é\t\x00\\" + mixed)
    assert diagnostic["first_component"] == "text_shape"
    for name in ("non_ascii", "whitespace", "control", "backslash"):
        assert diagnostic[name] == "present"


@pytest.mark.parametrize("value", [None, 123, [], "x" * 1025])
def test_type_bound_and_prior_gate_do_not_parse(value: object, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object) -> object:
        pytest.fail("route classification before the bounded shape gate")

    monkeypatch.setattr(subject, "diagnose_route", forbidden)
    for record in (row(audio=value), row(name="", audio=value)):
        with pytest.raises(subject.CandidateRefused) as error:
            subject.validate_candidate(record, FAKE_ID)
        assert error.value.route_diagnostic is None
    record = row()
    del record["audio"]
    with pytest.raises(subject.CandidateRefused, match="^candidate_stream_route_missing$") as error:
        subject.validate_candidate(record, FAKE_ID)
    assert error.value.route_diagnostic is None


def original_target(value: str) -> str | None:
    """Frozen pre-201 predicates and reconstruction for the differential corpus."""
    if len(value) > 1024 or not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return None
    try:
        parts = urlsplit(value)
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.netloc != subject.STREAM_HOST or parts.path != "/"
            or parts.fragment or "#" in value):
        return None
    expected = {"trackid": "168", "format": "mp32", "from": f"app-{FAKE_ID}"}
    return "/?" + urlencode(expected) if len(pairs) == 3 and dict(pairs) == expected else None


def test_original_acceptance_and_reconstruction_corpus() -> None:
    corpus = [VALID, VALID.replace("https", "HTTPS"), BASE + "?" + "&".join(reversed(QUERY.split("&"))),
              VALID.replace("trackid", "%74rackid").replace("168", "%31%36%38"),
              VALID.replace("trackid", "%2574rackid"), VALID.replace("mp32", "mp%2532")]
    for char in (" ", "\t", "\n", "\x00", "\x1f", "\x7f", "é", "\u200b", "\\", "#", "?", "%", "&"):
        for index in (0, 6, len(BASE) - 1, len(BASE), len(VALID)):
            corpus.append(VALID[:index] + char + VALID[index:])
    for value in corpus:
        expected = original_target(value)
        if expected is None:
            with pytest.raises(subject.CandidateRefused):
                subject.stream_target(value, FAKE_ID, "168")
        else:
            assert subject.stream_target(value, FAKE_ID, "168") == expected


@pytest.mark.parametrize("refresh", [False, True])
def test_propagation_bounded_report_and_no_later_dispatch(
    refresh: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "synthetic-private-marker"
    hostile = f"http://{marker}@invalid.test:443/{marker}?trackid={marker}&format={marker}&from={FAKE_ID}#"
    # Exactly the largest string the diagnostic may inspect.
    hostile += "x" * (1024 - len(hostile))
    calls = 0

    def exchange(_target: str, transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        calls += 1
        assert metadata and calls <= (2 if refresh else 1)
        transfer.requests_attempted = 1
        if refresh and calls == 1:
            return listing([row(), row(id="169")])
        return listing([row(audio=hostile)] * (1 if refresh else 10))

    monkeypatch.setattr(subject, "read_id", lambda _path: FAKE_ID)
    monkeypatch.setattr(subject, "exchange", exchange)
    assert subject.run_child("synthetic-path") == 1
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert output.err == "" and len(output.out.encode()) < subject.REPORT_BYTES
    assert calls == (2 if refresh else 1)
    assert report["reason"] == ("candidate_stream_route_shape" if refresh else "no_acceptable_candidate")
    expected = refused(hostile)
    assert report["route_diagnostic"] == (expected if refresh else None)
    assert report["stream"] == asdict(subject.Transfer()) and not report["metadata_validated"]
    assert report["candidate_refusals"] == ([] if refresh else [
        {"ordinal": n, "gate": "stream_route", "condition": "shape", "license_component": None,
         "license_text_detail": None, "route_diagnostic": expected} for n in range(1, 11)
    ])
    for forbidden in (hostile, marker, FAKE_ID, "http://", "invalid.test", hashlib.sha256(hostile.encode()).hexdigest()):
        assert forbidden not in output.out


@pytest.mark.parametrize("credential", ["scheme", "MISMATCH", "not_evaluated", "track_id"])
def test_fixed_labels_colliding_with_credential_are_suppressed(
    credential: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subject, "read_id", lambda _path: credential)
    monkeypatch.setattr(subject, "exchange", lambda *_args, **_kwargs: document(row(audio="http://invalid.test/")))
    assert subject.run_child("synthetic-path") == 1
    assert json.loads(capsys.readouterr().out) == {"outcome": "inconclusive", "reason": "report_redacted"}
