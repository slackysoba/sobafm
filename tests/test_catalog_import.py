"""Offline import behavior; real observations are separated from invented fixtures."""

import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

import pytest

from sobafm.catalog import CatalogInputError, Feature, Source, parse_catalog, parse_query, select
from sobafm.catalog_import import (
    MAX_IMPORT_BYTES,
    ImportContext,
    ImportResult,
    catalog_document,
    normalize,
    parse_context,
    parse_duration,
)

FIXTURES = Path(__file__).resolve().parents[1] / "evaluations/catalog"


def fixture(name: str) -> str:
    return (FIXTURES / ("import-" + name + ".json")).read_text(encoding="utf-8")


def context(source: str) -> ImportContext:
    return parse_context(fixture(source + "-context"))


def jamendo(**changes: object) -> ImportResult:
    payload = cast(dict[str, object], json.loads(fixture("jamendo-synthetic")))
    rows = cast(list[dict[str, object]], payload["results"])
    rows[0].update(changes)
    return normalize(Source.JAMENDO, json.dumps(payload), context("jamendo"))


def ccmixter(**changes: object) -> ImportResult:
    rows = cast(list[dict[str, object]], json.loads(fixture("ccmixter-observed")))
    rows[0].update(changes)
    return normalize(Source.CCMIXTER, json.dumps(rows), context("ccmixter"))


def test_jamendo_maps_concrete_fields_without_inventing_strong_or_available_evidence() -> None:
    result = jamendo()
    record = result.catalog.partitions[0].recordings[0]
    assert record.key.recording_id == "900001" and result.catalog.synthetic
    assert record.title == "Synthetic Piano Piece" and record.primary_artist == "Synthetic Pianist"
    assert record.duration_seconds == 420
    assert record.kind == "unknown" and record.availability == "unknown"
    assert {f.feature: f.value for f in record.facts} == {
        Feature.ACOUSTIC: True,
        Feature.JAZZ: True,
        Feature.PIANO: True,
        Feature.VOCALS: False,
    }
    assert all(f.strength == "weak" and f.evidence == record.provenance for f in record.facts)
    assert record.credits is not None
    assert record.credits.license_url == "https://creativecommons.org/licenses/by/4.0/"
    assert "artist_id: 800001" in record.credits.notices
    assert "Album: Synthetic Album" in record.credits.notices
    assert "speed_not_energy" in result.report.records[0].issues
    assert not select(result.catalog, parse_query({"title": record.title}), ()).matches


@pytest.mark.parametrize("value", [None, "", 0, 1, False, "unknown", "both"])
def test_vocal_defaults_never_become_known_absence(value: object) -> None:
    result = jamendo(musicinfo={"vocalinstrumental": value})
    assert not result.catalog.partitions[0].recordings[0].facts


def test_conflicting_tags_are_unknown_and_unknown_fields_are_reported_without_raw_values() -> None:
    result = jamendo(
        musicinfo={
            "vocalinstrumental": "vocal",
            "tags": {"vartags": ["instrumental", "alien_style"]},
        },
        client_id="synthetic-private-marker",
        audio="https://media.example.invalid/track.mp3?token=synthetic-hidden-marker",
        audiodownload_allowed=True,
    )
    encoded = json.dumps(asdict(result))
    assert "synthetic-private-marker" not in encoded and "synthetic-hidden-marker" not in encoded
    assert "media.example.invalid" not in encoded
    assert result.report.records[0].ignored_field_count == 1
    assert "contradictory_descriptor" in result.report.records[0].issues
    assert "unmapped_descriptor" in result.report.records[0].issues
    assert not result.catalog.partitions[0].recordings[0].facts


def test_unidentified_license_is_preserved_without_inventing_a_license_family() -> None:
    result = jamendo(license_ccurl="https://license.example.invalid/terms")
    credits = result.catalog.partitions[0].recordings[0].credits
    assert credits is not None and credits.license_label == "License terms (unidentified)"
    assert credits.license_url == "https://license.example.invalid/terms"
    assert "license_family_unidentified" in result.report.records[0].issues
    unsafe = jamendo(license_ccurl="https://media.example.invalid/audio.mp3")
    credits = unsafe.catalog.partitions[0].recordings[0].credits
    assert credits is not None and credits.license_url is None
    assert "license_requires_review" in unsafe.report.records[0].issues


@pytest.mark.parametrize(
    "duration, expected",
    [
        (1, 1),
        (86400, 86400),
        (0, None),
        (-1, None),
        (86401, None),
        (True, None),
        (1.5, None),
        ("192", None),
        (None, None),
        (float("nan"), None),
    ],
)
def test_numeric_duration_is_not_coerced(duration: object, expected: int | None) -> None:
    assert parse_duration(duration) == expected
    assert (
        jamendo(duration=duration).catalog.partitions[0].recordings[0].duration_seconds == expected
    )


@pytest.mark.parametrize(
    "duration, expected",
    [
        ("3:12", 192),
        ("0:01", 1),
        ("1:02:03", 3723),
        ("1440:00", 86400),
        ("24:00:00", 86400),
        ("3:60", None),
        ("1:60:00", None),
        ("3:2", None),
        ("3:12.5", None),
        ("-1:02", None),
        (" 3:12", None),
        ("0:00", None),
        ("25:00:00", None),
        (192, None),
        (True, None),
    ],
)
def test_ccmixter_clock_duration_has_strict_component_bounds(
    duration: object, expected: int | None
) -> None:
    assert parse_duration(duration, clock=True) == expected


def test_observed_ccmixter_preserves_partial_batch_and_incomplete_credits() -> None:
    result = ccmixter()
    record = result.catalog.partitions[0].recordings[0]
    assert not result.catalog.synthetic
    assert result.report.state == "partial" and result.report.missing_ids == ("70993",)
    assert result.report.returned == result.report.normalized == 1
    assert record.key.recording_id == "17241"
    assert record.duration_seconds == 192
    assert result.report.records[0].source_file_id == "18462"
    assert result.report.records[0].reported_bpm == 81
    assert record.creators == ("Admiral Bob", "Shannon Hurley", "shannonsongs")
    assert record.credits is not None
    assert record.credits.license_url == "https://creativecommons.org/licenses/by-nc/3.0/"
    notices = " ".join(record.credits.notices)
    assert "15139" in notices and "license: unknown" in notices
    assert "Source file identity: 18462" in notices and "Sample statement (as supplied)" in notices
    assert "upstream_notices_and_chain_unverified" in result.report.records[0].issues
    assert record.kind == "unknown" and record.availability == "unknown"
    assert not select(result.catalog, parse_query({"title": "Sunrise (unplugged)"}), ()).matches
    assert result.report.admission_reviews_created == 0


def test_prepared_ccmixter_chain_preserves_imported_facts_without_self_admission() -> None:
    result = normalize(
        Source.CCMIXTER,
        fixture("ccmixter-playback-observed"),
        context("ccmixter-playback"),
    )
    assert result.report.returned == result.report.normalized == 3
    assert result.report.missing_ids == () and result.report.state == "complete"
    imported = {r.key.recording_id: r for r in result.catalog.partitions[0].recordings}
    assert set(imported) == {"70993", "70072", "25112"}
    for record in imported.values():
        assert record.kind == "unknown" and record.availability == "unknown"
        assert record.credits is not None
        assert record.credits.license_url == "https://creativecommons.org/licenses/by-nc/3.0/"
    assert result.report.records[0].source_file_id == "129564"
    assert result.report.records[0].reported_bpm == 105
    assert result.report.admission_reviews_created == 0
    assert imported["70072"].duration_seconds is None
    assert imported["25112"].duration_seconds is None

    prepared = parse_catalog(
        (FIXTURES / "ccmixter-playback-candidate.json").read_text(encoding="utf-8")
    )
    assert not prepared.synthetic and len(prepared.partitions) == 1
    assert len(prepared.partitions[0].recordings) == 1
    candidate = prepared.partitions[0].recordings[0]
    raw_mix = imported["70993"]
    assert candidate.key == raw_mix.key
    assert candidate.title == raw_mix.title and candidate.primary_artist == raw_mix.primary_artist
    assert candidate.provenance == raw_mix.provenance
    assert candidate.duration_seconds == raw_mix.duration_seconds == 129
    assert candidate.facts == raw_mix.facts
    assert candidate.kind == "complete" and candidate.availability == "unknown"
    assert set(candidate.creators) == {
        "Ant.Survila",
        "Dan_Mantau",
        "Lisa DeBenedictis",
        "DeBenedictis",
    }
    assert candidate.credits is not None
    notices = " ".join(candidate.credits.notices)
    for required in ("129564", "70072", "25112", "4693829", "a cappella", "instrumental"):
        assert required in notices
    selection = select(prepared, parse_query({"title": "WILD WINDSTEPS"}), ())
    assert not selection.matches


def test_multiple_files_require_explicit_selection_and_do_not_infer_duration() -> None:
    files = [
        {"file_id": 1, "file_format_info": {"mime_type": "audio/mpeg", "ps": "3:12"}},
        {"file_id": 2, "file_format_info": {"mime_type": "application/zip", "ps": "9:00"}},
    ]
    result = ccmixter(files=files)
    assert result.catalog.partitions[0].recordings[0].duration_seconds is None
    assert "source_file_selection_required" in result.report.records[0].issues
    rows = cast(list[dict[str, object]], json.loads(fixture("ccmixter-observed")))
    rows[0]["files"] = files
    for file_id, expected in (("1", 192), ("2", None), ("3", None)):
        scope = replace(context("ccmixter"), selected_files=(("17241", file_id),))
        selected = normalize(Source.CCMIXTER, json.dumps(rows), scope)
        assert selected.catalog.partitions[0].recordings[0].duration_seconds == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"files": [{"file_id": 1, "file_upload": 9}]},
        {"files": [{"file_id": 1}, {"file_id": 1}]},
        {"upload_id": True},
        {"file_page_url": "https://ccmixter.org/files/admiralbob77/999"},
        {"upload_name": "unsafe\u202etitle"},
        {"upload_extra": {"featuring": "<b>hidden</b>"}},
        {"notices": ["notice"] * 13},
        {"upload_extra": {"usertags": ",".join(["piano"] * 65)}},
    ],
)
def test_malformed_ccmixter_records_are_visible_and_never_silently_partially_admitted(
    changes: dict[str, object],
) -> None:
    result = ccmixter(**changes)
    assert not result.catalog.partitions[0].recordings
    assert result.report.state == "partial" and not result.report.records[0].accepted_metadata


def test_duplicates_reject_both_records_regardless_of_input_order() -> None:
    rows = cast(list[dict[str, object]], json.loads(fixture("ccmixter-observed")))
    rows.append({**rows[0], "upload_name": "Conflicting title"})
    for values in (rows, list(reversed(rows))):
        result = normalize(Source.CCMIXTER, json.dumps(values), context("ccmixter"))
        assert not result.catalog.partitions[0].recordings
        assert all(not row.accepted_metadata for row in result.report.records)
        assert "duplicate_identities_returned" in result.report.issues


def test_unexpected_identity_is_reported_and_not_imported() -> None:
    scope = replace(context("ccmixter"), requested_ids=("70993",))
    result = normalize(Source.CCMIXTER, fixture("ccmixter-observed"), scope)
    assert result.report.unexpected_ids == ("17241",) and result.report.missing_ids == ("70993",)
    assert not result.catalog.partitions[0].recordings


def test_empty_partial_unavailable_and_failed_sources_are_distinct() -> None:
    scope = replace(context("ccmixter"), requested_ids=())
    assert normalize(Source.CCMIXTER, "[]", scope).report.state == "empty"
    assert (
        normalize(Source.CCMIXTER, "[]", replace(scope, state="partial")).report.state == "partial"
    )
    unavailable = normalize(Source.CCMIXTER, None, replace(scope, state="unavailable"))
    assert unavailable.report.state == "unavailable"
    with pytest.raises(CatalogInputError):
        normalize(Source.CCMIXTER, "[]", replace(scope, state="unavailable"))
    failed = json.dumps(
        {
            "headers": {"status": "failed", "code": 4, "error_message": "do not echo this"},
            "results": [],
        }
    )
    failure = normalize(Source.JAMENDO, failed, context("jamendo"))
    assert failure.report.state == "unavailable" and "do not echo this" not in json.dumps(
        asdict(failure)
    )
    for headers in (
        {"status": "mystery", "code": 0},
        {"status": "success", "code": 4},
        {"status": "success", "code": False},
    ):
        with pytest.raises(CatalogInputError):
            normalize(
                Source.JAMENDO, json.dumps({"headers": headers, "results": []}), context("jamendo")
            )


def test_jamendo_header_mismatch_and_warning_are_partial_without_raw_error_text() -> None:
    payload = cast(dict[str, object], json.loads(fixture("jamendo-synthetic")))
    payload["headers"] = {
        "status": "success",
        "code": 0,
        "results_count": 2,
        "warnings": "sensitive upstream message",
    }
    result = normalize(Source.JAMENDO, json.dumps(payload), context("jamendo"))
    assert result.report.state == "partial"
    assert "response_count_mismatch" in result.report.issues
    assert "sensitive upstream message" not in json.dumps(asdict(result))


def test_failed_response_with_warning_remains_unavailable() -> None:
    response = json.dumps(
        {
            "headers": {"status": "failed", "code": 4, "warnings": "not copied"},
            "results": [],
        }
    )
    result = normalize(Source.JAMENDO, response, context("jamendo"))
    assert result.report.state == "unavailable"
    assert result.catalog.partitions[0].state == "unavailable"
    assert "source_warning_not_copied" in result.report.issues


def test_malformed_requested_row_differs_from_absent_row_and_keeps_valid_peer() -> None:
    rows = cast(list[dict[str, object]], json.loads(fixture("ccmixter-observed")))
    rows.append({"upload_id": 70993, "upload_name": None})
    result = normalize(Source.CCMIXTER, json.dumps(rows), context("ccmixter"))
    assert result.report.returned == 2 and result.report.normalized == 1
    assert result.report.missing_ids == () and result.report.state == "partial"
    assert result.report.records[1].issues == ("malformed_record_not_imported",)
    assert result.catalog.partitions[0].recordings[0].key.recording_id == "17241"


def test_fma_operator_manifest_preserves_credits_evidence_and_remains_unadmitted() -> None:
    result = normalize(Source.FMA, fixture("fma-synthetic"), context("fma"))
    record = result.catalog.partitions[0].recordings[0]
    assert result.catalog.synthetic and record.key.recording_id.startswith("/music/")
    assert record.kind == "complete" and record.duration_seconds == 480
    assert record.availability == "unknown" and record.credits is not None
    assert any("Performer and composer" in notice for notice in record.credits.notices)
    assert any("Duration reported:" in notice for notice in record.credits.notices)
    assert not select(result.catalog, parse_query({"title": record.title}), ()).matches
    serialized = json.dumps(catalog_document(result.catalog))
    assert parse_catalog(serialized) == result.catalog


def test_snapshot_identity_binds_full_source_identity_and_changed_credits() -> None:
    payload = cast(dict[str, object], json.loads(fixture("fma-synthetic")))
    rows = cast(list[dict[str, object]], payload["recordings"])
    original = rows[0]
    rows.append({**original, "source_url": cast(str, original["source_url"]) + "Second/"})
    first = normalize(Source.FMA, json.dumps(payload), context("fma"))
    ids = [r.credits.snapshot_id for r in first.catalog.partitions[0].recordings if r.credits]
    assert len(set(ids)) == 2 and all(value.startswith("fma-") for value in ids)
    original["notices"] = ["Changed credit notice"]
    second = normalize(Source.FMA, json.dumps(payload), context("fma"))
    changed = second.catalog.partitions[0].recordings[0].credits
    assert changed is not None and changed.snapshot_id != ids[0]
    assert len(changed.snapshot_id) <= 128


@pytest.mark.parametrize(
    "changes",
    [
        {"import_status": "ready"},
        {"approved": True},
        {"source_url": "https://cdn.example.invalid/audio.mp3"},
        {"recording_kind": "fullish"},
        {"creators": []},
    ],
)
def test_invalid_fma_manifest_fields_fail_closed(changes: dict[str, object]) -> None:
    payload = cast(dict[str, object], json.loads(fixture("fma-synthetic")))
    rows = cast(list[dict[str, object]], payload["recordings"])
    rows[0].update(changes)
    result = normalize(Source.FMA, json.dumps(payload), context("fma"))
    assert not result.catalog.partitions[0].recordings and result.report.state == "partial"


def test_unbounded_and_invalid_inputs_never_emit_a_catalog() -> None:
    for value in (
        "x" * (MAX_IMPORT_BYTES + 1),
        "\ud800",
        "[" * 2000,
        '{"x":1,"x":2}',
        json.dumps([{}] * 257),
    ):
        with pytest.raises(CatalogInputError):
            normalize(Source.CCMIXTER, value, context("ccmixter"))
    for changes in (
        {"observed_at": "2026-99-01"},
        {"evidence_kind": "live_verified"},
        {"requested_ids": ["1", "1"]},
        {"scope": "https://private.example.invalid/?secret=hidden"},
        {"state": "ready"},
        {"client_id": "not-to-be-accepted"},
    ):
        data = cast(dict[str, object], json.loads(fixture("ccmixter-context")))
        data.update(changes)
        with pytest.raises(CatalogInputError):
            parse_context(json.dumps(data))


@pytest.mark.parametrize(
    "source, name",
    [
        (Source.JAMENDO, "jamendo-synthetic"),
        (Source.CCMIXTER, "ccmixter-observed"),
        (Source.FMA, "fma-synthetic"),
    ],
)
def test_cli_emits_selector_schema_and_separate_safe_report(source: Source, name: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sobafm.catalog_import",
            source,
            "--input",
            str(FIXTURES / ("import-" + name + ".json")),
            "--context",
            str(FIXTURES / ("import-" + source + "-context.json")),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    catalog = parse_catalog(completed.stdout)
    report = cast(dict[str, object], json.loads(completed.stderr))
    assert len(catalog.partitions) == 1 and report["admission_reviews_created"] == 0
    assert catalog.synthetic is (source != Source.CCMIXTER)


def test_cli_failure_has_no_catalog_or_private_path(tmp_path: Path) -> None:
    private_path = tmp_path / "private-input-name.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sobafm.catalog_import",
            "jamendo",
            "--context",
            str(private_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode != 0 and not completed.stdout
    assert str(private_path) not in completed.stderr and "Traceback" not in completed.stderr
