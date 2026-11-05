"""The offline FMA build stages on invented listings. No network or real decoder."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import struct
import sys
from argparse import Namespace
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from sobafm.catalog import Recording, eligible, parse_catalog, parse_reviews
from sobafm.catalog_prepare import MAX_FILE_BYTES, RULE, Listing, Row
from sobafm.decode import DecoderCommand
from sobafm.media import parse_media
from sobafm.pcm import BYTES_PER_SECOND, FRAME_BYTES, GLOBAL_PCM_BANKS

ACCEPTANCE = "https://github.com/slackysoba/sobafm/issues/122"


def load_tool() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "evaluations/catalog/fma_build.py"
    spec = importlib.util.spec_from_file_location("fma_build", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses and annotations look their module up here
    spec.loader.exec_module(module)
    return module


BUILD = load_tool()
TRACK_COLUMNS = (
    "album.information",
    "album.tags",
    "album.title",
    "album.type",
    "artist.bio",
    "artist.id",
    "artist.location",
    "artist.name",
    "artist.related_projects",
    "artist.tags",
    "track.bit_rate",
    "track.composer",
    "track.date_created",
    "track.duration",
    "track.information",
    "track.language_code",
    "track.listens",
    "track.lyricist",
    "track.publisher",
    "track.tags",
    "track.title",
)
RAW_COLUMNS = (
    "track_id",
    "license_title",
    "license_url",
    "track_copyright_c",
    "track_copyright_p",
    "track_explicit",
    "track_genres",
    "track_instrumental",
    "track_url",
)


def write_listings(state: Path, count: int) -> None:
    with (state / "tracks.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["", *(name.split(".")[0] for name in TRACK_COLUMNS)])
        writer.writerow(["", *(name.split(".")[1] for name in TRACK_COLUMNS)])
        writer.writerow(["track_id", *([""] * len(TRACK_COLUMNS))])
        for n in range(1, count + 1):
            values = {
                "album.information": "<p>An album.</p>",
                "album.tags": "['warm']",
                "album.title": "Album",
                "album.type": "Album",
                "artist.bio": "",
                "artist.id": str(n),
                "artist.location": "",
                "artist.name": f"Artist {n}",
                "artist.related_projects": "",
                "artist.tags": "[]",
                "track.bit_rate": "256000",
                "track.composer": "",
                "track.date_created": "2013-04-03 17:43:30",
                "track.duration": "180",
                "track.information": "",
                "track.language_code": "",
                "track.listens": str(10_000 - n),
                "track.lyricist": "",
                "track.publisher": "",
                "track.tags": "['melodic']",
                "track.title": f"Song #{n}",
            }
            writer.writerow([str(n), *(values[name] for name in TRACK_COLUMNS)])
    with (state / "raw_tracks.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(RAW_COLUMNS)
        for n in range(1, count + 1):
            writer.writerow(
                [
                    str(n),
                    "Attribution 3.0",
                    "http://creativecommons.org/licenses/by/3.0/",
                    "",
                    "",
                    "",
                    str([{"genre_id": "1", "genre_title": "Ambient", "genre_url": "x"}]),
                    "0",
                    f"http://freemusicarchive.org/music/Artist/Album/Song_{n}",
                ]
            )


def candidates(state: Path, count: int = 12, **options: object) -> None:
    write_listings(state, count)
    arguments = {"state": state, "target": 8, "spares": 2, "strike": "", **options}
    BUILD._candidates(Namespace(**arguments))  # pyright: ignore[reportPrivateUsage]


def rows(state: Path) -> dict[str, Listing]:
    return cast(dict[str, Listing], json.loads((state / "chosen-rows.json").read_text("utf-8")))


def test_the_candidate_stage_splits_first_choices_from_spares(tmp_path: Path) -> None:
    candidates(tmp_path)
    summary = json.loads((tmp_path / "candidates-summary.json").read_text(encoding="utf-8"))
    assert summary["first"] == 8 and summary["spares"] == 2
    assert summary["passing_rule"] == 12
    assert summary["importer_refused"] == [] and summary["credit_refused"] == []
    assert summary["song_list_characters"] < summary["song_list_limit"]
    assert summary["catalog_bytes"] < summary["catalog_limit"]
    assert summary["largest_expected_bytes"] <= MAX_FILE_BYTES
    assert len(json.loads((tmp_path / "chosen.json").read_text(encoding="utf-8"))) == 10
    first = (tmp_path / "candidates.md").read_text(encoding="utf-8")
    assert "[1](https://freemusicarchive.org/music/Artist/Album/Song_1)" in first
    assert "Song &#35;1" in first
    assert "by 3.0" in first
    assert "Song &#35;9" not in first
    assert "Song &#35;9" in (tmp_path / "candidates-spares.md").read_text(encoding="utf-8")


def test_struck_tracks_leave_and_spares_move_up(tmp_path: Path) -> None:
    candidates(tmp_path, strike="2,3")
    chosen = json.loads((tmp_path / "chosen.json").read_text(encoding="utf-8"))
    assert chosen == ["1", "4", "5", "6", "7", "8", "9", "10", "11", "12"]
    assert "Song &#35;2" not in (tmp_path / "candidates.md").read_text(encoding="utf-8")


def test_later_stages_get_only_the_chosen_rows(tmp_path: Path) -> None:
    candidates(tmp_path)
    assert sorted(rows(tmp_path), key=int) == [str(n) for n in range(1, 11)]


def test_cells_cannot_ping_link_or_break_the_table() -> None:
    assert (
        BUILD.escape_cell("Aussens@iter | #5 *bold* `x` <b> a_b")
        == "Aussens&#64;iter \\| &#35;5 \\*bold\\* 'x' &lt;b> a\\_b"
    )


def row_for_verify(**changes: str) -> Listing:
    base: Listing = {
        "track.duration": "4",
        "license_url": "http://creativecommons.org/licenses/by/3.0/",
        "track_url": "http://freemusicarchive.org/music/Artist/Album/Song",
    }
    base.update(changes)
    return base


def frame(name: bytes, text: str) -> bytes:
    body = b"\x00" + text.encode("latin-1")
    return name + struct.pack(">I", len(body)) + b"\x00\x00" + body


def tagged_file(tmp_path: Path, *frames: bytes) -> tuple[Path, Row]:
    tag = b"".join(frames)
    size = bytes((len(tag) >> shift) & 0x7F for shift in (21, 14, 7, 0))
    data = (b"ID3\x03\x00\x00" + size + tag if tag else b"") + b"\xff\xfb" + b"\x00" * 400
    path = tmp_path / "input.mp3"
    path.write_bytes(data)
    return path, {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def child(
    seconds: int, *, status: int = 0, sample: bytes = struct.pack("<h", 12_000)
) -> DecoderCommand:
    script = (
        "import os,sys\n"
        "while sys.stdin.buffer.read(65536):\n"
        "    pass\n"
        f"frame = {sample!r} * ({FRAME_BYTES} // 2)\n"
        f"for _ in range({seconds * BYTES_PER_SECOND // FRAME_BYTES}):\n"
        "    os.write(1, frame)\n"
        f"sys.exit({status})\n"
    )
    return DecoderCommand(argv=(sys.executable, "-I", "-c", script), limits=(), env={})


def test_a_good_file_passes_and_keeps_its_embedded_notices(tmp_path: Path) -> None:
    tags = (
        frame(b"TCOP", "CC BY: http://creativecommons.org/licenses/by/3.0/"),
        frame(b"TXXX", "URL: http://freemusicarchive.org/music/Artist/Album/Song/ Curator: X"),
    )
    path, saved = tagged_file(tmp_path, *tags)
    result = BUILD.verify_one(row_for_verify(), saved, path, command=child(4))
    assert result["problem"] is None
    assert result["pcm_bytes"] == 4 * BYTES_PER_SECOND
    assert result["embedded"] == ["CC BY: http://creativecommons.org/licenses/by/3.0/"]
    assert GLOBAL_PCM_BANKS.leased == 0


def test_a_decode_off_by_more_than_two_seconds_fails(tmp_path: Path) -> None:
    path, saved = tagged_file(tmp_path)
    result = BUILD.verify_one(
        row_for_verify(**{"track.duration": "7"}), saved, path, command=child(4)
    )
    assert result["problem"] == "decoded length differs from the listing by over 2 seconds"


def test_a_decode_within_two_seconds_passes(tmp_path: Path) -> None:
    path, saved = tagged_file(tmp_path)
    result = BUILD.verify_one(
        row_for_verify(**{"track.duration": "6"}), saved, path, command=child(4)
    )
    assert result["problem"] is None


def test_a_failing_decoder_fails_validation(tmp_path: Path) -> None:
    path, saved = tagged_file(tmp_path)
    result = BUILD.verify_one(row_for_verify(), saved, path, command=child(4, status=3))
    assert result["problem"] == "decode failed: decoder_exit"


def test_tags_naming_another_license_or_page_fail_before_any_decode(tmp_path: Path) -> None:
    other_license = frame(b"TCOP", "http://creativecommons.org/licenses/by-nd/3.0/")
    path, saved = tagged_file(tmp_path, other_license)
    never = DecoderCommand(argv=("/nonexistent/decoder",), limits=(), env={})
    result = BUILD.verify_one(row_for_verify(), saved, path, command=never)
    assert result["problem"] == "embedded tag names a different license"
    other_page = frame(b"TXXX", "URL: http://freemusicarchive.org/music/Someone/Else/Song")
    path, saved = tagged_file(tmp_path, other_page)
    result = BUILD.verify_one(row_for_verify(), saved, path, command=never)
    assert result["problem"] == "embedded tag names a different track page"


def test_unreadable_tags_fail_validation(tmp_path: Path) -> None:
    path = tmp_path / "input.mp3"
    data = b"ID3" + bytes([9, 0, 0, 0, 0, 0, 0]) + b"\xff\xfb"
    path.write_bytes(data)
    saved: Row = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    result = BUILD.verify_one(row_for_verify(), saved, path)
    assert str(result["problem"]).startswith("embedded tags can't be read")


def passed(track_id: str) -> Row:
    return {
        "problem": None,
        "size": 1_000,
        "sha256": hashlib.sha256(track_id.encode()).hexdigest(),
        "pcm_bytes": 180 * BYTES_PER_SECOND,
        "measured_seconds": 180.0,
        "embedded": ["CC BY: http://creativecommons.org/licenses/by/3.0/"],
    }


def finalize(tmp_path: Path) -> tuple[Path, Namespace]:
    candidates(tmp_path)
    out = tmp_path / "out"
    (tmp_path / "fetch-order.json").write_text(json.dumps([str(n) for n in range(1, 11)]))
    (tmp_path / "size-rejections.json").write_text(
        json.dumps({"10": "file is 50% of its predicted size"})
    )
    results: dict[str, Row] = {str(n): passed(str(n)) for n in range(1, 10)}
    results["2"] = {"problem": "silence over 30 seconds"}
    (tmp_path / "verified.json").write_text(json.dumps(results))
    args = Namespace(state=tmp_path, out=out, target=8, strike="")
    BUILD._finalize(args)  # pyright: ignore[reportPrivateUsage]
    return out, args


def test_finalize_writes_files_the_application_reads_and_admits_nothing_yet(tmp_path: Path) -> None:
    out, _ = finalize(tmp_path)
    catalog = parse_catalog((out / "catalog.json").read_text(encoding="utf-8"))
    assert not eligible(catalog, ()), "no review has been written yet"
    assert not (out / "reviews.json").exists()
    media = parse_media((out / "media.json").read_text(encoding="utf-8"), out)
    assert len(media) == 8
    summary = json.loads((tmp_path / "final-summary.json").read_text(encoding="utf-8"))
    assert summary["admitted_if_accepted"] == 8
    assert summary["rejected"] == [["silence over 30 seconds", 1]]
    assert summary["rejected_by_size"] == 1
    assert len(summary["spot_check"]) == 8
    table = (tmp_path / "final-rejections.md").read_text(encoding="utf-8")
    assert "file is 50% of its predicted size" in table and "silence over 30 seconds" in table
    final = (tmp_path / "final.md").read_text(encoding="utf-8")
    assert "Song &#35;1 " in final and "Song &#35;2 " not in final
    spot = (tmp_path / "spot-check.md").read_text(encoding="utf-8")
    assert "Attribution 3.0" in spot and "research dataset" in spot


def test_a_strike_changes_only_the_struck_track_and_keeps_the_spot_check(tmp_path: Path) -> None:
    out, args = finalize(tmp_path)

    def recordings() -> dict[str, Recording]:
        catalog = parse_catalog((out / "catalog.json").read_text(encoding="utf-8"))
        return {item.key.recording_id: item for item in catalog.partitions[0].recordings}

    before = recordings()
    spot = (tmp_path / "spot-check.md").read_text(encoding="utf-8")
    args.strike = "3"
    BUILD._finalize(args)  # pyright: ignore[reportPrivateUsage]
    after = recordings()
    assert set(before) - set(after) == {"3"}
    assert all(item == before[key] for key, item in after.items())
    assert (tmp_path / "spot-check.md").read_text(encoding="utf-8") == spot
    media = parse_media((out / "media.json").read_text(encoding="utf-8"), out)
    assert {key.recording_id for key in media} == set(after)
    table = (tmp_path / "final-rejections.md").read_text(encoding="utf-8")
    assert "struck by the maintainer" in table


def test_the_catalog_names_the_rule_that_selected_its_list(tmp_path: Path) -> None:
    out, args = finalize(tmp_path)

    def note() -> str:
        return parse_catalog((out / "catalog.json").read_text(encoding="utf-8")).partitions[0].note

    assert RULE in note()
    # A later version of the rule mustn't claim a list an earlier one selected.
    (tmp_path / "selection-rule.txt").write_text("fma-dataset-0\n", encoding="utf-8")
    BUILD._finalize(args)  # pyright: ignore[reportPrivateUsage]
    assert "fma-dataset-0" in note() and RULE not in note()


def test_a_track_that_failed_validation_cannot_be_struck(tmp_path: Path) -> None:
    out, args = finalize(tmp_path)
    catalog = (out / "catalog.json").read_bytes()
    args.strike = "2"
    with pytest.raises(SystemExit):
        BUILD._finalize(args)  # pyright: ignore[reportPrivateUsage]
    assert (out / "catalog.json").read_bytes() == catalog


def test_reviews_bind_the_acceptance_to_exactly_the_final_catalog(tmp_path: Path) -> None:
    out, _ = finalize(tmp_path)
    BUILD._reviews(  # pyright: ignore[reportPrivateUsage]
        Namespace(out=out, evidence=ACCEPTANCE, note="Rule fma-dataset-1, accepted.")
    )
    catalog = parse_catalog((out / "catalog.json").read_text(encoding="utf-8"))
    reviews = parse_reviews((out / "reviews.json").read_text(encoding="utf-8"))
    admitted = eligible(catalog, reviews)
    media = parse_media((out / "media.json").read_text(encoding="utf-8"), out)
    assert {item.key for item in admitted} == set(media)
    credits_ = admitted[0].credits
    assert credits_ is not None
    assert any(notice.startswith("Embedded in the file") for notice in credits_.notices)


def test_a_spot_check_comes_from_the_exact_catalog() -> None:
    assert BUILD._seed("a") == BUILD._seed("a")  # pyright: ignore[reportPrivateUsage]
    assert BUILD._seed("a") != BUILD._seed("b")  # pyright: ignore[reportPrivateUsage]
