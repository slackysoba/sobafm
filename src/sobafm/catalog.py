"""Bounded, offline selection over reviewed source metadata; no provider or audio I/O."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import cast
from urllib.parse import urlsplit

MAX_CATALOG_BYTES = 2_000_000
MAX_RECORDS_PER_SOURCE = 256
MAX_FEATURES = 32
MAX_QUERY_FEATURES = 16
MAX_DURATION_SECONDS = 86_400
MAX_RESULTS = 20


class CatalogInputError(ValueError):
    """Invalid or unbounded data; no catalog/query has been admitted."""


class Source(StrEnum):
    JAMENDO = "jamendo"
    CCMIXTER = "ccmixter"
    FMA = "fma"


class Strength(StrEnum):
    STRONG = "strong"
    WEAK = "weak"


class RecordingKind(StrEnum):
    COMPLETE = "complete"
    PREVIEW = "preview"
    EXCERPT = "excerpt"
    STEM = "stem"
    LOOP = "loop"
    SAMPLE_PACK = "sample_pack"
    UNKNOWN = "unknown"


class Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class SnapshotState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class CoverageState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    MISSING = "missing"
    EMPTY = "empty"


class Feature(StrEnum):
    PIANO = "piano"
    SOLO_PIANO = "solo_piano"
    LOW_ENERGY = "low_energy"
    HIGH_ENERGY = "high_energy"
    VOCALS = "vocals"
    DRUMS = "drums"
    ACOUSTIC = "acoustic"
    ELECTRONIC = "electronic"
    FOLK = "folk"
    JAZZ = "jazz"
    SWING = "swing"
    FUNK = "funk"
    BASS_GROOVE = "bass_groove"
    INDIE_POP = "indie_pop"
    CATCHY = "catchy"
    CHEERFUL = "cheerful"
    HIPHOP = "hiphop"
    LOFI = "lofi"
    WARM = "warm"
    SPACIOUS = "spacious"
    AMBIENT = "ambient"
    SYNTHWAVE = "synthwave"
    DREAMY = "dreamy"
    DRIVING = "driving"
    ORCHESTRAL = "orchestral"
    DRAMATIC = "dramatic"
    METAL = "metal"
    GUITAR = "guitar"
    BLUES = "blues"
    FRENCH = "french"
    RAP = "rap"
    OLD_SCHOOL = "old_school"


@dataclass(frozen=True, order=True)
class RecordingKey:
    source: Source
    recording_id: str


@dataclass(frozen=True)
class Fact:
    feature: Feature
    value: bool
    strength: Strength
    evidence: str


@dataclass(frozen=True)
class CreditSnapshot:
    snapshot_id: str
    notices: tuple[str, ...]
    license_label: str | None
    license_url: str | None


@dataclass(frozen=True)
class Recording:
    key: RecordingKey
    title: str
    primary_artist: str
    creators: tuple[str, ...]
    provenance: str
    kind: RecordingKind
    kind_evidence: str
    duration_seconds: int | None
    availability: Availability
    composition: str | None
    composer: str | None
    facts: tuple[Fact, ...]
    credits: CreditSnapshot | None


@dataclass(frozen=True)
class Partition:
    source: Source
    state: SnapshotState
    note: str
    recordings: tuple[Recording, ...]


@dataclass(frozen=True)
class Catalog:
    synthetic: bool
    partitions: tuple[Partition, ...]


@dataclass(frozen=True)
class AdmissionReview:
    """Trusted operator decision, supplied separately from provider/model metadata.

    Bind approval to the exact immutable record, including credits and descriptors.
    Booleans are assertions by that reviewer, not automated proof of their truth.
    """

    key: RecordingKey
    metadata_digest: str
    credits_complete: bool
    access_ready: bool
    synthetic: bool
    evidence: str
    note: str | None = None


@dataclass(frozen=True)
class Criterion:
    feature: Feature
    value: bool


@dataclass(frozen=True)
class Query:
    preferences: tuple[Criterion, ...] = ()
    required: tuple[Criterion, ...] = ()
    min_seconds: int | None = None
    max_seconds: int | None = None
    recording: RecordingKey | None = None
    title: str | None = None
    artist: str | None = None
    composition: str | None = None
    composer: str | None = None
    excluded_recordings: tuple[RecordingKey, ...] = ()
    excluded_creators: tuple[str, ...] = ()


@dataclass(frozen=True)
class Match:
    recording: Recording
    strong: tuple[str, ...]
    weak: tuple[str, ...]
    contradicted: tuple[str, ...]
    unknown: tuple[str, ...]


@dataclass(frozen=True)
class Rejection:
    key: RecordingKey
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Coverage:
    source: Source
    state: CoverageState
    note: str
    provided: int
    admitted: int
    matched: int


@dataclass(frozen=True)
class Selection:
    matches: tuple[Match, ...]
    rejected: tuple[Rejection, ...]
    coverage: tuple[Coverage, ...]
    synthetic: bool
    total_matches: int


def _object(value: object, fields: set[str], required: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or len(cast(dict[object, object], value)) > len(fields):
        raise CatalogInputError("expected an object")
    data = cast(dict[object, object], value)
    if not all(isinstance(k, str) for k in data):
        raise CatalogInputError("object keys must be strings")
    result = cast(dict[str, object], data)
    if not required <= result.keys() or not result.keys() <= fields:
        raise CatalogInputError("missing or unsupported fields")
    return result


def _list(value: object, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(cast(list[object], value)) > maximum:
        raise CatalogInputError("expected a bounded list")
    return cast(list[object], value)


def _text(value: object, maximum: int = 200) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise CatalogInputError("expected bounded nonempty text")
    if value != value.strip() or any(unicodedata.category(c).startswith("C") for c in value):
        raise CatalogInputError("text contains whitespace edges or control characters")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise CatalogInputError("expected a boolean")
    return value


def _choice(value: object, choices: tuple[str, ...]) -> str:
    text = _text(value)
    if text not in choices:
        raise CatalogInputError("unsupported enumerated value")
    return text


def _seconds(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_DURATION_SECONDS:
        raise CatalogInputError("duration must be an integer from 1 to 86400 seconds")
    return value


def _url(value: object) -> str:
    text = _text(value, 512)
    try:
        url = urlsplit(text)
        valid = (
            url.scheme == "https"
            and url.hostname is not None
            and "." in url.hostname
            and re.fullmatch(r"[a-zA-Z0-9.-]+", url.hostname) is not None
            and url.username is None
            and url.password is None
            and url.port in (None, 443)
            and not url.query
            and not url.fragment
            and "\\" not in text
            and not any(c.isspace() for c in text)
        )
    except ValueError as exc:
        raise CatalogInputError("invalid evidence URL") from exc
    if not valid:
        raise CatalogInputError(
            "evidence requires a credential-free HTTPS URL without query/fragment"
        )
    return text


def _source(value: object) -> Source:
    return Source(_choice(value, tuple(Source)))


def _key(value: object) -> RecordingKey:
    data = _object(value, {"source", "recording_id"}, {"source", "recording_id"})
    return RecordingKey(_source(data["source"]), _text(data["recording_id"], 256))


def _unique_strings(value: object, maximum: int, width: int = 200) -> tuple[str, ...]:
    result = tuple(_text(v, width) for v in _list(value, maximum))
    if len({_canonical(v) for v in result}) != len(result):
        raise CatalogInputError("duplicate text values")
    return result


def _canonical(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _credit(value: object) -> CreditSnapshot | None:
    if value is None:
        return None
    fields = {"snapshot_id", "notices", "license_label", "license_url"}
    data = _object(value, fields, fields)
    label = _optional_text(data["license_label"])
    url = None if data["license_url"] is None else _url(data["license_url"])
    if (label is None) != (url is None):
        raise CatalogInputError("license label and URL must be supplied together")
    return CreditSnapshot(
        _text(data["snapshot_id"], 128),
        _unique_strings(data["notices"], 16, 1024),
        label,
        url,
    )


def _recording(value: object, source: Source) -> Recording:
    fields = {
        "recording_id",
        "title",
        "primary_artist",
        "creators",
        "provenance",
        "kind",
        "kind_evidence",
        "duration_seconds",
        "availability",
        "composition",
        "composer",
        "facts",
        "credits",
    }
    data = _object(value, fields, fields)
    artist = _text(data["primary_artist"])
    creators = _unique_strings(data["creators"], 16)
    if _canonical(artist) not in {_canonical(c) for c in creators}:
        raise CatalogInputError("primary artist must be included in supplied creators")
    facts: list[Fact] = []
    for item in _list(data["facts"], MAX_FEATURES):
        fact = _object(
            item,
            {"feature", "value", "strength", "evidence"},
            {"feature", "value", "strength", "evidence"},
        )
        facts.append(
            Fact(
                Feature(_choice(fact["feature"], tuple(Feature))),
                _boolean(fact["value"]),
                Strength(_choice(fact["strength"], tuple(Strength))),
                _url(fact["evidence"]),
            )
        )
    if len({f.feature for f in facts}) != len(facts):
        raise CatalogInputError("duplicate or contradictory feature evidence")
    return Recording(
        RecordingKey(source, _text(data["recording_id"], 256)),
        _text(data["title"]),
        artist,
        creators,
        _url(data["provenance"]),
        RecordingKind(_choice(data["kind"], tuple(RecordingKind))),
        _url(data["kind_evidence"]),
        _seconds(data["duration_seconds"]),
        Availability(_choice(data["availability"], tuple(Availability))),
        _optional_text(data["composition"]),
        _optional_text(data["composer"]),
        tuple(sorted(facts, key=lambda f: f.feature)),
        _credit(data["credits"]),
    )


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogInputError("duplicate JSON key")
        result[key] = value
    return result


def _decode(payload: str, maximum: int) -> object:
    if len(payload) > maximum:
        raise CatalogInputError("JSON exceeds character limit")
    try:
        if len(payload.encode("utf-8")) > maximum:
            raise CatalogInputError("JSON exceeds byte limit")
        return cast(object, json.loads(payload, object_pairs_hook=_json_object))
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise CatalogInputError("invalid or oversized JSON") from exc


def parse_catalog(payload: str) -> Catalog:
    """Parse a bounded prepared snapshot atomically; never infer admission.

    COMPLETE describes only the declared local snapshot (at most 256/source),
    never the entire provider catalog. The required note records its scope/date.
    """
    raw = _decode(payload, MAX_CATALOG_BYTES)
    data = _object(
        raw, {"schema", "synthetic", "partitions"}, {"schema", "synthetic", "partitions"}
    )
    if data["schema"] != "sobafm-catalog-1":
        raise CatalogInputError("unsupported catalog schema")
    partitions: list[Partition] = []
    for item in _list(data["partitions"], 3):
        part = _object(
            item,
            {"source", "state", "note", "recordings"},
            {"source", "state", "note", "recordings"},
        )
        source = _source(part["source"])
        state = SnapshotState(_choice(part["state"], tuple(SnapshotState)))
        recordings = tuple(
            _recording(r, source) for r in _list(part["recordings"], MAX_RECORDS_PER_SOURCE)
        )
        if state == "unavailable" and recordings:
            raise CatalogInputError("unavailable partition cannot supply records")
        if len({r.key for r in recordings}) != len(recordings):
            raise CatalogInputError("duplicate recording identity")
        partitions.append(Partition(source, state, _text(part["note"], 512), recordings))
    if len({p.source for p in partitions}) != len(partitions):
        raise CatalogInputError("duplicate source partition")
    return Catalog(_boolean(data["synthetic"]), tuple(partitions))


def parse_reviews(payload: str) -> tuple[AdmissionReview, ...]:
    """Read a separately trusted operator review file, not a provider response."""
    data = _object(_decode(payload, 256_000), {"schema", "reviews"}, {"schema", "reviews"})
    if data["schema"] != "sobafm-admission-1":
        raise CatalogInputError("unsupported admission schema")
    result: list[AdmissionReview] = []
    fields = {"key", "metadata_digest", "credits_complete", "access_ready", "synthetic", "evidence"}
    for item in _list(data["reviews"], 3 * MAX_RECORDS_PER_SOURCE):
        review = _object(item, fields | {"note"}, fields)
        digest = _text(review["metadata_digest"], 64)
        if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise CatalogInputError("invalid admission digest")
        result.append(
            AdmissionReview(
                _key(review["key"]),
                digest,
                _boolean(review["credits_complete"]),
                _boolean(review["access_ready"]),
                _boolean(review["synthetic"]),
                _url(review["evidence"]),
                _optional_text(review.get("note")),
            )
        )
    if len({r.key for r in result}) != len(result):
        raise CatalogInputError("duplicate admission review")
    return tuple(result)


def _criteria(value: object) -> tuple[Criterion, ...]:
    result: list[Criterion] = []
    for item in _list(value, MAX_QUERY_FEATURES):
        criterion = _object(item, {"feature", "value"}, {"feature", "value"})
        result.append(
            Criterion(
                Feature(_choice(criterion["feature"], tuple(Feature))),
                _boolean(criterion["value"]),
            )
        )
    if len({c.feature for c in result}) != len(result):
        raise CatalogInputError("duplicate or contradictory query criteria")
    return tuple(sorted(result, key=lambda c: c.feature))


def parse_query(value: object) -> Query:
    """Boundary for explicit interpreter/model output; reject unknown fields."""
    fields = {
        "preferences",
        "required",
        "min_seconds",
        "max_seconds",
        "recording",
        "title",
        "artist",
        "composition",
        "composer",
        "excluded_recordings",
        "excluded_creators",
    }
    data = _object(value, fields, set())
    preferences = _criteria(data.get("preferences", []))
    required = _criteria(data.get("required", []))
    all_criteria = {c.feature: c.value for c in required}
    if any(c.feature in all_criteria and all_criteria[c.feature] != c.value for c in preferences):
        raise CatalogInputError("preference contradicts a hard constraint")
    minimum, maximum = _seconds(data.get("min_seconds")), _seconds(data.get("max_seconds"))
    if minimum is not None and maximum is not None and minimum > maximum:
        raise CatalogInputError("inverted duration range")
    recording = None if data.get("recording") is None else _key(data["recording"])
    excluded = tuple(_key(v) for v in _list(data.get("excluded_recordings", []), 32))
    if len(set(excluded)) != len(excluded) or recording in excluded:
        raise CatalogInputError("duplicate or conflicting excluded recording")
    query = Query(
        preferences,
        required,
        minimum,
        maximum,
        recording,
        _optional_text(data.get("title")),
        _optional_text(data.get("artist")),
        _optional_text(data.get("composition")),
        _optional_text(data.get("composer")),
        excluded,
        _unique_strings(data.get("excluded_creators", []), 32),
    )
    if query.artist and _canonical(query.artist) in {
        _canonical(c) for c in query.excluded_creators
    }:
        raise CatalogInputError("requested artist is excluded")
    if not any(
        (
            preferences,
            required,
            minimum,
            maximum,
            recording,
            query.title,
            query.artist,
            query.composition,
            query.composer,
        )
    ):
        raise CatalogInputError("query has no supported selection criteria")
    return query


def metadata_digest(recording: Recording) -> str:
    """Bind review to all source identity, credit, access and descriptor metadata."""
    encoded = json.dumps(
        asdict(recording), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _admission_reasons(
    recording: Recording, review: AdmissionReview | None, synthetic: bool
) -> list[str]:
    reasons: list[str] = []
    if recording.kind != "complete":
        reasons.append("not a confirmed complete recording")
    if recording.availability != "available":
        reasons.append("availability metadata is " + recording.availability)
    if recording.credits is None:
        reasons.append("credit snapshot missing")
    if review is None:
        reasons.append("no trusted admission review")
    else:
        if review.metadata_digest != metadata_digest(recording):
            reasons.append("admission review does not match current metadata")
        if review.synthetic != synthetic:
            reasons.append("synthetic and real admission scopes differ")
        if not review.credits_complete:
            reasons.append("complete credits not accepted")
        if not review.access_ready:
            reasons.append("source acquisition/storage route not ready")
    return reasons


def _constraint_reasons(recording: Recording, query: Query) -> list[str]:
    reasons: list[str] = []
    facts = {f.feature: f for f in recording.facts}
    for criterion in query.required:
        fact = facts.get(criterion.feature)
        if fact is None or fact.strength != "strong" or fact.value != criterion.value:
            reasons.append("hard feature not established: " + _criterion_label(criterion))
    if query.recording is not None and recording.key != query.recording:
        reasons.append("exact recording differs")
    for label, requested, actual in (
        ("title", query.title, recording.title),
        ("artist", query.artist, recording.primary_artist),
        ("composition", query.composition, recording.composition),
        ("composer", query.composer, recording.composer),
    ):
        if requested is not None and (
            actual is None or _canonical(actual) != _canonical(requested)
        ):
            reasons.append("exact " + label + " not established")
    if recording.key in query.excluded_recordings:
        reasons.append("recording excluded")
    if {_canonical(c) for c in recording.creators} & {
        _canonical(c) for c in query.excluded_creators
    }:
        reasons.append("creator excluded")
    duration = recording.duration_seconds
    if query.min_seconds is not None or query.max_seconds is not None:
        if duration is None:
            reasons.append("requested duration unknown")
        elif (query.min_seconds is not None and duration < query.min_seconds) or (
            query.max_seconds is not None and duration > query.max_seconds
        ):
            reasons.append("outside requested duration")
    return reasons


def _criterion_label(criterion: Criterion) -> str:
    return criterion.feature if criterion.value else "no " + criterion.feature


def _match(recording: Recording, query: Query) -> Match:
    facts = {f.feature: f for f in recording.facts}
    strong: list[str] = []
    weak: list[str] = []
    contradicted: list[str] = []
    unknown: list[str] = []
    for criterion in query.preferences:
        label = _criterion_label(criterion)
        fact = facts.get(criterion.feature)
        if fact is None:
            unknown.append(label)
        elif fact.value != criterion.value:
            contradicted.append(label)
        elif fact.strength == "strong":
            strong.append(label)
        else:
            weak.append(label)
    return Match(recording, tuple(strong), tuple(weak), tuple(contradicted), tuple(unknown))


def select(
    catalog: Catalog, query: Query, reviews: tuple[AdmissionReview, ...], *, limit: int = 5
) -> Selection:
    """Compare every supplied partition, then rank fit before length and stable identity.

    Catalog and Query come from the validated parsers. Reviews are trusted local
    decisions, never taken from a provider response or model-generated query.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise CatalogInputError("result limit must be from 1 to 20")
    if len(reviews) > 3 * MAX_RECORDS_PER_SOURCE or len({r.key for r in reviews}) != len(reviews):
        raise CatalogInputError("too many or duplicate admission reviews")
    for review in reviews:
        _url(review.evidence)
        if re.fullmatch(r"[a-f0-9]{64}", review.metadata_digest) is None:
            raise CatalogInputError("invalid review digest")
        for flag in (review.credits_complete, review.access_ready, review.synthetic):
            _boolean(flag)
    by_key = {r.key: r for r in reviews}
    by_source = {p.source: p for p in catalog.partitions}
    matches: list[Match] = []
    rejected: list[Rejection] = []
    coverage: list[Coverage] = []
    for source in Source:
        partition = by_source.get(source)
        if partition is None:
            coverage.append(
                Coverage(source, CoverageState.MISSING, "no prepared snapshot supplied", 0, 0, 0)
            )
            continue
        admitted = matched = 0
        for recording in partition.recordings:
            reasons = _admission_reasons(recording, by_key.get(recording.key), catalog.synthetic)
            if not reasons:
                admitted += 1
                reasons = _constraint_reasons(recording, query)
            if not reasons:
                match = _match(recording, query)
                if query.preferences and not (match.strong or match.weak):
                    reasons.append("no supported prompt preference")
                else:
                    matches.append(match)
                    matched += 1
            if reasons:
                rejected.append(Rejection(recording.key, tuple(reasons)))
        state = (
            CoverageState.EMPTY
            if partition.state == "complete" and not partition.recordings
            else CoverageState(partition.state)
        )
        coverage.append(
            Coverage(source, state, partition.note, len(partition.recordings), admitted, matched)
        )
    matches.sort(
        key=lambda m: (
            -len(m.strong),
            len(m.contradicted),
            -len(m.weak),
            len(m.unknown),
            -(m.recording.duration_seconds or 0),
            m.recording.key,
        )
    )
    return Selection(
        tuple(matches[:limit]),
        tuple(sorted(rejected, key=lambda r: r.key)),
        tuple(coverage),
        catalog.synthetic,
        len(matches),
    )
