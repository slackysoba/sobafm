"""One written, deterministic rule that prepares FMA dataset listings for the catalog.

This applies the batch rule the maintainer accepts on issue 122: which listed tracks are
candidates and in what order, how a listing becomes importer input, and what the catalog,
media manifest and admission reviews hold once a file has passed validation. It makes no
network request, runs no decoder and uses no model. A track that fails a check is dropped
with its reason. Nothing is fixed by hand.

The admission review the output carries is the maintainer's acceptance of this rule and
the resulting list. This module only binds that acceptance to each recording's digest.
"""

from __future__ import annotations

import ast
import csv
import html
import json
import re
import unicodedata
from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from sobafm.catalog import Catalog, Source, credential_free_url, metadata_digest, parse_catalog
from sobafm.catalog_import import catalog_document
from sobafm.pcm import BYTES_PER_SECOND, FRAME_BYTES

RULE = "fma-dataset-2"
MIN_SECONDS = 60
MAX_SECONDS = 900
MIN_BIT_RATE = 128_000
MIN_LISTENS = 1_000
PER_ARTIST = 2
MAX_FILE_BYTES = 20_000_000
MAX_TOTAL_BYTES = 3_000_000_000
# A file under this share of the size its length and bitrate predict looks cut short.
MIN_SIZE_SHARE = 0.97
# Whole frames above the measured decode: one second, so a slightly different decoder
# build doesn't fail a good file while the overrun bound stays tight.
PCM_MARGIN_BYTES = BYTES_PER_SECOND
# The dataset's copy decodes 34 seconds short of its listed length.
KNOWN_SHORT = frozenset({"42377"})
ANY_CC = frozenset({"by", "by-nc", "by-sa", "by-nc-sa", "by-nd", "by-nc-nd"})
_CC_LICENSE = re.compile(r"https?://creativecommons\.org/licenses/([a-z-]+)/(\d\.\d)(?:/[a-z]+)?/?")
_TRACK_PAGE = re.compile(r"https?://freemusicarchive\.org(/music/[A-Za-z0-9_./%-]+?)/?")
_NOT_A_SONG_ALBUMS = frozenset({"Radio Program"})
_NOT_MUSIC_GENRES = frozenset(
    {
        "Spoken",
        "Spoken Word",
        "Spoken Weird",
        "Sound Poetry",
        "Poetry",
        "Interview",
        "Radio",
        "Radio Art",
        "Radio Theater",
        "Talk Radio",
        "Banter",
        "Comedy",
        "Sound Effects",
        "Field Recordings",
    }
)
_NOT_A_SONG_WORDS = re.compile(
    r"\b(stems?|a ?cappella|acapella|samples?|sample ?pack|preview|excerpt|snippet|loops?"
    r"|interview)\b"
)
_UNSAFE_EXPLICIT = frozenset({"Radio-Unsafe", "Adults-Only"})
# A track is refused rather than have one of these dropped from its credits.
_REQUIRED_IF_LISTED = (
    "track_copyright_c",
    "track_copyright_p",
    "track.publisher",
    "track.composer",
    "track.lyricist",
)
_TRACK_COLUMNS = (
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
_RAW_COLUMNS = (
    "license_title",
    "license_url",
    "track_copyright_c",
    "track_copyright_p",
    "track_explicit",
    "track_genres",
    "track_instrumental",
    "track_url",
)
# The importer keeps less than this per detail; a long artist biography stops here.
_MAX_HINT_CHARACTERS = 200
# What some listings hold in a field nobody filled in. It isn't a name or a holder.
_MISSING = "null"
# A contact address in an artist's notes is personal data, not a description of the music.
# The lookbehind stops a retry at every position of a long word, which would be quadratic.
_EMAIL = re.compile(r"(?<![\w.+-])(?:mailto:)?[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
DATASET_NOTICE = (
    "Listing and file from the FMA research dataset (Defferrard et al., 2017); "
    "its metadata is under CC BY 4.0."
)
PLAYBACK_NOTICE = (
    "Playback decodes the published MP3 to 48 kHz stereo and does not otherwise edit the recording."
)

type Listing = dict[str, str]
type Row = dict[str, object]


def load_listing(tracks_csv: Path, raw_tracks_csv: Path) -> dict[str, Listing]:
    """The fields this rule reads from the dataset's two listing files, by track ID."""
    csv.field_size_limit(16_000_000)
    listings: dict[str, Listing] = {}
    with tracks_csv.open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream)
        # The file's three header rows are a top level, a field name and the index name.
        names = [f"{a}.{b}" for a, b in zip(next(reader), next(reader), strict=True)]
        next(reader)
        positions = {name: names.index(name) for name in _TRACK_COLUMNS}
        for row in reader:
            listings[row[0]] = {"id": row[0]} | {k: row[i] for k, i in positions.items()}
    with raw_tracks_csv.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["track_id"] in listings:
                listings[row["track_id"]] |= {key: row[key] for key in _RAW_COLUMNS}
    return listings


def _number(value: str) -> int:
    try:
        return int(float(value))
    except ValueError:
        return 0


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _importable(value: str, *, maximum: int = 200) -> bool:
    """Whether the importer can carry this text unchanged, apart from extra whitespace."""
    text = _single_line(value)
    return (
        1 <= len(text) <= maximum
        and "://" not in text
        and "<" not in text
        and ">" not in text
        and not any(unicodedata.category(c).startswith("C") for c in text)
    )


def _listed(row: Listing, key: str) -> str:
    """An optional credit field to reproduce, or "" if it's blank, a marker or unsafe."""
    text = _single_line(row.get(key, ""))
    return text if text.casefold() != _MISSING and _importable(text) else ""


def _credit_would_be_lost(row: Listing, key: str) -> bool:
    """Whether the listing gives this credit but the importer can't carry it in full."""
    text = _single_line(row.get(key, ""))
    return bool(text) and text.casefold() != _MISSING and not _importable(text)


def license_class(row: Listing) -> str | None:
    """The Creative Commons class of a listed license link, such as ``by-nc``."""
    match = _CC_LICENSE.fullmatch(row.get("license_url", ""))
    return match[1].lower() if match else None


def genres(row: Listing) -> list[str]:
    """The genre titles a track is directly listed under."""
    try:
        entries = ast.literal_eval(row.get("track_genres") or "[]")
    except ValueError, SyntaxError:
        return []
    if not isinstance(entries, list):
        return []
    titles = [
        cast(dict[str, object], item).get("genre_title") for item in cast(list[object], entries)
    ]
    return [title for title in titles if isinstance(title, str)]


def _words(value: str) -> str:
    return re.sub(r"[_\-.()\[\]]+", " ", value).casefold()


def _tag_list(value: str) -> list[str]:
    try:
        entries = ast.literal_eval(value or "[]")
    except ValueError, SyntaxError:
        return []
    if not isinstance(entries, list):
        return []
    return [tag for tag in cast(list[object], entries) if isinstance(tag, str)]


def track_page(row: Listing) -> str | None:
    """The track's source page over HTTPS, in the form the importer accepts."""
    match = _TRACK_PAGE.fullmatch(row.get("track_url", ""))
    return "https://freemusicarchive.org" + match[1] if match else None


def expected_bytes(row: Listing) -> int:
    return _number(row["track.bit_rate"]) * _number(row["track.duration"]) // 8


def rejection(row: Listing, licenses: frozenset[str] = ANY_CC) -> str | None:
    """Why this listed track can't be a candidate, or None. Checks only what the listing says."""
    if row["id"] in KNOWN_SHORT:
        return "known short file in the dataset"
    kind = license_class(row)
    if kind is None:
        return "no Creative Commons license link"
    if kind not in licenses:
        return "license outside the accepted set"
    if track_page(row) is None:
        return "no usable track page"
    duration = _number(row["track.duration"])
    if not MIN_SECONDS <= duration <= MAX_SECONDS:
        return "length outside 1 to 15 minutes"
    if _number(row["track.bit_rate"]) < MIN_BIT_RATE:
        return "bitrate under 128 kbps"
    if expected_bytes(row) > MAX_FILE_BYTES:
        return "file over 20 MB"
    if row.get("track_explicit") in _UNSAFE_EXPLICIT:
        return "flagged as not radio-safe"
    if row["album.type"] in _NOT_A_SONG_ALBUMS:
        return "part of a radio program"
    if (
        not row.get("license_title")
        or not all(_importable(row[key]) for key in ("track.title", "artist.name", "license_title"))
        or any(_credit_would_be_lost(row, key) for key in _REQUIRED_IF_LISTED)
    ):
        return "credit text can't be reproduced safely in full"
    if _NOT_A_SONG_WORDS.search(_words(row["track.title"])):
        return "title names a sample, stem, preview, excerpt, loop or similar"
    titles = genres(row)
    if not titles:
        return "no listed genre"
    if _NOT_MUSIC_GENRES.intersection(titles):
        return "listed as spoken word, radio or sound effects"
    if _number(row["track.listens"]) < MIN_LISTENS:
        return "under 1,000 listens"
    return None


def candidates(
    rows: Iterable[Listing], limit: int, licenses: frozenset[str] = ANY_CC
) -> list[Listing]:
    """Tracks in selection order: one per genre per round, most listened first.

    Genres take their turn from most to least listened, so every genre gets a place
    before a popular one gets its second. At most two tracks per artist, and one per
    artist and title, since the dataset holds exact duplicates.
    """
    eligible = [row for row in rows if rejection(row, licenses) is None]
    eligible.sort(key=lambda row: (-_number(row["track.listens"]), int(row["id"])))
    audience: Counter[str] = Counter()
    members: dict[str, list[Listing]] = {}
    for row in eligible:
        for title in genres(row):
            audience[title] += _number(row["track.listens"])
            members.setdefault(title, []).append(row)
    queues = {title: deque(members[title]) for title, _ in audience.most_common()}
    chosen: list[Listing] = []
    taken: set[str] = set()
    names: set[tuple[str, str]] = set()
    per_artist: Counter[str] = Counter()
    while len(chosen) < limit and any(queues.values()):
        for queue in queues.values():
            while queue and len(chosen) < limit:
                row = queue.popleft()
                name = (row["artist.name"].casefold(), row["track.title"].casefold())
                artist = row["artist.id"]
                if row["id"] in taken or name in names or per_artist[artist] >= PER_ARTIST:
                    continue
                taken.add(row["id"])
                names.add(name)
                per_artist[artist] += 1
                chosen.append(row)
                break
    return chosen


def _plain_text(value: str) -> str:
    """Description text without markup or email addresses, bounded, as a matching hint."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", value))
    return _single_line(_EMAIL.sub(" ", text))[:_MAX_HINT_CHARACTERS]


def manifest_row(row: Listing, embedded: Sequence[str] = ()) -> Row:
    """One listed track in the importer's FMA manifest form."""
    page = track_page(row)
    assert page is not None
    seconds = _number(row["track.duration"])
    creators: list[Row] = [
        {"name": _single_line(row["artist.name"]), "role": "Artist", "evidence": page}
    ]
    for key, role in (("track.composer", "Composer"), ("track.lyricist", "Lyricist")):
        if name := _listed(row, key):
            creators.append({"name": name, "role": role, "evidence": page})
    notices: list[str] = []
    for key, label in (
        ("album.title", "Album"),
        ("track_copyright_c", "Copyright (as listed)"),
        ("track_copyright_p", "Sound recording copyright (as listed)"),
        ("track.publisher", "Publisher (as listed)"),
    ):
        if text := _listed(row, key):
            notices.append(f"{label}: {text}")
    notices += [DATASET_NOTICE, PLAYBACK_NOTICE]
    tags = dict.fromkeys(
        tag.strip()
        for tag in (
            *genres(row),
            *_tag_list(row["track.tags"]),
            *_tag_list(row["album.tags"]),
            *_tag_list(row["artist.tags"]),
        )
        if _importable(tag, maximum=64)
    )
    details: list[list[str]] = []
    if row.get("track_instrumental") == "1":
        tags.setdefault("instrumental")  # 0 is the listing's default, so it isn't evidence
    for key, label in (
        ("album.title", "album"),
        ("track.information", "description"),
        ("album.information", "album description"),
        ("artist.bio", "artist"),
        ("artist.related_projects", "similar artists"),
        ("artist.location", "artist location"),
        ("track.language_code", "language"),
    ):
        text = _plain_text(row[key])
        if text and text.casefold() != _MISSING:
            details.append([label, text])
    if row["track.date_created"][:4].isdigit():
        details.append(["listed", row["track.date_created"][:4]])
    result: Row = {
        "id": row["id"],
        "source_url": page,
        "title": _single_line(row["track.title"]),
        "primary_artist": _single_line(row["artist.name"]),
        "creators": creators,
        "recording_kind": "unknown",
        "kind_evidence": page,
        "duration_seconds": seconds,
        "duration_evidence": {"kind": "reported", "url": page},
        "license": {
            "label": _single_line(row["license_title"]),
            "url": row["license_url"],
        },
        "notices": notices,
        "tags": list(tags)[:64],
        "import_status": "operator_copy_available",
        "details": details,
        "embedded_notices": list(embedded),
    }
    if composer := _listed(row, "track.composer"):
        result["composer"] = composer
    return result


def manifest(rows: Iterable[Row]) -> Row:
    return {"schema": "sobafm-fma-manifest-1", "recordings": list(rows)}


@dataclass(frozen=True)
class VerifiedFile:
    """What validation measured for one recording's exact local file."""

    recording_id: str
    file: str
    size: int
    sha256: str
    pcm_bytes: int

    @property
    def max_pcm_bytes(self) -> int:
        return (self.pcm_bytes + PCM_MARGIN_BYTES + FRAME_BYTES - 1) // FRAME_BYTES * FRAME_BYTES


def finalized_catalog(draft: Catalog, verified: Mapping[str, VerifiedFile]) -> Catalog:
    """Keep only validated recordings and record what validation established."""
    document = catalog_document(draft)
    for partition in cast(list[Row], document["partitions"]):
        kept: list[Row] = []
        for row in cast(list[Row], partition["recordings"]):
            if row["recording_id"] in verified:
                row["kind"] = "complete"
                row["kind_evidence"] = row["provenance"]
                row["availability"] = "available"
                kept.append(row)
        partition["recordings"] = kept
    return parse_catalog(json.dumps(document))


def media_manifest(verified: Iterable[VerifiedFile]) -> Row:
    return {
        "version": 1,
        "recordings": [
            {
                "key": {"source": Source.FMA.value, "recording_id": item.recording_id},
                "file": item.file,
                "size": item.size,
                "sha256": item.sha256,
                "max_pcm_bytes": item.max_pcm_bytes,
            }
            for item in sorted(verified, key=lambda item: int(item.recording_id))
        ],
    }


def admission_reviews(catalog: Catalog, evidence: str, note: str) -> Row:
    """Bind the maintainer's acceptance to each recording's digest."""
    credential_free_url(evidence)  # the application refuses anything else at startup
    return {
        "schema": "sobafm-admission-1",
        "reviews": [
            {
                "key": {"source": partition.source.value, "recording_id": item.key.recording_id},
                "metadata_digest": metadata_digest(item),
                "credits_complete": True,
                "access_ready": True,
                "synthetic": False,
                "evidence": evidence,
                "note": note,
            }
            for partition in catalog.partitions
            for item in partition.recordings
        ],
    }
