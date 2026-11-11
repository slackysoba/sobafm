"""Explicit offline evaluation tests. All bytes/IDs are synthetic; no live calls."""

from __future__ import annotations

import hashlib
import json
import signal
import socket
import ssl
import subprocess
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from evaluations.jamendo_stream import probe as subject

FAKE_ID = "synthetic-client-id"
AUDIO = b"synthetic bytes, not an audio file"


def row(**changes: object) -> dict[str, object]:
    return {
        "id": "168",
        "name": "Synthetic title",
        "artist_id": "9",
        "artist_name": "Synthetic",
        "album_id": "8",
        "album_name": "Synthetic album",
        "duration": 180,
        "license_ccurl": "http://creativecommons.org/licenses/by-nc-sa/3.0/",
        "shareurl": "http://www.jamendo.com/track/168",
        "audiodownload_allowed": False,
        "audio": f"https://{subject.STREAM_HOST}/?trackid=168&format=mp32&from=app-{FAKE_ID}",
        **changes,
    }


def document(record: dict[str, object] | None = None, /, **changes: object) -> bytes:
    return json.dumps(
        {
            "headers": {
                "status": "success",
                "code": 0,
                "error_message": "",
                "warnings": "",
                "results_count": 1,
                **changes,
            },
            "results": [row() if record is None else record],
        }
    ).encode()


def wire(
    body: bytes,
    *,
    metadata: bool = False,
    headers: bytes | None = None,
    status: int = 200,
    version: str = "1.0",
) -> bytes:
    if headers is None:
        headers = f"Content-Length: {len(body)}\r\n".encode()
    media = "application/json" if metadata else "audio/mpeg"
    return f"HTTP/{version} {status} Synthetic\r\nContent-Type: {media}\r\n".encode() + (
        headers + b"\r\n" + body
    )


class Receiver:
    def __init__(self, data: bytes, chunk: int = 999_999) -> None:
        self.data = data
        self.offset = 0
        self.chunk = chunk
        self.read_sizes: list[int] = []

    def recv(self, size: int) -> bytes:
        self.read_sizes.append(size)
        part = self.data[self.offset : self.offset + min(size, self.chunk)]
        self.offset += len(part)
        return part


def receive(data: bytes, *, metadata: bool = False, chunk: int = 999_999) -> subject.Transfer:
    transfer = subject.Transfer()
    result = subject.receive(
        cast(ssl.SSLSocket, Receiver(data, chunk)), transfer, metadata=metadata
    )
    assert result == (document() if metadata else b"")
    return transfer


def test_default_invalid_arguments_do_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unexpected credential/network/process operation")

    for name in ("read_id", "probe", "run_authorized"):
        monkeypatch.setattr(subject, name, forbidden)
    assert subject.main([]) == 0
    for args in ([FAKE_ID], ["--send-to-jamendo"], ["--bad", FAKE_ID]):
        assert subject.main(args) == 2
    output = capsys.readouterr()
    assert FAKE_ID not in output.out
    assert output.err == ""


@pytest.mark.parametrize("value", [b"", b"x" * 161, b"bad=id", b"\xff", b"709fa152"])
def test_invalid_id_file_never_connects(
    value: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_bytes(value)

    def forbidden(*_args: object) -> None:
        pytest.fail("invalid ID reached probe")

    monkeypatch.setattr(subject, "probe", forbidden)
    assert subject.run_child(str(path)) == 1
    output = capsys.readouterr()
    assert str(path) not in output.out
    assert output.err == ""


def test_only_regular_bounded_id_file(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_text(FAKE_ID + "\n")
    assert subject.read_id(str(path)) == FAKE_ID
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        subject.read_id(str(link))
    with pytest.raises(OSError):
        subject.read_id(str(tmp_path))


@pytest.mark.parametrize("version", ["1.0", "1.1"])
@pytest.mark.parametrize("chunk", [1, 7, 8192])
@pytest.mark.parametrize("length", [True, False])
def test_real_parser_valid_stream_controls(version: str, chunk: int, length: bool) -> None:
    transfer = receive(wire(AUDIO, version=version, headers=None if length else b""), chunk=chunk)
    assert transfer.body_bytes == len(AUDIO)
    assert transfer.sha256 == hashlib.sha256(AUDIO).hexdigest()
    assert transfer.eof
    assert transfer.framing == ("content_length" if length else "connection_eof")


@pytest.mark.parametrize(
    "headers",
    [
        b"Content-Length: 2\r\nContent-Length: 3\r\n",
        b"Content-Length: 2\r\nContent-Length: 2\r\n",
        b"Content-Length: -1\r\n",
        b"Content-Length: +2\r\n",
        b"Content-Length: 2,2\r\n",
        b"Content-Length: 9999999999999\r\n",
        b"Content-Length: 0\r\n",
        b"Content-Length: 999\r\n",
        b"Content-Length: 1\r\n",
        b"Transfer-Encoding: chunked\r\n",
        b"Transfer-Encoding: identity\r\n",
        b"Content-Encoding: gzip\r\n",
        b"Content-Encoding: identity\r\nContent-Encoding: identity\r\n",
        b"Content-Type: audio/mpeg\r\n",
        b"Content-Range: bytes 0-1/20\r\n",
        b"Bad header\r\n",
        b"Folded: value\r\n continuation\r\n",
    ],
)
def test_real_parser_rejects_ambiguous_or_unsupported_headers(headers: bytes) -> None:
    with pytest.raises(subject.Stopped):
        receive(wire(AUDIO, headers=headers))


@pytest.mark.parametrize("body", [b"1\r\nxXX0\r\n\r\n", b"1\r\nx\r\n0\r\n", b"1\r\nx\r\n0\r\n\r\n"])
def test_prior_review_chunk_cases_fail_closed(body: bytes) -> None:
    with pytest.raises(subject.Stopped, match="unsupported_transfer_encoding"):
        receive(wire(body, headers=b"Transfer-Encoding: chunked\r\n"))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"HTTP/1.0 200 OK\r\n",
        b"HTTP/1.0 200 OK\n\n",
        wire(b""),
        wire(AUDIO).replace(b"1.0", b"2.0", 1),
    ],
)
def test_incomplete_empty_or_bad_protocol(data: bytes) -> None:
    with pytest.raises(subject.Stopped):
        receive(data)


@pytest.mark.parametrize("status", [100, 206, 301, 302, 307, 308, 401, 403, 429, 500])
def test_status_never_followed(status: int) -> None:
    with pytest.raises(subject.Stopped):
        receive(wire(AUDIO, status=status))


def test_total_bytes_and_header_cap_no_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "STREAM_BYTES", 200)
    peer = Receiver(wire(b"x" * 500, headers=b""))
    transfer = subject.Transfer()
    with pytest.raises(subject.Stopped, match="response_limit"):
        subject.receive(cast(ssl.SSLSocket, peer), transfer, metadata=False)
    assert peer.offset == transfer.response_bytes == 200
    assert max(peer.read_sizes) <= subject.BLOCK_BYTES
    peer = Receiver(b"HTTP/1.0 200 OK\r\nX: " + b"x" * subject.HEADER_BYTES)
    monkeypatch.setattr(subject, "STREAM_BYTES", subject.STREAM_BYTES + subject.HEADER_BYTES * 2)
    with pytest.raises(subject.Stopped, match="header_limit"):
        subject.receive(cast(ssl.SSLSocket, peer), subject.Transfer(), metadata=False)
    assert peer.offset == subject.HEADER_BYTES


def test_exact_cap_is_inconclusive_and_declared_oversize_stops_before_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = wire(AUDIO)
    monkeypatch.setattr(subject, "STREAM_BYTES", len(data))
    with pytest.raises(subject.Stopped, match="response_limit"):
        receive(data, chunk=1)
    peer = Receiver(wire(b"x" * 500), chunk=1)
    with pytest.raises(subject.Stopped, match="response_limit"):
        subject.receive(cast(ssl.SSLSocket, peer), subject.Transfer(), metadata=False)
    assert peer.offset == peer.data.index(b"\r\n\r\n") + 4


def test_metadata_response_cap_is_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    assert receive(wire(document(), metadata=True), metadata=True).body_bytes == len(document())
    monkeypatch.setattr(subject, "METADATA_BYTES", 300)
    with pytest.raises(subject.Stopped, match="response_limit"):
        receive(wire(document(), metadata=True), metadata=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "169"},
        {"name": ""},
        {"artist_name": None},
        {"artist_id": "x"},
        {"album_id": "", "album_name": "unexpected"},
        {"type": "preview"},
        {"duration": 0},
        {"duration": 481},
        {"duration": True},
        {"duration": 1.5},
        {"duration": "inf"},
        {"license_ccurl": "https://example.test/license"},
        {"license_ccurl": "https://creativecommons.org/licenses/by/3.0/?secret=" + FAKE_ID},
        {"shareurl": "https://www.jamendo.com/track/169"},
        {"audiodownload_allowed": "true"},
    ],
)
def test_metadata_gate_rejects_invalid_candidate(changes: dict[str, object]) -> None:
    with pytest.raises(subject.Stopped):
        subject.validate_metadata(document(row(**changes)), FAKE_ID, subject.Report())


@pytest.mark.parametrize(
    "changes",
    [
        {"code": 5},
        {"code": 6},
        {"code": 11},
        {"code": True},
        {"code": "0"},
        {"status": "failed"},
        {"error_message": FAKE_ID},
        {"warnings": FAKE_ID},
        {"results_count": 0},
        {"results_count": True},
        {"results_count": 2},
    ],
)
def test_application_gate(changes: dict[str, object]) -> None:
    with pytest.raises(subject.Stopped):
        subject.validate_metadata(document(**changes), FAKE_ID, subject.Report())


@pytest.mark.parametrize(
    "data", [b"{}", b"null", b"[]", b"{bad", b"\xff", b'{"headers":{},"headers":{},"results":[]}']
)
def test_bad_json(data: bytes) -> None:
    with pytest.raises((subject.Stopped, ValueError)):
        subject.validate_metadata(data, FAKE_ID, subject.Report())


@pytest.mark.parametrize(
    "changes", [{}, {"duration": "480"}, {"album_id": "", "album_name": "", "type": "single"}]
)
def test_candidate_controls_are_not_admission(changes: dict[str, object]) -> None:
    report = subject.Report()
    assert subject.validate_metadata(document(row(**changes)), FAKE_ID, report) == (
        "/?trackid=168&format=mp32&from=app-" + FAKE_ID
    )
    assert report.metadata_validated
    assert report.outcome == "inconclusive"
    assert "Synthetic" not in json.dumps(asdict(report))


@pytest.mark.parametrize(
    "url",
    [
        "http://prod-1.storage.jamendo.com/",
        "https://evil.test/",
        "https://127.0.0.1/",
        "https://prod-1.storage.jamendo.com:443/",
        "https://user@prod-1.storage.jamendo.com/",
        "https://prod-1.storage.jamendo.com.evil.test/",
        "https://prod-1.storage.jamendo.com/download/",
        " https://prod-1.storage.jamendo.com/",
        "https://prod-1.storage.jamendo.com/\r\nX: x",
        f"https://{subject.STREAM_HOST}/?trackid=169&format=mp32&from=app-{FAKE_ID}",
        f"https://{subject.STREAM_HOST}/?trackid=168&format=mp31&from=app-{FAKE_ID}",
        f"https://{subject.STREAM_HOST}/?trackid=168&format=mp32&from=app-{FAKE_ID}#",
        f"https://{subject.STREAM_HOST}/?trackid=168&format=mp32&from=app-{FAKE_ID}&extra=x",
        f"https://{subject.STREAM_HOST}/?trackid=168&format=mp32&from=app-wrong",
        f"https://{subject.STREAM_HOST}/?trackid=168&format=mp32&format=mp32",
    ],
)
def test_stream_url_is_fixed(url: str) -> None:
    with pytest.raises((subject.Stopped, ValueError)):
        subject.stream_target(url, FAKE_ID)


def test_one_refresh_then_one_stream_and_no_raw_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bool]] = []

    def exchange(target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        calls.append((target, metadata))
        return document() if metadata else b""

    monkeypatch.setattr(subject, "exchange", exchange)
    report = subject.Report()
    subject.probe(FAKE_ID, report)
    assert report.outcome == "stream_received"
    assert [metadata for _, metadata in calls] == [True, False]
    assert parse_qs(urlsplit(calls[0][0]).query) == {
        "client_id": [FAKE_ID],
        "format": ["json"],
        "id": ["168"],
        "limit": ["1"],
        "type": ["single albumtrack"],
        "audioformat": ["mp32"],
    }
    assert FAKE_ID not in json.dumps(asdict(report))


def test_bad_refresh_prevents_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def exchange(_target: str, _transfer: subject.Transfer, *, metadata: bool) -> bytes:
        nonlocal calls
        calls += 1
        assert metadata
        return document(code=6)

    monkeypatch.setattr(subject, "exchange", exchange)
    with pytest.raises(subject.Stopped, match="application_rejected"):
        subject.probe(FAKE_ID, subject.Report())
    assert calls == 1


class PlainTLS:
    def wrap_socket(
        self, connection: socket.socket, *, server_hostname: str, suppress_ragged_eofs: bool
    ) -> socket.socket:
        assert server_hostname in (subject.API_HOST, subject.STREAM_HOST)
        assert not suppress_ragged_eofs
        return connection


@contextmanager
def peer(monkeypatch: pytest.MonkeyPatch, response: bytes) -> Generator[list[bytes]]:
    requests: list[bytes] = []
    errors: list[BaseException] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(3)

        # Send FIN but still observe the client's close; no retry is accepted.
        def simple_serve() -> None:
            try:
                with listener.accept()[0] as connection:
                    connection.settimeout(3)
                    data = bytearray()
                    while not data.endswith(b"\r\n\r\n"):
                        data.extend(connection.recv(4096))
                    requests.append(bytes(data))
                    connection.sendall(response)
                    connection.shutdown(socket.SHUT_WR)
                    assert connection.recv(4096) == b""
            except BaseException as error:
                errors.append(error)

        def addresses(*_args: object, **_kwargs: object) -> list[tuple[int, int, int, str, object]]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", listener.getsockname())]

        monkeypatch.setattr(subject.socket, "getaddrinfo", addresses)
        monkeypatch.setattr(subject, "tls_context", PlainTLS)
        thread = threading.Thread(target=simple_serve, daemon=True)
        thread.start()
        try:
            yield requests
        finally:
            thread.join(4)
            assert not thread.is_alive()
            assert not errors


@pytest.mark.parametrize("response", [wire(AUDIO), wire(AUDIO, status=302), b""])
def test_real_transport_single_request_and_closes(
    monkeypatch: pytest.MonkeyPatch, response: bytes
) -> None:
    with peer(monkeypatch, response) as requests:
        transfer = subject.Transfer()
        with suppress(subject.Stopped):
            subject.exchange("/?synthetic=1", transfer, metadata=False)
    assert transfer.connection_attempts == transfer.requests_attempted == 1
    assert len(requests) == 1
    request = requests[0]
    assert request.startswith(b"GET /?synthetic=1 HTTP/1.0\r\n")
    assert b"Host: prod-1.storage.jamendo.com\r\n" in request
    assert b"User-Agent: sobafm-jamendo-stream/1\r\n" in request
    assert b"Connection: close\r\n" in request
    for forbidden in (b"Range:", b"Referer:", b"Authorization:", b"Cookie:"):
        assert forbidden not in request


def test_no_dns_address_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))  # Not listening: deterministic connect refusal.

        def addresses(*_args: object, **_kwargs: object) -> list[tuple[int, int, int, str, object]]:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", listener.getsockname()),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("invalid", 0)),
            ]

        monkeypatch.setattr(subject.socket, "getaddrinfo", addresses)
        transfer = subject.Transfer()
        with pytest.raises(OSError):
            subject.exchange("/", transfer, metadata=False)
        assert transfer.connection_attempts == 1
        assert transfer.requests_attempted == 0


def test_real_tls_verifies_without_keylog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "must-not-exist"
    monkeypatch.setenv("SSLKEYLOGFILE", str(path))
    context = subject.tls_context()
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.keylog_filename is None
    assert not path.exists()


def test_alarm_restored_and_interrupts_work(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = signal.getsignal(signal.SIGALRM)
    with pytest.raises(TimeoutError), subject.deadline():
        signal.raise_signal(signal.SIGALRM)
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


@pytest.mark.parametrize("error", [RuntimeError(FAKE_ID), OSError(FAKE_ID), TimeoutError(FAKE_ID)])
def test_child_errors_and_raw_metadata_never_leak(
    error: Exception, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def read_id(_path: str) -> str:
        return FAKE_ID

    monkeypatch.setattr(subject, "read_id", read_id)

    def fail(_key: str, report: subject.Report) -> None:
        report.metadata_validated = True
        raise error

    monkeypatch.setattr(subject, "probe", fail)
    assert subject.run_child("synthetic-path") == 1
    output = capsys.readouterr()
    assert FAKE_ID not in output.out
    assert output.err == ""
    assert len(output.out) < subject.REPORT_BYTES


def test_accidental_digest_key_echo_is_suppressed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def read_id(_path: str) -> str:
        return FAKE_ID

    monkeypatch.setattr(subject, "read_id", read_id)

    def finish(_key: str, report: subject.Report) -> None:
        report.stream.sha256 = FAKE_ID
        report.outcome = "stream_received"

    monkeypatch.setattr(subject, "probe", finish)
    assert subject.run_child("synthetic-path") == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "report_redacted"


class Child:
    def __init__(self, *, interrupted: bool = False, waits: int = 0) -> None:
        self.returncode: int | None = None
        self.stdout = None
        self.killed = 0
        self.waits = waits
        self.interrupted = interrupted

    def communicate(self, timeout: float) -> tuple[bytes, None]:
        assert 0 < timeout <= subject.WORK_SECONDS
        if self.interrupted:
            raise KeyboardInterrupt
        raise subprocess.TimeoutExpired("synthetic", timeout)

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed += 1

    def wait(self, timeout: float) -> int:
        assert 0 < timeout <= 1
        if self.waits:
            self.waits -= 1
            raise subprocess.TimeoutExpired("synthetic", timeout)
        self.returncode = -9
        return -9


@pytest.mark.parametrize("interrupted", [False, True])
def test_supervisor_kills_reaps_and_keeps_original_failure(
    interrupted: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child = Child(interrupted=interrupted)

    def spawn(args: list[str], **kwargs: object) -> Child:
        assert FAKE_ID not in str(args)
        assert kwargs["env"] == {"LANG": "C.UTF-8"}
        assert kwargs["stderr"] == subprocess.DEVNULL
        return child

    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    assert subject.run_authorized("synthetic-path") == 1
    assert child.killed == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == ("interrupted" if interrupted else "timeout")
    assert result["cleanup_met"] and result["child_reaped"]


def test_late_reaping_preserves_failed_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    child = Child(waits=2)
    ticks = iter([0.0, 1.0, 4.0, 5.0, 6.0])
    monkeypatch.setattr(subject.time, "monotonic", lambda: next(ticks))
    assert not subject.settle(cast(subprocess.Popen[bytes], child))
    assert child.returncode == -9
    assert child.killed == 3


def test_real_supervisor_synthetic_invalid_file_is_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "synthetic-id.txt"
    path.write_text("invalid!")
    assert subject.run_authorized(str(path)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "application_id_file"
    assert result["metadata"]["requests_attempted"] == 0
    assert result["stream"]["requests_attempted"] == 0


def test_real_stalled_child_is_killed_and_reaped(
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
    monkeypatch.setattr(subject, "WORK_SECONDS", 0.1)
    assert subject.run_authorized("unused-synthetic-path") == 1
    assert len(children) == 1 and children[0].returncode == -signal.SIGKILL
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "timeout"
    assert result["child_reaped"] and result["cleanup_met"]


def test_sigterm_cancels_supervisor_but_restores_handler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class TerminatedChild(Child):
        def communicate(self, timeout: float) -> tuple[bytes, None]:
            signal.raise_signal(signal.SIGTERM)
            pytest.fail("SIGTERM was not handled")

    child = TerminatedChild()

    def spawn(_args: list[str], **_kwargs: object) -> Child:
        return child

    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    assert subject.run_authorized("synthetic-path") == 1
    assert child.returncode == -9
    assert signal.getsignal(signal.SIGTERM) == previous
    assert json.loads(capsys.readouterr().out)["reason"] == "interrupted"


def test_signal_during_spawn_cannot_lose_child(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child = Child()

    def spawn(_args: list[str], **_kwargs: object) -> Child:
        signal.raise_signal(signal.SIGINT)  # Delivered only after child handle assignment.
        return child

    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    assert subject.run_authorized("synthetic-path") == 1
    assert child.returncode == -9 and child.killed == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "interrupted"


def test_partial_stream_failure_preserves_hash_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    peer = Receiver(wire(AUDIO, headers=b"Content-Length: 999\r\n"))
    transfer = subject.Transfer()
    with pytest.raises(subject.Stopped, match="invalid_framing"):
        subject.receive(cast(ssl.SSLSocket, peer), transfer, metadata=False)
    assert transfer.body_bytes == len(AUDIO)
    assert transfer.sha256 == hashlib.sha256(AUDIO).hexdigest()
    assert transfer.eof


def test_late_cleanup_does_not_replace_child_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailedChild(Child):
        def communicate(self, timeout: float) -> tuple[bytes, None]:
            self.returncode = 1
            return b'{"outcome":"inconclusive","reason":"http_rejected"}', None

    def spawn(_args: list[str], **_kwargs: object) -> Child:
        return FailedChild()

    def late_settle(_child: subprocess.Popen[bytes]) -> bool:
        return False

    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    monkeypatch.setattr(subject, "settle", late_settle)
    assert subject.run_authorized("synthetic-path") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "http_rejected"
    assert result["cleanup_met"] is False
