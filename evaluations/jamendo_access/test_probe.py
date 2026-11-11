"""Explicit offline checks only: synthetic files, fake DNS/TLS and loopback peers."""

from __future__ import annotations

import json
import signal
import socket
import ssl
import subprocess
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from evaluations.jamendo_access import probe as subject

FAKE_ID = "synthetic-operator-id"


def document(count: int = 1, /, **changes: object) -> dict[str, object]:
    return {
        "headers": {
            "status": "success",
            "code": 0,
            "error_message": "",
            "warnings": "",
            "results_count": count,
            **changes,
        },
        "results": [{"id": str(100 + i), "name": "synthetic"} for i in range(count)],
    }


def wire(payload: object, *, status: int = 200, extra: bytes = b"") -> bytes:
    body = json.dumps(payload).encode()
    return (
        f"HTTP/1.1 {status} Synthetic\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n".encode()
        + extra
        + b"\r\n"
        + body
    )


def evaluate(monkeypatch: pytest.MonkeyPatch, response: bytes) -> subject.Report:
    def exchange(_key: str, report: subject.Report) -> bytes:
        report.response_bytes = len(response)
        return response

    monkeypatch.setattr(subject, "exchange", exchange)
    return subject.probe(FAKE_ID)


def test_default_and_invalid_arguments_never_read_or_connect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unapproved file/network operation")

    monkeypatch.setattr(subject, "read_application_id", forbidden)
    monkeypatch.setattr(subject, "probe", forbidden)
    monkeypatch.setattr(subject, "run_authorized", forbidden)
    assert subject.main([]) == 0
    for args in ([FAKE_ID], ["--send-to-jamendo"], ["--other", FAKE_ID]):
        assert subject.main(args) == 2
    output = capsys.readouterr()
    assert FAKE_ID not in output.out
    assert output.err == ""


@pytest.mark.parametrize("value", [b"", b"x" * 161, b"bad=id", b"\xff", b"709fa152"])
def test_bad_or_suspended_id_never_connects(
    value: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_bytes(value)

    def forbidden(_key: str) -> None:
        pytest.fail("invalid ID reached probe")

    monkeypatch.setattr(subject, "probe", forbidden)
    assert subject.run_child(str(path)) == 1
    output = capsys.readouterr()
    assert str(path) not in output.out
    assert output.err == ""


def test_file_is_bounded_regular_and_not_symlink(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_text(FAKE_ID + "\r\n")
    assert subject.read_application_id(path) == FAKE_ID
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        subject.read_application_id(link)
    with pytest.raises(OSError):
        subject.read_application_id(tmp_path)


@pytest.mark.parametrize("status", [200, 301, 302, 307, 308, 401, 403, 429, 500])
def test_http_status(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    report = evaluate(monkeypatch, wire(document(), status=status))
    assert report.http_status == status
    assert report.outcome == ("metadata_received" if status == 200 else "failed")


@pytest.mark.parametrize("code", [1, 5, 6, 11, 12, 13, 101])
def test_application_refusal_even_with_http_200(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    report = evaluate(monkeypatch, wire(document(code=code, error_message=FAKE_ID)))
    assert report.reason == "application_rejected"
    assert report.application_code == code
    assert report.records == []
    assert FAKE_ID not in json.dumps(asdict(report))


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "failed"},
        {"status": "unknown"},
        {"code": False},
        {"code": "0"},
        {"warnings": "quota warning " + FAKE_ID},
        {"error_message": FAKE_ID},
        {"results_count": 2},
        {"results_count": True},
    ],
)
def test_invalid_application_fields(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, object]
) -> None:
    report = evaluate(monkeypatch, wire(document(**changes)))
    assert report.outcome == "failed"
    assert report.records == []
    assert FAKE_ID not in json.dumps(asdict(report))


@pytest.mark.parametrize("count", [0, 10, 11])
def test_result_bound(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    report = evaluate(monkeypatch, wire(document(count)))
    assert report.outcome == ("metadata_received" if count <= 10 else "failed")
    assert len(report.records) <= 10


def test_no_remote_text_urls_or_headers_in_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "12345678"  # Synthetic ID deliberately echoed as a numeric recording ID.
    path = tmp_path / "synthetic-id.txt"
    path.write_text(key)
    payload = document()
    payload["results"] = [
        {
            "id": key,
            "name": "secret-text " + key,
            "audio": "https://invalid.example/audio?client_id=" + key,
            "license_ccurl": key,
            "musicinfo": {"tags": [key]},
        }
    ]
    response = wire(payload, extra=f"X-Private: {key}\r\n".encode())

    def exchange(_key: str, report: subject.Report) -> bytes:
        report.response_bytes = len(response)
        return response

    monkeypatch.setattr(subject, "exchange", exchange)
    assert subject.run_child(str(path)) == 0
    output = capsys.readouterr()
    assert key not in output.out
    assert "secret-text" not in output.out and "https://" not in output.out
    assert str(path) not in output.out
    assert output.err == ""
    assert len(output.out.encode()) <= subject.MAX_REPORT_BYTES
    parsed = cast(dict[str, object], json.loads(output.out))
    assert parsed["outcome"] == "metadata_received"


@pytest.mark.parametrize("error", [OSError(FAKE_ID), ValueError(FAKE_ID), RuntimeError(FAKE_ID)])
def test_exception_text_never_reported(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def exchange(_key: str, _report: subject.Report) -> bytes:
        raise error

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.probe(FAKE_ID)
    assert report.outcome == "failed"
    assert FAKE_ID not in json.dumps(asdict(report))


@pytest.mark.parametrize(
    "response",
    [
        b"not HTTP " + FAKE_ID.encode(),
        wire(document())[:-4],
        wire(document(), extra=b"Content-Encoding: gzip\r\n"),
        wire(document()).replace(b"application/json", b"text/html"),
        wire({"headers": {}, "results": []}),
        wire({"headers": [], "results": []}),
        wire(document()).replace(b'"code": 0', b'"code": 0, "code": 0'),
        wire(document()).replace(b'"id": "100"', b'"id": "https://invalid.example/"'),
    ],
)
def test_malformed_response(monkeypatch: pytest.MonkeyPatch, response: bytes) -> None:
    report = evaluate(monkeypatch, response)
    assert report.outcome == "failed"
    assert report.records == []


class PlainTls:
    """Loopback-only TLS stand-in; leaves the real socket/HTTP path in place."""

    def wrap_socket(self, connection: socket.socket, *, server_hostname: str) -> socket.socket:
        assert server_hostname == subject.HOST
        return connection


@contextmanager
def peer(
    monkeypatch: pytest.MonkeyPatch, action: Callable[[socket.socket], None]
) -> Generator[list[bytes]]:
    requests: list[bytes] = []
    failures: list[BaseException] = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    address = listener.getsockname()

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                data = bytearray()
                while b"\r\n\r\n" not in data:
                    part = connection.recv(8192)
                    if not part:
                        break
                    data.extend(part)
                requests.append(bytes(data))
                action(connection)
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=serve)
    worker.start()
    monkeypatch.setattr(subject, "tls_context", PlainTls)

    def addresses(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", address)]

    monkeypatch.setattr(subject.socket, "getaddrinfo", addresses)
    try:
        yield requests
        listener.setblocking(False)
        with pytest.raises(BlockingIOError):
            extra, _ = listener.accept()
            extra.close()
    finally:
        worker.join(timeout=2)
        listener.close()
        assert not worker.is_alive()
        assert not failures


@pytest.mark.parametrize("status", [200, 302, 403, 429])
def test_real_socket_one_query_no_redirect_or_retry(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    response = wire(document(), status=status, extra=b"Location: https://invalid.example/\r\n")
    with peer(monkeypatch, lambda connection: connection.sendall(response)) as requests:
        report = subject.probe(FAKE_ID)
    assert len(requests) == 1
    first = requests[0].split(b"\r\n")[0].decode()
    target = first.split(" ")[1]
    query = parse_qs(urlsplit(target).query)
    assert query == {
        "client_id": [FAKE_ID],
        "format": ["json"],
        "limit": ["10"],
        "type": ["single albumtrack"],
        "include": ["licenses musicinfo"],
    }
    assert target.startswith("/v3.0/tracks/?")
    assert b"Referer:" not in requests[0] and b"Authorization:" not in requests[0]
    assert report.requests_attempted == report.connection_attempts == 1
    assert report.outcome == ("metadata_received" if status == 200 else "failed")


def test_disconnect_does_not_trigger_hidden_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    with peer(monkeypatch, lambda _connection: None) as requests:
        report = subject.probe(FAKE_ID)
    assert report.outcome == "failed"
    assert len(requests) == report.requests_attempted == report.connection_attempts == 1


@pytest.mark.parametrize("size", [subject.MAX_RESPONSE_BYTES - 1, subject.MAX_RESPONSE_BYTES])
def test_total_response_bound_includes_headers(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    base = wire(document())
    # Whitespace after JSON is valid and still consumes the response budget.
    body = json.dumps(document()).encode()
    padding = size - len(base)
    padded = body + b" " * padding
    # Build to the exact requested total length despite Content-Length digit growth.
    response = base.split(b"\r\n\r\n")[0] + b"\r\n\r\n" + padded
    response = response.replace(
        str(len(body)).encode() + b"\r\n", str(len(padded)).encode() + b"\r\n"
    )
    excess = len(response) - size
    padded = padded[:-excess] if excess else padded
    response = (
        f"HTTP/1.1 200 Synthetic\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(padded)}\r\n\r\n".encode()
        + padded
    )
    assert len(response) == size
    with peer(monkeypatch, lambda connection: connection.sendall(response)):
        report = subject.probe(FAKE_ID)
    assert report.response_bytes == size
    assert report.outcome == (
        "metadata_received" if size < subject.MAX_RESPONSE_BYTES else "failed"
    )
    if size == subject.MAX_RESPONSE_BYTES:
        assert report.reason == "response_limit"


def test_deadline_interrupts_open_connection_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stalled(connection: socket.socket) -> None:
        # Deterministically expire while the client awaits more bytes; no sleeps.
        connection.sendall(b"HTTP/1.1 200 Synthetic\r\n")
        signal.pthread_kill(threading.main_thread().ident or 0, signal.SIGALRM)
        assert connection.recv(1) == b""

    with peer(monkeypatch, stalled) as requests:
        report = subject.probe(FAKE_ID)
    assert report.reason == "timeout"
    assert len(requests) == 1
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_deadline_covers_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def addresses(*_args: object, **_kwargs: object) -> None:
        signal.raise_signal(signal.SIGALRM)

    monkeypatch.setattr(subject.socket, "getaddrinfo", addresses)
    report = subject.probe(FAKE_ID)
    assert report.reason == "timeout"
    assert report.connection_attempts == report.requests_attempted == 0


def test_exact_ten_second_timer_and_handler_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, float]] = []
    previous = signal.getsignal(signal.SIGALRM)

    def set_timer(which: int, seconds: float) -> tuple[float, float]:
        calls.append((which, seconds))
        return (0, 0)

    monkeypatch.setattr(signal, "setitimer", set_timer)
    assert evaluate(monkeypatch, wire(document())).outcome == "metadata_received"
    assert calls == [(signal.ITIMER_REAL, 10), (signal.ITIMER_REAL, 0)]
    assert signal.getsignal(signal.SIGALRM) == previous


def test_tls_checks_and_no_environment_key_logging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "must-not-be-created"
    monkeypatch.setenv("SSLKEYLOGFILE", str(path))
    context = subject.tls_context()
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.keylog_filename is None
    assert not path.exists()


def test_no_address_fallback_after_connection_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = socket.socket()
    sentinel.bind(("127.0.0.1", 0))
    sentinel.listen(1)
    # The first address is a bound socket that is not listening: deterministic refusal.
    refused = socket.socket()
    refused.bind(("127.0.0.1", 0))

    def addresses(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", item.getsockname())
            for item in (refused, sentinel)
        ]

    monkeypatch.setattr(subject.socket, "getaddrinfo", addresses)
    try:
        report = subject.probe(FAKE_ID)
        assert report.reason == "connection"
        assert report.connection_attempts == 1 and report.requests_attempted == 0
        sentinel.setblocking(False)
        with pytest.raises(BlockingIOError):
            connection, _ = sentinel.accept()
            connection.close()
    finally:
        refused.close()
        sentinel.close()


def test_supervisor_starts_no_network_for_invalid_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_text(subject.SUSPENDED_TEST_ID)
    assert subject.main(["--send-to-jamendo", str(path)]) == 1
    output = capsys.readouterr()
    assert '"suspended_test_id"' in output.out
    assert '"requests_attempted":0' in output.out
    assert subject.SUSPENDED_TEST_ID not in output.out
    assert str(path) not in output.out
    assert output.err == ""


def test_supervisor_kills_and_reaps_noncooperative_child(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def spawn(_args: list[str], **_kwargs: object) -> subprocess.Popen[bytes]:
        child = original(
            [sys.executable, "-I", "-c", "import signal; signal.pause()"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        return child

    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    monkeypatch.setattr(subject, "REQUEST_SECONDS", 0.05)
    assert subject.run_authorized("unused-synthetic-path") == 1
    assert len(children) == 1
    assert children[0].poll() == -signal.SIGKILL
    assert capsys.readouterr().out == '{"outcome":"failed","reason":"timeout"}\n'


def test_suspended_id_is_also_rejected_by_network_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("suspended ID reached network")

    monkeypatch.setattr(subject, "exchange", forbidden)
    report = subject.probe(subject.SUSPENDED_TEST_ID)
    assert report.reason == "suspended_test_id"
    assert report.requests_attempted == 0
