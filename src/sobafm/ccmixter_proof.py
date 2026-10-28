"""Opt-in, fixed ccMixter transport evaluation. Never grants catalog admission."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import mmap
import os
import re
import select
import signal
import struct
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import cast
from urllib.parse import urlsplit

from sobafm.catalog import Source, metadata_digest, parse_catalog
from sobafm.catalog_import import normalize, parse_context

HOST = "https://ccmixter.org"
MEDIA = HOST + "/content/AntSurvila/AntSurvila_-_WILD_WINDSTEPS.mp3"
EXPECTED_BYTES = 4_693_829
METADATA = {
    "mix_page": HOST + "/files/AntSurvila/70993",
    "parent_page": HOST + "/files/Dan_Mantau/70072",
    "root_page": HOST + "/files/lisadb/25112",
    "info": HOST + "/api/query?f=json&dataview=info&ids=70993&limit=1",
    "terms": HOST + "/terms",
    "about": HOST + "/about",
}
BODY_CAP = 10_000_000
METADATA_CAP = 500_000
RUN_SECONDS = 600.0
CLEANUP_SECONDS = 3.0
CHUNK = 65_536


class ProofError(ValueError):
    """Safe fixed failure code; never include provider text or local paths."""


@dataclass(frozen=True)
class Request:
    url: str
    cap: int
    expected: int | None = None
    media: bool = False
    connect_seconds: float = 10.0
    inactivity_seconds: float = 20.0
    absolute_seconds: float = 120.0
    loopback: bool = False

    def validate(self) -> None:
        parsed = urlsplit(self.url)
        if self.loopback:
            valid = parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
        else:
            valid = self.url in (*METADATA.values(), MEDIA)
        if not valid or parsed.username or parsed.password or parsed.fragment:
            raise ProofError("route_rejected")
        limit = BODY_CAP if self.media else METADATA_CAP
        if (
            not 0 < self.cap <= limit
            or self.expected is not None
            and not 0 <= self.expected <= self.cap
        ):
            raise ProofError("invalid_capacity")
        if not (
            0 < self.connect_seconds <= 10
            and 0 < self.inactivity_seconds <= 20
            and 0 < self.absolute_seconds <= 120
        ):
            raise ProofError("invalid_deadline")


@dataclass(frozen=True)
class Transfer:
    verdict: str
    received: int
    stored: int
    sha256: str | None
    transport_eof: bool
    status: int | None


def _alarm(_signum: int, _frame: FrameType | None) -> None:
    raise TimeoutError


def _headers(response: http.client.HTTPResponse, request: Request) -> int | None:
    # Ambiguous framing, encodings and partial responses are never evidence of this file.
    if response.status != 200:
        raise ProofError("http_status_rejected")
    if response.getheader("Location") is not None or response.getheader("WWW-Authenticate"):
        raise ProofError("redirect_or_auth_rejected")
    if response.getheader("Content-Range") is not None:
        raise ProofError("partial_response_rejected")
    if response.getheader("Content-Encoding", "identity").lower() != "identity":
        raise ProofError("encoding_rejected")
    length_headers = response.headers.get_all("Content-Length", [])
    transfer_headers = response.headers.get_all("Transfer-Encoding", [])
    if len(length_headers) > 1 or len(transfer_headers) > 1:
        raise ProofError("ambiguous_framing")
    if transfer_headers and (length_headers or transfer_headers[0].lower() != "chunked"):
        raise ProofError("ambiguous_framing")
    length: int | None = None
    if length_headers:
        if re.fullmatch(r"[0-9]{1,10}", length_headers[0]) is None:
            raise ProofError("invalid_length")
        length = int(length_headers[0])
        if length > request.cap:
            raise ProofError("advertised_oversize")
        if request.expected is not None and length != request.expected:
            raise ProofError("advertised_size_conflict")
    content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
    allowed = {"audio/mpeg"} if request.media else {"text/html", "application/json"}
    if content_type not in allowed:
        raise ProofError("content_type_rejected")
    return length


def download(request: Request, directory_fd: int, counter: mmap.mmap) -> Transfer:
    """Only the child calls HTTP. The parent owns its lifetime and sole reserved file."""
    request.validate()
    signal.signal(signal.SIGALRM, _alarm)
    deadline = time.monotonic() + request.absolute_seconds
    connection: http.client.HTTPConnection | None = None
    response: http.client.HTTPResponse | None = None
    fd: int | None = None
    received = stored = 0
    status: int | None = None
    digest = hashlib.sha256()
    verdict, eof = "transport_error", False
    try:
        parsed = urlsplit(request.url)
        connection_type = (
            http.client.HTTPConnection if request.loopback else http.client.HTTPSConnection
        )
        connection = connection_type(
            parsed.hostname or "", parsed.port, timeout=request.connect_seconds
        )
        signal.setitimer(signal.ITIMER_REAL, min(request.connect_seconds, request.absolute_seconds))
        connection.connect()  # The alarm also bounds DNS and TLS, not just socket.connect().
        counter[8] = 1
        assert connection.sock is not None
        connection.sock.settimeout(request.inactivity_seconds)
        signal.setitimer(signal.ITIMER_REAL, max(0.000001, deadline - time.monotonic()))
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection.request(
            "GET", path, headers={"Accept-Encoding": "identity", "Connection": "close"}
        )
        response = connection.getresponse()
        status = response.status
        advertised = _headers(response, request)
        fd = os.open(
            "body", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd
        )
        while True:
            # No overflow probe: reaching the cap without framed completion fails closed.
            remaining = request.cap - received
            if not remaining:
                raise ProofError("body_cap_reached")
            read_limit = min(CHUNK, remaining)
            # Killing a read can precede its count commit. Precharge capacity;
            # only a completed read refunds the part it did not receive.
            struct.pack_into("Q", counter, 0, received + read_limit)
            try:
                data = response.read1(read_limit)
            except http.client.IncompleteRead as error:
                received += len(error.partial)
                struct.pack_into("Q", counter, 0, received)
                raise ProofError("truncated_body") from error
            received += len(data)
            struct.pack_into("Q", counter, 0, received)  # Failed/partial bodies count too.
            if not data:
                eof = True
                break
            count = os.write(fd, data)
            if count != len(data):
                raise ProofError("storage_write_failed")
            stored += count
            digest.update(data)
            if response.isclosed():
                eof = True
                break
        if not eof or advertised is not None and received != advertised:
            raise ProofError("truncated_body")
        if request.expected is not None and received != request.expected:
            raise ProofError("size_conflict")
        verdict = "transferred"
    except ProofError as error:
        verdict = str(error)
    except TimeoutError:
        verdict = "request_timeout"
    except OSError, http.client.HTTPException, ValueError:
        verdict = "transport_error"
    finally:
        # Publish the original close deadline before any close/join can stall.
        # The bounded stdout marker wakes the supervisor even without a final result.
        struct.pack_into("d", counter, 9, time.monotonic() + CLEANUP_SECONDS)
        counter[8] = 2
        os.write(1, b".")
        signal.setitimer(signal.ITIMER_REAL, CLEANUP_SECONDS)
        if fd is not None:
            os.close(fd)
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
        signal.setitimer(signal.ITIMER_REAL, 0)
    return Transfer(verdict, received, stored, digest.hexdigest() if eof else None, eof, status)


@dataclass
class OwnedRequest:
    process: subprocess.Popen[bytes]
    counter: mmap.mmap
    pid_fd: int
    deadline: float
    connect_deadline: float = 0
    cleanup_deadline: float | None = None
    cleanup_missed: bool = False
    accounted: bool = False
    response: bytearray = field(default_factory=bytearray)
    stdout_eof: bool = False


class RequestOwner:
    """A single reservation; cancellation/deadlines do not release it."""

    def __init__(self, directory: Path, *, run_seconds: float = RUN_SECONDS) -> None:
        if (directory / "body").exists():
            raise ProofError("unresolved_previous_file")
        self.directory = directory
        self.directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.active: OwnedRequest | None = None
        self.run_deadline = time.monotonic() + min(RUN_SECONDS, run_seconds)
        self.metadata_bytes = 0
        self.media_bytes = 0
        self.metadata_requests = 0
        self.media_requests = 0
        self.cleanup_missed = False
        self.request_evidence: list[dict[str, object]] = []
        self._media = False
        self._closed = False

    def start(self, request: Request) -> None:
        request.validate()
        if self._closed or self.active is not None or (self.directory / "body").exists():
            raise ProofError("ownership_remains")
        if time.monotonic() >= self.run_deadline:
            raise ProofError("run_deadline")
        # Admission reserves the entire per-request storage/transfer capacity before spawn.
        if request.media:
            if self.media_requests or self.media_bytes + request.cap > 20_000_000:
                raise ProofError("media_attempt_limit")
            self.media_requests += 1  # This first component deliberately implements zero retries.
        else:
            if self.metadata_requests >= 6 or self.metadata_bytes + request.cap > 3_000_000:
                raise ProofError("metadata_limit")
            self.metadata_requests += 1
        self._media = request.media
        counter_fd = os.memfd_create("source-proof-count", os.MFD_CLOEXEC)
        try:
            os.ftruncate(counter_fd, 17)
            counter = mmap.mmap(counter_fd, 17)
        except BaseException:
            os.close(counter_fd)
            raise
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "sobafm.ccmixter_proof",
                    "--request-child",
                    str(counter_fd),
                    str(self.directory_fd),
                    json.dumps(asdict(request)),
                ],
                pass_fds=(counter_fd, self.directory_fd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.active = OwnedRequest(
                process,
                counter,
                -1,
                min(self.run_deadline, started + request.absolute_seconds),
                started + request.connect_seconds,
            )
            # Linux pidfds let the supervisor wait for actual exit, not stdout delivery.
            self.active.pid_fd = os.pidfd_open(process.pid)
        except BaseException:
            if self.active is None:
                counter.close()
            else:
                self.cleanup()
            raise
        finally:
            os.close(counter_fd)

    def wait(self, cancel_fd: int | None = None) -> Transfer:
        active = self.active
        if active is None:
            raise ProofError("no_request")
        assert active.process.stdout is not None
        while active.process.poll() is None:
            now = time.monotonic()
            if active.cleanup_deadline is None and active.counter[8] == 2:
                active.cleanup_deadline = struct.unpack_from("d", active.counter, 9)[0]
            deadline = active.cleanup_deadline or active.deadline
            remaining = deadline - now
            if remaining <= 0:
                raise ProofError(
                    "cleanup_deadline" if active.cleanup_deadline else "absolute_deadline"
                )
            if not active.counter[8]:
                if now >= active.connect_deadline:
                    raise ProofError("connect_deadline")
                remaining = min(remaining, active.connect_deadline - now)
            readers = [active.pid_fd] + ([] if cancel_fd is None else [cancel_fd])
            if not active.stdout_eof:
                readers.append(active.process.stdout.fileno())
            ready, _, _ = select.select(readers, [], [], remaining)
            if cancel_fd is not None and cancel_fd in ready:
                raise ProofError("cancelled")
            if active.process.stdout.fileno() in ready:
                data = os.read(active.process.stdout.fileno(), 4096 - len(active.response))
                active.stdout_eof = not data
                active.response.extend(data)
                if len(active.response) >= 4096:
                    raise ProofError("result_oversize")
        active.process.wait()
        self._account(active)
        active.response.extend(active.process.stdout.read(4096 - len(active.response)))
        data = bytes(active.response)
        if active.process.returncode != 0 or not data.startswith(b".") or len(data) >= 4096:
            raise ProofError("request_child_failed")
        result = Transfer(**cast(dict[str, object], json.loads(data[1:])))  # type: ignore[arg-type]
        if result.received > struct.unpack_from("Q", active.counter)[0]:
            raise ProofError("counter_conflict")
        return result

    def _account(self, active: OwnedRequest) -> None:
        if active.accounted:
            return
        if active.process.poll() is None:
            raise ProofError("counter_still_owned")
        count = struct.unpack_from("Q", active.counter)[0]
        if self._media:
            self.media_bytes += count
        else:
            self.metadata_bytes += count
        active.accounted = True

    def cleanup(self) -> bool:
        active = self.active
        if active is None:
            return True
        if active.cleanup_deadline is None:
            active.cleanup_deadline = (
                struct.unpack_from("d", active.counter, 9)[0]
                if active.counter[8] == 2
                else time.monotonic() + CLEANUP_SECONDS
            )
        try:
            if active.process.poll() is None:
                active.process.kill()
            active.process.wait(timeout=max(0, active.cleanup_deadline - time.monotonic()))
            self._account(active)
            if active.process.stdout is not None:
                active.process.stdout.close()
            (self.directory / "body").unlink(missing_ok=True)
            if (self.directory / "body").exists():
                raise OSError
        except OSError, subprocess.TimeoutExpired:
            active.cleanup_missed |= time.monotonic() >= active.cleanup_deadline
            self.cleanup_missed |= active.cleanup_missed
            return False
        active.cleanup_missed |= time.monotonic() > active.cleanup_deadline
        self.cleanup_missed |= active.cleanup_missed
        if active.pid_fd >= 0:
            os.close(active.pid_fd)
        active.counter.close()
        self.active = None
        return True

    def close(self) -> bool:
        if not self.cleanup():
            return False
        if not self._closed:
            os.close(self.directory_fd)
            self._closed = True
        return True


def bounded_read(path: Path, cap: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise ProofError("local_input_oversize")
    return data


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProofError("invalid_review_or_metadata")
    return cast(dict[str, object], value)


def _json(data: bytes) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ProofError("duplicate_json_key")
            result[key] = value
        return result

    return cast(object, json.loads(data, object_pairs_hook=unique))


def review_manifest(data: bytes, candidate_digest: str) -> dict[str, str]:
    review = _object(_json(data))
    if set(review) != {"schema", "candidate_digest", "responses", "expires_at", "review_reference"}:
        raise ProofError("invalid_review")
    if (
        review["schema"] != "sobafm-source-proof-review-1"
        or review["candidate_digest"] != candidate_digest
    ):
        raise ProofError("review_candidate_conflict")
    reference = review["review_reference"]
    if (
        not isinstance(reference, str)
        or re.fullmatch(
            r"https://github\.com/slackysoba/sobafm/(?:issues|pull)/[0-9]+#(?:issuecomment|pullrequestreview)-[0-9]+",
            reference,
        )
        is None
    ):
        raise ProofError("review_reference_missing")
    expires = review["expires_at"]
    if not isinstance(expires, str):
        raise ProofError("review_expired")
    expiry = datetime.fromisoformat(expires)
    if expiry.tzinfo is None or not 0 < (expiry - datetime.now(UTC)).total_seconds() <= RUN_SECONDS:
        raise ProofError("review_expired")
    responses = _object(review["responses"])
    if set(responses) != set(METADATA) or any(
        not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None
        for v in responses.values()
    ):
        raise ProofError("invalid_response_fingerprints")
    return cast(dict[str, str], responses)


def _projection(current: object, expected: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(current, dict) and all(
            key in current and _projection(cast(dict[str, object], current)[key], value)
            for key, value in cast(dict[str, object], expected).items()
        )
    if isinstance(expected, list):
        return (
            isinstance(current, list)
            and len(cast(list[object], current)) == len(cast(list[object], expected))
            and all(
                _projection(a, b)
                for a, b in zip(
                    cast(list[object], current), cast(list[object], expected), strict=True
                )
            )
        )
    return type(current) is type(expected) and current == expected


def validate_info(data: bytes, catalog_directory: Path) -> None:
    rows = _json(data)
    expected = _json(
        bounded_read(catalog_directory / "import-ccmixter-playback-observed.json", METADATA_CAP)
    )
    if (
        not isinstance(rows, list)
        or len(cast(list[object], rows)) != 1
        or not isinstance(expected, list)
    ):
        raise ProofError("metadata_identity_conflict")
    row = _object(cast(list[object], rows)[0])
    if not _projection(row, cast(list[object], expected)[0]):
        raise ProofError("metadata_identity_conflict")
    context = parse_context(
        bounded_read(catalog_directory / "import-ccmixter-playback-context.json", 10_000).decode()
    )
    imported = normalize(Source.CCMIXTER, data.decode("utf-8"), context)
    if (
        len(imported.catalog.partitions[0].recordings) != 1
        or imported.report.records[0].source_file_id != "129564"
    ):
        raise ProofError("metadata_import_rejected")
    files = cast(list[object], row["files"])
    file = _object(files[0])
    if (
        file.get("download_url") != MEDIA
        or type(file.get("file_rawsize")) is not int
        or file.get("file_rawsize") != EXPECTED_BYTES
    ):
        raise ProofError("route_or_size_conflict")


def run_proof(
    owner: RequestOwner,
    catalog_directory: Path,
    fingerprints: dict[str, str],
    cancel_fd: int | None = None,
) -> Transfer:
    for name, url in METADATA.items():
        _check_cancel(cancel_fd)
        owner.start(Request(url, METADATA_CAP))
        try:
            result = owner.wait(cancel_fd)
            owner.request_evidence.append(
                {
                    "request": name,
                    **asdict(result),
                    "review_match": result.sha256 == fingerprints[name],
                }
            )
            if result.verdict != "transferred":
                raise ProofError(result.verdict)
            body = bounded_read(owner.directory / "body", METADATA_CAP)
            if hashlib.sha256(body).hexdigest() != fingerprints[name]:
                raise ProofError("metadata_changed")
            if name == "info":
                validate_info(body, catalog_directory)
        finally:
            if not owner.cleanup() or owner.cleanup_missed:
                raise ProofError("cleanup_failed")
    _check_cancel(cancel_fd)
    owner.start(Request(MEDIA, BODY_CAP, EXPECTED_BYTES, media=True))
    try:
        result = owner.wait(cancel_fd)
        owner.request_evidence.append({"request": "media", **asdict(result)})
        return result
    finally:
        if not owner.cleanup() or owner.cleanup_missed:
            raise ProofError("cleanup_failed")


def _check_cancel(cancel_fd: int | None) -> None:
    if cancel_fd is not None and select.select([cancel_fd], [], [], 0)[0]:
        raise ProofError("cancelled")


def main() -> None:
    if not sys.platform.startswith("linux"):
        raise SystemExit("Linux only")
    if len(sys.argv) == 5 and sys.argv[1] == "--request-child":
        counter_fd, directory_fd = int(sys.argv[2]), int(sys.argv[3])
        request = Request(**cast(dict[str, object], json.loads(sys.argv[4])))  # type: ignore[arg-type]
        with mmap.mmap(counter_fd, 17) as counter:
            result = download(request, directory_fd, counter)
            os.close(directory_fd)
            os.close(counter_fd)
            print(json.dumps(asdict(result)), flush=True)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-ccmixter", action="store_true", required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--catalog-directory", type=Path, required=True)
    args = parser.parse_args()
    # The concrete invocation supplies a private tmpfs here; no arbitrary persistent audio path.
    directory = Path("/run/sobafm-source-proof")
    owner: RequestOwner | None = None
    retention_deadline = time.monotonic() + 3600
    cancel_read, cancel_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)

    def cancel(_signum: int, _frame: FrameType | None) -> None:
        with suppress(BlockingIOError):
            os.write(cancel_write, b"x")

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    evidence: dict[str, object] = {
        "schema": "sobafm-source-proof-result-1",
        "started_at": datetime.now(UTC).isoformat(),
        "transport": "not_started",
        "decode": "pending",
        "file_integrity": "pending",
        "embedded_notices": "pending",
        "audible_drain": "unobserved",
        "admission": "not_granted",
    }
    lock_fd: int | None = None
    try:
        import fcntl

        if (
            directory.is_symlink()
            or directory.stat().st_uid != os.getuid()
            or directory.stat().st_mode & 0o077
        ):
            raise ProofError("private_directory_required")
        lock_fd = os.open(directory / "owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        candidate = parse_catalog(
            bounded_read(
                args.catalog_directory / "ccmixter-playback-candidate.json", METADATA_CAP
            ).decode()
        )
        record = candidate.partitions[0].recordings[0]
        if (
            record.key.source != Source.CCMIXTER
            or record.key.recording_id != "70993"
            or record.availability != "unknown"
        ):
            raise ProofError("candidate_conflict")
        evidence["candidate_digest"] = metadata_digest(record)
        review = bounded_read(args.review, 10_000)
        fingerprints = review_manifest(review, metadata_digest(record))
        evidence["review_sha256"] = hashlib.sha256(review).hexdigest()
        owner = RequestOwner(directory)
        expiry = datetime.fromisoformat(cast(str, _object(_json(review))["expires_at"]))
        owner.run_deadline = min(
            owner.run_deadline, time.monotonic() + (expiry - datetime.now(UTC)).total_seconds()
        )
        result = run_proof(owner, args.catalog_directory, fingerprints, cancel_read)
        evidence["transport"] = asdict(result)
    except ProofError as error:
        evidence["failure"] = str(error)
    except OSError, ValueError, IndexError, KeyError, TypeError, RecursionError:
        evidence["failure"] = "invalid_input_or_operation_failed"
    finally:
        if owner is not None:
            if owner.media_requests and evidence["transport"] == "not_started":
                # Cleanup failure must not erase an already observed transport fact.
                evidence["transport"] = next(
                    (
                        {key: value for key, value in item.items() if key != "request"}
                        for item in owner.request_evidence
                        if item["request"] == "media"
                    ),
                    "incomplete",
                )
            released = owner.close()
            evidence.update(
                cleanup_released=released,
                cleanup_deadline_met=not owner.cleanup_missed,
                metadata_bytes=owner.metadata_bytes,
                media_bytes=owner.media_bytes,
                metadata_requests=owner.metadata_requests,
                media_requests=owner.media_requests,
                requests=owner.request_evidence,
            )
            if not released:
                # Emit one safe checkpoint, then retain the owner and lock until actual release.
                print(json.dumps(evidence), flush=True)
                backstop_reported = False
                while not owner.close():
                    if not backstop_reported and time.monotonic() >= retention_deadline:
                        print(
                            json.dumps(
                                {"failure": "retention_backstop_failed", "cleanup_released": False}
                            ),
                            flush=True,
                        )
                        backstop_reported = True
                    select.select([], [], [], 1.0)
                evidence.update(
                    cleanup_released=True,
                    cleanup_deadline_met=False,
                    metadata_bytes=owner.metadata_bytes,
                    media_bytes=owner.media_bytes,
                )
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(cancel_read)
        os.close(cancel_write)
    evidence["finished_at"] = datetime.now(UTC).isoformat()
    print(json.dumps(evidence), flush=True)
    raise SystemExit(
        0
        if "failure" not in evidence
        and isinstance(evidence["transport"], dict)
        and evidence["transport"]["verdict"] == "transferred"
        else 1
    )


if __name__ == "__main__":
    main()
