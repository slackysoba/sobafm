"""One separately authorized Jamendo metadata request; inert by default (Linux only)."""

from __future__ import annotations

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
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import FrameType
from typing import cast
from urllib.parse import urlencode

HOST = "api.jamendo.com"
MAX_RESPONSE_BYTES = 256_000
REQUEST_SECONDS = 10
MAX_RESULTS = 10
MAX_REPORT_BYTES = 100_000
MAX_ID_FILE_BYTES = 160
# The published shared test ID is suspended. It must never be sent again.
SUSPENDED_TEST_ID = "709fa152"  # pragma: allowlist secret
FIELDS = (
    "name",
    "artist_id",
    "artist_name",
    "album_id",
    "album_name",
    "duration",
    "license_ccurl",
    "shareurl",
    "audio",
    "audiodownload_allowed",
    "licenses",
    "musicinfo",
)


class Stopped(Exception):
    """Only fixed local reason strings are used; never remote text."""


@dataclass
class Identity:
    recording_id: str | None
    fields_present: dict[str, bool]


@dataclass
class Report:
    outcome: str = "failed"
    reason: str | None = None
    connection_attempts: int = 0
    requests_attempted: int = 0
    response_bytes: int = 0
    http_status: int | None = None
    application_code: int | None = None
    results_count: int = 0
    records: list[Identity] = field(default_factory=list[Identity])


@contextmanager
def deadline() -> Generator[None]:
    """A process-local Linux deadline also interrupts DNS and slow trickle reads.

    This isolated command runs on the main thread, with no existing real timer.
    Do not use it inside the bot, an event loop or another timed operation.
    """
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise Stopped("timer_in_use")

    def expired(_signum: int, _frame: FrameType | None) -> None:
        raise TimeoutError

    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, REQUEST_SECONDS)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def read_application_id(path: Path) -> str:
    """Called only after explicit CLI opt-in; no environment-file loading."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ID_FILE_BYTES:
            raise Stopped("application_id_file")
        raw = source.read(MAX_ID_FILE_BYTES + 1)
    if len(raw) > MAX_ID_FILE_BYTES:
        raise Stopped("application_id_file")
    return checked_application_id(raw.decode("ascii").strip())


def checked_application_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", value):
        raise Stopped("application_id_file")
    if value.lower() == SUSPENDED_TEST_ID:
        raise Stopped("suspended_test_id")
    return value


def exchange(application_id: str, report: Report) -> bytes:
    """One TLS connection and GET, no address fallback, proxy or HTTP retry.

    Read the close-delimited *raw HTTP response*, including headers and chunk
    framing, into a capped buffer before http.client parses it. Connection: close
    is intentional: a peer that keeps the connection open stops at the same
    deadline. No content is decompressed, saved or followed.
    """
    query = urlencode(
        {
            "client_id": application_id,
            "format": "json",
            "limit": str(MAX_RESULTS),
            "type": "single albumtrack",
            "include": "licenses musicinfo",
        }
    )
    request = (
        f"GET /v3.0/tracks/?{query} HTTP/1.1\r\n"
        f"Host: {HOST}\r\n"
        "User-Agent: sobafm-jamendo-access/1\r\n"
        "Accept: application/json\r\n"
        "Accept-Encoding: identity\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    context = tls_context()
    addresses = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise Stopped("connection")
    family, kind, protocol, _name, address = addresses[0]
    report.connection_attempts = 1
    with socket.socket(family, kind, protocol) as connection:
        connection.settimeout(REQUEST_SECONDS)
        connection.connect(address)
        with context.wrap_socket(connection, server_hostname=HOST) as secure:
            report.requests_attempted = 1  # Count before a possibly partial write.
            secure.sendall(request)
            data = bytearray()
            while len(data) < MAX_RESPONSE_BYTES:
                block = secure.recv(min(8192, MAX_RESPONSE_BYTES - len(data)))
                if not block:
                    return bytes(data)
                data.extend(block)
                report.response_bytes = len(data)
            # Never consume an extra byte just to distinguish exact-fit from overflow.
            raise Stopped("response_limit")


def tls_context() -> ssl.SSLContext:
    # Explicit secure defaults avoid create_default_context's SSLKEYLOGFILE output.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    context.set_alpn_protocols(["http/1.1"])
    return context


class _SavedResponse:
    """The standard HTTP parser reads only the already bounded in-memory bytes."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def makefile(self, mode: str) -> io.BytesIO:
        assert mode == "rb"
        return io.BytesIO(self.data)


def parse_response(data: bytes, application_id: str, report: Report) -> None:
    if len(data) >= MAX_RESPONSE_BYTES:
        raise Stopped("response_limit")
    # HTTPResponse accepts this documented socket-like makefile boundary at runtime.
    with http.client.HTTPResponse(cast(socket.socket, _SavedResponse(data))) as response:
        response.begin()
        report.http_status = response.status
        if response.status != 200:
            raise Stopped("http_rejected")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise Stopped("encoded_response")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != (
            "application/json"
        ):
            raise Stopped("content_type")
        body = response.read(MAX_RESPONSE_BYTES)
        if response.length not in (None, 0):
            raise Stopped("truncated_response")
    payload: object = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    root = _object(payload)
    headers = _object(root.get("headers"))
    code = headers.get("code")
    if type(code) is not int or not 0 <= code <= 999:
        raise Stopped("invalid_response")
    report.application_code = code
    if code != 0:
        raise Stopped("application_rejected")
    # The tracks example says success; the response-codes prose says succeed.
    if headers.get("status") not in ("success", "succeed"):
        raise Stopped("application_status")
    if headers.get("error_message") != "":
        raise Stopped("application_error")
    # A high-consumption/account warning can accompany success. Stop, don't retry.
    if headers.get("warnings") != "":
        raise Stopped("application_warning")
    rows = root.get("results")
    count = headers.get("results_count")
    if not isinstance(rows, list) or type(count) is not int:
        raise Stopped("invalid_response")
    entries = cast(list[object], rows)
    if count != len(entries) or not 0 <= count <= MAX_RESULTS:
        raise Stopped("result_limit_or_count")
    records: list[Identity] = []
    seen: set[str] = set()
    for entry in entries:
        row = _object(entry)
        identity = row.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", identity):
            raise Stopped("invalid_identity")
        if identity in seen:
            raise Stopped("duplicate_identity")
        seen.add(identity)
        # Even a malicious numeric echo of an operator ID must not be reported.
        safe_id = None if application_id.lower() in identity.lower() else identity
        records.append(Identity(safe_id, {name: name in row for name in FIELDS}))
    report.records = records
    report.results_count = count
    report.outcome = "metadata_received"


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise Stopped("invalid_response")
    return cast(dict[str, object], value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stopped("invalid_response")
        result[key] = value
    return result


def probe(application_id: str) -> Report:
    """Opt-in network entry; deterministic tests replace exchange or socket peers."""
    report = Report()
    try:
        application_id = checked_application_id(application_id)
        with deadline():
            parse_response(exchange(application_id, report), application_id, report)
    except Stopped as error:
        report.reason = str(error)
    except TimeoutError:
        report.reason = "timeout"
    except ValueError, RecursionError, http.client.HTTPException:
        report.reason = "invalid_response"
    except OSError:
        report.reason = "connection"
    except Exception:
        report.reason = "internal"
    if report.reason is not None:
        report.outcome = "failed"
        report.records = []
        report.results_count = 0
    return report


def run_child(application_id_path: str) -> int:
    """Isolated child: only this process opens the supplied application-ID file."""
    report = Report()
    try:
        # Only this isolated process; no host setting, environment file or app import.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        application_id = read_application_id(Path(application_id_path))
        report = probe(application_id)
    except Stopped as error:
        report.reason = str(error)
    except OSError, ValueError:
        report.reason = "application_id_file"
    except KeyboardInterrupt:
        report.reason = "interrupted"
    except Exception:
        report.reason = "internal"
    encoded = json.dumps(asdict(report), separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("ascii")) + 1 > MAX_REPORT_BYTES:
        print('{"outcome":"failed","reason":"report_limit"}')
        return 1
    print(encoded)
    return 0 if report.outcome == "metadata_received" else 1


def run_authorized(application_id_path: str) -> int:
    """Kill and reap the one child at ten seconds, even if libc DNS ignores SIGALRM.

    Child stdout is its single bounded report; stderr is discarded. No credential
    value enters arguments, the parent, exception output or an environment file.
    """
    child: subprocess.Popen[bytes] | None = None
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
        )
        output, _ = child.communicate(timeout=REQUEST_SECONDS)
        if len(output) > MAX_REPORT_BYTES or not output:
            print('{"outcome":"failed","reason":"child_report"}')
            return 1
        sys.stdout.write(output.decode("ascii"))
        return 0 if child.returncode == 0 else 1
    except subprocess.TimeoutExpired:
        print('{"outcome":"failed","reason":"timeout"}')
        return 1
    except KeyboardInterrupt:
        print('{"outcome":"failed","reason":"interrupted"}')
        return 1
    except Exception:
        print('{"outcome":"failed","reason":"internal"}')
        return 1
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.communicate()  # Reap; never replace an unsettled request.


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print('{"outcome":"disabled","reason":"explicit_opt_in_required"}')
        return 0
    if len(args) != 2 or args[0] != "--send-to-jamendo" or not args[1]:
        print('{"outcome":"failed","reason":"arguments"}')
        return 2
    return run_authorized(args[1])


if __name__ == "__main__":
    sys.exit(main())
