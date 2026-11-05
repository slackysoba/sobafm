"""The reviewed FMA catalog in Git: the application reads it, and its three files agree."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import pytest

from sobafm.catalog import Recording, eligible, parse_catalog, parse_reviews
from sobafm.credits import render_credits
from sobafm.gemini_match import MAX_SONG_CHARACTERS, render_songs
from sobafm.media import parse_media

SNAPSHOT = Path(__file__).resolve().parents[1] / "evaluations/catalog/fma-2026-11"
ACCEPTANCE = "https://github.com/slackysoba/sobafm/issues/122"
ACCEPTANCE_COMMENT = "6659945011"
# Struck by the maintainer from the listing (first three) or the final list (last two).
STRUCK = {"57892", "23943", "37909", "48387", "91974"}
MISSING_VALUE = re.compile(r":\s*null\b", re.IGNORECASE)


def text(name: str) -> str:
    return (SNAPSHOT / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def admitted() -> tuple[Recording, ...]:
    return eligible(parse_catalog(text("catalog.json")), parse_reviews(text("reviews.json")))


def test_every_recording_is_reviewed_and_has_exactly_one_file(
    admitted: tuple[Recording, ...],
) -> None:
    assert len(admitted) == 178
    media = parse_media(text("media.json"), SNAPSHOT)
    assert {item.key for item in admitted} == set(media)
    assert not STRUCK & {item.key.recording_id for item in admitted}


def test_every_review_links_the_recorded_acceptance() -> None:
    document = cast(dict[str, list[dict[str, object]]], json.loads(text("reviews.json")))
    assert {review["evidence"] for review in document["reviews"]} == {ACCEPTANCE}
    assert all(ACCEPTANCE_COMMENT in str(review["note"]) for review in document["reviews"])
    assert not any(review["synthetic"] for review in document["reviews"])


def test_the_catalog_fits_the_song_list_and_every_credit_renders_in_full(
    admitted: tuple[Recording, ...],
) -> None:
    assert len(render_songs(admitted)) <= MAX_SONG_CHARACTERS
    for recording in admitted:
        credits_ = recording.credits
        assert credits_ is not None
        assert not any(MISSING_VALUE.search(notice) for notice in credits_.notices)
        assert render_credits(recording).pages


def test_no_audio_is_kept_in_the_repository() -> None:
    assert sorted(path.name for path in SNAPSHOT.iterdir()) == [
        "catalog.json",
        "media.json",
        "reviews.json",
    ]
