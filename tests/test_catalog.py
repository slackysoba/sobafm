"""Synthetic offline regressions for catalog parsing, admission and bounds."""

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

import pytest

from sobafm.catalog import (
    MAX_CATALOG_BYTES,
    MAX_DETAIL_CHARACTERS,
    MAX_DETAILS,
    MAX_RECORDS_PER_SOURCE,
    AdmissionReview,
    Catalog,
    CatalogInputError,
    eligible,
    metadata_digest,
    parse_catalog,
    parse_reviews,
)
from sobafm.gemini_match import checked_request

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


def identifiers(value: Catalog, given: tuple[AdmissionReview, ...]) -> list[str]:
    return [r.key.recording_id for r in eligible(value, given)]


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
    assert identifiers(value, reviews(value)) == ["complete"]


def test_every_source_is_pooled_in_stable_identity_order() -> None:
    first = catalog(
        partition("fma", [recording("a")]),
        partition("jamendo", [recording("b"), recording("a")]),
        partition("ccmixter", [recording("c")]),
    )
    second = catalog(
        partition("ccmixter", [recording("c")]),
        partition("jamendo", [recording("a"), recording("b")]),
        partition("fma", [recording("a")]),
    )
    assert eligible(first, reviews(first)) == eligible(second, reviews(second))
    assert {r.key.source for r in eligible(first, reviews(first))} == {
        "jamendo",
        "ccmixter",
        "fma",
    }


def test_no_self_admission_stale_reviews_or_synthetic_scope_leakage() -> None:
    value = catalog(partition("jamendo", [recording()]))
    assert not eligible(value, ())
    review = reviews(value)[0]
    for rejected_review in (
        replace(review, metadata_digest="0" * 64),
        replace(review, credits_complete=False),
        replace(review, access_ready=False),
        replace(review, synthetic=False),
    ):
        assert not eligible(value, (rejected_review,))
    modified = catalog(partition("jamendo", [recording(facts=[fact("piano", False)])]))
    assert not eligible(modified, (review,))
    missing_credit = catalog(partition("jamendo", [recording(credits=None)]))
    assert not eligible(missing_credit, reviews(missing_credit))
    unavailable = catalog(partition("jamendo", [recording(availability="unknown")]))
    assert not eligible(unavailable, reviews(unavailable))
    assert eligible(value, (review,))[0].credits == value.partitions[0].recordings[0].credits


def test_invalid_reviews_are_refused() -> None:
    value = catalog(partition("jamendo", [recording()]))
    review = reviews(value)[0]
    with pytest.raises(CatalogInputError):
        eligible(value, (review, review))
    with pytest.raises(CatalogInputError):
        eligible(value, (replace(review, metadata_digest="not-a-digest"),))
    with pytest.raises(CatalogInputError):
        eligible(value, (replace(review, evidence="http://example.invalid/insecure"),))


def test_details_are_optional_and_kept_as_matching_hints() -> None:
    without = catalog(partition("jamendo", [recording()]))
    assert without.partitions[0].recordings[0].details == ()
    details = ["genre: dark cabaret", "bpm: 94"]
    enriched = catalog(partition("jamendo", [recording(details=details)]))
    assert enriched.partitions[0].recordings[0].details == tuple(details)


def test_details_stay_outside_the_review_binding() -> None:
    plain = catalog(partition("jamendo", [recording()]))
    enriched = catalog(partition("jamendo", [recording(details=["genre: dark cabaret"])]))
    (kept,) = eligible(enriched, reviews(plain))
    assert kept.details == ("genre: dark cabaret",)
    assert metadata_digest(kept) == metadata_digest(plain.partitions[0].recordings[0])


def test_details_cannot_stand_in_for_the_bound_metadata() -> None:
    value = catalog(partition("jamendo", [recording()]))
    changed = catalog(partition("jamendo", [recording(title="Other", details=["tag: x"])]))
    assert not eligible(changed, reviews(value))


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_seconds": True},
        {"duration_seconds": 0},
        {"duration_seconds": 86_401},
        {"duration_seconds": float("nan")},
        {"title": "x" * 201},
        {"title": "unsafe‮text"},
        {"provenance": "https://user:secret@example.invalid/file"},  # pragma: allowlist secret
        {"provenance": "https://example.invalid/?token=x"},
        {"kind": "full-ish"},
        {"facts": [fact("vocals"), fact("vocals", False)]},
        {"facts": [fact("unrecognized")]},
        {"facts": [{"feature": "piano", "value": True}]},
        {"approved": True},
        {"creators": []},
        {"details": "tag: not a list"},
        {"details": [5]},
        {"details": ["tag: same", "TAG: SAME"]},
        {"details": ["tag: unsafe‮text"]},
        {"details": ["x" * (MAX_DETAIL_CHARACTERS + 1)]},
        {"details": [f"tag: {n}" for n in range(MAX_DETAILS + 1)]},
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


def test_review_input_does_not_accept_extra_provider_approval_flags() -> None:
    review = asdict(reviews(catalog(partition("jamendo", [recording()])))[0])
    valid = json.dumps({"schema": "sobafm-admission-1", "reviews": [review]})
    assert parse_reviews(valid)[0].credits_complete
    with pytest.raises(CatalogInputError):
        parse_reviews(json.dumps({"schema": "sobafm-admission-1", "reviews": [review, review]}))
    review["credits_complete"] = "true"
    with pytest.raises(CatalogInputError):
        parse_reviews(json.dumps({"schema": "sobafm-admission-1", "reviews": [review]}))


def test_fixed_evaluation_requests_are_valid_request_text() -> None:
    path = Path(__file__).resolve().parents[1] / "evaluations/catalog/requests.json"
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    requests = cast(dict[str, object], payload)["requests"]
    assert isinstance(requests, list)
    for item in cast(list[object], requests):
        assert isinstance(item, dict)
        text = cast(dict[str, object], item)["text"]
        assert isinstance(text, str)
        assert checked_request(text)
