"""Bounded pure request/response boundary for Jamendo; no network or persistence.

The caller owns transport, cancellation, byte-limited receipt and quota accounting.
A SearchWindow permits only one outstanding request and two sequential pages.
Refresh always consumes a new by-ID response; no search URL reaches a candidate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import cast

from sobafm.catalog import MAX_RECORDS_PER_SOURCE, RecordingKey, Source
from sobafm.jamendo_admission import (
    AdmissionInputError,
    Candidate,
    Policy,
    Purpose,
    Reason,
    checked_time,
    decision_matches,
    evaluate,
    object_fields,
    parse_listing,
    text,
    track_id,
)

MAX_RESPONSE_BYTES = 1_048_576
PAGE_SIZE = 100
MAX_PAGES = 2
# The accepted 768-cycle lifetime can advance at most 200 source positions/cycle.
MAX_OFFSET = 768 * MAX_PAGES * PAGE_SIZE
TRACKS_ENDPOINT = "https://api.jamendo.com/v3.0/tracks/"
DISABLED_POLICY = Policy()
_COMMON = (
    ("format", "json"),
    ("type", "single albumtrack"),
    ("audioformat", "mp32"),
    ("include", "musicinfo licenses"),
)


class Failure(StrEnum):
    DISABLED = "disabled"
    REQUEST = "invalid_request"
    SEQUENCE = "page_sequence"
    LIMIT = "window_limit"
    OVERSIZED = "response_too_large"
    MALFORMED = "malformed_response"
    PROVIDER = "provider_failure"
    WARNING = "provider_warning"
    REMOVED = "recording_missing"
    IDENTITY = "identity_mismatch"
    CHANGED = "ranked_content_changed"
    CONFLICT = "conflicting_duplicate"
    TRANSPORT = "transport_failure"


@dataclass(frozen=True)
class Refusal:
    reason: Failure | Reason


@dataclass(frozen=True)
class Query:
    """Musical fields only. Semantic intent validation belongs to catalog_search.

    Construct after that validation, never directly from a listener's raw text.
    The request builder additionally checks the structural bounds and URL exclusion.
    """

    search: str = ""
    artist_name: str = ""
    name: str = ""
    tags: tuple[str, ...] = ()

    def parameters(self) -> tuple[tuple[str, str], ...]:
        params: list[tuple[str, str]] = []
        for key, value in (
            ("search", self.search),
            ("artist_name", self.artist_name),
            ("name", self.name),
        ):
            if value != "":
                params.append((key, text(value)))
        if type(self.tags) is not tuple or len(self.tags) > 8:
            raise AdmissionInputError(Reason.INVALID)
        if self.tags:
            tags = tuple(text(tag, 64) for tag in self.tags)
            if any("+" in tag for tag in tags):
                raise AdmissionInputError(Reason.INVALID)
            params.append(("tags", "+".join(tags)))
        if not params:
            raise AdmissionInputError(Reason.INVALID)
        return tuple(params)


@dataclass(frozen=True)
class Request:
    """Credential-free request specification; transport injects its own client ID."""

    parameters: tuple[tuple[str, str], ...]
    key: RecordingKey | None = None
    endpoint: str = field(default=TRACKS_ENDPOINT, init=False)
    maximum_bytes: int = field(default=MAX_RESPONSE_BYTES, init=False)


class Coverage(StrEnum):
    DISABLED = "disabled"
    PARTIAL = "partial"
    EMPTY = "empty_window"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SearchResult:
    candidates: tuple[Candidate, ...]
    coverage: Coverage
    refusals: tuple[Refusal, ...]
    pages_issued: int
    response_bytes: int
    next_offset: int


@dataclass(frozen=True)
class Refreshed:
    """A fresh metadata decision, NOT streaming integrity/readiness approval.

    Only candidate is suitable for future bounded provenance persistence. Consume
    stream_url for this immediate open only; never log/serialize/cache this wrapper.
    Every later open (also a cache hit) must request and evaluate another refresh.
    """

    candidate: Candidate
    previous_decision_matches: bool
    stream_url: str = field(repr=False, compare=False)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate JSON key")
        data[key] = value
    return data


def _constant(value: str) -> object:
    raise ValueError("nonfinite JSON number")


def _bounded(value: object, depth: int = 0) -> None:
    if depth > 8:
        raise ValueError("nesting limit")
    if isinstance(value, dict):
        data = cast(dict[str, object], value)
        if len(data) > 64 or any(len(key) > 64 for key in data):
            raise ValueError("object limit")
        for item in data.values():
            _bounded(item, depth + 1)
    elif isinstance(value, list):
        items = cast(list[object], value)
        if len(items) > PAGE_SIZE:
            raise ValueError("list limit")
        for item in items:
            _bounded(item, depth + 1)
    elif isinstance(value, str) and len(value) > 32_768:
        raise ValueError("string limit")


def _rows(payload: bytes, maximum: int) -> tuple[object, ...] | Refusal:
    if len(payload) > MAX_RESPONSE_BYTES:
        return Refusal(Failure.OVERSIZED)
    try:
        raw: object = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
        )
        _bounded(raw)
        data = object_fields(raw, {"headers", "results"}, {"headers", "results"})
        fields = {"status", "code", "error_message", "warnings", "results_count"}
        headers = object_fields(data["headers"], fields | {"results_fullcount"}, fields)
        if (
            type(headers["code"]) is not int
            or headers["status"] != "success"
            or headers["code"] != 0
        ):
            return Refusal(Failure.PROVIDER)
        if headers["warnings"] != "" or headers["error_message"] != "":
            return Refusal(Failure.WARNING)
        results = data["results"]
        if not isinstance(results, list):
            return Refusal(Failure.MALFORMED)
        rows = cast(list[object], results)
        if (
            len(rows) > maximum
            or type(headers["results_count"]) is not int
            or headers["results_count"] != len(rows)
        ):
            return Refusal(Failure.MALFORMED)
        if "results_fullcount" in headers and (
            type(headers["results_fullcount"]) is not int
            or headers["results_fullcount"] < len(rows)
        ):
            return Refusal(Failure.MALFORMED)
        return tuple(rows)
    except ValueError, RecursionError, UnicodeError:
        return Refusal(Failure.MALFORMED)


class SearchWindow:
    """One selection attempt, owned serially by the future discovery pipeline."""

    def __init__(self, query: Query, policy: Policy = DISABLED_POLICY, *, offset: int = 0) -> None:
        self._policy = policy
        self._offset = offset
        self._pages = 0
        self._bytes = 0
        self._pending: Request | None = None
        self._candidates: dict[RecordingKey, Candidate] = {}
        self._seen: dict[RecordingKey, tuple[str, str]] = {}
        self._conflicts: set[RecordingKey] = set()
        self._refusals: list[Refusal] = []
        self._ended = False
        try:
            self._query = query.parameters()
            if type(offset) is not int or not 0 <= offset < MAX_OFFSET or offset % PAGE_SIZE:
                raise AdmissionInputError(Reason.INVALID)
        except AdmissionInputError:
            self._query = ()
            self._refusals.append(Refusal(Failure.REQUEST))
            self._ended = True

    def request(self) -> Request | Refusal:
        if not self._policy.enabled:
            return Refusal(Failure.DISABLED)
        if self._pending is not None:
            return Refusal(Failure.SEQUENCE)
        if self._ended or self._pages >= MAX_PAGES or self._offset >= MAX_OFFSET:
            return (
                self._refusals[-1] if self._refusals and not self._pages else Refusal(Failure.LIMIT)
            )
        self._pending = Request(
            _COMMON + self._query + (("limit", str(PAGE_SIZE)), ("offset", str(self._offset)))
        )
        self._pages += 1
        return self._pending

    def receive(
        self, request: Request, payload: bytes | Refusal, observed_at: datetime
    ) -> SearchResult:
        """Supply only the exact outstanding response; transport errors stop this window."""
        checked_time(observed_at)
        if request is not self._pending or self._pending is None:
            raise ValueError(Failure.SEQUENCE.value)
        self._pending = None
        if isinstance(payload, Refusal):
            rows = payload
        else:
            self._bytes += len(payload)
            rows = _rows(payload, PAGE_SIZE)
        if isinstance(rows, Refusal):
            self._refusals.append(rows)
            self._ended = True
            return self.result()
        self._offset += PAGE_SIZE
        self._ended = len(rows) < PAGE_SIZE
        for row in rows:
            try:
                listing = parse_listing(row)
                decision = evaluate(listing, self._policy, observed_at, Purpose.RANKING)
            except AdmissionInputError as exc:
                self._refusals.append(Refusal(exc.reason))
                # Even a malformed duplicate must revoke its earlier good occurrence.
                if isinstance(row, dict):
                    try:
                        identity = track_id(cast(dict[str, object], row).get("id"))
                        self._conflicts.add(RecordingKey(Source.JAMENDO, identity))
                    except AdmissionInputError:
                        pass
                continue
            key = decision.key
            binding = (decision.metadata_digest, decision.evidence_digest)
            previous = self._seen.get(key)
            if previous is not None and previous != binding:
                self._conflicts.add(key)
                self._refusals.append(Refusal(Failure.CONFLICT))
            self._seen[key] = binding
            if decision.admitted:
                self._candidates[key] = Candidate(
                    listing.recording, decision, listing.content_digest
                )
            else:
                self._conflicts.add(key)
                self._refusals.append(Refusal(decision.reason))
        for key in self._conflicts:
            self._candidates.pop(key, None)
        if len(self._seen) > MAX_RECORDS_PER_SOURCE:
            self._candidates.clear()
            self._ended = True
            self._refusals.append(Refusal(Failure.LIMIT))
        return self.result()

    def result(self) -> SearchResult:
        if not self._policy.enabled:
            coverage = Coverage.DISABLED
        elif self._candidates:
            coverage = Coverage.PARTIAL
        elif self._refusals:
            coverage = Coverage.UNAVAILABLE
        else:
            coverage = Coverage.EMPTY
        return SearchResult(
            tuple(self._candidates.values()),
            coverage,
            tuple(self._refusals),
            self._pages,
            self._bytes,
            self._offset,
        )


def refresh_request(key: RecordingKey, policy: Policy = DISABLED_POLICY) -> Request | Refusal:
    if not policy.enabled:
        return Refusal(Failure.DISABLED)
    try:
        if key.source != Source.JAMENDO:
            return Refusal(Failure.IDENTITY)
        identity = track_id(key.recording_id)
    except AdmissionInputError:
        return Refusal(Failure.IDENTITY)
    return Request(_COMMON + (("id", identity), ("limit", "1")), key)


def refresh(
    request: Request,
    payload: bytes | Refusal,
    ranked: Candidate,
    policy: Policy,
    observed_at: datetime,
) -> Refreshed | Refusal:
    """Revalidate a just-received by-ID response, including before every cache hit.

    This function stores no prior approval and cannot fall back to ranked metadata.
    The caller must drop old prepared input on refusal and own a new request for
    each audio open. Leading/later embedded notice reconciliation belongs to stream.
    """
    checked_time(observed_at)
    expected = refresh_request(ranked.recording.key, policy)
    if isinstance(expected, Refusal):
        return expected
    if request != expected:
        return Refusal(Failure.IDENTITY)
    if isinstance(payload, Refusal):
        return payload
    rows = _rows(payload, 1)
    if isinstance(rows, Refusal):
        return rows
    if not rows:
        return Refusal(Failure.REMOVED)
    try:
        listing = parse_listing(rows[0], ranked.recording.key.recording_id)
        decision = evaluate(listing, policy, observed_at, Purpose.REFRESH)
    except AdmissionInputError as exc:
        return Refusal(exc.reason)
    if not decision.admitted:
        return Refusal(decision.reason)
    if listing.content_digest != ranked.content_digest:
        return Refusal(Failure.CHANGED)
    return Refreshed(
        Candidate(listing.recording, decision, listing.content_digest),
        decision_matches(ranked.decision, listing, policy),
        listing.stream_url,
    )
