"""Synthetic offline regressions for source coverage, admission and ranking."""

import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

import pytest

from sobafm.catalog import (
    MAX_CATALOG_BYTES,
    MAX_RECORDS_PER_SOURCE,
    AdmissionReview,
    Catalog,
    CatalogInputError,
    CoverageState,
    Feature,
    Source,
    metadata_digest,
    parse_catalog,
    parse_query,
    parse_reviews,
    select,
)
from sobafm.catalog_prompt import normalize_prompt

EVIDENCE = "https://example.invalid/synthetic/evidence"


def fact(feature: str, value: bool = True, strength: str = "strong") -> dict[str, object]:
    return {"feature": feature, "value": value, "strength": strength, "evidence": EVIDENCE}


def recording(identifier: str = "one", **changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "recording_id": identifier,
        "title": "Synthetic Song",
        "primary_artist": "Synthetic Artist",
        "creators": ["Synthetic Artist", "Synthetic Contributor"],
        "provenance": EVIDENCE,
        "kind": "complete",
        "kind_evidence": EVIDENCE,
        "duration_seconds": 240,
        "availability": "available",
        "composition": None,
        "composer": None,
        "facts": [fact("piano"), fact("low_energy"), fact("vocals", False)],
        "credits": {
            "snapshot_id": "synthetic-credit-v1",
            "notices": ["Synthetic test; no audio exists."],
            "license_label": None,
            "license_url": None,
        },
    }
    result.update(changes)
    return result


def partition(
    source: str, records: list[dict[str, object]], state: str = "complete"
) -> dict[str, object]:
    return {
        "source": source,
        "state": state,
        "note": "Synthetic dated prepared subset only.",
        "recordings": records,
    }


def catalog(*parts: dict[str, object]) -> Catalog:
    return parse_catalog(
        json.dumps({"schema": "sobafm-catalog-1", "synthetic": True, "partitions": parts})
    )


def reviews(value: Catalog) -> tuple[AdmissionReview, ...]:
    return tuple(
        AdmissionReview(r.key, metadata_digest(r), True, True, True, EVIDENCE)
        for p in value.partitions
        for r in p.recordings
    )


def test_stronger_fit_beats_longer_and_weak_matches() -> None:
    value = catalog(
        partition(
            "jamendo",
            [
                recording(
                    "long-weak",
                    duration_seconds=7200,
                    facts=[fact("piano", strength="weak"), fact("low_energy", strength="weak")],
                ),
                recording("short-strong", duration_seconds=120),
                recording("long-one-match", duration_seconds=3600, facts=[fact("piano")]),
            ],
        )
    )
    query = parse_query(
        {
            "preferences": [
                {"feature": "piano", "value": True},
                {"feature": "low_energy", "value": True},
            ]
        }
    )
    result = select(value, query, reviews(value))
    assert [m.recording.key.recording_id for m in result.matches] == [
        "short-strong",
        "long-one-match",
        "long-weak",
    ]
    assert result.matches[1].unknown == ("low_energy",)
    assert result.matches[2].strong == ()


def test_equal_fit_prefers_longer_complete_recording_not_short_song_ceiling() -> None:
    value = catalog(
        partition(
            "jamendo",
            [recording("short", duration_seconds=60), recording("long", duration_seconds=18_000)],
        )
    )
    result = select(
        value, parse_query({"preferences": [{"feature": "piano", "value": True}]}), reviews(value)
    )
    assert result.matches[0].recording.key.recording_id == "long"


@pytest.mark.parametrize("kind", ["preview", "excerpt", "stem", "loop", "sample_pack", "unknown"])
def test_duration_and_review_cannot_turn_noncomplete_content_into_song(kind: str) -> None:
    value = catalog(
        partition(
            "jamendo",
            [
                recording("not-song", kind=kind, duration_seconds=8000),
                recording("complete", duration_seconds=30),
            ],
        )
    )
    result = select(value, parse_query({"title": "Synthetic Song"}), reviews(value))
    assert [m.recording.key.recording_id for m in result.matches] == ["complete"]
    assert "not a confirmed complete recording" in result.rejected[0].reasons


def test_explicit_duration_bounds_unknown_and_exact_duration() -> None:
    value = catalog(
        partition(
            "fma",
            [
                recording("unknown", duration_seconds=None),
                recording("short", duration_seconds=179),
                recording("right", duration_seconds=180),
                recording("long", duration_seconds=181),
            ],
        )
    )
    result = select(value, parse_query({"min_seconds": 180, "max_seconds": 180}), reviews(value))
    assert [m.recording.key.recording_id for m in result.matches] == ["right"]
    assert any(r.reasons == ("requested duration unknown",) for r in result.rejected)


def test_hard_identity_and_creator_exclusion_never_fall_back_to_style() -> None:
    value = catalog(
        partition(
            "ccmixter",
            [
                recording("exact"),
                recording("cover", primary_artist="Cover Artist", creators=["Cover Artist"]),
            ],
        )
    )
    query = parse_query({"title": "synthetic song", "artist": "synthetic artist"})
    assert [m.recording.key.recording_id for m in select(value, query, reviews(value)).matches] == [
        "exact"
    ]
    assert not select(value, replace(query, title="Unavailable Song"), reviews(value)).matches
    excluded = parse_query(
        {"title": "Synthetic Song", "excluded_creators": ["synthetic contributor"]}
    )
    assert [
        m.recording.key.recording_id for m in select(value, excluded, reviews(value)).matches
    ] == ["cover"]
    identity = parse_query({"recording": {"source": "jamendo", "recording_id": "exact"}})
    assert not select(value, identity, reviews(value)).matches
    no_exact = parse_query(
        {
            "title": "Synthetic Song",
            "excluded_recordings": [
                {"source": "ccmixter", "recording_id": "exact"},
            ],
        }
    )
    assert [
        m.recording.key.recording_id for m in select(value, no_exact, reviews(value)).matches
    ] == ["cover"]


def test_named_composition_requires_composer_and_arrangement_evidence() -> None:
    value = catalog(
        partition(
            "fma",
            [
                recording(
                    "right",
                    composition="Gymnopédie No. 1",
                    composer="Erik Satie",
                    facts=[fact("solo_piano")],
                ),
                recording("wrong-work", composition="Gymnopédie No. 2", composer="Erik Satie"),
                recording("unknown-composer", composition="Gymnopédie No. 1"),
            ],
        )
    )
    query = parse_query(
        {
            "composition": "Gymnopédie No. 1",
            "composer": "Erik Satie",
            "required": [{"feature": "solo_piano", "value": True}],
        }
    )
    assert [m.recording.key.recording_id for m in select(value, query, reviews(value)).matches] == [
        "right"
    ]


def test_hard_negative_requires_strong_absence_evidence() -> None:
    value = catalog(
        partition(
            "jamendo",
            [
                recording("unknown", facts=[fact("piano")]),
                recording("weak", facts=[fact("piano"), fact("vocals", False, "weak")]),
                recording("contradicted", facts=[fact("piano"), fact("vocals")]),
                recording("right"),
            ],
        )
    )
    query = parse_query(
        {
            "preferences": [{"feature": "piano", "value": True}],
            "required": [{"feature": "vocals", "value": False}],
        }
    )
    result = select(value, query, reviews(value))
    assert [m.recording.key.recording_id for m in result.matches] == ["right"]
    assert len(result.rejected) == 3


def test_soft_contradictions_unknowns_and_no_fit_are_visible() -> None:
    value = catalog(
        partition(
            "jamendo",
            [
                recording("contradiction", facts=[fact("piano"), fact("low_energy", False)]),
                recording("unknown", facts=[fact("piano")]),
                recording("none", facts=[]),
            ],
        )
    )
    query = parse_query(
        {
            "preferences": [
                {"feature": "piano", "value": True},
                {"feature": "low_energy", "value": True},
            ]
        }
    )
    result = select(value, query, reviews(value))
    assert [m.recording.key.recording_id for m in result.matches] == ["unknown", "contradiction"]
    assert result.matches[1].contradicted == ("low_energy",)
    assert result.rejected[0].reasons == ("no supported prompt preference",)


@pytest.mark.parametrize("winner", list(Source))
def test_winner_can_come_from_every_source_regardless_of_partition_order(winner: Source) -> None:
    parts = [
        partition(
            s,
            [
                recording(
                    s, facts=[fact("piano"), fact("low_energy")] if s == winner else [fact("piano")]
                )
            ],
        )
        for s in reversed(list(Source))
    ]
    value = catalog(*parts)
    query = parse_query(
        {
            "preferences": [
                {"feature": "piano", "value": True},
                {"feature": "low_energy", "value": True},
            ]
        }
    )
    result = select(value, query, reviews(value), limit=1)
    assert result.matches[0].recording.key.source == winner
    assert [c.matched for c in result.coverage] == [1, 1, 1]
    assert result.total_matches == 3


def test_missing_empty_partial_and_unavailable_sources_are_distinct() -> None:
    value = catalog(partition("jamendo", []), partition("ccmixter", [recording()], "partial"))
    result = select(value, parse_query({"title": "Synthetic Song"}), reviews(value))
    assert [c.state for c in result.coverage] == [
        CoverageState.EMPTY,
        CoverageState.PARTIAL,
        CoverageState.MISSING,
    ]
    assert result.coverage[1].provided == 1
    unavailable = catalog(partition("fma", [], "unavailable"))
    assert (
        select(unavailable, parse_query({"title": "Synthetic Song"}), ()).coverage[2].state
        == CoverageState.UNAVAILABLE
    )
    partial_empty = catalog(partition("fma", [], "partial"))
    assert (
        select(partial_empty, parse_query({"title": "Synthetic Song"}), ()).coverage[2].state
        == CoverageState.PARTIAL
    )


def test_ties_use_source_and_recording_identity_not_input_order() -> None:
    first = catalog(
        partition("jamendo", [recording("b"), recording("a")]), partition("fma", [recording("a")])
    )
    second = catalog(
        partition("fma", [recording("a")]), partition("jamendo", [recording("a"), recording("b")])
    )
    query = parse_query({"title": "Synthetic Song"})
    assert (
        select(first, query, reviews(first)).matches
        == select(second, query, reviews(second)).matches
    )


def test_no_self_admission_stale_reviews_or_synthetic_scope_leakage() -> None:
    value = catalog(partition("jamendo", [recording()]))
    query = parse_query({"title": "Synthetic Song"})
    assert not select(value, query, ()).matches
    review = reviews(value)[0]
    for rejected_review in (
        replace(review, metadata_digest="0" * 64),
        replace(review, credits_complete=False),
        replace(review, access_ready=False),
        replace(review, synthetic=False),
    ):
        assert not select(value, query, (rejected_review,)).matches
    modified = catalog(partition("jamendo", [recording(facts=[fact("piano", False)])]))
    assert not select(modified, query, (review,)).matches
    missing_credit = catalog(partition("jamendo", [recording(credits=None)]))
    assert not select(missing_credit, query, reviews(missing_credit)).matches
    unavailable = catalog(partition("jamendo", [recording(availability="unknown")]))
    assert not select(unavailable, query, reviews(unavailable)).matches
    assert (
        select(value, query, (review,)).matches[0].recording.credits
        == value.partitions[0].recordings[0].credits
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_seconds": True},
        {"duration_seconds": 0},
        {"duration_seconds": 86_401},
        {"duration_seconds": float("nan")},
        {"title": "x" * 201},
        {"title": "unsafe\u202etext"},
        {"provenance": "https://user:secret@example.invalid/file"},  # pragma: allowlist secret
        {"provenance": "https://example.invalid/?token=x"},
        {"kind": "full-ish"},
        {"facts": [fact("vocals"), fact("vocals", False)]},
        {"facts": [fact("unrecognized")]},
        {"facts": [{"feature": "piano", "value": True}]},
        {"approved": True},
        {"creators": []},
    ],
)
def test_invalid_external_metadata_fails_atomically(changes: dict[str, object]) -> None:
    record = recording()
    record.update(changes)
    with pytest.raises(CatalogInputError):
        catalog(partition("jamendo", [record]))


def test_bounded_inputs_duplicate_keys_and_incomplete_real_inventory() -> None:
    for payload in ("x" * (MAX_CATALOG_BYTES + 1), "\ud800", "{" * 2000, '{"schema":1,"schema":2}'):
        with pytest.raises(CatalogInputError):
            parse_catalog(payload)
    with pytest.raises(CatalogInputError):
        catalog(
            partition("jamendo", [recording(str(i)) for i in range(MAX_RECORDS_PER_SOURCE + 1)])
        )
    with pytest.raises(CatalogInputError):
        catalog(partition("jamendo", [recording(), recording()]))
    with pytest.raises(CatalogInputError):
        catalog(partition("fma", []), partition("fma", []))
    with pytest.raises(CatalogInputError):
        catalog(partition("fma", [recording()], "unavailable"))
    inventory = (
        Path(__file__).resolve().parents[1] / "evaluations/catalog/three-source-inventory.json"
    )
    with pytest.raises(CatalogInputError):
        parse_catalog(inventory.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"min_seconds": True},
        {"min_seconds": 600, "max_seconds": 200},
        {"prompt": "piano"},
        {"preferences": [{"feature": "piano", "value": "yes"}]},
        {"preferences": [{"feature": "piano", "value": True}] * 17},
        {
            "required": [{"feature": "vocals", "value": False}],
            "preferences": [{"feature": "vocals", "value": True}],
        },
        {"recording": {"recording_id": "missing-source"}},
        {"artist": "Artist", "excluded_creators": ["artist"]},
    ],
)
def test_model_query_validation_rejects_unsafe_or_unbounded_constraints(value: object) -> None:
    with pytest.raises(CatalogInputError):
        parse_query(value)


def test_review_input_does_not_accept_extra_provider_approval_flags() -> None:
    review = asdict(reviews(catalog(partition("jamendo", [recording()])))[0])
    valid = json.dumps({"schema": "sobafm-admission-1", "reviews": [review]})
    assert parse_reviews(valid)[0].credits_complete
    with pytest.raises(CatalogInputError):
        parse_reviews(json.dumps({"schema": "sobafm-admission-1", "reviews": [review, review]}))
    review["credits_complete"] = "true"
    with pytest.raises(CatalogInputError):
        parse_reviews(json.dumps({"schema": "sobafm-admission-1", "reviews": [review]}))


def test_prompt_normalizer_reports_unknowns_and_keeps_explicit_constraints() -> None:
    result = normalize_prompt(
        "Quiet solo piano with celestial textures, no singing, under 5 minutes."
    )
    assert result.query is not None
    assert result.query.max_seconds == 299
    assert result.unsupported_terms == ("celestial", "textures")
    assert result.query.required[0].feature == Feature.VOCALS
    assert result.query.required[0].value is False
    assert normalize_prompt("Play Blinding Lights by The Weeknd.").query == parse_query(
        {"title": "blinding lights", "artist": "the weeknd"}
    )
    assert normalize_prompt("ambient no drums or singing").query is not None


@pytest.mark.parametrize(
    "prompt",
    [
        "piano without bagpipes",
        "not only jazz",
        "no vocals and upbeat funk",
        "only jazz or blues",
        "only piano or guitar",
        "only piano and guitar",
        "no solo piano only piano",
        "piano for 5 minutes",
        "between 3 and 5 minutes piano",
        "piano no vocals with singing",
        "only vocals instrumental",
        "recording of Satie",
        "avoid vocals",
        "I don't want singing",
        "I dont want singing",
        "never vocals",
        "excluding vocals",
        "must have piano",
        "do not include singing",
        "anything but vocals",
        "vocals-free jazz",
    ],
)
def test_unsupported_hard_prompt_syntax_never_becomes_soft_preference(prompt: str) -> None:
    assert normalize_prompt(prompt).query is None


def test_fixed_requests_remain_bounded_and_do_not_invent_ratings() -> None:
    path = Path(__file__).resolve().parents[1] / "evaluations/catalog/requests.json"
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    requests = cast(dict[str, object], payload)["requests"]
    assert isinstance(requests, list)
    for item in cast(list[object], requests):
        assert isinstance(item, dict)
        text = cast(dict[str, object], item)["text"]
        assert isinstance(text, str)
        result = normalize_prompt(text)
        assert result.query is not None or result.problem


@pytest.mark.parametrize("synthetic", [True, False])
def test_demo_reads_catalog_and_separate_reviews_with_full_coverage(
    tmp_path: Path, synthetic: bool
) -> None:
    root = Path(__file__).resolve().parents[1]
    catalog_path = tmp_path / "catalog.json"
    review_path = tmp_path / "reviews.json"
    # Test both ingestion modes with invented data; this creates no real-source approval.
    for source_name, destination in (
        ("selector-demo.json", catalog_path),
        ("selector-demo-reviews.json", review_path),
    ):
        payload = (root / "evaluations/catalog" / source_name).read_text(encoding="utf-8")
        if not synthetic:
            payload = payload.replace('"synthetic": true', '"synthetic": false')
        destination.write_text(payload, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sobafm.catalog_demo",
            "Quiet solo piano, no singing",
            "--catalog",
            str(catalog_path),
            "--reviews",
            str(review_path),
            "--limit",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output: object = json.loads(result.stdout)
    assert isinstance(output, dict)
    assert output["synthetic"] is synthetic
    assert "demo-c-fit" in result.stdout
    assert "partial" in result.stdout and "no trusted admission review" in result.stdout
    assert all(source in result.stdout for source in Source)


@pytest.mark.parametrize("limit", [0, 21, True])
def test_result_limits_are_bounded(limit: int) -> None:
    with pytest.raises(CatalogInputError):
        select(catalog(), parse_query({"title": "Synthetic Song"}), (), limit=limit)
