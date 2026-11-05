"""Build the FMA catalog from the dataset's saved listings. This stage makes no network request.

Run it from a container of the project's image:

    python fma_build.py candidates --state DIR --target 200 --spares 30

`candidates` applies the written rule in `sobafm.catalog_prepare` and the real importer, then
writes the list for the maintainer to review, its summary and `chosen.json`. ADR 0007 records
the rule and what a review of it can't establish.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import cast

from sobafm import catalog_prepare as prepare
from sobafm.catalog import (
    MAX_CATALOG_BYTES,
    MAX_RECORDS_PER_SOURCE,
    Catalog,
    Partition,
    Recording,
    SnapshotState,
    Source,
    parse_catalog,
)
from sobafm.catalog_import import ImportContext, catalog_document, normalize
from sobafm.catalog_prepare import Listing, Row
from sobafm.credits import CreditRejected, render_credits
from sobafm.gemini_match import MAX_SONG_CHARACTERS, render_songs

OBSERVED_AT = "2026-11-05"
SCOPE = "FMA research dataset, chosen by rule " + prepare.RULE
NARROW_LICENSES = frozenset({"by", "by-nc"})


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _listing(state: Path) -> dict[str, Listing]:
    return prepare.load_listing(state / "tracks.csv", state / "raw_tracks.csv")


def import_rows(rows: list[Row]) -> list[Recording]:
    """Run the real importer. It drops anything it can't carry in full, and says so."""
    context = ImportContext(
        OBSERVED_AT, "observed", SCOPE, "partial", tuple(str(row["id"]) for row in rows), ()
    )
    payload = json.dumps(prepare.manifest(rows), ensure_ascii=False)
    catalog = normalize(Source.FMA, payload, context).catalog
    return list(catalog.partitions[0].recordings)


def escape_cell(value: object) -> str:
    text = " ".join(str(value).split())
    for plain, safe in (
        ("\\", "\\\\"),
        ("|", "\\|"),
        ("#", "&#35;"),
        ("@", "&#64;"),
        ("*", "\\*"),
        ("_", "\\_"),
        ("<", "&lt;"),
        ("`", "'"),
    ):
        text = text.replace(plain, safe)
    return text


def _table(rows: list[Listing]) -> list[str]:
    lines = [
        "| ID | Title | Artist | Genres | Length | License | MB | Listens |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        seconds = int(float(row["track.duration"]))
        match = prepare.license_class(row) or ""
        version = row["license_url"].split("/licenses/")[1].split("/")[1]
        genres = escape_cell(", ".join(prepare.genres(row)[:2]))
        lines.append(
            f"| [{row['id']}]({prepare.track_page(row)}) | {escape_cell(row['track.title'])} "
            f"| {escape_cell(row['artist.name'])} | {genres} "
            f"| {seconds // 60}:{seconds % 60:02d} | {match} {version} "
            f"| {prepare.expected_bytes(row) / 1_000_000:.1f} "
            f"| {int(float(row['track.listens'])):,} |"
        )
    return lines


def _candidates(args: argparse.Namespace) -> None:
    state, target, spares = cast(Path, args.state), cast(int, args.target), cast(int, args.spares)
    listing = _listing(state)
    wanted = target + spares
    drops = Counter(
        reason for reason in map(prepare.rejection, listing.values()) if reason is not None
    )
    # Take the most one import can hold, because the importer can still refuse a few.
    ordered = prepare.candidates(listing.values(), MAX_RECORDS_PER_SOURCE)
    recordings = import_rows([prepare.manifest_row(row) for row in ordered])
    imported = {recording.key.recording_id: recording for recording in recordings}
    refused = [row["id"] for row in ordered[:wanted] if row["id"] not in imported]
    kept = [row for row in ordered if row["id"] in imported][:wanted]
    first, spare = kept[:target], kept[target:]
    chosen_recordings = [imported[row["id"]] for row in kept]
    pages = 0
    credit_refused: list[str] = []
    for recording in chosen_recordings:
        try:
            pages = max(pages, len(render_credits(recording).pages))
        except CreditRejected:
            credit_refused.append(recording.key.recording_id)
    first_recordings = tuple(imported[row["id"]] for row in first)
    songs = render_songs(first_recordings)
    partition = Partition(Source.FMA, SnapshotState.PARTIAL, SCOPE, first_recordings)
    document = json.dumps(
        catalog_document(Catalog(False, (partition,))), ensure_ascii=False, separators=(",", ":")
    )
    parse_catalog(document)  # the size below is of a snapshot the application would accept
    narrow = prepare.candidates(listing.values(), wanted, NARROW_LICENSES)
    narrow_eligible = sum(
        1 for row in listing.values() if prepare.rejection(row, NARROW_LICENSES) is None
    )
    summary = {
        "rule": prepare.RULE,
        "listed_tracks": len(listing),
        "passing_rule": len(listing) - sum(drops.values()),
        "drops": drops.most_common(),
        "first": len(first),
        "spares": len(spare),
        "importer_refused": refused,
        "credit_refused": credit_refused,
        "most_credit_pages": pages,
        "uploaders": len({row["artist.id"] for row in first}),
        "expected_bytes_first": sum(prepare.expected_bytes(row) for row in first),
        "expected_bytes_all": sum(prepare.expected_bytes(row) for row in kept),
        "largest_expected_bytes": max(prepare.expected_bytes(row) for row in kept),
        "hours_first": round(sum(int(float(r["track.duration"])) for r in first) / 3600, 1),
        "licenses": Counter(prepare.license_class(row) for row in first).most_common(),
        "genres": Counter(g for row in first for g in prepare.genres(row)).most_common(),
        "flagged_instrumental": sum(1 for row in first if row.get("track_instrumental") == "1"),
        "lowest_listens": min(int(float(row["track.listens"])) for row in first),
        "song_list_characters": len(songs),
        "song_list_limit": MAX_SONG_CHARACTERS,
        "catalog_bytes": len(document.encode("utf-8")),
        "catalog_limit": MAX_CATALOG_BYTES,
        "narrow_licenses_passing": narrow_eligible,
        "narrow_overlap_with_first": len({r["id"] for r in narrow} & {r["id"] for r in first}),
        "sample_song": json.loads(songs)[1],
    }
    (state / "candidates.md").write_text(
        "\n".join(_table(first)) + "\n", encoding="utf-8", newline="\n"
    )
    (state / "candidates-spares.md").write_text(
        "\n".join(_table(spare)) + "\n", encoding="utf-8", newline="\n"
    )
    _write_json(state / "candidates-summary.json", summary)
    _write_json(state / "chosen.json", [row["id"] for row in kept])
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    candidates = commands.add_parser("candidates")
    candidates.add_argument("--state", type=Path, required=True)
    candidates.add_argument("--target", type=int, required=True)
    candidates.add_argument("--spares", type=int, required=True)
    args = parser.parse_args()
    {"candidates": _candidates}[args.command](args)


if __name__ == "__main__":
    main()
