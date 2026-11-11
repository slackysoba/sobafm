"""One separately authorized refresh and transient stream check (Linux only)."""

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
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import FrameType
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit

API_HOST = "api.jamendo.com"
STREAM_HOST = "prod-1.storage.jamendo.com"
TRACK_ID = "168"
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
class Report:
    outcome: str = "inconclusive"
    reason: str | None = None
    metadata: Transfer = field(default_factory=Transfer)
    stream: Transfer = field(default_factory=Transfer)
    metadata_validated: bool = False
    application_code: int | None = None


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


def text_field(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise Stopped("credit_fields")
    if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise Stopped("credit_fields")
    return value


def stream_target(value: object, application_id: str) -> str:
    """Validate the fresh audio field, then build only that exact allowed request."""
    if not isinstance(value, str) or not value.isascii() or len(value) > 1024:
        raise Stopped("stream_url")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise Stopped("stream_url")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or parts.netloc != STREAM_HOST
        or parts.path != "/"
        or parts.fragment
        or "#" in value
    ):
        raise Stopped("stream_url")
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
    expected = {"trackid": TRACK_ID, "format": "mp32", "from": f"app-{application_id}"}
    if len(pairs) != 3 or dict(pairs) != expected:
        raise Stopped("stream_url")
    return "/?" + urlencode(expected)


def validate_metadata(data: bytes, application_id: str, report: Report) -> str:
    payload: object = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    root = object_value(payload)
    headers = object_value(root.get("headers"))
    code = headers.get("code")
    if type(code) is not int or not 0 <= code <= 999:
        raise Stopped("invalid_metadata")
    report.application_code = code
    if code != 0:
        raise Stopped("application_rejected")
    if headers.get("status") not in ("success", "succeed") or (
        headers.get("error_message") != "" or headers.get("warnings") != ""
    ):
        raise Stopped("application_status_or_warning")
    rows = root.get("results")
    count = headers.get("results_count")
    if type(count) is not int or count != 1 or not isinstance(rows, list):
        raise Stopped("candidate_missing_or_count")
    entries = cast(list[object], rows)
    if len(entries) != 1:
        raise Stopped("candidate_missing_or_count")
    row = object_value(entries[0])
    if row.get("id") != TRACK_ID:
        raise Stopped("candidate_identity")
    text_field(row.get("name"))
    text_field(row.get("artist_name"))
    if not re.fullmatch(r"[1-9][0-9]{0,19}", text_field(row.get("artist_id"))):
        raise Stopped("credit_fields")
    # Album shape is the documented distinction; type is not a required returned field.
    if row.get("album_id") == "" and row.get("album_name") == "":
        kind = "single"
    elif re.fullmatch(r"[1-9][0-9]{0,19}", text_field(row.get("album_id"))):
        text_field(row.get("album_name"))
        kind = "albumtrack"
    else:
        raise Stopped("candidate_type")
    if "type" in row and row["type"] != kind:
        raise Stopped("candidate_type")
    duration = row.get("duration")
    if isinstance(duration, str) and re.fullmatch(r"[0-9]{1,3}", duration):
        duration = int(duration)
    if type(duration) is not int or not 1 <= duration <= 480:
        raise Stopped("duration_bound")
    license_url = text_field(row.get("license_ccurl"))
    if not re.fullmatch(
        r"https?://creativecommons\.org/licenses/"
        r"(?:by|by-sa|by-nd|by-nc|by-nc-sa|by-nc-nd)/(?:1\.0|2\.0|2\.5|3\.0|4\.0)/",
        license_url,
    ):
        raise Stopped("license_field")
    if row.get("shareurl") not in (
        f"http://www.jamendo.com/track/{TRACK_ID}",
        f"https://www.jamendo.com/track/{TRACK_ID}",
    ):
        raise Stopped("credit_fields")
    if type(row.get("audiodownload_allowed")) is not bool:
        raise Stopped("invalid_metadata")
    # The download flag is not a streaming permission, and no download is made.
    target = stream_target(row.get("audio"), application_id)
    report.metadata_validated = True
    return target


def probe(application_id: str, report: Report) -> None:
    """Called only within the isolated child's one deadline."""
    query = urlencode(
        {
            "client_id": checked_id(application_id),
            "format": "json",
            "id": TRACK_ID,
            "limit": "1",
            "type": "single albumtrack",
            "audioformat": "mp32",
        }
    )
    data = exchange("/v3.0/tracks/?" + query, report.metadata, metadata=True)
    target = validate_metadata(data, application_id, report)
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
