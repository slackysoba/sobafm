"""One separately authorized listing, refresh and stream check (Linux only)."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import re
import resource
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import unicodedata
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

API_HOST = "api.jamendo.com"
STREAM_HOST = "prod-1.storage.jamendo.com"
SEARCH_RESULTS = 10
METADATA_BYTES = 256_000
STREAM_BYTES = 20_000_000
HEADER_BYTES = 16_384
BLOCK_BYTES = 8_192
TOTAL_SECONDS = 120
CLEANUP_SECONDS = 3
WORK_SECONDS = TOTAL_SECONDS - CLEANUP_SECONDS
REPORT_BYTES = 100_000
ID_FILE_BYTES = 160
SUSPENDED_ID = "709fa152"  # pragma: allowlist secret


class Stopped(Exception):
    """Only fixed local reasons, never provider or exception text."""


class Gate(StrEnum):
    ROW = "row"
    IDENTITY = "identity"
    TITLE = "title"
    ARTIST = "artist"
    ALBUM = "album"
    DURATION = "duration"
    LICENSE = "license"
    BACKLINK = "backlink"
    DOWNLOAD_FLAG = "download_flag"
    STREAM_ROUTE = "stream_route"


class Condition(StrEnum):
    MISSING = "missing"
    TYPE = "type"
    SHAPE = "shape"
    BOUND = "bound"


class LicenseComponent(StrEnum):
    TEXT_SHAPE = "text_shape"
    SCHEME = "scheme"
    AUTHORITY = "authority"
    QUERY_OR_FRAGMENT = "query_or_fragment"
    PATH_SHAPE = "path_shape"
    FAMILY = "family"
    VERSION = "version"
    JURISDICTION = "jurisdiction"
    COMBINATION = "combination"


class LicenseTextDetail(StrEnum):
    BLANK = "blank"
    WHITESPACE = "whitespace"
    CONTROL = "control"
    BACKSLASH = "backslash"


type Presence = Literal["present", "absent", "not_evaluated"]
type Match = Literal["match", "mismatch", "not_evaluated"]
type RouteComponent = Literal[
    "text_shape", "url_parser", "query_parser", "scheme", "authority", "path",
    "fragment", "query_keys", "track_id", "format", "application_binding",
]


@dataclass
class RouteDiagnostic:
    """Fixed classifications only; never retain any part of the supplied URL."""

    first_component: RouteComponent | None = None
    non_ascii: Presence = "not_evaluated"
    whitespace: Presence = "not_evaluated"
    control: Presence = "not_evaluated"
    backslash: Presence = "not_evaluated"
    url_parser: Literal["success", "failure", "not_evaluated"] = "not_evaluated"
    scheme: Literal["https", "http", "empty", "other", "not_evaluated"] = "not_evaluated"
    authority: Literal[
        "exact", "userinfo", "port", "userinfo_and_port", "other", "not_evaluated"
    ] = "not_evaluated"
    path: Literal["root", "empty", "other", "not_evaluated"] = "not_evaluated"
    fragment: Presence = "not_evaluated"
    query_parser: Literal[
        "success", "field_limit", "syntax", "not_evaluated"
    ] = "not_evaluated"
    query_keys: Literal["exact", "mismatch", "not_evaluated"] = "not_evaluated"
    missing_keys: Presence = "not_evaluated"
    extra_keys: Presence = "not_evaluated"
    duplicate_keys: Presence = "not_evaluated"
    track_id: Match = "not_evaluated"
    format: Match = "not_evaluated"
    application_binding: Match = "not_evaluated"


class CandidateRefused(Stopped):
    """Typed first gate failure, with no source value or exception text."""

    def __init__(
        self,
        gate: Gate,
        condition: Condition,
        license_component: LicenseComponent | None = None,
        license_text_detail: LicenseTextDetail | None = None,
        route_diagnostic: RouteDiagnostic | None = None,
    ) -> None:
        self.gate = Gate(gate)
        self.condition = Condition(condition)
        self.license_component = (
            LicenseComponent(license_component) if license_component is not None else None
        )
        self.license_text_detail = (
            LicenseTextDetail(license_text_detail) if license_text_detail is not None else None
        )
        self.route_diagnostic = route_diagnostic
        super().__init__(f"candidate_{self.gate}_{self.condition}")


@dataclass(frozen=True)
class Refusal:
    ordinal: int
    gate: Gate
    condition: Condition
    license_component: LicenseComponent | None = None
    license_text_detail: LicenseTextDetail | None = None
    route_diagnostic: RouteDiagnostic | None = None


@dataclass
class Transfer:
    connection_attempts: int = 0
    requests_attempted: int = 0
    response_bytes: int = 0
    body_bytes: int = 0
    http_status: int | None = None
    declared_bytes: int | None = None
    framing: str | None = None
    eof: bool = False
    sha256: str | None = None


@dataclass
class Results:
    application_code: int | None = None
    declared_count_type: str | None = None
    declared_count_value: int | str | None = None
    container_type: str | None = None
    list_count: int | None = None


@dataclass(frozen=True)
class Candidate:
    track_id: str
    target: str
    evidence: str


@dataclass
class Report:
    outcome: str = "inconclusive"
    reason: str | None = None
    license_component: LicenseComponent | None = None
    license_text_detail: LicenseTextDetail | None = None
    route_diagnostic: RouteDiagnostic | None = None
    search: Transfer = field(default_factory=Transfer)
    metadata: Transfer = field(default_factory=Transfer)
    stream: Transfer = field(default_factory=Transfer)
    search_results: Results = field(default_factory=Results)
    refresh_results: Results = field(default_factory=Results)
    selected_track_id: str | None = None
    rejected_candidates: int = 0
    candidate_refusals: list[Refusal] = field(default_factory=list[Refusal])
    metadata_validated: bool = False


@contextmanager
def deadline() -> Generator[None]:
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise Stopped("timer_in_use")

    def expired(_signum: int, _frame: FrameType | None) -> None:
        raise TimeoutError

    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, WORK_SECONDS)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def checked_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", value):
        raise Stopped("application_id_file")
    if value.lower() == SUSPENDED_ID:
        raise Stopped("suspended_test_id")
    return value


def read_id(path: str) -> str:
    # Only run_child calls this, after opt-in and under its deadline.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= ID_FILE_BYTES:
            raise Stopped("application_id_file")
        raw = source.read(ID_FILE_BYTES + 1)
    if len(raw) > ID_FILE_BYTES:
        raise Stopped("application_id_file")
    return checked_id(raw.decode("ascii").strip())


def tls_context() -> ssl.SSLContext:
    # Normal verification/SNI, no SSLKEYLOGFILE or HTTP/1.1 ALPN advertisement.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    return context


class SavedHeaders:
    def __init__(self, data: bytes) -> None:
        self.stream = io.BytesIO(data)

    def makefile(self, mode: str) -> io.BytesIO:
        assert mode == "rb"
        return self.stream


def check_headers(data: bytes, transfer: Transfer, limit: int, media_type: str) -> None:
    """Use the standard parser only on bounded, terminated header bytes.

    Fail closed on its permissive cases. No transfer-coding parser is needed.
    Body length is checked against actual bytes and EOF by receive().
    """
    lines = data.split(b"\r\n")
    if not re.fullmatch(rb"HTTP/1\.[01] [1-5][0-9]{2} [\x20-\x7e]*", lines[0]):
        raise Stopped("invalid_framing")
    transfer.http_status = int(lines[0].split(b" ", 2)[1])
    if transfer.http_status != 200:
        raise Stopped("http_rejected")
    if lines[-2:] != [b"", b""]:
        raise Stopped("invalid_framing")
    for line in lines[1:-2]:
        if not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+:[\x20-\x7e\t]*", line):
            raise Stopped("invalid_framing")
    saved = SavedHeaders(data)
    with http.client.HTTPResponse(cast(socket.socket, saved)) as response:
        response.begin()
        transfer.http_status = response.status
        if response.status != 200:
            raise Stopped("http_rejected")
        if response.headers.defects:
            raise Stopped("invalid_framing")
        if response.headers.get_all("Transfer-Encoding") is not None:
            raise Stopped("unsupported_transfer_encoding")
        if response.headers.get_all("Content-Range") is not None:
            raise Stopped("partial_response")
        for name in ("Content-Type", "Content-Encoding", "Content-Length"):
            values = response.headers.get_all(name)
            if values is not None and len(values) != 1:
                raise Stopped("invalid_framing")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise Stopped("encoded_response")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != media_type:
            raise Stopped("content_type")
        length = response.getheader("Content-Length")
        if length is not None:
            if not re.fullmatch(r"[0-9]{1,9}", length):
                raise Stopped("invalid_framing")
            declared = int(length)
            # HTTP header bytes share the response budget; exact cap is inconclusive.
            if declared + len(data) >= limit:
                raise Stopped("response_limit")
            transfer.declared_bytes = declared
            transfer.framing = "content_length"
        else:
            transfer.framing = "connection_eof"


def receive(secure: ssl.SSLSocket, transfer: Transfer, *, metadata: bool) -> bytes:
    """Bound raw HTTP reads; retain JSON only, hash and discard stream blocks."""
    limit = METADATA_BYTES if metadata else STREAM_BYTES
    headers = bytearray()
    body = bytearray()
    parsed = False
    digest = hashlib.sha256()
    try:
        while transfer.response_bytes < limit:
            remaining = limit - transfer.response_bytes
            if not parsed:
                remaining = min(remaining, HEADER_BYTES - len(headers))
                if remaining == 0:
                    raise Stopped("header_limit")
            block = secure.recv(min(BLOCK_BYTES, remaining))
            if not block:
                transfer.eof = True
                if not parsed:
                    raise Stopped("invalid_framing")
                if transfer.declared_bytes is not None and (
                    transfer.body_bytes != transfer.declared_bytes
                ):
                    raise Stopped("invalid_framing")
                if transfer.body_bytes == 0:
                    raise Stopped("empty_body")
                return bytes(body)
            transfer.response_bytes += len(block)
            if not parsed:
                headers.extend(block)
                end = headers.find(b"\r\n\r\n")
                if end < 0:
                    continue
                end += 4
                check_headers(
                    bytes(headers[:end]),
                    transfer,
                    limit,
                    "application/json" if metadata else "audio/mpeg",
                )
                block = bytes(headers[end:])
                headers.clear()
                parsed = True
            transfer.body_bytes += len(block)
            if not metadata:
                digest.update(block)
            if transfer.declared_bytes is not None and (
                transfer.body_bytes > transfer.declared_bytes
            ):
                raise Stopped("invalid_framing")
            if metadata:
                body.extend(block)
        # Do not read a sentinel byte beyond either budget.
        raise Stopped("response_limit")
    finally:
        if not metadata and parsed:
            transfer.sha256 = digest.hexdigest()


def exchange(target: str, transfer: Transfer, *, metadata: bool) -> bytes:
    """Two fixed destinations; one address/connection/GET per call, no retries."""
    host = API_HOST if metadata else STREAM_HOST
    request = (
        f"GET {target} HTTP/1.0\r\nHost: {host}\r\n"
        "User-Agent: sobafm-jamendo-stream/1\r\n"
        f"Accept: {'application/json' if metadata else 'audio/mpeg'}\r\n"
        "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
    ).encode("ascii")
    context = tls_context()
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise Stopped("connection")
    family, kind, protocol, _name, address = addresses[0]
    transfer.connection_attempts = 1
    with socket.socket(family, kind, protocol) as connection:
        connection.settimeout(WORK_SECONDS)
        connection.connect(address)
        with context.wrap_socket(
            connection, server_hostname=host, suppress_ragged_eofs=False
        ) as secure:
            transfer.requests_attempted = 1
            secure.sendall(request)
            return receive(secure, transfer, metadata=metadata)


def object_value(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise Stopped("invalid_metadata")
    return cast(dict[str, object], value)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stopped("invalid_metadata")
        result[key] = value
    return result


def required_field(row: dict[str, object], key: str, gate: Gate) -> object:
    if key not in row:
        raise CandidateRefused(gate, Condition.MISSING)
    return row[key]


def text_field(value: object, gate: Gate) -> str:
    if not isinstance(value, str):
        raise CandidateRefused(gate, Condition.TYPE)
    if len(value) > 512:
        raise CandidateRefused(gate, Condition.BOUND)
    if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CandidateRefused(gate, Condition.SHAPE)
    return value


def numeric_id(value: object, gate: Gate) -> str:
    value = text_field(value, gate)
    if len(value) > 20:
        raise CandidateRefused(gate, Condition.BOUND)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise CandidateRefused(gate, Condition.SHAPE)
    return value


def check_license(value: object) -> None:
    """Recognize only the production boundary's finite CC formats, not rights."""
    if not isinstance(value, str):
        raise CandidateRefused(Gate.LICENSE, Condition.TYPE)
    if len(value) > 512:
        raise CandidateRefused(Gate.LICENSE, Condition.BOUND)
    # Preserve the text gate's rejection set; report only the first fixed detail.
    detail = None
    if not value.strip():
        detail = LicenseTextDetail.BLANK
    elif any(c.isspace() for c in value):
        detail = LicenseTextDetail.WHITESPACE
    elif any(unicodedata.category(c).startswith("C") for c in value):
        detail = LicenseTextDetail.CONTROL
    elif "\\" in value:
        detail = LicenseTextDetail.BACKSLASH
    if detail is not None:
        raise CandidateRefused(Gate.LICENSE, Condition.SHAPE, LicenseComponent.TEXT_SHAPE, detail)
    # Match production's lowercase public-HTTP upgrade and parsed HTTPS scheme.
    # Keep authority/path case, escaping, ports and historical family exact.
    scheme, separator, _ = value.partition(":")
    if not separator or (scheme != "http" and scheme.lower() != "https"):
        raise CandidateRefused(Gate.LICENSE, Condition.SHAPE, LicenseComponent.SCHEME)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise CandidateRefused(Gate.LICENSE, Condition.SHAPE, LicenseComponent.AUTHORITY) from None
    if parts.netloc != "creativecommons.org":
        raise CandidateRefused(Gate.LICENSE, Condition.SHAPE, LicenseComponent.AUTHORITY)
    if "?" in value or "#" in value:
        raise CandidateRefused(Gate.LICENSE, Condition.SHAPE, LicenseComponent.QUERY_OR_FRAGMENT)
    path = parts.path.removesuffix("/").split("/")
    if len(path) not in (4, 5) or path[:2] != ["", "licenses"] or not all(path[2:]):
        raise CandidateRefused(Gate.LICENSE, Condition.SHAPE, LicenseComponent.PATH_SHAPE)
    family, version = path[2:4]
    jurisdiction = path[4] if len(path) == 5 else None
    if family not in {"by", "by-sa", "by-nd", "by-nc", "by-nc-sa", "by-nc-nd", "by-nd-nc"}:
        raise CandidateRefused(Gate.LICENSE, Condition.SHAPE, LicenseComponent.FAMILY)
    if version not in {"1.0", "2.0", "2.5", "3.0", "4.0"}:
        raise CandidateRefused(Gate.LICENSE, Condition.SHAPE, LicenseComponent.VERSION)
    if jurisdiction is not None and jurisdiction not in {"us", "fr"}:
        raise CandidateRefused(Gate.LICENSE, Condition.SHAPE, LicenseComponent.JURISDICTION)
    if (
        (family == "by-nd-nc" and version != "1.0")
        or (family == "by-nc-nd" and version == "1.0")
        or (
            jurisdiction is not None
            and (version, jurisdiction) not in {("3.0", "us"), ("2.0", "fr")}
        )
    ):
        raise CandidateRefused(Gate.LICENSE, Condition.SHAPE, LicenseComponent.COMBINATION)


def album_shape(row: dict[str, object]) -> None:
    # Missing and empty optional fields both describe a single. Null or partial
    # album evidence is not an omission, and an explicit type must still agree.
    album_id = row.get("album_id", "")
    album_name = row.get("album_name", "")
    if not isinstance(album_id, str) or not isinstance(album_name, str):
        raise CandidateRefused(Gate.ALBUM, Condition.TYPE)
    if album_id == "" and album_name == "":
        kind = "single"
    else:
        numeric_id(required_field(row, "album_id", Gate.ALBUM), Gate.ALBUM)
        text_field(required_field(row, "album_name", Gate.ALBUM), Gate.ALBUM)
        kind = "albumtrack"
    if "type" in row:
        if not isinstance(row["type"], str):
            raise CandidateRefused(Gate.ALBUM, Condition.TYPE)
        if row["type"] != kind:
            raise CandidateRefused(Gate.ALBUM, Condition.SHAPE)


def stream_target(value: object, application_id: str, track_id: str) -> str:
    try:
        return checked_stream_target(value, application_id, track_id)
    except CandidateRefused as error:
        # Missing/type/bound failures and earlier candidate gates have no URL
        # diagnostics. Classification never participates in acceptance or repair.
        if error.condition == Condition.SHAPE and isinstance(value, str):
            error.route_diagnostic = diagnose_route(value, application_id, track_id)
        raise


def diagnose_route(value: str, application_id: str, track_id: str) -> RouteDiagnostic:
    """Classify a bounded refused string, including independent mixed failures.

    Parsing here describes the standard parser's interpretation, even when the
    original text gate rejects characters that parser would strip. No parsed
    value survives in this record or replaces the original validation input.
    """
    assert len(value) <= 1024
    diagnostic = RouteDiagnostic()

    def presence(found: bool) -> Presence:
        return "present" if found else "absent"

    def first(component: RouteComponent, failed: bool) -> None:
        if failed and diagnostic.first_component is None:
            diagnostic.first_component = component

    diagnostic.non_ascii = presence(not value.isascii())
    diagnostic.whitespace = presence(any(char.isspace() for char in value))
    diagnostic.control = presence(any(ord(char) < 32 or ord(char) == 127 for char in value))
    diagnostic.backslash = presence("\\" in value)
    diagnostic.fragment = presence("#" in value)
    first("text_shape", not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value))
    try:
        parts = urlsplit(value)
    except ValueError:
        diagnostic.url_parser = "failure"
        first("url_parser", True)
        return diagnostic
    diagnostic.url_parser = "success"
    diagnostic.scheme = (
        "https" if parts.scheme == "https" else "http" if parts.scheme == "http"
        else "empty" if not parts.scheme else "other"
    )
    authority = parts.netloc
    host_port = authority.rsplit("@", 1)[-1]
    userinfo = "@" in authority
    # A bracketed IPv6 address's internal colons are not a port delimiter.
    port = ":" in (host_port.partition("]")[2] if host_port.startswith("[") else host_port)
    diagnostic.authority = (
        "exact" if authority == STREAM_HOST else "userinfo_and_port" if userinfo and port
        else "userinfo" if userinfo else "port" if port else "other"
    )
    diagnostic.path = "root" if parts.path == "/" else "empty" if not parts.path else "other"
    # Match parse_qsl's field-limit-before-syntax precedence without inspecting
    # its exception text or relaxing its three-field bound for diagnostics.
    pairs: list[tuple[str, str]] | None = None
    if parts.query and 1 + parts.query.count("&") > 3:
        diagnostic.query_parser = "field_limit"
    else:
        try:
            pairs = parse_qsl(
                parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3
            )
        except ValueError:
            diagnostic.query_parser = "syntax"
        else:
            diagnostic.query_parser = "success"
    first("query_parser", pairs is None)
    first("scheme", parts.scheme != "https")
    first("authority", authority != STREAM_HOST)
    first("path", parts.path != "/")
    first("fragment", bool(parts.fragment) or "#" in value)
    if pairs is None:
        return diagnostic
    expected = {"trackid": track_id, "format": "mp32", "from": f"app-{application_id}"}
    keys = {key for key, _ in pairs}
    missing = bool(expected.keys() - keys)
    extra = bool(keys - expected.keys())
    duplicate = len(keys) != len(pairs)
    diagnostic.missing_keys = presence(missing)
    diagnostic.extra_keys = presence(extra)
    diagnostic.duplicate_keys = presence(duplicate)
    diagnostic.query_keys = "mismatch" if missing or extra or duplicate else "exact"

    def match(key: str) -> Match:
        values = [item for name, item in pairs if name == key]
        if len(values) != 1:
            return "not_evaluated"
        return "match" if values[0] == expected[key] else "mismatch"

    diagnostic.track_id = match("trackid")
    diagnostic.format = match("format")
    diagnostic.application_binding = match("from")
    first("query_keys", diagnostic.query_keys != "exact")
    first("track_id", diagnostic.track_id == "mismatch")
    first("format", diagnostic.format == "mismatch")
    first("application_binding", diagnostic.application_binding == "mismatch")
    return diagnostic


def checked_stream_target(value: object, application_id: str, track_id: str) -> str:
    """Validate the fresh audio field, then build only that exact allowed request."""
    if not isinstance(value, str):
        raise CandidateRefused(Gate.STREAM_ROUTE, Condition.TYPE)
    if len(value) > 1024:
        raise CandidateRefused(Gate.STREAM_ROUTE, Condition.BOUND)
    if not value.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise CandidateRefused(Gate.STREAM_ROUTE, Condition.SHAPE)
    try:
        parts = urlsplit(value)
        pairs = parse_qsl(
            parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3
        )
    except ValueError:
        raise CandidateRefused(Gate.STREAM_ROUTE, Condition.SHAPE) from None
    if (
        parts.scheme != "https"
        or parts.netloc != STREAM_HOST
        or parts.path != "/"
        or parts.fragment
        or "#" in value
    ):
        raise CandidateRefused(Gate.STREAM_ROUTE, Condition.SHAPE)
    expected = {"trackid": track_id, "format": "mp32", "from": f"app-{application_id}"}
    if len(pairs) != 3 or dict(pairs) != expected:
        raise CandidateRefused(Gate.STREAM_ROUTE, Condition.SHAPE)
    return "/?" + urlencode(expected)


def json_type(mapping: dict[str, object], key: str) -> str:
    """Fixed JSON type labels only; never echo an untrusted value or type name."""
    if key not in mapping:
        return "missing"
    value = mapping[key]
    labels: dict[type[object], str] = {
        type(None): "null",
        bool: "boolean",
        int: "integer",
        float: "number",
        str: "string",
        list: "list",
        dict: "object",
    }
    return labels.get(type(value), "unknown")


def reject_constant(_value: str) -> object:
    raise Stopped("invalid_metadata")


def metadata_rows(data: bytes, diagnostics: Results, limit: int) -> list[object]:
    payload: object = json.loads(
        data.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant
    )
    root = object_value(payload)
    headers = object_value(root.get("headers"))
    code = headers.get("code")
    if type(code) is not int or not 0 <= code <= 999:
        raise Stopped("invalid_metadata")
    diagnostics.application_code = code
    if code != 0:
        raise Stopped("application_rejected")
    if headers.get("status") not in ("success", "succeed") or (
        headers.get("error_message") != "" or headers.get("warnings") != ""
    ):
        raise Stopped("application_status_or_warning")
    rows = root.get("results")
    count = headers.get("results_count")
    diagnostics.declared_count_type = json_type(headers, "results_count")
    diagnostics.container_type = json_type(root, "results")
    if isinstance(rows, list):
        diagnostics.list_count = len(cast(list[object], rows))
    if type(count) is not int:
        diagnostics.declared_count_value = "not_integer"
        raise Stopped("declared_count_type")
    # Only small counts can enter a report. Large or negative numbers receive
    # fixed labels, not a covert channel for arbitrary response text/numbers.
    diagnostics.declared_count_value = (
        "negative" if count < 0 else "above_limit" if count > limit else count
    )
    if not 0 <= count <= limit:
        raise Stopped("declared_count_value")
    if not isinstance(rows, list):
        raise Stopped("results_container")
    entries = cast(list[object], rows)
    if len(entries) > limit:
        raise Stopped("results_limit")
    if len(entries) != count:
        raise Stopped("results_count_mismatch")
    return entries


def validate_candidate(value: object, application_id: str) -> Candidate:
    if not isinstance(value, dict):
        raise CandidateRefused(Gate.ROW, Condition.TYPE)
    row = cast(dict[str, object], value)
    track_id = numeric_id(required_field(row, "id", Gate.IDENTITY), Gate.IDENTITY)
    text_field(required_field(row, "name", Gate.TITLE), Gate.TITLE)
    text_field(required_field(row, "artist_name", Gate.ARTIST), Gate.ARTIST)
    numeric_id(required_field(row, "artist_id", Gate.ARTIST), Gate.ARTIST)
    album_shape(row)
    duration = required_field(row, "duration", Gate.DURATION)
    if not isinstance(duration, (int, str)) or isinstance(duration, bool):
        raise CandidateRefused(Gate.DURATION, Condition.TYPE)
    if isinstance(duration, str) and re.fullmatch(r"[0-9]{1,3}", duration):
        duration = int(duration)
    if not isinstance(duration, int):
        raise CandidateRefused(Gate.DURATION, Condition.SHAPE)
    if not 1 <= duration <= 480:
        raise CandidateRefused(Gate.DURATION, Condition.BOUND)
    check_license(required_field(row, "license_ccurl", Gate.LICENSE))
    backlink = text_field(required_field(row, "shareurl", Gate.BACKLINK), Gate.BACKLINK)
    if backlink not in (
        f"http://www.jamendo.com/track/{track_id}",
        f"https://www.jamendo.com/track/{track_id}",
    ):
        raise CandidateRefused(Gate.BACKLINK, Condition.SHAPE)
    if type(required_field(row, "audiodownload_allowed", Gate.DOWNLOAD_FLAG)) is not bool:
        raise CandidateRefused(Gate.DOWNLOAD_FLAG, Condition.TYPE)
    # The download flag is not a streaming permission, and no download is made.
    target = stream_target(
        required_field(row, "audio", Gate.STREAM_ROUTE), application_id, track_id
    )
    # Conservatively bind every returned field, including optional details and
    # notices. Normalize only the checked stream query and duration. This is
    # transient consistency evidence, never an admission/rights certificate.
    evidence = hashlib.sha256(
        json.dumps(
            {**row, "audio": target, "duration": duration},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    return Candidate(track_id, target, evidence)


def select_candidate(data: bytes, application_id: str, report: Report) -> Candidate:
    entries = metadata_rows(data, report.search_results, SEARCH_RESULTS)
    for ordinal, value in enumerate(entries, start=1):
        try:
            candidate = validate_candidate(value, application_id)
        except Stopped as error:
            report.rejected_candidates += 1
            if len(report.candidate_refusals) < SEARCH_RESULTS:
                # Never parse exception text, including an unexpected Stopped.
                gate, condition = Gate.ROW, Condition.SHAPE
                component = None
                detail = None
                route = None
                if isinstance(error, CandidateRefused):
                    gate, condition = error.gate, error.condition
                    component = error.license_component
                    detail = error.license_text_detail
                    route = error.route_diagnostic
                report.candidate_refusals.append(
                    Refusal(ordinal, gate, condition, component, detail, route)
                )
            continue
        report.selected_track_id = candidate.track_id
        return candidate
    raise Stopped("no_acceptable_candidate")


def validate_refresh(data: bytes, application_id: str, selected: Candidate, report: Report) -> str:
    entries = metadata_rows(data, report.refresh_results, 1)
    if not entries:
        raise Stopped("refresh_empty")
    row = object_value(entries[0])
    if row.get("id") != selected.track_id:
        raise Stopped("candidate_identity")
    refreshed = validate_candidate(row, application_id)
    if refreshed.evidence != selected.evidence:
        raise Stopped("refresh_evidence_changed")
    report.metadata_validated = True
    return refreshed.target


def probe(application_id: str, report: Report) -> None:
    """Called only within the isolated child's one deadline."""
    common = {
        "client_id": checked_id(application_id),
        "format": "json",
        "type": "single albumtrack",
        "audioformat": "mp32",
    }
    query = urlencode(
        {
            **common,
            "limit": str(SEARCH_RESULTS),
            "order": "id_desc",
            "ccnc": "true",
            "durationbetween": "1_480",
        }
    )
    data = exchange("/v3.0/tracks/?" + query, report.search, metadata=True)
    selected = select_candidate(data, application_id, report)
    del data
    query = urlencode({**common, "id": selected.track_id, "limit": "1"})
    data = exchange("/v3.0/tracks/?" + query, report.metadata, metadata=True)
    target = validate_refresh(data, application_id, selected, report)
    del data
    exchange(target, report.stream, metadata=False)
    report.outcome = "stream_received"


def run_child(application_id_path: str) -> int:
    report = Report()
    application_id = ""
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        with deadline():
            application_id = read_id(application_id_path)
            probe(application_id, report)
    except Stopped as error:
        report.reason = str(error)
        if isinstance(error, CandidateRefused):
            report.license_component = error.license_component
            report.license_text_detail = error.license_text_detail
            report.route_diagnostic = error.route_diagnostic
    except TimeoutError:
        report.reason = "timeout"
    except KeyboardInterrupt:
        report.reason = "interrupted"
    except ValueError, RecursionError, http.client.HTTPException:
        report.reason = "invalid_response"
    except OSError:
        report.reason = "file_or_connection"
    except Exception:
        report.reason = "internal"
    if report.reason is not None:
        report.outcome = "inconclusive"
    encoded = json.dumps(asdict(report), separators=(",", ":"), ensure_ascii=True)
    # Even accidental numeric/digest echoes never expose the ID.
    if application_id and application_id.lower() in encoded.lower():
        encoded = '{"outcome":"inconclusive","reason":"report_redacted"}'
        report.outcome = "inconclusive"
    if len(encoded) + 1 > REPORT_BYTES:
        encoded = '{"outcome":"inconclusive","reason":"report_limit"}'
        report.outcome = "inconclusive"
    print(encoded)
    return 0 if report.outcome == "stream_received" else 1


def settle(child: subprocess.Popen[bytes]) -> bool:
    """One original cleanup deadline; retain the child until observed reaping.

    A missed deadline remains failed even after late release. Each wait is bounded;
    no replacement request or abandonment on repeated interruption is possible.
    """
    end = time.monotonic() + CLEANUP_SECONDS
    expired = False
    while True:
        try:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=1.0 if expired else max(0.001, min(1.0, end - time.monotonic())))
            return not expired and time.monotonic() <= end
        except subprocess.TimeoutExpired, KeyboardInterrupt:
            expired = expired or time.monotonic() >= end
        except ProcessLookupError:
            continue


def supervise(application_id_path: str) -> int:
    started = time.monotonic()
    child: subprocess.Popen[bytes] | None = None
    output = b""
    reason: str | None = None
    cleanup_met = True
    try:
        # Defer cancellation only through handle assignment; an interrupt during
        # Popen must not lose the newly created child before we can reap it.
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "import runpy,sys; m=runpy.run_path(sys.argv[1]); "
                    "sys.exit(m['run_child'](sys.argv[2]))",
                    str(Path(__file__).resolve()),
                    application_id_path,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"LANG": "C.UTF-8"},
            )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        output, _ = child.communicate(
            timeout=max(0.001, WORK_SECONDS - (time.monotonic() - started))
        )
    except subprocess.TimeoutExpired:
        reason = "timeout"
    except KeyboardInterrupt:
        reason = "interrupted"
    except Exception:
        reason = "internal"
    finally:
        if child is not None:
            cleanup_met = settle(child)
            if child.stdout is not None:
                child.stdout.close()
    if reason is not None:
        print(
            json.dumps(
                {
                    "outcome": "inconclusive",
                    "reason": reason,
                    "child_reaped": child is not None,
                    "cleanup_met": cleanup_met,
                }
            )
        )
        return 1
    if not output or len(output) > REPORT_BYTES:
        print('{"outcome":"inconclusive","reason":"child_report"}')
        return 1
    # Only our bounded child writer reaches this pipe. Preserve its original
    # verdict alongside observed cleanup, including a failed late release.
    try:
        result: object = json.loads(output)
        report = object_value(result)
    except ValueError, Stopped:
        print('{"outcome":"inconclusive","reason":"child_report"}')
        return 1
    report["child_reaped"] = True
    report["cleanup_met"] = cleanup_met
    if not cleanup_met:
        report["outcome"] = "inconclusive"
        if report.get("reason") is None:
            report["reason"] = "cleanup_expired"
    encoded = json.dumps(report, separators=(",", ":"), ensure_ascii=True)
    if len(encoded) + 1 > REPORT_BYTES:
        print('{"outcome":"inconclusive","reason":"report_limit"}')
        return 1
    print(encoded)
    return 0 if cleanup_met and child is not None and child.returncode == 0 else 1


def run_authorized(application_id_path: str) -> int:
    def interrupted(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return supervise(application_id_path)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print('{"outcome":"disabled","reason":"explicit_opt_in_required"}')
        return 0
    if len(args) != 2 or args[0] != "--send-to-jamendo" or not args[1]:
        print('{"outcome":"inconclusive","reason":"arguments"}')
        return 2
    return run_authorized(args[1])


if __name__ == "__main__":
    sys.exit(main())
