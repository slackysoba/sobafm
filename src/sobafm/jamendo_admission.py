"""Local, default-off Jamendo admission. No I/O and no provider-supplied authority."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from urllib.parse import parse_qsl, urlsplit

from sobafm.catalog import (
    MAX_DETAIL_CHARACTERS,
    MAX_DETAILS,
    MAX_DURATION_SECONDS,
    Availability,
    CatalogInputError,
    CreditSnapshot,
    Recording,
    RecordingKey,
    RecordingKind,
    Source,
    credential_free_url,
    metadata_digest,
)
from sobafm.credits import CreditRejected, render_credits

POLICY_VERSION = "jamendo-stream-1"
MAX_NOTICE_CHARACTERS = 1024
MAX_NOTICES = 16


class Reason(StrEnum):
    DISABLED = "disabled"
    INVALID = "invalid_metadata"
    UNSUPPORTED = "unsupported_evidence"
    IDENTITY = "identity_mismatch"
    LICENSE = "unsupported_license"
    ROUTE = "invalid_stream_route"
    EXCLUDED = "excluded_recording_form"
    RIGHTS = "unresolved_rights_or_notice"
    CREDITS = "unreadable_or_oversized_credits"
    PROVISIONAL = "provisional_listing"
    CHANGED = "ranked_content_changed"


class AdmissionInputError(ValueError):
    """Only a safe reason escapes; never include provider text or a stream URL."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class Purpose(StrEnum):
    RANKING = "ranking"
    REFRESH = "refresh"


@dataclass(frozen=True)
class Policy:
    """Construct only from trusted operator configuration, never a response/model."""

    enabled: bool = False

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise AdmissionInputError(Reason.INVALID)


@dataclass(frozen=True)
class Listing:
    recording: Recording
    # Canonical, bounded evidence with no transport URLs, credentials or approval flags.
    evidence: str = field(repr=False)
    content_digest: str
    reason: Reason
    # Transient only. Never serialize this object or include it in logs/journals.
    stream_url: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class Decision:
    key: RecordingKey
    metadata_digest: str
    evidence_digest: str
    policy_version: str
    reason: Reason
    refreshed_at: datetime
    purpose: Purpose

    @property
    def admitted(self) -> bool:
        return self.reason == Reason.PROVISIONAL


@dataclass(frozen=True)
class Candidate:
    """Safe immutable ranking input; deliberately has no audio location."""

    recording: Recording
    decision: Decision
    content_digest: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def checked_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AdmissionInputError(Reason.INVALID)
    return value.astimezone(UTC)


def evaluate(
    listing: Listing, policy: Policy, refreshed_at: datetime, purpose: Purpose
) -> Decision:
    """Re-evaluate current evidence. A ranking decision never permits audio open."""
    return Decision(
        listing.recording.key,
        metadata_digest(listing.recording),
        _digest(listing.evidence),
        POLICY_VERSION,
        listing.reason if policy.enabled else Reason.DISABLED,
        checked_time(refreshed_at),
        purpose,
    )


def decision_matches(decision: Decision, listing: Listing, policy: Policy) -> bool:
    """Evidence comparison only, not freshness or authorization to reuse audio."""
    return (
        policy.enabled
        and decision.admitted
        and listing.reason == Reason.PROVISIONAL
        and decision.policy_version == POLICY_VERSION
        and decision.key == listing.recording.key
        and decision.metadata_digest == metadata_digest(listing.recording)
        and decision.evidence_digest == _digest(listing.evidence)
    )


def object_fields(value: object, allowed: set[str], required: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AdmissionInputError(Reason.INVALID)
    data = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in data):
        raise AdmissionInputError(Reason.INVALID)
    result = cast(dict[str, object], data)
    if not required <= result.keys():
        raise AdmissionInputError(Reason.INVALID)
    if not result.keys() <= allowed:
        raise AdmissionInputError(Reason.UNSUPPORTED)
    return result


def text(value: object, maximum: int = 200, *, links: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(unicodedata.category(c).startswith("C") for c in value)
        or "<" in value
        or ">" in value
    ):
        raise AdmissionInputError(Reason.CREDITS)
    # Retained text must not smuggle a signed URL into credits or a future journal.
    urls = re.findall(r"\S+://\S+", value)
    if urls and not links:
        raise AdmissionInputError(Reason.CREDITS)
    for url in urls:
        try:
            credential_free_url(url)
        except CatalogInputError:
            raise AdmissionInputError(Reason.CREDITS) from None
    return value


def track_id(value: object) -> str:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,17}", value) is None:
        raise AdmissionInputError(Reason.IDENTITY)
    return value


def _texts(value: object, maximum: int, width: int = 200) -> tuple[str, ...]:
    if not isinstance(value, list) or len(cast(list[object], value)) > maximum:
        raise AdmissionInputError(Reason.CREDITS)
    return tuple(text(item, width, links=True) for item in cast(list[object], value))


def _backlink(value: object, identity: str) -> str:
    if not isinstance(value, str):
        raise AdmissionInputError(Reason.IDENTITY)
    # Historical Jamendo links use HTTP; only this known public authority is upgraded.
    normalized = value.replace("http://www.jamendo.com/", "https://www.jamendo.com/", 1)
    try:
        credential_free_url(normalized)
        url = urlsplit(normalized)
        valid = url.netloc == "www.jamendo.com" and re.fullmatch(
            rf"/track/{identity}(?:/[A-Za-z0-9_-]+)?/?", url.path
        )
    except ValueError:
        valid = False
    if not valid:
        raise AdmissionInputError(Reason.IDENTITY)
    return normalized


def _license(value: object) -> tuple[str, str, str]:
    if not isinstance(value, str):
        raise AdmissionInputError(Reason.LICENSE)
    normalized = value.replace("http://creativecommons.org/", "https://creativecommons.org/", 1)
    try:
        credential_free_url(normalized)
        url = urlsplit(normalized)
        match = re.fullmatch(
            r"/licenses/(by|by-sa|by-nd|by-nc|by-nc-sa|by-nc-nd|by-nd-nc)/"
            r"(1\.0|2\.0|2\.5|3\.0|4\.0)(?:/(us|fr))?/?",
            url.path,
        )
        valid = url.netloc == "creativecommons.org" and match is not None
    except ValueError:
        valid, match = False, None
    if not valid or match is None:
        raise AdmissionInputError(Reason.LICENSE)
    family, version, jurisdiction = match.groups()
    if (family == "by-nd-nc" and version != "1.0") or (family == "by-nc-nd" and version == "1.0"):
        raise AdmissionInputError(Reason.LICENSE)
    # A finite supported set, not acceptance of any syntactically plausible port.
    if jurisdiction and (version, jurisdiction) not in {("3.0", "us"), ("2.0", "fr")}:
        raise AdmissionInputError(Reason.LICENSE)
    jurisdiction_label = (
        jurisdiction
        or {
            "1.0": "Generic",
            "2.0": "Generic",
            "2.5": "Generic",
            "3.0": "Unported",
            "4.0": "International",
        }[version]
    )
    label = f"CC {family.upper()} {version} {jurisdiction_label}"
    return label, normalized, family


def _stream(value: object, identity: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise AdmissionInputError(Reason.ROUTE)
    try:
        url = urlsplit(value)
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        query = dict(pairs)
        valid = (
            url.scheme == "https"
            and re.fullmatch(r"prod-[0-9]+\.storage\.jamendo\.com", url.netloc) is not None
            and url.path == "/"
            and not url.fragment
            and len(query) == len(pairs)
            and query.keys() <= {"trackid", "format", "from", "client_id"}
            and query.get("trackid") == identity
            and query.get("format") == "mp32"
            and not any(c.isspace() or unicodedata.category(c).startswith("C") for c in value)
            and "\\" not in value
        )
    except ValueError:
        valid = False
    if not valid:
        raise AdmissionInputError(Reason.ROUTE)
    return value


_REQUIRED = {
    "id",
    "name",
    "artist_id",
    "artist_name",
    "shareurl",
    "duration",
    "license_ccurl",
    "audio",
}
_TEXT_FIELDS = {
    "album_name",
    "artist_idstr",
    "releasedate",
    "description",
    "type",
    "composition",
    "composer",
}
_LIST_FIELDS = {"creators", "notices", "credits", "warnings", "rights_warnings", "details"}
# These documented fields cannot approve, add a credit, or assert completeness.
# Their contents are never retained, hashed or used as evidence.
_IGNORED = {
    "audiodownload",
    "audiodownload_allowed",
    "image",
    "album_image",
    "shorturl",
    "prourl",
    "waveform",
    "stats",
    "position",
}
_EXCLUDED = re.compile(
    r"\b(previews?|excerpts?|snippets?|stems?|loops?|sample[ _-]*packs?)\b", re.I
)
_CONFLICT = re.compile(
    r"\b(all rights reserved|permission required|rights? (?:unknown|reserved|unresolved)|"
    r"unlicensed|uncleared|samples?|cover(?:s| version)?|takedown|removed)\b",
    re.I,
)


def _notice_conflicts(value: object, license_url: str, backlink: str) -> bool:
    """An alternate license/track link cannot silently qualify the main listing."""
    if isinstance(value, dict):
        return any(
            _notice_conflicts(v, license_url, backlink)
            for v in cast(dict[str, object], value).values()
        )
    if isinstance(value, tuple):
        return any(
            _notice_conflicts(v, license_url, backlink) for v in cast(tuple[object, ...], value)
        )
    if not isinstance(value, str):
        return False
    for link in cast(list[str], re.findall(r"https://\S+", value)):
        url = urlsplit(link)
        if url.hostname == "creativecommons.org" and link.rstrip("/") != license_url.rstrip("/"):
            return True
        if url.hostname == "www.jamendo.com":
            expected = re.match(r"/track/([0-9]+)", urlsplit(backlink).path)
            actual = re.match(r"/track/([0-9]+)(?:/|$)", url.path)
            if actual is None or expected is None or actual[1] != expected[1]:
                return True
    # Text-only license assertions are unsupported evidence, not guesses about
    # an omitted version or jurisdiction. A precise compatible URL is supported.
    without_links = re.sub(r"https://\S+", "", value)
    return re.search(r"\b(?:CC[ -]+BY|Creative Commons)\b", without_links, re.I) is not None


def parse_listing(value: object, expected_id: str | None = None) -> Listing:
    """Normalize the complete supplied row; unsupported evidence fails closed.

    This is separate from the historical saved-metadata importer, which deliberately
    drops unknown fields and cannot furnish admission evidence for this policy.
    """
    row = object_fields(
        value,
        _REQUIRED | _TEXT_FIELDS | _LIST_FIELDS | _IGNORED | {"album_id", "musicinfo", "licenses"},
        _REQUIRED,
    )
    identity = track_id(row["id"])
    if expected_id is not None and identity != track_id(expected_id):
        raise AdmissionInputError(Reason.IDENTITY)
    link = _backlink(row["shareurl"], identity)
    label, license_url, family = _license(row["license_ccurl"])
    stream = _stream(row["audio"], identity)
    title, artist = text(row["name"]), text(row["artist_name"])
    duration = row["duration"]
    if type(duration) is not int or not 1 <= duration <= MAX_DURATION_SECONDS:
        raise AdmissionInputError(Reason.INVALID)
    evidence: dict[str, object] = {
        "id": identity,
        "name": title,
        "artist_name": artist,
        "artist_id": track_id(row["artist_id"]),
        "shareurl": link,
        "duration": duration,
        "license_ccurl": license_url,
        "stream_route": "jamendo-storage-track-mp32",
    }
    for name in _TEXT_FIELDS:
        if name in row:
            evidence[name] = "" if row[name] == "" else text(row[name], 1024, links=True)
    if row.get("album_id") not in (None, ""):
        evidence["album_id"] = track_id(row["album_id"])
    elif "album_id" in row:
        evidence["album_id"] = ""
    for name in _LIST_FIELDS:
        if name in row:
            evidence[name] = _texts(row[name], MAX_NOTICES, MAX_NOTICE_CHARACTERS)
    creators = tuple(dict.fromkeys((artist, *cast(tuple[str, ...], evidence.get("creators", ())))))
    if len(creators) > 16 or any(len(c) > 200 for c in creators):
        raise AdmissionInputError(Reason.CREDITS)
    notices = ["Provider: Jamendo"]
    for name in ("notices", "credits", "details"):
        notices.extend(cast(tuple[str, ...], evidence.get(name, ())))
    # Descriptions can carry attribution as well as musical hints. Carry the whole
    # supplied text into credits rather than trying to guess which sentence matters.
    if evidence.get("description"):
        notices.append(cast(str, evidence["description"]))
    for name in ("composer", "composition"):
        if evidence.get(name):
            notices.append(f"{name}: {evidence[name]}")
    if len(notices) > MAX_NOTICES or any(len(n) > MAX_NOTICE_CHARACTERS for n in notices):
        raise AdmissionInputError(Reason.CREDITS)
    details: list[str] = list(cast(tuple[str, ...], evidence.get("details", ())))
    if evidence.get("album_name"):
        details.append(f"album: {evidence['album_name']}")
    if "musicinfo" in row:
        music = object_fields(
            row["musicinfo"],
            {
                "tags",
                "vocalinstrumental",
                "gender",
                "acousticelectric",
                "speed",
                "lang",
            },
            set(),
        )
        normalized_music: dict[str, object] = {}
        for name, item in music.items():
            if name == "tags":
                groups = object_fields(item, {"genres", "instruments", "vartags"}, set())
                tags = {key: _texts(values, 32, 100) for key, values in groups.items()}
                normalized_music[name] = tags
                details.extend(f"{key}: {v}" for key, values in tags.items() for v in values)
            else:
                normalized_music[name] = "" if item == "" else text(item, 100)
                if item != "":
                    details.append(f"{name}: {normalized_music[name]}")
        evidence["musicinfo"] = normalized_music
    if "licenses" in row:
        licenses = object_fields(
            row["licenses"], {"ccnc", "ccnd", "ccsa", "prolicensing", "probackground"}, set()
        )
        normalized_licenses: dict[str, bool] = {}
        for name, flag in licenses.items():
            if type(flag) is bool:
                boolean = flag
            elif isinstance(flag, str) and flag in ("true", "false"):
                boolean = flag == "true"
            else:
                raise AdmissionInputError(Reason.INVALID)
            normalized_licenses[name] = boolean
            if name in {"ccnc", "ccnd", "ccsa"} and boolean != (name[2:] in family.split("-")):
                raise AdmissionInputError(Reason.RIGHTS)
        evidence["licenses"] = normalized_licenses
    if len(details) > MAX_DETAILS or any(len(d) > MAX_DETAIL_CHARACTERS for d in details):
        raise AdmissionInputError(Reason.UNSUPPORTED)
    serialized = _json(evidence)
    # Scan values, not field names ("sample_pack" in a supplied value still rejects).
    searchable = _json([v for k, v in evidence.items() if k not in {"shareurl", "license_ccurl"}])
    reason = Reason.PROVISIONAL
    normalized_text = unicodedata.normalize("NFKC", searchable).replace("_", " ")
    if _EXCLUDED.search(normalized_text):
        reason = Reason.EXCLUDED
    elif (
        evidence.get("warnings")
        or evidence.get("rights_warnings")
        or _CONFLICT.search(normalized_text)
        or _notice_conflicts(
            {k: v for k, v in evidence.items() if k not in {"shareurl", "license_ccurl"}},
            license_url,
            link,
        )
    ):
        reason = Reason.RIGHTS
    if (
        evidence.get("type", "") not in ("", "single", "albumtrack", "track")
        and reason == Reason.PROVISIONAL
    ):
        reason = Reason.UNSUPPORTED
    credit_data = _json((title, creators, link, license_url, notices))
    recording = Recording(
        RecordingKey(Source.JAMENDO, identity),
        title,
        artist,
        creators,
        link,
        RecordingKind.UNKNOWN,
        link,
        duration,
        Availability.AVAILABLE,
        cast(str | None, evidence.get("composition")) or None,
        cast(str | None, evidence.get("composer")) or None,
        (),
        CreditSnapshot("jamendo-" + _digest(credit_data), tuple(notices), label, license_url),
        tuple(details),
    )
    try:
        render_credits(recording)
    except CreditRejected:
        raise AdmissionInputError(Reason.CREDITS) from None
    # Conservatively treat any descriptive change as material for the ranked choice.
    content = {
        key: val
        for key, val in evidence.items()
        if key
        not in {
            "notices",
            "credits",
            "license_ccurl",
            "licenses",
            "warnings",
            "rights_warnings",
            "stream_route",
        }
    }
    return Listing(recording, serialized, _digest(_json(content)), reason, stream)
