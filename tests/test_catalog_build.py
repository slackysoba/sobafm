"""The offline FMA build stages on invented listings. No network, decoder or audio."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path
from types import ModuleType

from sobafm.catalog_prepare import MAX_FILE_BYTES


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


def test_the_candidate_stage_splits_first_choices_from_spares(tmp_path: Path) -> None:
    write_listings(tmp_path, 12)
    BUILD._candidates(Namespace(state=tmp_path, target=8, spares=2))  # pyright: ignore[reportPrivateUsage]
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


def test_cells_cannot_ping_link_or_break_the_table() -> None:
    assert (
        BUILD.escape_cell("Aussens@iter | #5 *bold* `x` <b> a_b")
        == "Aussens&#64;iter \\| &#35;5 \\*bold\\* 'x' &lt;b> a\\_b"
    )
