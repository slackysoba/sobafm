"""The written FMA batch rule: which listings qualify and what reaches the catalog. Offline."""

from __future__ import annotations

import csv
import json
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from sobafm.catalog import Availability, RecordingKind, Source, eligible, parse_reviews
from sobafm.catalog_import import ImportContext, ImportResult, normalize
from sobafm.catalog_prepare import (
    ANY_CC,
    DATASET_NOTICE,
    MAX_FILE_BYTES,
    PER_ARTIST,
    PLAYBACK_NOTICE,
    Listing,
    Row,
    VerifiedFile,
    admission_reviews,
    candidates,
    finalized_catalog,
    genres,
    license_class,
    load_listing,
    manifest,
    manifest_row,
    media_manifest,
    rejection,
    track_page,
)
from sobafm.media import parse_media
from sobafm.pcm import BYTES_PER_SECOND, FRAME_BYTES

BY_NC = "http://creativecommons.org/licenses/by-nc/3.0/"
PAGE = "http://freemusicarchive.org/music/Some_Artist/Some_Album/Song_{}"


def genre_field(*titles: str) -> str:
    return str([{"genre_id": "1", "genre_title": title, "genre_url": "x"} for title in titles])


def listing(track_id: int = 100, /, **changes: str) -> Listing:
    base: Listing = {
        "id": str(track_id),
        "album.information": "<p>A warm &amp; slow album.</p>",
        "album.tags": "['warm', 'slow']",
        "album.title": "Some Album",
        "album.type": "Album",
        "artist.bio": "<p>Plays piano.</p>",
        "artist.id": str(track_id),
        "artist.location": "Portland OR",
        "artist.name": f"Artist {track_id}",
        "artist.related_projects": "Air, Helios",
        "artist.tags": "['piano']",
        "track.bit_rate": "256000",
        "track.composer": "",
        "track.date_created": "2013-04-03 17:43:30",
        "track.duration": "180",
        "track.information": "",
        "track.language_code": "",
        "track.listens": "5000",
        "track.lyricist": "",
        "track.publisher": "",
        "track.tags": "['melodic']",
        "track.title": f"Song {track_id}",
        "license_title": "Attribution-NonCommercial 3.0 International",
        "license_url": BY_NC,
        "track_copyright_c": "",
        "track_copyright_p": "",
        "track_explicit": "",
        "track_genres": genre_field("Ambient"),
        "track_instrumental": "0",
        "track_url": PAGE.format(track_id),
    }
    base.update(changes)
    return base


def imported(rows: list[Row]) -> ImportResult:
    context = ImportContext("2026-11-05", "observed", "test", "partial", (), ())
    return normalize(Source.FMA, json.dumps(manifest(rows), ensure_ascii=False), context)


def test_a_listed_song_with_a_cc_license_and_a_page_qualifies() -> None:
    assert rejection(listing()) is None


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("http://creativecommons.org/licenses/by/3.0/", "by"),
        ("https://creativecommons.org/licenses/by/4.0/", "by"),
        ("http://creativecommons.org/licenses/by-nc-sa/3.0/us/", "by-nc-sa"),
        ("http://creativecommons.org/licenses/by-nc-nd/2.5/", "by-nc-nd"),
        ("http://creativecommons.org/licenses/by-sa/4.0", "by-sa"),
    ],
)
def test_license_links_are_read_by_class(url: str, kind: str) -> None:
    assert license_class(listing(license_url=url)) == kind
    assert rejection(listing(license_url=url)) is None


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://freemusicarchive.org/Sound_Recording_Common_Law",
        "https://example.org/licenses/by/3.0/",
        "http://creativecommons.org/publicdomain/zero/1.0/",
    ],
)
def test_anything_without_a_cc_license_link_is_rejected(url: str) -> None:
    assert rejection(listing(license_url=url)) == "no Creative Commons license link"


def test_a_narrower_license_set_rejects_the_rest() -> None:
    narrow = frozenset({"by", "by-nc"})
    assert rejection(listing(), narrow) is None
    shared = listing(license_url="http://creativecommons.org/licenses/by-nc-sa/3.0/")
    assert rejection(shared, narrow) == "license outside the accepted set"
    assert rejection(shared, ANY_CC) is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"track_url": ""}, "no usable track page"),
        ({"track_url": "http://example.org/music/a"}, "no usable track page"),
        ({"track_url": "http://freemusicarchive.org/music/a b"}, "no usable track page"),
        ({"track.duration": "59"}, "length outside"),
        ({"track.duration": "901"}, "length outside"),
        ({"track.duration": ""}, "length outside"),
        ({"track.bit_rate": "127999"}, "bitrate under"),
        ({"track.bit_rate": ""}, "bitrate under"),
        ({"track_explicit": "Radio-Unsafe"}, "flagged as not radio-safe"),
        ({"track_explicit": "Adults-Only"}, "flagged as not radio-safe"),
        ({"album.type": "Radio Program"}, "part of a radio program"),
        ({"track.title": "<b>Song</b>"}, "credit text"),
        ({"artist.name": "See https://example.org"}, "credit text"),
        ({"artist.name": "x" * 201}, "credit text"),
        ({"license_title": ""}, "credit text"),
        ({"track.title": "Rain (Acapella)"}, "title names"),
        ({"track.title": "Rain_Stems"}, "title names"),
        ({"track.title": "Interview with Rain"}, "title names"),
        ({"track_genres": "[]"}, "no listed genre"),
        ({"track_genres": "not a list"}, "no listed genre"),
        ({"track_genres": genre_field("Ambient", "Poetry")}, "listed as spoken"),
        ({"track_genres": genre_field("Field Recordings")}, "listed as spoken"),
        ({"track.listens": "999"}, "under 1,000 listens"),
    ],
)
def test_listing_rejections_name_their_reason(changes: dict[str, str], reason: str) -> None:
    assert (rejection(listing(**changes)) or "").startswith(reason)


def test_a_file_predicted_over_the_size_limit_is_rejected() -> None:
    row = listing(
        **{"track.bit_rate": "320000", "track.duration": str(MAX_FILE_BYTES // 40_000 + 1)}
    )
    assert rejection(row) == "file over 20 MB"


def test_the_datasets_short_night_owl_stays_out() -> None:
    assert rejection(listing(42377)) == "known short file in the dataset"


def test_the_track_page_becomes_https_and_keeps_its_path() -> None:
    assert (
        track_page(listing(7)) == "https://freemusicarchive.org/music/Some_Artist/Some_Album/Song_7"
    )


def test_genres_come_from_the_listing() -> None:
    assert genres(listing(track_genres=genre_field("Jazz", "Funk"))) == ["Jazz", "Funk"]


def test_candidates_go_round_robin_with_most_listened_genres_first() -> None:
    def tagged(track_id: int, genre: str, listens: int) -> Listing:
        return listing(track_id, track_genres=genre_field(genre), **{"track.listens": str(listens)})

    rows = [
        tagged(1, "Ambient", 2_000),
        tagged(2, "Ambient", 9_000),
        tagged(3, "Jazz", 50_000),
        tagged(4, "Jazz", 7_000),
    ]
    assert [row["id"] for row in candidates(rows, 4)] == ["3", "2", "4", "1"]


def test_every_genre_gets_a_place_before_a_popular_one_gets_its_second() -> None:
    rows = [
        listing(1, track_genres=genre_field("Polka"), **{"track.listens": "1000"}),
        listing(2, track_genres=genre_field("Rock"), **{"track.listens": "90000"}),
        listing(3, track_genres=genre_field("Rock"), **{"track.listens": "80000"}),
    ]
    assert [row["id"] for row in candidates(rows, 2)] == ["2", "1"]


def test_candidates_stop_at_the_limit_and_skip_rejected_rows() -> None:
    rows = [listing(1, license_url=""), listing(2), listing(3), listing(4)]
    assert [row["id"] for row in candidates(rows, 2)] == ["2", "3"]


def test_at_most_two_tracks_per_artist() -> None:
    rows = [listing(n, **{"artist.id": "9"}) for n in range(1, 6)]
    assert len(candidates(rows, 10)) == PER_ARTIST


def test_one_track_per_artist_and_title_because_the_dataset_holds_duplicates() -> None:
    rows = [
        listing(1, **{"artist.name": "Same", "track.title": "Twice", "artist.id": "9"}),
        listing(2, **{"artist.name": "same", "track.title": "TWICE", "artist.id": "9"}),
    ]
    assert [row["id"] for row in candidates(rows, 10)] == ["1"]


def test_listings_load_from_the_datasets_two_file_shapes(tmp_path: Path) -> None:
    columns = [key for key in listing() if key.split(".")[0] in ("album", "artist", "track")]
    tracks = tmp_path / "tracks.csv"
    with tracks.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["", *(name.split(".")[0] for name in columns)])
        writer.writerow(["", *(name.split(".")[1] for name in columns)])
        writer.writerow(["track_id", *([""] * len(columns))])
        writer.writerow(["100", *(listing()[name] for name in columns)])
    raw = tmp_path / "raw_tracks.csv"
    raw_columns = [
        "track_id",
        "license_title",
        "license_url",
        "track_copyright_c",
        "track_copyright_p",
        "track_explicit",
        "track_genres",
        "track_instrumental",
        "track_url",
    ]
    with raw.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(raw_columns)
        writer.writerow(["100", *(listing()[name] for name in raw_columns[1:])])
        writer.writerow(["999", *(listing()[name] for name in raw_columns[1:])])
    loaded = load_listing(tracks, raw)
    assert list(loaded) == ["100"]
    assert rejection(loaded["100"]) is None


def test_a_manifest_row_imports_with_full_credits_and_rich_details() -> None:
    row = listing(
        **{
            "track.composer": "A Composer",
            "track.publisher": "A Publisher (ASCAP)",
            "track_copyright_c": "A Holder",
            "track_copyright_p": "A Label",
            "track_instrumental": "1",
            "track.information": "<p>Solo piano &amp; rain.</p>",
        }
    )
    result = imported([manifest_row(row, ["CC: http://creativecommons.org/licenses/by-nc/3.0/"])])
    assert result.report.normalized == 1
    recording = result.catalog.partitions[0].recordings[0]
    assert recording.key.recording_id == "100"
    assert (
        recording.provenance == "https://freemusicarchive.org/music/Some_Artist/Some_Album/Song_100"
    )
    assert recording.creators == ("Artist 100", "A Composer")
    assert recording.composer == "A Composer"
    assert recording.duration_seconds == 180
    assert recording.credits is not None
    assert recording.credits.license_url == "https://creativecommons.org/licenses/by-nc/3.0/"
    notices = recording.credits.notices
    assert "Album: Some Album" in notices
    assert "Copyright (as listed): A Holder" in notices
    assert "Sound recording copyright (as listed): A Label" in notices
    assert "Publisher (as listed): A Publisher (ASCAP)" in notices
    assert DATASET_NOTICE in notices and PLAYBACK_NOTICE in notices
    assert (
        "Embedded in the file (as supplied): CC: http://creativecommons.org/licenses/by-nc/3.0/"
        in notices
    )
    details = recording.details
    assert "description: Solo piano & rain." in details
    assert "album description: A warm & slow album." in details
    assert "artist: Plays piano." in details
    assert "similar artists: Air, Helios" in details
    assert "listed: 2013" in details
    for tag in ("tag: Ambient", "tag: melodic", "tag: warm", "tag: piano", "tag: instrumental"):
        assert tag in details


def test_the_instrumental_flag_counts_only_when_set() -> None:
    plain = imported([manifest_row(listing())]).catalog.partitions[0].recordings[0]
    assert "tag: instrumental" not in plain.details
    flagged = imported([manifest_row(listing(track_instrumental="1"))])
    assert "tag: instrumental" in flagged.catalog.partitions[0].recordings[0].details


def test_details_stay_bounded_and_keep_the_descriptions_ahead_of_tags() -> None:
    many = str([f"tag{n}" for n in range(100)])
    row = listing(**{"track.tags": many, "artist.bio": "b" * 100_000})
    recording = imported([manifest_row(row)]).catalog.partitions[0].recordings[0]
    assert len(recording.details) == 24
    assert recording.details[0] == "album: Some Album"
    assert all(len(line) <= 160 for line in recording.details)


@pytest.mark.parametrize(
    "field",
    [
        "track_copyright_c",
        "track_copyright_p",
        "track.publisher",
        "track.composer",
        "track.lyricist",
    ],
)
@pytest.mark.parametrize("text", ["See https://example.org", "<b>x</b>", "x" * 201])
def test_a_listed_credit_that_cant_be_carried_in_full_rejects_the_track(
    field: str, text: str
) -> None:
    assert rejection(listing(**{field: text})) == "credit text can't be reproduced safely in full"


@pytest.mark.parametrize("text", ["", "   ", "null", "NULL"])
def test_a_blank_or_null_credit_is_not_a_lost_one(text: str) -> None:
    fields = {"track.composer": text, "track_copyright_c": text, "track.publisher": text}
    assert rejection(listing(**fields)) is None


@pytest.mark.parametrize(
    "title", ["Drum Loop 4", "Helplessness Loop_02", "Loops", "A Cappella", "Snippet (2)"]
)
def test_titles_naming_a_loop_a_cappella_or_snippet_are_rejected(title: str) -> None:
    assert (rejection(listing(**{"track.title": title})) or "").startswith("title names")


@pytest.mark.parametrize("title", ["Loophole", "Fruit Loopy", "Stemless"])
def test_title_words_match_whole_words_only(title: str) -> None:
    assert rejection(listing(**{"track.title": title})) is None


def test_a_very_long_word_in_notes_does_not_slow_the_email_check() -> None:
    row = listing(**{"artist.bio": "x" * 2_000_000})
    started = time.monotonic()
    manifest_row(row)
    assert time.monotonic() - started < 5


def test_email_addresses_never_reach_the_details() -> None:
    row = listing(
        **{
            "artist.bio": (
                "Write to a.person+tag@mail.example.org or <a href='mailto:b@c.org'>me</a>."
            ),
            "track.information": "Booking: x&#64;example.com today",
            "album.information": "Mail mailto:y@example.net for more",
        }
    )
    built = manifest_row(row)
    details = {label: value for label, value in cast(list[list[str]], built["details"])}
    assert details["artist"] == "Write to or me ."
    assert details["description"] == "Booking: today"
    assert details["album description"] == "Mail for more"
    assert "@" not in json.dumps(built["details"])


def test_the_listings_null_marker_is_not_a_credit() -> None:
    row = listing(
        **{
            "track.composer": "null",
            "track.lyricist": "NULL",
            "track.publisher": "null",
            "track_copyright_c": "null",
            "track_copyright_p": "null",
            "track.information": "null",
        }
    )
    built = manifest_row(row)
    assert "composer" not in built
    assert cast(list[Row], built["creators"])[1:] == []
    assert not any("null" in str(notice).casefold() for notice in cast(list[str], built["notices"]))
    assert not any(value == "null" for _label, value in cast(list[list[str]], built["details"]))
    assert imported([built]).report.normalized == 1


def test_the_importer_refuses_a_malformed_detail() -> None:
    row = manifest_row(listing())
    row["details"] = [["only a label"]]
    result = imported([row])
    assert result.report.normalized == 0


def test_the_draft_is_not_marked_complete_or_available() -> None:
    recording = imported([manifest_row(listing())]).catalog.partitions[0].recordings[0]
    assert recording.kind == RecordingKind.UNKNOWN
    assert recording.availability == Availability.UNKNOWN


def verified(*identities: str) -> dict[str, VerifiedFile]:
    return {
        identity: VerifiedFile(identity, f"fma/{identity}.mp3", 10, "a" * 64, FRAME_BYTES * 500)
        for identity in identities
    }


def draft(*track_ids: int) -> ImportResult:
    return imported([manifest_row(listing(track_id)) for track_id in track_ids])


def test_max_pcm_bytes_is_whole_frames_with_at_least_a_second_of_margin() -> None:
    for measured in (FRAME_BYTES * 100, FRAME_BYTES * 100 + 4, 37_260_508):
        bound = VerifiedFile("1", "fma/1.mp3", 10, "0" * 64, measured).max_pcm_bytes
        assert bound % FRAME_BYTES == 0
        assert measured + BYTES_PER_SECOND <= bound < measured + BYTES_PER_SECOND + FRAME_BYTES


def test_only_validated_recordings_are_kept_and_marked() -> None:
    catalog = finalized_catalog(draft(100, 101).catalog, verified("101"))
    recordings = catalog.partitions[0].recordings
    assert [item.key.recording_id for item in recordings] == ["101"]
    assert recordings[0].kind == RecordingKind.COMPLETE
    assert recordings[0].availability == Availability.AVAILABLE


def test_the_draft_alone_admits_nothing() -> None:
    assert not eligible(draft(100).catalog, ())


def test_reviews_admit_exactly_what_they_bind_and_nothing_after_a_change() -> None:
    catalog = finalized_catalog(draft(100, 101).catalog, verified("100", "101"))
    assert not eligible(catalog, ())
    reviews = parse_reviews(
        json.dumps(
            admission_reviews(catalog, "https://github.com/slackysoba/sobafm/issues/122", "rule")
        )
    )
    assert len(eligible(catalog, reviews)) == 2
    changed = replace(
        catalog.partitions[0],
        recordings=(
            replace(catalog.partitions[0].recordings[0], title="Different"),
            catalog.partitions[0].recordings[1],
        ),
    )
    assert len(eligible(replace(catalog, partitions=(changed,)), reviews)) == 1


def test_reviews_need_an_https_acceptance_link() -> None:
    catalog = finalized_catalog(draft(100).catalog, verified("100"))
    with pytest.raises(ValueError, match="HTTPS"):
        admission_reviews(catalog, "http://example.org/", "rule")
    with pytest.raises(ValueError, match="fragment"):
        admission_reviews(catalog, "https://example.org/issues/1#issuecomment-2", "rule")


def test_manifest_parses_with_the_applications_own_reader(tmp_path: Path) -> None:
    built = media_manifest(verified("101", "100").values())
    parsed = parse_media(json.dumps(built), tmp_path)
    assert len(parsed) == 2
    assert [cast(Row, item)["file"] for item in cast(list[object], built["recordings"])] == [
        "fma/100.mp3",
        "fma/101.mp3",
    ]


def test_the_rule_does_not_depend_on_row_order() -> None:
    rows = [listing(n, **{"track.listens": str(1_000 + n)}) for n in range(1, 8)]
    forward = [row["id"] for row in candidates(rows, 5)]
    backward = [row["id"] for row in candidates(list(reversed(rows)), 5)]
    assert forward == backward
    assert Counter(forward).most_common(1)[0][1] == 1
