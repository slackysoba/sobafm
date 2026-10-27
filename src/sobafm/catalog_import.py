"""Offline, bounded transforms of saved source metadata; never grant admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from sobafm.catalog import (
    MAX_DURATION_SECONDS,
    MAX_RECORDS_PER_SOURCE,
    Catalog,
    CatalogInputError,
    Feature,
    Partition,
    Recording,
    SnapshotState,
    Source,
    parse_catalog,
)

MAX_IMPORT_BYTES = 1_000_000


@dataclass(frozen=True)
class ImportContext:
    observed_at: str
    evidence_kind: str
    scope: str
    state: str
    requested_ids: tuple[str, ...]
    selected_files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RecordReport:
    index: int
    recording_id: str | None
    source_file_id: str | None
    accepted_metadata: bool
    issues: tuple[str, ...]
    ignored_field_count: int
    reported_bpm: int | None = None


@dataclass(frozen=True)
class ImportReport:
    source: Source
    context: ImportContext
    state: str
    returned: int
    normalized: int
    missing_ids: tuple[str, ...]
    unexpected_ids: tuple[str, ...]
    issues: tuple[str, ...]
    records: tuple[RecordReport, ...]
    admission_reviews_created: int = 0


@dataclass(frozen=True)
class ImportResult:
    catalog: Catalog
    report: ImportReport


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogInputError("duplicate JSON key")
        result[key] = value
    return result


def _decode(payload: str) -> object:
    try:
        if len(payload) > MAX_IMPORT_BYTES or len(payload.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise CatalogInputError("metadata exceeds byte limit")
        result = cast(object, json.loads(payload, object_pairs_hook=_pairs))
        _bound(result)
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CatalogInputError("invalid or oversized metadata JSON") from exc


def _bound(value: object, depth: int = 0) -> None:
    if depth > 12:
        raise CatalogInputError("metadata nesting exceeds limit")
    if isinstance(value, dict):
        data = cast(dict[str, object], value)
        if len(data) > 128 or any(len(k) > 128 for k in data):
            raise CatalogInputError("metadata object exceeds limit")
        for child in data.values():
            _bound(child, depth + 1)
    elif isinstance(value, list):
        children = cast(list[object], value)
        if len(children) > MAX_RECORDS_PER_SOURCE:
            raise CatalogInputError("metadata list exceeds limit")
        for child in children:
            _bound(child, depth + 1)
    elif isinstance(value, str) and len(value) > 32_768:
        raise CatalogInputError("metadata string exceeds limit")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CatalogInputError("expected object")
    return cast(dict[str, object], value)


def _list(value: object, maximum: int = MAX_RECORDS_PER_SOURCE) -> list[object]:
    if not isinstance(value, list) or len(cast(list[object], value)) > maximum:
        raise CatalogInputError("expected bounded list")
    return cast(list[object], value)


def _text(value: object, maximum: int = 200) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise CatalogInputError("expected bounded text")
    if value != value.strip() or any(unicodedata.category(c).startswith("C") for c in value):
        raise CatalogInputError("unsafe text")
    # Notices requiring links/HTML need explicit safe preparation, never silent stripping.
    if "://" in value or "<" in value or ">" in value:
        raise CatalogInputError("text requires safe link or markup preparation")
    return value


def _id(value: object) -> str:
    if type(value) is int and value > 0:
        value = str(value)
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,17}", value) is None:
        raise CatalogInputError("invalid provider identity")
    return value


def _link(value: object, *, source: Source | None = None, identity: str | None = None) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise CatalogInputError("invalid source link")
    url = urlsplit(value)
    host = url.hostname or ""
    allowed_http = {"creativecommons.org", "www.jamendo.com", "ccmixter.org"}
    if (
        url.scheme not in ("https", "http")
        or (url.scheme == "http" and host not in allowed_http)
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or url.port not in (None, 443)
        or re.fullmatch(r"[a-z0-9.-]+", host) is None
        or "." not in host
        or any(c.isspace() or unicodedata.category(c).startswith("C") for c in value)
        or "\\" in value
    ):
        raise CatalogInputError("unsafe source link")
    if source == Source.JAMENDO:
        if host != "www.jamendo.com" or not re.fullmatch(
            rf"/track/{re.escape(identity or '')}(?:/[A-Za-z0-9_-]+)?/?", url.path
        ):
            raise CatalogInputError("source identity and link differ")
    elif source == Source.CCMIXTER:
        if host != "ccmixter.org" or not re.fullmatch(
            rf"/files/[A-Za-z0-9_.-]+/{re.escape(identity or '')}/?", url.path
        ):
            raise CatalogInputError("source identity and link differ")
    elif source == Source.FMA:
        if host != "freemusicarchive.org" or not re.fullmatch(
            r"/music/[A-Za-z0-9_./%-]+/?", url.path
        ):
            raise CatalogInputError("invalid FMA recording page")
    elif re.search(r"\.(mp3|wav|ogg|flac|m4a|aac|mp4|zip|jpg|png)$", url.path, re.IGNORECASE):
        raise CatalogInputError("media link is not license evidence")
    return urlunsplit(("https", host, url.path, "", ""))


def parse_context(payload: str) -> ImportContext:
    data = _object(_decode(payload))
    required = {"schema", "observed_at", "evidence_kind", "scope", "state", "requested_ids"}
    if not required <= data.keys() or not data.keys() <= required | {"selected_files"}:
        raise CatalogInputError("invalid import context fields")
    if data["schema"] != "sobafm-import-context-1":
        raise CatalogInputError("unsupported import context")
    observed = _text(data["observed_at"], 10)
    try:
        valid_date = date.fromisoformat(observed).isoformat() == observed
    except ValueError as exc:
        raise CatalogInputError("expected observation date YYYY-MM-DD") from exc
    if not valid_date:
        raise CatalogInputError("expected observation date YYYY-MM-DD")
    kind, state = _text(data["evidence_kind"]), _text(data["state"])
    if kind not in ("observed", "operator", "example", "synthetic"):
        raise CatalogInputError("unsupported evidence kind")
    if state not in ("complete", "partial", "unavailable"):
        raise CatalogInputError("unsupported load state")
    identities = tuple(_text(v, 256) for v in _list(data["requested_ids"]))
    if len(set(identities)) != len(identities):
        raise CatalogInputError("duplicate requested identities")
    files: list[tuple[str, str]] = []
    for item in _list(data.get("selected_files", [])):
        selected = _object(item)
        if set(selected) != {"recording_id", "file_id"}:
            raise CatalogInputError("invalid selected file")
        files.append((_id(selected["recording_id"]), _id(selected["file_id"])))
    if len({key for key, _ in files}) != len(files):
        raise CatalogInputError("duplicate selected files")
    return ImportContext(observed, kind, _text(data["scope"], 200), state, identities, tuple(files))


def parse_duration(value: object, *, clock: bool = False) -> int | None:
    """No rounding, boolean coercion, malformed clock rollover or invented default."""
    if clock:
        if not isinstance(value, str) or not re.fullmatch(
            r"(?:[0-9]{1,2}:)?[0-9]{1,4}:[0-5][0-9]", value
        ):
            return None
        parts = [int(p) for p in value.split(":")]
        if len(parts) == 3 and parts[1] > 59:
            return None
        seconds = parts[-1] + 60 * parts[-2] + (3600 * parts[0] if len(parts) == 3 else 0)
    elif type(value) is int:
        seconds = value
    else:
        return None
    return seconds if 1 <= seconds <= MAX_DURATION_SECONDS else None


_TAGS: dict[str, tuple[Feature, bool]] = {
    "piano": (Feature.PIANO, True),
    "guitar": (Feature.GUITAR, True),
    "drums": (Feature.DRUMS, True),
    "acoustic": (Feature.ACOUSTIC, True),
    "electronic": (Feature.ELECTRONIC, True),
    "jazz": (Feature.JAZZ, True),
    "blues": (Feature.BLUES, True),
    "folk": (Feature.FOLK, True),
    "funk": (Feature.FUNK, True),
    "hiphop": (Feature.HIPHOP, True),
    "ambient": (Feature.AMBIENT, True),
    "synthwave": (Feature.SYNTHWAVE, True),
    "metal": (Feature.METAL, True),
    "instrumental": (Feature.VOCALS, False),
    "vocals": (Feature.VOCALS, True),
    "vocal": (Feature.VOCALS, True),
    "female_vocals": (Feature.VOCALS, True),
    "male_vocals": (Feature.VOCALS, True),
}


def _facts(tags: list[str], evidence: str, issues: list[str]) -> list[dict[str, object]]:
    values: dict[Feature, set[bool]] = {}
    for tag in tags:
        mapped = _TAGS.get(tag.casefold())
        if mapped is None:
            issues.append("unmapped_descriptor")
        else:
            feature, value = mapped
            values.setdefault(feature, set()).add(value)
    facts: list[dict[str, object]] = []
    for feature, choices in sorted(values.items()):
        if len(choices) != 1:
            issues.append("contradictory_descriptor")
        else:
            facts.append(
                {
                    "feature": feature,
                    "value": next(iter(choices)),
                    "strength": "weak",
                    "evidence": evidence,
                }
            )
    return facts


def _tags(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        parts = value.split(",")
        if len(parts) > 64:
            raise CatalogInputError("too many tags")
        return [_text(v.strip(), 64) for v in parts if v.strip()]
    return [_text(v, 64) for v in _list(value, 64)]


def _base(identity: str, title: object, artist: object, link: str) -> dict[str, object]:
    name = _text(artist)
    return {
        "recording_id": identity,
        "title": _text(title),
        "primary_artist": name,
        "creators": [name],
        "provenance": link,
        "kind": "unknown",
        "kind_evidence": link,
        "duration_seconds": None,
        "availability": "unknown",
        "composition": None,
        "composer": None,
        "facts": [],
        "credits": None,
    }


def _license(label: object, url: object, issues: list[str]) -> tuple[str | None, str | None]:
    if not url:
        issues.append("license_missing")
        return None, None
    try:
        link = _link(url)
        if label:
            return _text(label), link
        parsed = urlsplit(link)
        if parsed.hostname == "creativecommons.org" and re.fullmatch(
            r"/(?:licenses/[a-z-]+/[0-9.]+(?:/[a-z]{2})?|publicdomain/zero/[0-9.]+)/?",
            parsed.path,
        ):
            return "Creative Commons " + parsed.path.strip("/"), link
        issues.append("license_family_unidentified")
        return "License terms (unidentified)", link
    except ValueError, CatalogInputError:
        issues.append("license_requires_review")
        return None, None


def _credits(
    source: Source,
    record: dict[str, object],
    context: ImportContext,
    notices: list[str],
    license_pair: tuple[str | None, str | None],
) -> dict[str, object]:
    notices = list(dict.fromkeys(notices))
    identity = json.dumps(
        {
            "source": source,
            "recording_id": record["recording_id"],
            "title": record["title"],
            "creators": record["creators"],
            "provenance": record["provenance"],
            "observed_at": context.observed_at,
            "evidence_kind": context.evidence_kind,
            "notices": notices,
            "license": license_pair,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "snapshot_id": source + "-" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "notices": notices,
        "license_label": license_pair[0],
        "license_url": license_pair[1],
    }


def _jamendo(
    row: dict[str, object], context: ImportContext
) -> tuple[dict[str, object], RecordReport]:
    identity = _id(row.get("id"))
    link = _link(row.get("shareurl"), source=Source.JAMENDO, identity=identity)
    record = _base(identity, row.get("name"), row.get("artist_name"), link)
    issues = ["complete_recording_unverified", "credits_require_review", "access_unverified"]
    duration = parse_duration(row.get("duration"))
    if duration is None:
        issues.append("duration_unknown_or_malformed")
    record["duration_seconds"] = duration
    notices = ["Provider: Jamendo", "Source: " + link]
    for field in ("artist_id", "album_id"):
        if row.get(field) not in (None, ""):
            notices.append(field + ": " + _id(row[field]))
    if row.get("album_name"):
        notices.append("Album: " + _text(row["album_name"]))
    music = _object({} if row.get("musicinfo") is None else row["musicinfo"])
    tag_groups = _object({} if music.get("tags") is None else music["tags"])
    tags: list[str] = []
    for field in ("genres", "instruments", "vartags"):
        tags.extend(_tags(tag_groups.get(field)))
    vocal = music.get("vocalinstrumental")
    if vocal in ("vocal", "instrumental"):
        tags.append(cast(str, vocal))
    elif vocal not in (None, ""):
        issues.append("vocal_value_unknown")
    if music.get("acousticelectric") == "acoustic":
        tags.append("acoustic")
    elif music.get("acousticelectric") not in (None, ""):
        issues.append("acoustic_value_unmapped")
    # Speed is not energy; language defaults and absent vocal flags are not facts.
    if music.get("speed") not in (None, ""):
        issues.append("speed_not_energy")
    if music.get("lang") not in (None, ""):
        issues.append("language_requires_review")
    if row.get("licenses"):
        issues.append("license_flags_not_permissions")
    record["facts"] = _facts(tags, link, issues)
    supplied = [_text(v, 1024) for v in _list(row.get("notices", []), 12)]
    if not supplied:
        issues.append("source_notices_not_supplied")
    record["credits"] = _credits(
        Source.JAMENDO,
        record,
        context,
        notices + supplied,
        _license(None, row.get("license_ccurl"), issues),
    )
    known = {
        "id",
        "name",
        "duration",
        "artist_id",
        "artist_name",
        "album_id",
        "album_name",
        "shareurl",
        "license_ccurl",
        "musicinfo",
        "notices",
    }
    discarded = {
        "audio",
        "audiodownload",
        "audiodownload_allowed",
        "image",
        "album_image",
        "releasedate",
        "licenses",
        "stats",
        "lyrics",
        "shorturl",
    }
    unknown = (
        len(row.keys() - known - discarded)
        + len(
            music.keys()
            - {"tags", "vocalinstrumental", "acousticelectric", "speed", "lang", "gender"}
        )
        + len(tag_groups.keys() - {"genres", "instruments", "vartags"})
    )
    if unknown:
        issues.append("unknown_fields_not_copied")
    return record, RecordReport(0, identity, None, True, tuple(dict.fromkeys(issues)), unknown)


def _ccmixter(
    row: dict[str, object], context: ImportContext
) -> tuple[dict[str, object], RecordReport]:
    identity = _id(row.get("upload_id"))
    link = _link(row.get("file_page_url"), source=Source.CCMIXTER, identity=identity)
    artist = row.get("user_real_name") or row.get("user_name")
    record = _base(identity, row.get("upload_name"), artist, link)
    issues = ["complete_recording_unverified", "credits_require_review", "access_unverified"]
    notices = ["Provider: ccMixter", "Source: " + link]
    creators = [_text(artist)]
    extra = _object(row.get("upload_extra", {}))
    if extra.get("featuring"):
        credit = _text(extra["featuring"])
        creators.append(credit)
        notices.append("Featuring (as supplied): " + credit)
    if row.get("user_name"):
        notices.append("Uploader: " + _text(row["user_name"]))
    how = _object(extra.get("howididit", {}))
    if how.get("samples"):
        notices.append("Sample statement (as supplied): " + _text(how["samples"], 900))
    parents = _list(row.get("remix_parents", []), 12)
    if "remix_parents" not in row:
        issues.append("upstream_relationships_unknown")
    for item in parents:
        parent = _object(item)
        parent_id = _id(parent.get("upload_id"))
        parent_link = _link(parent.get("file_page_url"), source=Source.CCMIXTER, identity=parent_id)
        creator = _text(parent.get("user_real_name") or parent.get("user_name"))
        creators.append(creator)
        title = _text(parent.get("upload_name"))
        parent_license = _license(parent.get("license_name"), parent.get("license_url"), issues)
        notices.append(
            "Upstream "
            + parent_id
            + ": "
            + title
            + " — "
            + creator
            + "; "
            + parent_link
            + "; license: "
            + (parent_license[1] or "unknown")
        )
        if parent.get("remix_parents"):
            raise CatalogInputError("nested credit chain requires explicit preparation")
        if parent.keys() - {
            "upload_id",
            "file_page_url",
            "upload_name",
            "user_name",
            "user_real_name",
            "artist_page_url",
            "license_name",
            "license_url",
            "remix_parents",
        }:
            issues.append("upstream_fields_require_review")
    if parents:
        issues.append("upstream_notices_and_chain_unverified")
    record["creators"] = list(dict.fromkeys(creators))
    tags = _tags(extra.get("usertags"))
    record["facts"] = _facts(tags, link, issues)
    files = [_object(item) for item in _list(row.get("files", []), 16)]
    file_ids = [_id(item.get("file_id")) for item in files]
    if len(set(file_ids)) != len(file_ids):
        raise CatalogInputError("duplicate source file identity")
    requested_file = dict(context.selected_files).get(identity)
    selected = None
    if requested_file is not None:
        selected = next((item for item in files if _id(item["file_id"]) == requested_file), None)
        if selected is None:
            issues.append("selected_file_missing")
    elif len(files) == 1:
        selected = files[0]
    else:
        issues.append("source_file_selection_required")
    selected_id: str | None = None
    if selected is not None:
        selected_id = _id(selected["file_id"])
        if selected.get("file_upload") is not None and _id(selected["file_upload"]) != identity:
            raise CatalogInputError("source file belongs to another recording")
        notices.append("Source file identity: " + selected_id)
        info = _object(selected.get("file_format_info", {}))
        if info.get("mime_type") == "audio/mpeg":
            record["duration_seconds"] = parse_duration(info.get("ps"), clock=True)
        else:
            issues.append("unsupported_or_unknown_file_type")
    if record["duration_seconds"] is None:
        issues.append("duration_unknown_or_malformed")
    # System/editorial/remix tags do not prove completeness or absence of vocals.
    if _tags(row.get("upload_tags")):
        issues.append("system_tags_not_descriptors")
    bpm = extra.get("bpm")
    reported_bpm = bpm if type(bpm) is int and 1 <= bpm <= 400 else None
    if bpm not in (None, "") and reported_bpm is None:
        issues.append("bpm_unknown_or_malformed")
    notices.extend(_text(v, 1024) for v in _list(row.get("notices", []), 12))
    record["credits"] = _credits(
        Source.CCMIXTER,
        record,
        context,
        notices,
        _license(row.get("license_name"), row.get("license_url"), issues),
    )
    known = {
        "upload_id",
        "upload_name",
        "upload_extra",
        "upload_tags",
        "file_page_url",
        "user_name",
        "user_real_name",
        "license_name",
        "license_url",
        "files",
        "remix_parents",
        "notices",
    }
    discarded = {
        "user_id",
        "upload_user",
        "artist_page_url",
        "license_logo_url",
        "upload_date",
        "remix_children",
        "children_overflow",
        "upload_taglinks",
    }
    unknown = len(row.keys() - known - discarded) + len(
        extra.keys()
        - {
            "usertags",
            "featuring",
            "bpm",
            "howididit",
            "ccud",
            "systags",
            "relative_dir",
            "nsfw",
            "num_reviews",
            "edpicks",
            "ccplus",
        }
    )
    if unknown:
        issues.append("unknown_fields_not_copied")
    return record, RecordReport(
        0, identity, selected_id, True, tuple(dict.fromkeys(issues)), unknown, reported_bpm
    )


def _fma(row: dict[str, object], context: ImportContext) -> tuple[dict[str, object], RecordReport]:
    required = {
        "source_url",
        "title",
        "primary_artist",
        "creators",
        "recording_kind",
        "kind_evidence",
        "duration_seconds",
        "duration_evidence",
        "license",
        "notices",
        "tags",
        "import_status",
    }
    if not required <= row.keys() or not row.keys() <= required | {"id", "composition", "composer"}:
        raise CatalogInputError("invalid FMA manifest fields")
    link = _link(row["source_url"], source=Source.FMA)
    identity = _id(row["id"]) if "id" in row else urlsplit(link).path.rstrip("/")
    record = _base(identity, row["title"], row["primary_artist"], link)
    issues = ["credits_require_review", "operator_asset_and_access_unverified"]
    record["kind"] = _text(row["recording_kind"])
    record["kind_evidence"] = _link(row["kind_evidence"], source=Source.FMA)
    record["duration_seconds"] = parse_duration(row["duration_seconds"])
    notices = ["Provider: Free Music Archive", "Source: " + link]
    if record["duration_seconds"] is not None:
        evidence = _object(row["duration_evidence"])
        if set(evidence) != {"kind", "url"} or evidence["kind"] not in ("reported", "measured"):
            raise CatalogInputError("invalid duration evidence")
        notices.append(
            "Duration "
            + cast(str, evidence["kind"])
            + ": "
            + _link(evidence["url"], source=Source.FMA)
        )
    else:
        issues.append("duration_unknown_or_malformed")
    creators: list[str] = []
    for item in _list(row["creators"], 12):
        creator = _object(item)
        if set(creator) != {"name", "role", "evidence"}:
            raise CatalogInputError("invalid contributor evidence")
        name = _text(creator["name"])
        creators.append(name)
        notices.append(
            _text(creator["role"])
            + ": "
            + name
            + "; "
            + _link(creator["evidence"], source=Source.FMA)
        )
    record["creators"] = list(dict.fromkeys(creators))
    license_data = _object(row["license"])
    if set(license_data) != {"label", "url"}:
        raise CatalogInputError("invalid license fields")
    notices.extend(_text(v, 1024) for v in _list(row["notices"], 12))
    record["credits"] = _credits(
        Source.FMA,
        record,
        context,
        notices,
        _license(license_data["label"], license_data["url"], issues),
    )
    record["facts"] = _facts(_tags(row["tags"]), link, issues)
    for field in ("composition", "composer"):
        if row.get(field) is not None:
            record[field] = _text(row[field])
    if row["import_status"] not in ("metadata_only", "operator_copy_available", "unavailable"):
        raise CatalogInputError("unknown operator import status")
    if row["import_status"] == "unavailable":
        record["availability"] = "unavailable"
    # A claimed local copy is not verified access, file integrity or audio readiness.
    return record, RecordReport(0, identity, None, True, tuple(dict.fromkeys(issues)), 0)


def catalog_document(catalog: Catalog) -> dict[str, object]:
    """Serialize the existing catalog input schema, not dataclass internals."""
    partitions: list[dict[str, object]] = []
    for partition in catalog.partitions:
        rows: list[dict[str, object]] = []
        for recording in partition.recordings:
            row = asdict(recording)
            del row["key"]
            row["recording_id"] = recording.key.recording_id
            rows.append(row)
        partitions.append(
            {
                "source": partition.source,
                "state": partition.state,
                "note": partition.note,
                "recordings": rows,
            }
        )
    return {"schema": "sobafm-catalog-1", "synthetic": catalog.synthetic, "partitions": partitions}


def normalize(source: Source, payload: str | None, context: ImportContext) -> ImportResult:
    """Validate saved data and keep malformed rows/partial source scope visible."""
    # Revalidate contexts supplied directly by typed callers as well as by the CLI.
    context_data = asdict(context)
    context_data["schema"] = "sobafm-import-context-1"
    context_data["selected_files"] = [
        {"recording_id": key, "file_id": value} for key, value in context.selected_files
    ]
    context = parse_context(json.dumps(context_data))
    if source != Source.CCMIXTER and context.selected_files:
        raise CatalogInputError("file selection only applies to ccMixter")
    if source != Source.FMA:
        for identity in context.requested_ids:
            _id(identity)
    synthetic = context.evidence_kind in ("example", "synthetic")
    state = context.state
    issues: list[str] = []
    rows: list[object] = []
    if state == "unavailable":
        if payload is not None:
            raise CatalogInputError("unavailable source must not supply a response")
        issues.append("source_unavailable")
    else:
        if payload is None:
            raise CatalogInputError("loaded source requires saved metadata")
        raw = _decode(payload)
        if source == Source.JAMENDO:
            envelope = _object(raw)
            headers = _object(envelope.get("headers"))
            rows = _list(envelope.get("results"))
            if (
                headers.get("status") not in ("success", "failed")
                or type(headers.get("code")) is not int
            ):
                raise CatalogInputError("malformed source response status")
            if headers["status"] == "failed":
                if rows:
                    raise CatalogInputError("failed source response contains results")
                state = "unavailable"
                issues.append("source_reported_failure")
            elif headers["code"] != 0:
                raise CatalogInputError("inconsistent source response status")
            elif type(headers.get("results_count")) is not int or headers["results_count"] != len(
                rows
            ):
                state = "partial"
                issues.append("response_count_mismatch")
            if headers.get("warnings") not in (None, "", []):
                if state != "unavailable":
                    state = "partial"
                issues.append("source_warning_not_copied")
        elif source == Source.CCMIXTER:
            rows = _list(raw)
        else:
            envelope = _object(raw)
            if (
                set(envelope) != {"schema", "recordings"}
                or envelope["schema"] != "sobafm-fma-manifest-1"
            ):
                raise CatalogInputError("unsupported FMA manifest")
            rows = _list(envelope["recordings"])
    records: list[Recording] = []
    reports: list[RecordReport] = []
    returned_ids: list[str] = []
    duplicate_ids: set[str] = set()
    note = context.observed_at + " " + context.evidence_kind + "; bounded subset: " + context.scope
    for index, value in enumerate(rows):
        identity: str | None = None
        try:
            row = _object(value)
            if source != Source.FMA:
                identity = _id(row.get("id" if source == Source.JAMENDO else "upload_id"))
            else:
                identity = (
                    _id(row["id"])
                    if "id" in row
                    else urlsplit(_link(row.get("source_url"), source=Source.FMA)).path.rstrip("/")
                )
            if identity in returned_ids:
                duplicate_ids.add(identity)
                raise CatalogInputError("duplicate recording identity")
            returned_ids.append(identity)
            if context.requested_ids and identity not in context.requested_ids:
                state = "partial"
                reports.append(
                    RecordReport(
                        index, identity, None, False, ("unexpected_identity_not_imported",), 0
                    )
                )
                continue
            if source == Source.JAMENDO:
                prepared, report = _jamendo(row, context)
            elif source == Source.CCMIXTER:
                prepared, report = _ccmixter(row, context)
            else:
                prepared, report = _fma(row, context)
            validated = parse_catalog(
                json.dumps(
                    {
                        "schema": "sobafm-catalog-1",
                        "synthetic": synthetic,
                        "partitions": [
                            {
                                "source": source,
                                "state": "complete",
                                "note": note,
                                "recordings": [prepared],
                            }
                        ],
                    }
                )
            )
            records.append(validated.partitions[0].recordings[0])
            reports.append(
                RecordReport(
                    index,
                    identity,
                    report.source_file_id,
                    True,
                    report.issues,
                    report.ignored_field_count,
                    report.reported_bpm,
                )
            )
        except CatalogInputError, ValueError, TypeError:
            state = "partial"
            reports.append(
                RecordReport(index, identity, None, False, ("malformed_record_not_imported",), 0)
            )
    if duplicate_ids:
        records = [record for record in records if record.key.recording_id not in duplicate_ids]
        reports = [
            RecordReport(
                r.index,
                r.recording_id,
                None,
                False,
                ("duplicate_identity_not_imported",),
                r.ignored_field_count,
            )
            if r.recording_id in duplicate_ids
            else r
            for r in reports
        ]
        issues.append("duplicate_identities_returned")
    missing = tuple(sorted(set(context.requested_ids) - set(returned_ids)))
    unexpected = (
        tuple(sorted(set(returned_ids) - set(context.requested_ids)))
        if context.requested_ids
        else ()
    )
    if missing and state != "unavailable":
        state = "partial"
        issues.append("requested_ids_missing")
    if unexpected:
        state = "partial"
        issues.append("unexpected_ids_returned")
    # Serialize through the common validator again; no partial mutation of a caller's catalog.
    result_catalog = Catalog(
        synthetic, (Partition(source, SnapshotState(state), note, tuple(records)),)
    )
    result_catalog = parse_catalog(json.dumps(catalog_document(result_catalog)))
    report_state = "empty" if state == "complete" and not rows else state
    return ImportResult(
        result_catalog,
        ImportReport(
            source,
            context,
            report_state,
            len(rows),
            len(records),
            missing,
            unexpected,
            tuple(issues),
            tuple(reports),
        ),
    )


def _read(path: Path) -> str:
    with path.open("rb") as stream:
        data = stream.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise CatalogInputError("input file exceeds byte limit")
    return data.decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=tuple(Source))
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument(
        "--input", type=Path, help="saved JSON; omit only for an unavailable source"
    )
    args = parser.parse_args()
    try:
        context = parse_context(_read(args.context))
        result = normalize(
            Source(args.source), None if args.input is None else _read(args.input), context
        )
        print(json.dumps(catalog_document(result.catalog), ensure_ascii=False, indent=2))
        print(json.dumps(asdict(result.report), ensure_ascii=False, indent=2), file=sys.stderr)
    except OSError, ValueError, UnicodeError:
        parser.error(
            "metadata import failed: invalid, oversized or unavailable input; no catalog emitted"
        )


if __name__ == "__main__":
    main()
