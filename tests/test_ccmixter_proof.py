"""Owned loopback fixtures only: no provider, real audio, decoder or Docker."""

from __future__ import annotations

import hashlib
import io
import json
import mmap
import os
import select as readiness
import signal
import struct
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, cast
from unittest.mock import Mock, patch

import pytest

from sobafm import ccmixter_proof as proof
from sobafm.catalog import metadata_digest, parse_catalog, parse_query, select

CATALOG = Path(__file__).resolve().parents[1] / "evaluations/catalog"
# A separate process avoids forking a test runner with a server thread. The pipe
# acknowledgement fixes the failure schedule; no sleep is used to win a race.
SERVER = """
import socket, sys
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    print(listener.getsockname()[1], flush=True)
    with listener.accept()[0] as peer:
        data = b''
        while b'\\r\\n\\r\\n' not in data:
            data += peer.recv(4096)
        peer.sendall(bytes.fromhex(sys.argv[1]))
        print('delivered', flush=True)
        if sys.argv[2] == 'hold':
            sys.stdin.buffer.read(1)
"""


@contextmanager
def server(
    response: bytes, *, hold: bool = False
) -> Generator[tuple[str, subprocess.Popen[bytes]]]:
    process = subprocess.Popen(
        [sys.executable, "-c", SERVER, response.hex(), "hold" if hold else "close"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        port = int(line(process.stdout))
        yield f"http://127.0.0.1:{port}/owned", process
    finally:
        if process.stdin is not None and hold:
            with suppress(BrokenPipeError):
                process.stdin.write(b"x")
                process.stdin.flush()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with suppress(BrokenPipeError):
                    stream.close()


def response(body: bytes, *, headers: str = "", status: int = 200) -> bytes:
    return f"HTTP/1.1 {status} Fixture\r\nContent-Type: audio/mpeg\r\n{headers}\r\n".encode() + body


def line(stream: IO[bytes]) -> bytes:
    assert readiness.select([stream], [], [], 15)[0], "owned fixture did not acknowledge"
    return stream.readline()


def acquire(
    tmp_path: Path,
    wire: bytes,
    *,
    cap: int = 32,
    expected: int | None = None,
    hold: bool = False,
    inactivity: float = 20,
) -> tuple[proof.Transfer, int]:
    owner = proof.RequestOwner(tmp_path)
    try:
        with server(wire, hold=hold) as (url, process):
            owner.start(
                proof.Request(
                    url, cap, expected, media=True, loopback=True, inactivity_seconds=inactivity
                )
            )
            assert process.stdout is not None and line(process.stdout) == b"delivered\n"
            result = owner.wait()
            assert owner.active is not None  # Exit alone has not released the file reservation.
            assert owner.cleanup()
            assert not (tmp_path / "body").exists()
            return result, owner.media_bytes
    finally:
        assert owner.close()


def test_framed_transfer_hash_and_verified_removal(tmp_path: Path) -> None:
    body = b"owned non-audio fixture"
    result, total = acquire(
        tmp_path, response(body, headers=f"Content-Length: {len(body)}\r\n"), expected=len(body)
    )
    assert result == proof.Transfer(
        "transferred", len(body), len(body), hashlib.sha256(body).hexdigest(), True, 200
    )
    assert total == len(body)


@pytest.mark.parametrize(
    ("wire", "cap", "expected", "verdict", "count"),
    [
        (response(b"x" * 33), 32, None, "body_cap_reached", 32),
        (response(b"x", headers="Content-Length: 33\r\n"), 32, None, "advertised_oversize", 0),
        (response(b"short", headers="Content-Length: 12\r\n"), 32, 12, "truncated_body", 5),
        (response(b"short"), 32, 12, "size_conflict", 5),
        (response(b"", headers="Content-Length: 3\r\n"), 32, 12, "advertised_size_conflict", 0),
        (
            response(b"", headers="Location: https://example.invalid\r\n", status=302),
            32,
            None,
            "http_status_rejected",
            0,
        ),
        (response(b"", status=401), 32, None, "http_status_rejected", 0),
        (response(b"", status=403), 32, None, "http_status_rejected", 0),
        (response(b"", status=503), 32, None, "http_status_rejected", 0),
        (response(b"", headers="Content-Encoding: gzip\r\n"), 32, None, "encoding_rejected", 0),
        (
            response(b"", headers="Content-Length: 1\r\nContent-Length: 1\r\n"),
            32,
            None,
            "ambiguous_framing",
            0,
        ),
        (
            response(b"", headers="Content-Length: 1\r\nTransfer-Encoding: chunked\r\n"),
            32,
            None,
            "ambiguous_framing",
            0,
        ),
        (
            response(b"", headers="Content-Range: bytes 0-1/20\r\n"),
            32,
            None,
            "partial_response_rejected",
            0,
        ),
        (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\nbad",
            32,
            None,
            "content_type_rejected",
            0,
        ),
    ],
)
def test_fail_closed_and_failed_body_accounting(
    tmp_path: Path, wire: bytes, cap: int, expected: int | None, verdict: str, count: int
) -> None:
    result, total = acquire(tmp_path, wire, cap=cap, expected=expected)
    assert result.verdict == verdict
    assert result.received == total == count


def test_inactivity_deadline_is_active_during_stalled_read(tmp_path: Path) -> None:
    result, total = acquire(
        tmp_path,
        response(b"x", headers="Content-Length: 12\r\n"),
        expected=12,
        hold=True,
        inactivity=0.05,
    )
    assert result.verdict == "request_timeout" and result.received == 1
    assert total == 32  # Interrupted read retains its prepaid capacity.


def test_cancellation_keeps_owner_until_close_join_and_removal(tmp_path: Path) -> None:
    owner = proof.RequestOwner(tmp_path)
    read_fd, write_fd = os.pipe()
    try:
        with server(response(b"x", headers="Content-Length: 12\r\n"), hold=True) as (url, process):
            owner.start(proof.Request(url, 32, 12, media=True, loopback=True))
            assert process.stdout is not None and line(process.stdout) == b"delivered\n"
            os.write(write_fd, b"cancel")
            with pytest.raises(proof.ProofError, match="cancelled"):
                owner.wait(read_fd)
            assert owner.active is not None
            with pytest.raises(proof.ProofError, match="ownership_remains"):
                owner.start(proof.Request(url, 32, media=True, loopback=True))
            assert owner.cleanup() and owner.active is None
            assert not (tmp_path / "body").exists()
            with pytest.raises(proof.ProofError, match="media_attempt_limit"):
                owner.start(proof.Request(url, 32, media=True, loopback=True))
    finally:
        assert owner.close()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize("deadline", ["absolute", "connect"])
def test_delivered_terminal_result_is_not_process_completion(tmp_path: Path, deadline: str) -> None:
    owner = proof.RequestOwner(tmp_path)
    read_fd, write_fd = os.pipe()
    ready_read, ready_write = os.pipe()
    original = subprocess.Popen
    script = (
        "import os,sys; print('{}',flush=True); "
        "os.write(int(sys.argv[1]),b'x'); os.read(int(sys.argv[2]),1)"
    )

    def pending_process(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        return original(
            [sys.executable, "-c", script, str(ready_write), str(read_fd)],
            pass_fds=(ready_write, read_fd),
            stdout=subprocess.PIPE,
        )

    try:
        with patch.object(proof.subprocess, "Popen", side_effect=pending_process):
            owner.start(proof.Request("http://127.0.0.1:1/", 32, media=True, loopback=True))
        assert os.read(ready_read, 1) == b"x"
        assert owner.active is not None
        if deadline == "absolute":
            owner.active.deadline = 0
        else:
            owner.active.connect_deadline = 0
        with pytest.raises(proof.ProofError, match=deadline + "_deadline"):
            owner.wait()
        with pytest.raises(proof.ProofError, match="ownership_remains"):
            owner.start(proof.Request("http://127.0.0.1:1/", 32, loopback=True))
        assert owner.cleanup()
    finally:
        assert owner.close()
        for fd in (read_fd, write_fd, ready_read, ready_write):
            os.close(fd)


def test_read_return_before_counter_commit_retains_prepaid_bytes(tmp_path: Path) -> None:
    original_alarm = signal.getsignal(signal.SIGALRM)
    owner = proof.RequestOwner(tmp_path)
    counter = mmap.mmap(-1, 17)
    process = Mock()
    process.poll.return_value = 0
    process.stdout = io.BytesIO()
    owner.active = proof.OwnedRequest(cast(subprocess.Popen[bytes], process), counter, -1, 120)
    original_pack = struct.pack_into
    commits: list[int] = []

    def interrupt_commit(fmt: str, buffer: mmap.mmap, offset: int, value: int | float) -> None:
        if fmt == "d":
            original_pack(fmt, buffer, offset, value)
            return
        assert isinstance(value, int)
        commits.append(value)
        if len(commits) == 2:
            # The actual loopback read has returned three bytes, but its committed
            # count is not visible. This models abrupt child death at that boundary.
            raise SystemExit
        original_pack(fmt, buffer, offset, value)

    try:
        with (
            server(response(b"abc", headers="Content-Length: 3\r\n")) as (url, _),
            patch.object(proof.struct, "pack_into", side_effect=interrupt_commit),
            pytest.raises(SystemExit),
        ):
            proof.download(
                proof.Request(url, 32, 3, media=True, loopback=True), owner.directory_fd, counter
            )
        assert commits == [32, 3]
        assert struct.unpack_from("Q", counter)[0] == 32
        assert owner.cleanup() and owner.metadata_bytes == 32
        assert not (tmp_path / "body").exists()
    finally:
        signal.signal(signal.SIGALRM, original_alarm)
        assert owner.close()


def test_cleanup_debt_keeps_original_deadline_and_releases_only_after_join(tmp_path: Path) -> None:
    owner = proof.RequestOwner(tmp_path)
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("owned fixture", 3)
    process.stdout = io.BytesIO()
    counter = mmap.mmap(-1, 17)
    struct.pack_into("Q", counter, 0, 7)
    active = proof.OwnedRequest(cast(subprocess.Popen[bytes], process), counter, -1, 120)
    owner.active = active
    (tmp_path / "body").write_bytes(b"partial")
    with patch.object(proof.time, "monotonic", return_value=10):
        assert not owner.cleanup()
        assert active.cleanup_deadline == 13 and not active.cleanup_missed
    with patch.object(proof.time, "monotonic", return_value=14):
        assert not owner.cleanup()
        assert active.cleanup_deadline == 13 and active.cleanup_missed
        assert owner.active is active and (tmp_path / "body").exists()
        with pytest.raises(proof.ProofError, match="ownership_remains"):
            owner.start(proof.Request("http://127.0.0.1:1/", 32, loopback=True))
        process.poll.return_value = 0
        process.wait.side_effect = None
        assert owner.cleanup()
    assert owner.metadata_bytes == 7 and owner.cleanup_missed
    assert owner.active is None and not (tmp_path / "body").exists()
    assert owner.close()


def test_stalled_http_close_starts_original_deadline_before_child_exit(tmp_path: Path) -> None:
    owner = proof.RequestOwner(tmp_path)
    ready_read, ready_write = os.pipe()
    gate_read, gate_write = os.pipe()
    original = subprocess.Popen
    script = """
import os, sys
from sobafm import ccmixter_proof as proof
ready, gate = int(sys.argv[1]), int(sys.argv[2])
original = proof.http.client.HTTPResponse.close
def stalled_close(self):
    os.write(ready, b'x')
    os.read(gate, 1)
    original(self)
proof.http.client.HTTPResponse.close = stalled_close
sys.argv = ['proof', '--request-child', *sys.argv[3:]]
proof.main()
"""

    def stalled_process(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        command = cast(list[str], args[0])
        fds = cast(tuple[int, ...], kwargs["pass_fds"])
        return original(
            [sys.executable, "-c", script, str(ready_write), str(gate_read), *command[-3:]],
            pass_fds=(*fds, ready_write, gate_read),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    try:
        with server(response(b"abc", headers="Content-Length: 3\r\n")) as (url, _):
            with patch.object(proof.subprocess, "Popen", side_effect=stalled_process):
                owner.start(proof.Request(url, 32, 3, media=True, loopback=True))
            assert readiness.select([ready_read], [], [], 15)[0]
            assert os.read(ready_read, 1) == b"x"
            active = owner.active
            assert active is not None and active.process.poll() is None
            assert active.counter[8] == 2
            original_deadline = struct.unpack_from("d", active.counter, 9)[0]
            with patch.object(proof.time, "monotonic", return_value=original_deadline + 0.01):
                with pytest.raises(proof.ProofError, match="cleanup_deadline"):
                    owner.wait()
                assert active.cleanup_deadline == original_deadline
                owner.cleanup()
            active.process.wait(timeout=5)
            assert owner.cleanup() and owner.cleanup_missed
            assert active.cleanup_deadline == original_deadline
            assert owner.media_bytes == 3 and not (tmp_path / "body").exists()
    finally:
        assert owner.close()
        for fd in (ready_read, ready_write, gate_read, gate_write):
            os.close(fd)


def test_failed_unlink_retains_file_owner_and_accounting_once(tmp_path: Path) -> None:
    owner = proof.RequestOwner(tmp_path)
    try:
        with server(response(b"abc", headers="Content-Length: 3\r\n")) as (url, _):
            owner.start(proof.Request(url, 32, 3, media=True, loopback=True))
            assert owner.wait().verdict == "transferred"
        with patch.object(Path, "unlink", side_effect=PermissionError):
            assert not owner.cleanup() and owner.active is not None
            assert owner.media_bytes == 3
        assert owner.cleanup() and owner.media_bytes == 3
    finally:
        assert owner.close()


def valid_info() -> bytes:
    rows = cast(
        list[dict[str, object]],
        json.loads((CATALOG / "import-ccmixter-playback-observed.json").read_bytes()),
    )
    row = rows[0]
    files = cast(list[dict[str, object]], row["files"])
    files[0].update(download_url=proof.MEDIA, file_rawsize=proof.EXPECTED_BYTES)
    return json.dumps([row]).encode()


@pytest.mark.parametrize("change", ["route", "size", "chain", "license", "identity", "duplicate"])
def test_current_mix_conflicts_fail_closed(change: str) -> None:
    proof.validate_info(valid_info(), CATALOG)
    rows = cast(list[dict[str, object]], json.loads(valid_info()))
    files = cast(list[dict[str, object]], rows[0]["files"])
    if change == "route":
        files[0]["download_url"] = "https://example.invalid/other.mp3"
    elif change == "size":
        files[0]["file_rawsize"] = proof.EXPECTED_BYTES + 1
    elif change == "chain":
        rows[0]["remix_parents"] = []
    elif change == "license":
        rows[0]["license_url"] = "https://example.invalid/license"
    elif change == "identity":
        files[0]["file_id"] = 1
    else:
        rows.append(rows[0])
    with pytest.raises(proof.ProofError):
        proof.validate_info(json.dumps(rows).encode(), CATALOG)


def test_review_binds_exact_candidate_six_responses_and_window() -> None:
    candidate = parse_catalog((CATALOG / "ccmixter-playback-candidate.json").read_text())
    digest = metadata_digest(candidate.partitions[0].recordings[0])
    fingerprints = {name: hashlib.sha256(name.encode()).hexdigest() for name in proof.METADATA}
    data: dict[str, object] = {
        "schema": "sobafm-source-proof-review-1",
        "candidate_digest": digest,
        "responses": fingerprints,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "review_reference": "https://github.com/slackysoba/sobafm/issues/19#issuecomment-1",
    }
    assert proof.review_manifest(json.dumps(data).encode(), digest) == fingerprints
    assert not select(candidate, parse_query({"title": "WILD WINDSTEPS"}), ()).matches
    variations: tuple[dict[str, object], ...] = (
        {"candidate_digest": "x"},
        {"expires_at": "2000-01-01T00:00:00+00:00"},
        {"responses": {}},
        {"approved": True},
    )
    for changes in variations:
        with pytest.raises(proof.ProofError):
            proof.review_manifest(json.dumps(data | changes).encode(), digest)


def test_changed_metadata_cannot_reach_media_get(tmp_path: Path) -> None:
    owner = proof.RequestOwner(tmp_path)
    calls: list[proof.Request] = []

    def start(request: proof.Request) -> None:
        calls.append(request)
        (tmp_path / "body").write_bytes(b"changed notice")

    result = proof.Transfer("transferred", 14, 14, None, True, 200)
    try:
        with (
            patch.object(owner, "start", side_effect=start),
            patch.object(owner, "wait", return_value=result),
            pytest.raises(proof.ProofError, match="metadata_changed"),
        ):
            proof.run_proof(owner, CATALOG, {name: "0" * 64 for name in proof.METADATA})
        assert len(calls) == 1 and not calls[0].media
    finally:
        (tmp_path / "body").unlink(missing_ok=True)
        assert owner.close()


def test_default_cli_requires_explicit_opt_in_without_activity() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "sobafm.ccmixter_proof"], capture_output=True, timeout=5, check=False
    )
    assert completed.returncode != 0 and not completed.stdout
    assert b"--live-ccmixter" in completed.stderr


def test_request_limits_and_fixed_routes() -> None:
    base = proof.Request(proof.MEDIA, proof.BODY_CAP, proof.EXPECTED_BYTES, media=True)
    for request in (
        replace(base, url=proof.MEDIA + "?token=not-allowed"),
        replace(base, cap=proof.BODY_CAP + 1),
        replace(base, connect_seconds=11),
        replace(base, inactivity_seconds=21),
        replace(base, absolute_seconds=121),
        replace(base, media=False),
    ):
        with pytest.raises(proof.ProofError):
            request.validate()
