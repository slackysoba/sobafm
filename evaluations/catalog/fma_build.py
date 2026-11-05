"""Build the FMA catalog from saved listings and validated files. This makes no network request.

The stages run in order, each from a container of the project's image with no network:

    python fma_build.py candidates --state DIR --target 200 --spares 30 --strike ID,ID
    python fma_build.py verify --state DIR --media DIR
    python fma_build.py finalize --state DIR --out DIR --target 200 [--strike ID,ID]
    python fma_build.py reviews --state DIR --out DIR --evidence URL --note TEXT

`candidates` applies the written rule in `sobafm.catalog_prepare` and the real importer, then
writes the list for the maintainer to review. `verify` decodes each fetched file with the real
producer, at the service's limits, and reads its tags. `finalize` writes the catalog and media
manifest for what passed, the final list, every rejection and the spot-check. Run it again with
`--strike` to apply what the maintainer strikes from that list. `reviews` runs only after the
maintainer accepts the list: the acceptance is the trusted review, and this binds it to each
recording's digest. ADR 0007 records the rule and what a review can't establish.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
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
from sobafm.catalog_prepare import Listing, Row, VerifiedFile
from sobafm.credits import CreditRejected, render_credits
from sobafm.decode import DecodeInput, DecoderCommand
from sobafm.gemini_match import MAX_SONG_CHARACTERS, render_songs
from sobafm.media_check import (
    TagError,
    check_decode,
    embedded_notices,
    read_tag_values,
    tag_conflict,
    tag_page_conflict,
)
from sobafm.pcm import BYTES_PER_SECOND

OBSERVED_AT = "2026-11-05"
NARROW_LICENSES = frozenset({"by", "by-nc"})
MAX_LENGTH_DIFFERENCE_SECONDS = 2.0
# Room above the listed length, so an overrun means a file far longer than its listing.
PCM_LIMIT_MARGIN_SECONDS = 30
SPOT_CHECK_COUNT = 10
SELECTION_RULE_FILE = "selection-rule.txt"


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _scope(rule: str) -> str:
    return "FMA research dataset, chosen by rule " + rule


def _import(rows: list[Row], rule: str) -> Catalog:
    """Run the real importer. It drops anything it can't carry in full, and says so."""
    context = ImportContext(
        OBSERVED_AT, "observed", _scope(rule), "partial", tuple(str(row["id"]) for row in rows), ()
    )
    payload = json.dumps(prepare.manifest(rows), ensure_ascii=False)
    return normalize(Source.FMA, payload, context).catalog


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
        kind = prepare.license_class(row) or ""
        version = row["license_url"].split("/licenses/")[1].split("/")[1]
        genres = escape_cell(", ".join(prepare.genres(row)[:2]))
        lines.append(
            f"| [{row['id']}]({prepare.track_page(row)}) | {escape_cell(row['track.title'])} "
            f"| {escape_cell(row['artist.name'])} | {genres} "
            f"| {seconds // 60}:{seconds % 60:02d} | {kind} {version} "
            f"| {prepare.expected_bytes(row) / 1_000_000:.1f} "
            f"| {int(float(row['track.listens'])):,} |"
        )
    return lines


def _candidates(args: argparse.Namespace) -> None:
    state, target, spares = cast(Path, args.state), cast(int, args.target), cast(int, args.spares)
    struck = frozenset(cast(str, args.strike).split(",")) if args.strike else frozenset[str]()
    listing = prepare.load_listing(state / "tracks.csv", state / "raw_tracks.csv")
    wanted = target + spares
    drops = Counter(
        reason for reason in map(prepare.rejection, listing.values()) if reason is not None
    )
    # Take the most one import can hold, because the importer can still refuse a few.
    ordered = [
        row
        for row in prepare.candidates(listing.values(), MAX_RECORDS_PER_SOURCE)
        if row["id"] not in struck
    ]
    catalog = _import([prepare.manifest_row(row) for row in ordered], prepare.RULE)
    imported = {item.key.recording_id: item for item in catalog.partitions[0].recordings}
    refused = [row["id"] for row in ordered[:wanted] if row["id"] not in imported]
    kept = [row for row in ordered if row["id"] in imported][:wanted]
    first, spare = kept[:target], kept[target:]
    pages = 0
    credit_refused: list[str] = []
    for row in kept:
        try:
            pages = max(pages, len(render_credits(imported[row["id"]]).pages))
        except CreditRejected:
            credit_refused.append(row["id"])
    first_recordings = tuple(imported[row["id"]] for row in first)
    songs = render_songs(first_recordings)
    partition = Partition(Source.FMA, SnapshotState.PARTIAL, _scope(prepare.RULE), first_recordings)
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
        "struck": sorted(struck),
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
        "sample_song": json.loads(songs)[min(1, len(first) - 1)],
    }
    (state / "candidates.md").write_text(
        "\n".join(_table(first)) + "\n", encoding="utf-8", newline="\n"
    )
    (state / "candidates-spares.md").write_text(
        "\n".join(_table(spare)) + "\n", encoding="utf-8", newline="\n"
    )
    _write_json(state / "candidates-summary.json", summary)
    _write_json(state / "chosen.json", [row["id"] for row in kept])
    # Later stages read only these rows: the listing files don't fit in the service's limits.
    _write_json(state / "chosen-rows.json", {row["id"]: row for row in kept})
    (state / SELECTION_RULE_FILE).write_text(prepare.RULE + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def verify_one(
    row: Listing, saved: Row, path: Path, *, command: DecoderCommand | None = None
) -> Row:
    """Everything validation establishes about one file. ``problem`` is None if it passed."""
    try:
        values = read_tag_values(path)
    except TagError as exc:
        return {"problem": f"embedded tags can't be read: {exc}"}
    page = prepare.track_page(row)
    assert page is not None
    conflict = tag_conflict(values, row["license_url"]) or tag_page_conflict(values, page)
    if conflict is not None:
        return {"problem": conflict}
    listed = int(float(row["track.duration"]))
    limit = (listed + PCM_LIMIT_MARGIN_SECONDS) * BYTES_PER_SECOND
    source = DecodeInput(path, cast(int, saved["size"]), str(saved["sha256"]), limit)
    check = check_decode(source, command=command)
    if check.problem is not None:
        return {"problem": check.problem, "pcm_bytes": check.pcm_bytes}
    measured = check.pcm_bytes / BYTES_PER_SECOND
    if abs(measured - listed) > MAX_LENGTH_DIFFERENCE_SECONDS:
        return {
            "problem": "decoded length differs from the listing by over 2 seconds",
            "measured_seconds": round(measured, 2),
            "listed_seconds": listed,
        }
    return {
        "problem": None,
        "size": saved["size"],
        "sha256": saved["sha256"],
        "pcm_bytes": check.pcm_bytes,
        "measured_seconds": round(measured, 2),
        "embedded": embedded_notices(values),
        "longest_silence_seconds": round(check.longest_silence_seconds, 2),
        "longest_clipping_seconds": round(check.longest_clipping_seconds, 2),
    }


def _verify(args: argparse.Namespace) -> None:
    state, media = cast(Path, args.state), cast(Path, args.media)
    rows = cast(dict[str, Listing], _read_json(state / "chosen-rows.json"))
    order = cast(list[str], _read_json(state / "fetch-order.json"))
    saved: dict[str, Row] = {}
    for line in (state / "fma-files.jsonl").read_text(encoding="utf-8").splitlines():
        item = cast(Row, json.loads(line))
        saved[str(item["id"])] = item
    results_path = state / "verified.json"
    results = cast(dict[str, Row], _read_json(results_path)) if results_path.exists() else {}
    for track_id in order:
        if track_id in results or track_id not in saved:
            continue
        results[track_id] = verify_one(rows[track_id], saved[track_id], media / f"{track_id}.mp3")
        _write_json(results_path, results)
        print(track_id, results[track_id].get("problem"), flush=True)
    passed = sum(1 for result in results.values() if result["problem"] is None)
    print(f"{len(results)} checked, {passed} passed")
    print(Counter(str(r["problem"]) for r in results.values() if r["problem"]).most_common())


def _seed(document: str) -> int:
    """A seed from the exact catalog, so the spot check can't be picked after the fact."""
    return int(hashlib.sha256(document.encode("utf-8")).hexdigest()[:16], 16)


def _credit_block(row: Listing, recording: Recording) -> list[str]:
    credits_ = recording.credits
    assert credits_ is not None
    lines = [
        f"**[{escape_cell(recording.title)}]({recording.provenance})** by "
        f"{escape_cell(recording.primary_artist)} (ID {row['id']})",
        "",
        f"- License: [{escape_cell(credits_.license_label)}]({credits_.license_url})",
    ]
    lines += [f"- {escape_cell(notice)}" for notice in credits_.notices]
    return [*lines, ""]


def _finalize(args: argparse.Namespace) -> None:
    state, out, target = cast(Path, args.state), cast(Path, args.out), cast(int, args.target)
    rows = cast(dict[str, Listing], _read_json(state / "chosen-rows.json"))
    order = cast(list[str], _read_json(state / "fetch-order.json"))
    results = cast(dict[str, Row], _read_json(state / "verified.json"))
    rejected: dict[str, str] = {
        track_id: str(results[track_id]["problem"])
        for track_id in order
        if track_id in results and results[track_id]["problem"] is not None
    }
    passed = [t for t in order if t in results and results[t]["problem"] is None]
    struck = {track_id for track_id in cast(str, args.strike).split(",") if track_id}
    if not struck <= set(passed):
        raise SystemExit(f"can't strike what didn't pass: {sorted(struck - set(passed))}")
    rejected |= dict.fromkeys(struck, "struck by the maintainer")
    passed = [t for t in passed if t not in struck]
    prepared = [
        prepare.manifest_row(rows[t], cast(list[str], results[t]["embedded"])) for t in passed
    ]
    # The catalog names the rule that selected this list, which a later version of it didn't.
    draft = _import(prepared, (state / SELECTION_RULE_FILE).read_text(encoding="utf-8").strip())
    imported = {item.key.recording_id for item in draft.partitions[0].recordings}
    for track_id in passed:
        if track_id not in imported:
            rejected[track_id] = "importer refused the credits"
    chosen = [t for t in passed if t in imported][:target]
    files = {
        t: VerifiedFile(
            t,
            f"fma/{t}.mp3",
            cast(int, results[t]["size"]),
            str(results[t]["sha256"]),
            cast(int, results[t]["pcm_bytes"]),
        )
        for t in chosen
    }
    catalog = prepare.finalized_catalog(draft, files)
    recordings = {item.key.recording_id: item for item in catalog.partitions[0].recordings}
    for track_id, recording in list(recordings.items()):
        try:
            render_credits(recording)
        except CreditRejected:
            rejected[track_id] = "credits can't render in full"
            raise SystemExit(f"credits can't render in full for {track_id}") from None
    document = json.dumps(catalog_document(catalog), ensure_ascii=False, separators=(",", ":"))
    songs = render_songs(tuple(recordings[t] for t in chosen))
    if len(document.encode("utf-8")) > MAX_CATALOG_BYTES or len(songs) > MAX_SONG_CHARACTERS:
        raise SystemExit(f"over a limit: catalog {len(document)}, song list {len(songs)}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "catalog.json").write_text(document + "\n", encoding="utf-8", newline="\n")
    _write_json(out / "media.json", prepare.media_manifest(files.values()))
    final_rows = [rows[t] for t in chosen]
    (state / "final.md").write_text(
        "\n".join(_table(final_rows)) + "\n", encoding="utf-8", newline="\n"
    )
    rejection_lines = ["| ID | Title | Artist | Why it's out |", "| --- | --- | --- | --- |"]
    size_rejections_path = state / "size-rejections.json"
    size_rejections = (
        cast(dict[str, str], _read_json(size_rejections_path))
        if size_rejections_path.exists()
        else {}
    )
    for track_id, reason in {**size_rejections, **rejected}.items():
        row = rows[track_id]
        rejection_lines.append(
            f"| [{track_id}]({prepare.track_page(row)}) | {escape_cell(row['track.title'])} "
            f"| {escape_cell(row['artist.name'])} | {escape_cell(reason)} |"
        )
    (state / "final-rejections.md").write_text(
        "\n".join(rejection_lines) + "\n", encoding="utf-8", newline="\n"
    )
    audio = sum(file.size for file in files.values())
    seconds = sum(cast(float, results[t]["measured_seconds"]) for t in chosen)
    summary: Row = {
        "admitted_if_accepted": len(chosen),
        "rejected": Counter(rejected.values()).most_common(),
        "rejected_by_size": len(size_rejections),
        "unused_passing": len(passed) - len(chosen) - sum(1 for t in passed if t not in imported),
        "audio_bytes": audio,
        "hours": round(seconds / 3600, 1),
        "artists": len({rows[t]["artist.id"] for t in chosen}),
        "genres": len({g for t in chosen for g in prepare.genres(rows[t])}),
        "licenses": Counter(prepare.license_class(rows[t]) for t in chosen).most_common(),
        "catalog_bytes": len(document.encode("utf-8")),
        "catalog_limit": MAX_CATALOG_BYTES,
        "song_list_characters": len(songs),
        "song_list_limit": MAX_SONG_CHARACTERS,
    }
    # The spot-check belongs to the list the maintainer read. A later strike doesn't redraw it.
    if not struck:
        spot = random.Random(_seed(document)).sample(chosen, min(SPOT_CHECK_COUNT, len(chosen)))
        blocks: list[str] = []
        for track_id in sorted(spot, key=int):
            blocks += _credit_block(rows[track_id], recordings[track_id])
        (state / "spot-check.md").write_text("\n".join(blocks), encoding="utf-8", newline="\n")
        summary["spot_check"] = sorted(spot, key=int)
    _write_json(state / "final-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def _reviews(args: argparse.Namespace) -> None:
    out = cast(Path, args.out)
    catalog = parse_catalog((out / "catalog.json").read_text(encoding="utf-8"))
    reviews = prepare.admission_reviews(catalog, cast(str, args.evidence), cast(str, args.note))
    _write_json(out / "reviews.json", reviews)
    print(len(cast(list[object], reviews["reviews"])), "reviews written")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    candidates = commands.add_parser("candidates")
    candidates.add_argument("--target", type=int, required=True)
    candidates.add_argument("--spares", type=int, required=True)
    candidates.add_argument("--strike", default="")
    verify = commands.add_parser("verify")
    verify.add_argument("--media", type=Path, required=True)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--out", type=Path, required=True)
    finalize.add_argument("--target", type=int, required=True)
    finalize.add_argument("--strike", default="")
    reviews = commands.add_parser("reviews")
    reviews.add_argument("--out", type=Path, required=True)
    reviews.add_argument("--evidence", required=True)
    reviews.add_argument("--note", required=True)
    for command in (candidates, verify, finalize, reviews):
        command.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    stages = {
        "candidates": _candidates,
        "verify": _verify,
        "finalize": _finalize,
        "reviews": _reviews,
    }
    stages[args.command](args)


if __name__ == "__main__":
    main()
