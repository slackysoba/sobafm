"""Synthetic metadata only: request, admission and refresh failure boundaries."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from sobafm import jamendo_catalog as subject
from sobafm.catalog import RecordingKey, RecordingKind, Source, metadata_digest
from sobafm.credits import render_credits
from sobafm.jamendo_admission import (
    AdmissionInputError,
    Candidate,
    Policy,
    Purpose,
    Reason,
    decision_matches,
    evaluate,
    parse_listing,
)
from sobafm.jamendo_catalog import (
    MAX_OFFSET,
    MAX_RESPONSE_BYTES,
    Coverage,
    Failure,
    Query,
    Refreshed,
    Refusal,
    Request,
    SearchWindow,
    refresh,
    refresh_request,
)

NOW = datetime(2026, 11, 11, tzinfo=UTC)
ON = Policy(enabled=True)


def nested(levels: int) -> object:
    value: object = "leaf"
    for _ in range(levels):
        value = [value]
    return value


@pytest.mark.parametrize(
    ("accepted", "refused", "condition"),
    [
        (nested(5), nested(6), subject.EnvelopeCondition.DEPTH),
        (
            {str(n): None for n in range(64)},
            {str(n): None for n in range(65)},
            subject.EnvelopeCondition.OBJECT_MEMBERS,
        ),
        ({"k" * 64: None}, {"k" * 65: None}, subject.EnvelopeCondition.KEY_LENGTH),
        ([None] * 100, [None] * 101, subject.EnvelopeCondition.LIST_LENGTH),
        ("v" * 32_768, "v" * 32_769, subject.EnvelopeCondition.STRING_LENGTH),
    ],
)
def test_envelope_structural_boundaries(
    accepted: object, refused: object, condition: subject.EnvelopeCondition
) -> None:
    diagnostic = subject.EnvelopeDiagnostic()
    for value, expected in ((refused, condition), (accepted, None)):
        payload = response([{"private-key": value}])
        plain = subject._rows(payload, 10)  # pyright: ignore[reportPrivateUsage]
        observed = subject._rows(payload, 10, diagnostic=diagnostic)  # pyright: ignore[reportPrivateUsage]
        assert observed == plain
        assert diagnostic.condition == expected
        assert isinstance(observed, Refusal) == (expected is not None)


@pytest.mark.parametrize(
    ("accepted", "refused", "condition"),
    [
        ({"extra": nested(6)}, {"extra": nested(7)}, subject.EnvelopeCondition.DEPTH),
        (
            {str(n): None for n in range(59)},
            {str(n): None for n in range(60)},
            subject.EnvelopeCondition.OBJECT_MEMBERS,
        ),
        (
            {"extra": {str(n): None for n in range(64)}},
            {"extra": {str(n): None for n in range(65)}},
            subject.EnvelopeCondition.OBJECT_MEMBERS,
        ),
        ({"k" * 64: None}, {"k" * 65: None}, subject.EnvelopeCondition.KEY_LENGTH),
        (
            {"extra": [None] * 100},
            {"extra": [None] * 101},
            subject.EnvelopeCondition.LIST_LENGTH,
        ),
        (
            {"extra": "v" * 32_768},
            {"extra": "v" * 32_769},
            subject.EnvelopeCondition.STRING_LENGTH,
        ),
    ],
)
def test_additive_headers_keep_recursive_bounds(
    accepted: dict[str, object],
    refused: dict[str, object],
    condition: subject.EnvelopeCondition,
) -> None:
    diagnostic = subject.EnvelopeDiagnostic()
    for header, expected in ((refused, condition), (accepted, None)):
        payload = response([], **header)
        result = subject._rows(payload, 10, diagnostic=diagnostic)  # pyright: ignore[reportPrivateUsage]
        assert result == (Refusal(Failure.MALFORMED) if expected is not None else ())
        assert diagnostic.condition == expected
        assert subject._rows(payload, 10) == result  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "members",
    [
        '"code":0',
        '"extra":1,"extra":2',
        '"extra":{"nested":1,"nested":2}',
        '"extra":NaN',
    ],
)
def test_additive_headers_cannot_bypass_json_refusal(members: str) -> None:
    payload = (
        '{"headers":{"status":"success","code":0,"warnings":"",'
        '"error_message":"","results_count":0,' + members + '},"results":[]}'
    ).encode()
    diagnostic = subject.EnvelopeDiagnostic()
    assert subject._rows(payload, 10, diagnostic=diagnostic) == Refusal(Failure.MALFORMED)  # pyright: ignore[reportPrivateUsage]
    assert diagnostic.condition == subject.EnvelopeCondition.JSON


@pytest.mark.parametrize("field", ["status", "code", "error_message", "warnings", "results_count"])
def test_additive_headers_cannot_replace_required_fields(field: str) -> None:
    header: dict[str, object] = {
        "status": "success",
        "code": 0,
        "warnings": "",
        "error_message": "",
        "results_count": 0,
        "private-extra": {field: "private-value"},
    }
    del header[field]
    payload = json.dumps({"headers": header, "results": []}).encode()
    diagnostic = subject.EnvelopeDiagnostic()
    assert subject._rows(payload, 10, diagnostic=diagnostic) == Refusal(Failure.MALFORMED)  # pyright: ignore[reportPrivateUsage]
    assert diagnostic.condition == subject.EnvelopeCondition.HEADER_SHAPE
    assert "private" not in repr(diagnostic)


@pytest.mark.parametrize(
    ("payload", "condition", "failure"),
    [
        (b" " * (MAX_RESPONSE_BYTES + 1), "payload_size", Failure.OVERSIZED),
        (b"invalid", "json", Failure.MALFORMED),
        (b"\xff", "json", Failure.MALFORMED),
        (b'{"private-key":1,"private-key":2}', "json", Failure.MALFORMED),
        (b'{"private-key":NaN}', "json", Failure.MALFORMED),
        (b"[]", "root_shape", Failure.MALFORMED),
        (
            b'{"headers":{},"results":[],"private-key":null}',
            "unexpected_root_keys",
            Failure.MALFORMED,
        ),
        (b'{"headers":{},"results":[]}', "header_shape", Failure.MALFORMED),
        (b'{"headers":[],"results":[]}', "header_shape", Failure.MALFORMED),
        (b'{"headers":null,"results":[]}', "header_shape", Failure.MALFORMED),
    ],
)
def test_earlier_envelope_failures_are_safe(
    payload: bytes, condition: str, failure: Failure
) -> None:
    diagnostic = subject.EnvelopeDiagnostic()
    assert subject._rows(payload, 10, diagnostic=diagnostic) == Refusal(failure)  # pyright: ignore[reportPrivateUsage]
    assert diagnostic.condition == condition
    assert "private" not in repr(diagnostic)
    assert_search_and_refresh_refuse(payload, failure)


def test_parser_recursion_error_has_no_invented_structural_diagnosis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Do not depend on the interpreter JSON decoder's nesting implementation.
    def refuse(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("private-parser-detail")

    monkeypatch.setattr(subject.json, "loads", refuse)
    diagnostic = subject.EnvelopeDiagnostic()
    assert subject._rows(b"[]", 10, diagnostic=diagnostic) == Refusal(Failure.MALFORMED)  # pyright: ignore[reportPrivateUsage]
    assert diagnostic.condition == subject.EnvelopeCondition.JSON
    assert "private" not in repr(diagnostic)


@pytest.mark.parametrize(
    ("header", "condition", "failure"),
    [
        ({"status": "succeed"}, "provider", Failure.PROVIDER),
        ({"status": None}, "provider", Failure.PROVIDER),
        ({"code": True}, "provider", Failure.PROVIDER),
        ({"code": 5}, "provider", Failure.PROVIDER),
        ({"code": "0"}, "provider", Failure.PROVIDER),
        ({"warnings": "private-value"}, "warning", Failure.WARNING),
        ({"warnings": []}, "warning", Failure.WARNING),
        ({"error_message": "private-value"}, "warning", Failure.WARNING),
        ({"error_message": None}, "warning", Failure.WARNING),
        ({"results_count": True}, "results_count", Failure.MALFORMED),
        ({"results_count": "1"}, "results_count", Failure.MALFORMED),
        ({"results_count": 2}, "results_count", Failure.MALFORMED),
        ({"results_fullcount": True}, "total_type", Failure.MALFORMED),
        ({"results_fullcount": "1"}, "total_type", Failure.MALFORMED),
        ({"results_fullcount": 1.0}, "total_type", Failure.MALFORMED),
        ({"results_fullcount": None}, "total_type", Failure.MALFORMED),
        ({"results_fullcount": 0}, "total_value", Failure.MALFORMED),
        ({"results_fullcount": -1}, "total_value", Failure.MALFORMED),
    ],
)
def test_envelope_header_conditions_preserve_verdict(
    header: dict[str, object], condition: str, failure: Failure
) -> None:
    payload = response([row()], **{"private-header": {"private-key": "private-value"}, **header})
    diagnostic = subject.EnvelopeDiagnostic()
    assert subject._rows(payload, 10, diagnostic=diagnostic) == Refusal(failure)  # pyright: ignore[reportPrivateUsage]
    assert subject._rows(payload, 10) == Refusal(failure)  # pyright: ignore[reportPrivateUsage]
    assert diagnostic.condition == condition
    assert "private" not in repr(diagnostic)
    assert_search_and_refresh_refuse(payload, failure)


def test_envelope_first_refusal_order_and_count_boundaries() -> None:
    diagnostic = subject.EnvelopeDiagnostic()
    # The existing traversal checks the whole structure before envelope fields,
    # with dictionary insertion order and member count before key length.
    cases: list[tuple[bytes, str | None]] = [
        (response([{"k" * 65: None, **{str(n): None for n in range(64)}}]), "object_members"),
        (
            response([{"first": "x" * 32_769, "second": [None] * 101}], status="bad"),
            "string_length",
        ),
        (response([{"second": [None] * 101, "first": "x" * 32_769}]), "list_length"),
        (response([{}], warnings="bad", results_fullcount=False), "warning"),
        (response([{}], results_count=2, results_fullcount=False), "results_count"),
        (response([{}], results_fullcount=1), None),
        (response([], results_fullcount=0), None),
        (response([{}] * 10), None),
        (response([{}] * 11), "results_count"),
        (
            b'{"headers":{"status":"success","code":0,"warnings":"",'
            b'"error_message":"","results_count":0},"results":{}}',
            "results_shape",
        ),
        # Missing required keys wins before unknown keys in object_fields.
        (b'{"private-key":null}', "root_shape"),
    ]
    for payload, condition in cases:
        result = subject._rows(payload, 10, diagnostic=diagnostic)  # pyright: ignore[reportPrivateUsage]
        assert diagnostic.condition == condition
        assert result == subject._rows(payload, 10)  # pyright: ignore[reportPrivateUsage]
    # Exact byte cap is still parsed; only cap+1 gives OVERSIZED.
    payload = response([])
    payload += b" " * (MAX_RESPONSE_BYTES - len(payload))
    assert subject._rows(payload, 10, diagnostic=diagnostic) == ()  # pyright: ignore[reportPrivateUsage]
    assert diagnostic.condition is None


def row(identity: str = "123", /, **changes: object) -> dict[str, object]:
    return {
        "id": identity,
        "name": "Synthetic nocturne",
        "artist_id": "77",
        "artist_name": "Synthetic artist",
        "duration": 180,
        "shareurl": f"https://www.jamendo.com/track/{identity}",
        "license_ccurl": "http://creativecommons.org/licenses/by-nc-sa/3.0/",
        "audio": f"https://prod-1.storage.jamendo.com/?trackid={identity}&format=mp32&from=app-test",
        "album_name": "",
        "album_id": "",
        "musicinfo": {"tags": {"genres": ["classical"], "instruments": ["piano"]}},
        "licenses": {"ccnc": True, "ccnd": False, "ccsa": True},
        **changes,
    }


def response(rows: list[dict[str, object]], **header: object) -> bytes:
    return json.dumps(
        {
            "headers": {
                "status": "success",
                "code": 0,
                "warnings": "",
                "error_message": "",
                "results_count": len(rows),
                **header,
            },
            "results": rows,
        }
    ).encode()


def candidate(value: dict[str, object] | None = None) -> Candidate:
    listing = parse_listing(row() if value is None else value)
    return Candidate(
        listing.recording, evaluate(listing, ON, NOW, Purpose.RANKING), listing.content_digest
    )


def refreshed(value: dict[str, object], ranked: Candidate | None = None) -> Refreshed | Refusal:
    ranked = ranked or candidate()
    request = refresh_request(ranked.recording.key, ON)
    assert isinstance(request, Request)
    return refresh(request, response([value]), ranked, ON, NOW + timedelta(seconds=1))


def assert_search_and_refresh_refuse(payload: bytes, reason: Failure | Reason) -> None:
    window = SearchWindow(Query(search="piano"), ON)
    request = window.request()
    assert isinstance(request, Request)
    result = window.receive(request, payload, NOW)
    assert not result.candidates
    assert result.refusals == (Refusal(reason),)
    assert result.coverage == Coverage.UNAVAILABLE
    ranked = candidate()
    by_id = refresh_request(ranked.recording.key, ON)
    assert isinstance(by_id, Request)
    assert refresh(by_id, payload, ranked, ON, NOW) == Refusal(reason)


@pytest.mark.parametrize(
    "extra",
    [
        None,
        True,
        42,
        1.5,
        "private-value",
        {"private-key": [None, True, 42, {"url": "https://unvisited.example/private-value"}]},
    ],
)
def test_additive_headers_do_not_change_search_or_refresh_evidence(extra: object) -> None:
    payload = response([row()], **{"private-header": extra, "results_fullcount": 1})
    window = SearchWindow(Query(search="piano"), ON)
    request = window.request()
    assert isinstance(request, Request)
    result = window.receive(request, payload, NOW)
    ranked = candidate()
    assert result.candidates == (ranked,)
    assert not result.refusals
    assert result.pages_issued == 1 and result.response_bytes == len(payload)
    by_id = refresh_request(ranked.recording.key, ON)
    assert isinstance(by_id, Request)
    current = refresh(by_id, payload, ranked, ON, NOW)
    baseline = refresh(by_id, response([row()]), ranked, ON, NOW)
    assert isinstance(current, Refreshed) and isinstance(baseline, Refreshed)
    assert current == baseline
    assert current.stream_url == baseline.stream_url
    assert "private" not in repr((result, current))


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            response([row()], **{"private-header": None})[:-1] + b',"private-root":null}',
            Failure.MALFORMED,
        ),
        (response([row(private_recording="private-value")], extra=True), Reason.UNSUPPORTED),
        (
            response([row(notices={"private-notice": "private-value"})], extra=True),
            Reason.CREDITS,
        ),
        (response([row(licenses={"approved": True})], extra=True), Reason.UNSUPPORTED),
        (response([row(notices=["All rights reserved"])], extra=True), Reason.RIGHTS),
    ],
)
def test_additive_headers_do_not_open_root_recordings_or_notices(
    payload: bytes, reason: Failure | Reason
) -> None:
    assert_search_and_refresh_refuse(payload, reason)


def test_disabled_policy_never_constructs_a_request() -> None:
    window = SearchWindow(Query(search="piano"))
    assert window.request() == Refusal(Failure.DISABLED)
    assert window.result().coverage == Coverage.DISABLED
    assert window.result().pages_issued == 0
    assert refresh_request(RecordingKey(Source.JAMENDO, "123")) == Refusal(Failure.DISABLED)
    assert not evaluate(parse_listing(row()), Policy(), NOW, Purpose.RANKING).admitted
    with pytest.raises(AdmissionInputError):
        Policy(enabled=cast(bool, "true"))


def test_requests_are_explicit_bounded_and_include_singles() -> None:
    window = SearchWindow(Query(search="piano", artist_name="Synthetic artist", tags=("jazz",)), ON)
    request = window.request()
    assert isinstance(request, Request)
    params = dict(request.parameters)
    assert params == {
        "format": "json",
        "type": "single albumtrack",
        "audioformat": "mp32",
        "include": "musicinfo licenses",
        "search": "piano",
        "artist_name": "Synthetic artist",
        "tags": "jazz",
        "limit": "100",
        "offset": "0",
    }
    assert request.maximum_bytes == 1_048_576
    assert "client_id" not in repr(request)
    assert window.request() == Refusal(Failure.SEQUENCE)
    result = window.receive(request, response([row()]), NOW)
    assert result.coverage == Coverage.PARTIAL
    assert len(result.candidates) == 1
    assert result.candidates[0].decision.purpose == Purpose.RANKING
    assert result.candidates[0].recording.kind == RecordingKind.UNKNOWN
    assert window.request() == Refusal(Failure.LIMIT)


@pytest.mark.parametrize(
    "query,offset",
    [
        (Query(), 0),
        (Query(search="https://example.com"), 0),
        (Query(search="x" * 201), 0),
        (Query(tags=("piano+rock",)), 0),
        (Query(tags=tuple("tag" for _ in range(9))), 0),
        (Query(search="piano"), -1),
        (Query(search="piano"), 1),
        (Query(search="piano"), True),
        (Query(search="piano"), MAX_OFFSET),
    ],
)
def test_invalid_search_input_makes_no_request(query: Query, offset: int) -> None:
    window = SearchWindow(query, ON, offset=offset)
    assert window.request() == Refusal(Failure.REQUEST)
    assert window.result().pages_issued == 0


def test_two_pages_sequential_and_deduplicated_with_no_third_page() -> None:
    window = SearchWindow(Query(search="piano"), ON)
    first = window.request()
    assert isinstance(first, Request)
    body = response([row(str(n)) for n in range(1, 101)])
    window.receive(first, body, NOW)
    second = window.request()
    assert isinstance(second, Request)
    assert dict(second.parameters)["offset"] == "100"
    result = window.receive(second, body, NOW)
    assert len(result.candidates) == 100
    assert result.pages_issued == 2
    assert result.response_bytes == len(body) * 2
    assert result.next_offset == 200
    assert window.request() == Refusal(Failure.LIMIT)
    with pytest.raises(ValueError, match="page_sequence"):
        window.receive(first, body, NOW)


@pytest.mark.parametrize(
    "change", [{"name": "Changed"}, {"notices": ["Additional credit"]}, {"name": None}]
)
def test_conflicting_duplicate_is_quarantined_even_if_last_row_is_good(
    change: dict[str, object],
) -> None:
    window = SearchWindow(Query(search="piano"), ON)
    request = window.request()
    assert isinstance(request, Request)
    result = window.receive(request, response([row(), row(**change), row()]), NOW)
    assert not result.candidates
    assert result.refusals


@pytest.mark.parametrize(
    "payload,reason",
    [
        (b"x" * (MAX_RESPONSE_BYTES + 1), Failure.OVERSIZED),
        (b' {"headers":{},"headers":{},"results":[]}', Failure.MALFORMED),
        (b"\xff", Failure.MALFORMED),
        (b'{"value":NaN}', Failure.MALFORMED),
        (response([], results_count=True), Failure.MALFORMED),
        (response([row()], results_count=0), Failure.MALFORMED),
        (response([], warnings="secret synthetic warning"), Failure.WARNING),
        (response([], status="failed", code=5, error_message="synthetic secret"), Failure.PROVIDER),
        (response([row(str(n)) for n in range(1, 102)]), Failure.MALFORMED),
    ],
)
def test_response_bounds_and_header_failures_are_safe(payload: bytes, reason: Failure) -> None:
    window = SearchWindow(Query(search="piano"), ON)
    request = window.request()
    assert isinstance(request, Request)
    result = window.receive(request, payload, NOW)
    assert result.refusals == (Refusal(reason),)
    assert result.coverage == Coverage.UNAVAILABLE
    assert not result.candidates
    assert "secret" not in repr(result)
    assert window.request() == Refusal(Failure.LIMIT)


def test_exact_byte_limit_and_empty_window_do_not_claim_exhaustion() -> None:
    window = SearchWindow(Query(search="piano"), ON)
    request = window.request()
    assert isinstance(request, Request)
    body = response([])
    result = window.receive(request, body + b" " * (MAX_RESPONSE_BYTES - len(body)), NOW)
    assert result.coverage == Coverage.EMPTY
    assert not result.refusals


@pytest.mark.parametrize(
    "field",
    ["id", "artist_id", "name", "artist_name", "shareurl", "duration", "license_ccurl", "audio"],
)
def test_every_required_field_is_required(field: str) -> None:
    value = row()
    del value[field]
    with pytest.raises(AdmissionInputError):
        parse_listing(value)


@pytest.mark.parametrize(
    "change",
    [
        {"id": "0"},
        {"id": True},
        {"artist_id": "unknown"},
        {"name": ""},
        {"artist_name": " "},
        {"duration": 0},
        {"duration": True},
        {"duration": 1.5},
        {"duration": float("inf")},
        {"duration": 86_401},
        {"shareurl": "https://www.jamendo.com/track/999"},
        {"shareurl": "https://www.jamendo.com.attacker.example/track/123"},
        {"license_ccurl": "https://creativecommons.org/licenses/by/9.0/"},
        {"license_ccurl": "https://creativecommons.org/licenses/by/4.0/us/"},
        {"license_ccurl": "https://creativecommons.org/licenses/by/3.0/xx/"},
        {"license_ccurl": "https://creativecommons.org/licenses/by/3.0/?key=synthetic"},
        {"licenses": {"ccnc": False}},
        {"audio": "https://prod-1.storage.jamendo.com/?trackid=999&format=mp32"},
        {"audio": "https://prod-1.storage.jamendo.com/download/track/123/mp32/"},
        {"audio": "https://prod-1.storage.jamendo.com/?trackid=123&format=mp31"},
        {"audio": "https://evil.example/?trackid=123&format=mp32"},
        {"audio": "https://prod-1.storage.jamendo.com/?trackid=123&trackid=999&format=mp32"},
        {"notices": {"unparsed": "copyright"}},
        {"notices": ["x" * 1025]},
        {"notices": ["https://example.com/?token=synthetic-secret"]},
        {"notices": ["https://name:synthetic-secret@example.com/"]},  # pragma: allowlist secret
        {"credits": ["<b>credit</b>"]},
        {"creators": ["x" * 201]},
        {"musicinfo": {"unknown_notice": "credit"}},
        {"unknown": "credit"},
        {"approved": True},
        {"access_ready": True},
        {"policy": {"enabled": True}},
    ],
)
def test_invalid_conflicting_or_unreadable_evidence_fails_closed(change: dict[str, object]) -> None:
    with pytest.raises(AdmissionInputError) as error:
        parse_listing(row(**change))
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("word", ["preview", "excerpt", "snippet", "stem", "loop", "sample pack"])
@pytest.mark.parametrize("location", ["name", "description", "type", "notices", "details", "tags"])
def test_excluded_forms_in_all_policy_text(word: str, location: str) -> None:
    change: dict[str, object] = {location: word}
    if location in {"notices", "details"}:
        change[location] = [word]
    elif location == "tags":
        change = {"musicinfo": {"tags": {"vartags": [word]}}}
    listing = parse_listing(row(**change))
    assert evaluate(listing, ON, NOW, Purpose.RANKING).reason == Reason.EXCLUDED


@pytest.mark.parametrize(
    "change",
    [
        {"description": "Contains uncleared samples"},
        {"notices": ["All rights reserved"]},
        {"warnings": ["Check the composition rights"]},
        {"rights_warnings": ["Required credit pending"]},
    ],
)
def test_unresolved_rights_and_warnings_refuse(change: dict[str, object]) -> None:
    listing = parse_listing(row(**change))
    assert evaluate(listing, ON, NOW, Purpose.RANKING).reason == Reason.RIGHTS


@pytest.mark.parametrize("duration", [1, 59, 901, 86_400])
def test_provisional_listing_has_no_invented_one_to_fifteen_minute_filter(duration: int) -> None:
    assert candidate(row(duration=duration)).decision.admitted


def test_all_evidence_binding_covers_details_and_credit_changes() -> None:
    original = parse_listing(row())
    decision = evaluate(original, ON, NOW, Purpose.RANKING)
    changed = parse_listing(row(musicinfo={"tags": {"genres": ["jazz"]}}))
    assert metadata_digest(original.recording) == metadata_digest(changed.recording)
    assert not decision_matches(decision, changed, ON)
    for change in (
        {"description": "New description"},
        {"notices": ["New notice"]},
        {"licenses": {"ccnc": True}},
        {"artist_id": "78"},
    ):
        assert not decision_matches(decision, parse_listing(row(**change)), ON)
    assert not decision_matches(replace(decision, policy_version="old-policy"), original, ON)
    assert not decision_matches(decision, original, Policy())
    assert decision_matches(
        decision,
        parse_listing(row(audio=cast(str, row()["audio"]) + "&client_id=synthetic-key")),
        ON,
    )


def test_credits_are_complete_immutable_and_safe_without_stream_url() -> None:
    original = row(
        creators=["Second musician", "Third musician"],
        notices=["Credit " + "z" * 900, "https://example.com/notice"],
    )
    result = candidate(original)
    cast(list[str], original["notices"]).append("Later mutation")
    assert result.recording.creators == ("Synthetic artist", "Second musician", "Third musician")
    assert result.recording.credits is not None
    assert len(result.recording.credits.notices) == 3
    rendered = render_credits(result.recording)
    assert "z" * 900 in "".join(rendered.pages)
    serialized = json.dumps(asdict(result), default=str)
    assert "prod-1" not in serialized and "client_id" not in serialized
    with pytest.raises(FrozenInstanceError):
        result.recording.title = "forged"  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize("suffix", ["by-nc-sa/3.0/us/", "by-nc-sa/2.0/fr/", "by-nc-sa/4.0/"])
def test_supported_exact_license_version_and_jurisdiction_retained(suffix: str) -> None:
    result = candidate(row(license_ccurl="https://creativecommons.org/licenses/" + suffix))
    assert result.recording.credits is not None
    assert result.recording.credits.license_url == "https://creativecommons.org/licenses/" + suffix


def test_refresh_reissues_by_id_and_keeps_only_new_immutable_credits() -> None:
    ranked = candidate()
    result = refreshed(row(notices=["Additional required notice"]), ranked)
    assert isinstance(result, Refreshed)
    assert not result.previous_decision_matches
    assert result.candidate.decision.purpose == Purpose.REFRESH
    assert result.candidate.decision.refreshed_at == NOW + timedelta(seconds=1)
    assert result.candidate.recording.credits != ranked.recording.credits
    assert "Additional required notice" not in repr(ranked)
    assert "prod-1" not in repr(result)
    assert ranked.decision.purpose == Purpose.RANKING


@pytest.mark.parametrize(
    "change",
    [
        {"name": "New title"},
        {"duration": 181},
        {"details": ["New style"]},
        {"creators": ["New performer"]},
    ],
)
def test_material_change_discards_ranked_choice(change: dict[str, object]) -> None:
    assert refreshed(row(**change)) == Refusal(Failure.CHANGED)


def test_refresh_rejects_id_mismatch_removal_failure_and_conflict_without_fallback() -> None:
    ranked = candidate()
    request = refresh_request(ranked.recording.key, ON)
    assert isinstance(request, Request)
    assert refreshed(row("124")) == Refusal(Reason.IDENTITY)
    assert refreshed(row(notices=["All rights reserved"])) == Refusal(Reason.RIGHTS)
    assert refresh(request, response([]), ranked, ON, NOW) == Refusal(Failure.REMOVED)
    failure = Refusal(Failure.TRANSPORT)
    assert refresh(request, failure, ranked, ON, NOW) == failure
    assert refresh(request, response([row()]), ranked, Policy(), NOW) == Refusal(Failure.DISABLED)
    assert refresh(replace(request, key=None), response([row()]), ranked, ON, NOW) == Refusal(
        Failure.IDENTITY
    )
    assert refresh_request(RecordingKey(Source.FMA, "123"), ON) == Refusal(Failure.IDENTITY)


def test_refresh_policy_change_does_not_inherit_old_approval() -> None:
    old = candidate()
    old = replace(old, decision=replace(old.decision, policy_version="old-policy"))
    result = refreshed(row(), old)
    assert isinstance(result, Refreshed)
    assert not result.previous_decision_matches
    assert result.candidate.decision.policy_version == "jamendo-stream-1"


def test_naive_time_is_not_accepted_as_refresh_provenance() -> None:
    with pytest.raises(AdmissionInputError):
        evaluate(parse_listing(row()), ON, datetime(2026, 11, 11), Purpose.REFRESH)


@pytest.mark.parametrize(
    "notice",
    [
        "https://creativecommons.org/licenses/by-nc-sa/3.0/us/",
        "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        "https://www.jamendo.com/track/999",
        "Licensed CC BY 4.0",
    ],
)
def test_conflicting_or_ambiguous_notice_license_and_identity_refuse(notice: str) -> None:
    assert refreshed(row(notices=[notice])) == Refusal(Reason.RIGHTS)


def test_matching_license_notice_and_full_description_are_preserved() -> None:
    notice = "https://creativecommons.org/licenses/by-nc-sa/3.0/"
    result = refreshed(
        row(notices=[notice], description="Arranged by a second musician"),
        candidate(row(description="Arranged by a second musician")),
    )
    assert isinstance(result, Refreshed)
    credits = result.candidate.recording.credits
    assert credits is not None
    assert notice in credits.notices
    assert "Arranged by a second musician" in credits.notices


@pytest.mark.parametrize("scheme", ["https", "HTTPS", "Https"])
@pytest.mark.parametrize(
    "target",
    [
        "creativecommons.org/licenses/by/4.0/",
        "creativecommons.org/licenses/by-nc-sa/3.0/us/",
        "www.jamendo.com/track/999",
    ],
)
def test_notice_scheme_case_cannot_bypass_search_or_refresh_conflicts(
    scheme: str,
    target: str,
) -> None:
    value = row(notices=[f"{scheme}://{target}"])
    window = SearchWindow(Query(search="piano"), ON)
    request = window.request()
    assert isinstance(request, Request)
    result = window.receive(request, response([value]), NOW)
    assert not result.candidates
    assert result.refusals == (Refusal(Reason.RIGHTS),)
    assert refreshed(value) == Refusal(Reason.RIGHTS)


@pytest.mark.parametrize("scheme", ["https", "HTTPS", "Https"])
@pytest.mark.parametrize(
    "target",
    [
        "creativecommons.org/licenses/by-nc-sa/3.0/",
        "CREATIVECOMMONS.ORG:443/licenses/by-nc-sa/3.0/",
        "www.jamendo.com/track/123",
    ],
)
def test_matching_notice_url_components_preserve_complete_original_credit(
    scheme: str,
    target: str,
) -> None:
    notice = f"{scheme}://{target}"
    value = row(notices=[notice])
    window = SearchWindow(Query(search="piano"), ON)
    request = window.request()
    assert isinstance(request, Request)
    result = window.receive(request, response([value]), NOW)
    assert len(result.candidates) == 1
    assert not result.refusals
    result_refresh = refreshed(value)
    assert isinstance(result_refresh, Refreshed)
    for recording in (result.candidates[0].recording, result_refresh.candidate.recording):
        assert recording.credits is not None
        assert notice in recording.credits.notices


def test_unknown_evidence_nested_and_credit_count_limits_refuse() -> None:
    for change in (
        {"musicinfo": {"tags": {"copyright": ["New credit"]}}},
        {"licenses": {"approved": True}},
        {"notices": [str(n) for n in range(16)]},
        {"details": ["x" * 161]},
    ):
        with pytest.raises(AdmissionInputError):
            parse_listing(row(**change))


def test_partial_failure_preserves_valid_candidates_and_stops_window() -> None:
    window = SearchWindow(Query(search="piano"), ON)
    first = window.request()
    assert isinstance(first, Request)
    window.receive(first, response([row(str(n)) for n in range(1, 101)]), NOW)
    second = window.request()
    assert isinstance(second, Request)
    result = window.receive(second, Refusal(Failure.TRANSPORT), NOW)
    assert result.coverage == Coverage.PARTIAL
    assert len(result.candidates) == 100
    assert result.refusals == (Refusal(Failure.TRANSPORT),)
    assert window.request() == Refusal(Failure.LIMIT)


def test_exact_end_of_cursor_has_no_unbounded_continuation() -> None:
    window = SearchWindow(Query(search="piano"), ON, offset=MAX_OFFSET - 100)
    request = window.request()
    assert isinstance(request, Request)
    window.receive(request, response([row(str(n)) for n in range(1, 101)]), NOW)
    assert window.result().next_offset == MAX_OFFSET
    assert window.request() == Refusal(Failure.LIMIT)


def test_generic_one_point_zero_does_not_invent_a_license_uri() -> None:
    with pytest.raises(AdmissionInputError):
        parse_listing(row(license_ccurl="https://creativecommons.org/licenses/by-nc-nd/1.0/"))
    listing = parse_listing(
        row(
            license_ccurl="https://creativecommons.org/licenses/by-nd-nc/1.0/",
            licenses={"ccnc": True, "ccnd": True, "ccsa": False},
        )
    )
    assert evaluate(listing, ON, NOW, Purpose.RANKING).admitted
