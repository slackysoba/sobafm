"""One opt-in local Night Owl copy; pending evidence never grants catalog admission."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import sys
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast

from sobafm.catalog import Catalog, CatalogInputError, Source, metadata_digest, parse_catalog

MAX_BYTES = 20_000_000
CHUNK_BYTES = 65_536
MAX_MANIFEST_BYTES = 16_384
CLEANUP_SECONDS = 3.0
RECORDING_ID = (
    "/music/Broke_For_Free/Directionless_EP/Broke_For_Free_-_Directionless_EP_-_01_Night_Owl"
)
SOURCE_URL = "https://freemusicarchive.org" + RECORDING_ID + "/"
# Public parsed catalog digest; this is an integrity binding, not a credential.
METADATA_DIGEST = (
    "741591d22c948f3caf790285d74ac4d855d5732b76463babe41a8ab73e1198bb"  # pragma: allowlist secret
)
CREDIT_SNAPSHOT = "fma-aba6502c18a4540d8394516aca393cbba8dda7c4dfa417f97aa379e0e366d483"


class IntakeError(ValueError):
    """A bounded public error code, without personal paths or raw OS diagnostics."""


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(cast(dict[str, object], value)) != keys:
        raise IntakeError("invalid_fields")
    return cast(dict[str, object], value)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise IntakeError("duplicate_key")
        result[key] = value
    return result


def _decode(payload: bytes) -> object:
    if len(payload) > MAX_MANIFEST_BYTES:
        raise IntakeError("manifest_oversize")
    try:
        return cast(object, json.loads(payload, object_pairs_hook=_pairs))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise IntakeError("invalid_json") from error


def _utc(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value
    ):
        raise IntakeError("invalid_utc_timestamp")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise IntakeError("invalid_utc_timestamp") from error


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _integer(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or value < int(positive):
        raise IntakeError("invalid_file_identity")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise IntakeError("invalid_digest")
    return value


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    bytes: int
    mtime_ns: int
    ctime_ns: int
    sha256: str

    def matches(self, observed: os.stat_result) -> bool:
        return (
            stat.S_ISREG(observed.st_mode)
            and observed.st_nlink == 1
            and (observed.st_dev, observed.st_ino, observed.st_size)
            == (self.device, self.inode, self.bytes)
            and (observed.st_mtime_ns, observed.st_ctime_ns) == (self.mtime_ns, self.ctime_ns)
        )


@dataclass(frozen=True)
class Acquisition:
    retention_anchor_utc: str
    basis: Literal["exact", "conservative_lower_bound"]
    operator_evidence: str | None

    def validate(self, *, legacy: bool = False) -> None:
        _utc(self.retention_anchor_utc)
        if legacy:
            if self.basis != "exact" or self.operator_evidence is not None:
                raise IntakeError("v1_requires_original_exact_time")
            return
        evidence = self.operator_evidence
        if (
            self.basis not in ("exact", "conservative_lower_bound")
            or not isinstance(evidence, str)
            or not 1 <= len(evidence) <= 1024
            or len(_encoded(evidence)) > 4096
            or evidence != evidence.strip()
            or any(unicodedata.category(c).startswith("C") for c in evidence)
        ):
            raise IntakeError("invalid_acquisition_evidence")

    def record(self, *, safe: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": "sobafm-acquisition-summary-1" if safe else "sobafm-acquisition-1",
            "retention_anchor_utc": self.retention_anchor_utc,
            "basis": self.basis,
        }
        if safe:
            result["operator_evidence_sha256"] = hashlib.sha256(
                _encoded(self.operator_evidence)
            ).hexdigest()
        else:
            result["operator_evidence"] = self.operator_evidence
        return result


@dataclass(frozen=True)
class IntakeRequest:
    source_url: str
    metadata_digest: str
    credit_snapshot: str
    acquisition: Acquisition
    expires_at: str
    download_notices: tuple[str, ...]
    file: FileIdentity
    version: Literal[1, 2] = 2

    def time_fields(self, *, safe: bool = False) -> dict[str, object]:
        if self.version == 1:
            return {"acquired_at": self.acquisition.retention_anchor_utc}
        return {"acquisition": self.acquisition.record(safe=safe)}

    def manifest(self) -> dict[str, object]:
        return {
            "schema": f"sobafm-fma-intake-{self.version}",
            "source_url": self.source_url,
            "metadata_digest": self.metadata_digest,
            "credit_snapshot": self.credit_snapshot,
            **self.time_fields(),
            "expires_at": self.expires_at,
            "download_notices": self.download_notices,
            "file": asdict(self.file),
        }

    def validate(self, now: datetime, *, recovery: bool = False) -> None:
        if (
            self.source_url != SOURCE_URL
            or self.metadata_digest != METADATA_DIGEST
            or self.credit_snapshot != CREDIT_SNAPSHOT
        ):
            raise IntakeError("metadata_credit_mismatch")
        if type(self.version) is not int or self.version not in (1, 2):
            raise IntakeError("invalid_request_version")
        self.acquisition.validate(legacy=self.version == 1)
        acquired, expires = _utc(self.acquisition.retention_anchor_utc), _utc(self.expires_at)
        if (
            now.tzinfo is None
            or acquired > now
            or not acquired < expires <= acquired + timedelta(days=7)
        ):
            raise IntakeError("invalid_retention_window")
        if not recovery and now >= expires:
            raise IntakeError("expired")
        if (
            not 1 <= len(self.download_notices) <= 16
            or len(_encoded(self.download_notices)) > 4096
            or any(
                not 1 <= len(n) <= 1024
                or n != n.strip()
                or any(unicodedata.category(c).startswith("C") for c in n)
                for n in self.download_notices
            )
        ):
            raise IntakeError("invalid_notices")
        for value in (self.file.device, self.file.mtime_ns, self.file.ctime_ns):
            _integer(value)
        _integer(self.file.inode, positive=True)
        _integer(self.file.bytes, positive=True)
        _digest(self.file.sha256)
        if self.file.bytes > MAX_BYTES:
            raise IntakeError("input_oversize")

    @property
    def fingerprint(self) -> str:
        fields = self.manifest()
        if self.version == 1:
            # Preserve historical v1 fingerprints byte-for-byte; never reinterpret time.
            del fields["schema"]
        return hashlib.sha256(_encoded(fields)).hexdigest()


def parse_request(
    payload: bytes, catalog: Catalog, now: datetime, *, recovery: bool = False
) -> IntakeRequest:
    raw = _decode(payload)
    if not isinstance(raw, dict):
        raise IntakeError("invalid_fields")
    schema = cast(dict[str, object], raw).get("schema")
    if schema not in ("sobafm-fma-intake-1", "sobafm-fma-intake-2"):
        raise IntakeError("invalid_request_version")
    version: Literal[1, 2] = 1 if schema == "sobafm-fma-intake-1" else 2
    time_key = "acquired_at" if version == 1 else "acquisition"
    data = _object(
        cast(dict[str, object], raw),
        {
            "schema",
            "source_url",
            "metadata_digest",
            "credit_snapshot",
            time_key,
            "expires_at",
            "download_notices",
            "file",
        },
    )
    if data["source_url"] != SOURCE_URL:
        raise IntakeError("wrong_recording")
    recordings = [
        r
        for p in catalog.partitions
        for r in p.recordings
        if r.key.source == Source.FMA and r.key.recording_id == RECORDING_ID
    ]
    if (
        len(recordings) != 1
        or metadata_digest(recordings[0]) != METADATA_DIGEST
        or recordings[0].credits is None
        or recordings[0].credits.snapshot_id != CREDIT_SNAPSHOT
        or data["metadata_digest"] != METADATA_DIGEST
        or data["credit_snapshot"] != CREDIT_SNAPSHOT
    ):
        raise IntakeError("metadata_credit_mismatch")
    if version == 1:
        acquisition = Acquisition(_timestamp(_utc(data["acquired_at"])), "exact", None)
    else:
        record = _object(
            data["acquisition"], {"schema", "retention_anchor_utc", "basis", "operator_evidence"}
        )
        if (
            record["schema"] != "sobafm-acquisition-1"
            or record["basis"] not in ("exact", "conservative_lower_bound")
            or not isinstance(record["operator_evidence"], str)
        ):
            raise IntakeError("invalid_acquisition_evidence")
        acquisition = Acquisition(
            _timestamp(_utc(record["retention_anchor_utc"])),
            record["basis"],
            record["operator_evidence"],
        )
    raw_notices = data["download_notices"]
    if not isinstance(raw_notices, list) or not 1 <= len(cast(list[object], raw_notices)) <= 16:
        raise IntakeError("invalid_notices")
    notices: list[str] = []
    for notice in cast(list[object], raw_notices):
        if (
            not isinstance(notice, str)
            or not 1 <= len(notice) <= 1024
            or notice != notice.strip()
            or any(unicodedata.category(c).startswith("C") for c in notice)
        ):
            raise IntakeError("invalid_notices")
        notices.append(notice)
    if len(_encoded(notices)) > 4096:
        raise IntakeError("notices_oversize")
    raw_file = _object(data["file"], {"device", "inode", "bytes", "mtime_ns", "ctime_ns", "sha256"})
    identity = FileIdentity(
        _integer(raw_file["device"]),
        _integer(raw_file["inode"], positive=True),
        _integer(raw_file["bytes"], positive=True),
        _integer(raw_file["mtime_ns"]),
        _integer(raw_file["ctime_ns"]),
        _digest(raw_file["sha256"]),
    )
    if identity.bytes > MAX_BYTES:
        raise IntakeError("input_oversize")
    request = IntakeRequest(
        SOURCE_URL,
        METADATA_DIGEST,
        CREDIT_SNAPSHOT,
        acquisition,
        _timestamp(_utc(data["expires_at"])),
        tuple(notices),
        identity,
        version,
    )
    request.validate(now, recovery=recovery)
    return request


class ReadConsumer(Protocol):
    """Initially inert. Own all downstream work/backing until join returns True.

    write consumes one bounded chunk synchronously; retained copies must have an
    explicit consumer capacity reservation. cancel must be nonblocking and may
    overlap write/finish/join. No path opening, preexec_fn, or fork of Python
    native-proof code from this multithreaded process is part of this contract.
    """

    def inspect(self, fd: int) -> None:
        """Borrow the owned read-only fd; settle inspection seeks/reads before return."""
        ...

    def write(self, chunk: bytes) -> None: ...
    def finish(self) -> None: ...
    def cancel(self) -> None: ...
    def join(self, timeout: float) -> bool: ...


class OwnedRead:
    """One capability returned by LocalIntake.read_into; exposes no path or fd."""

    def __init__(self, owner: LocalIntake, consumer: ReadConsumer, expected: FileIdentity) -> None:
        self._owner, self._consumer, self._expected = owner, consumer, expected
        self._thread: threading.Thread | None = None
        self._cancel_thread: threading.Thread | None = None
        self._fd: int | None = None
        self._io: str | None = None
        self._error: str | None = None
        self._read_bytes = 0
        self._delivered_bytes = 0
        self._sha256: str | None = None
        self._verified = False
        self._consumer_released = False
        self._finished = False
        self._completed = False
        self._retry = threading.Event()
        self._started = threading.Event()

    def _start(self) -> None:
        with self._owner.lock:
            self._owner._check_stop()  # pyright: ignore[reportPrivateUsage]
            self._thread = threading.Thread(target=self._run, name="fma-owned-read", daemon=False)
            try:
                self._thread.start()
            except RuntimeError as error:
                self._thread = None
                self._consumer_released = self._finished = True
                raise IntakeError("reader_start_failed") from error
            finally:
                self._started.set()

    def _admit(self, operation: str) -> None:
        # Linearize ownership, then release the gate before any blocking call.
        with self._owner.lock:
            self._owner._check_stop()  # pyright: ignore[reportPrivateUsage]
            self._io = operation

    def _check_identity(self) -> None:
        assert self._fd is not None
        if (fcntl.fcntl(self._fd, fcntl.F_GETFL) & os.O_ACCMODE) != os.O_RDONLY:
            raise IntakeError("reader_descriptor_changed")
        if not self._expected.matches(os.fstat(self._fd)) or not self._expected.matches(
            os.stat("payload", dir_fd=self._owner.fds["directory"], follow_symlinks=False)
        ):
            raise IntakeError("reader_payload_changed")

    def _cancel(self) -> None:
        with self._owner.lock:
            if self._thread is None:
                # Registration was revoked before any consumer operation. It is inert.
                self._consumer_released = self._finished = True
                self._started.set()
            elif self._cancel_thread is None and not self._consumer_released:
                self._cancel_thread = threading.Thread(
                    target=self._cancel_consumer, name="fma-consumer-cancel", daemon=False
                )
                try:
                    self._cancel_thread.start()
                except RuntimeError:
                    self._cancel_thread = None
                    self._error = self._error or "consumer_cancel_start_failed"

    def _cancel_consumer(self) -> None:
        try:
            self._consumer.cancel()
        except Exception:
            self._error = "consumer_cancel_failed"
        finally:
            self._owner.wake.set()

    def join(self, timeout: float = 0) -> bool:
        """Observe actual read/cancel worker exit; this does not request cancellation."""
        deadline = time.monotonic() + max(0, timeout)
        if not self._started.wait(max(0, deadline - time.monotonic())):
            return False
        # Registration and Thread.start share this gate. Snapshot only a settled
        # start outcome, including failure, without exceeding the caller's wait.
        if not self._owner.lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            workers = (self._thread, self._cancel_thread)
        finally:
            self._owner.lock.release()
        for worker in workers:
            if worker is not None:
                worker.join(max(0, deadline - time.monotonic()))
        # Cancellation can register after the snapshot; its start and actual
        # exit must also be observed before certifying release.
        if not self._owner.lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            settled = (
                self._finished
                and self._consumer_released
                and self._fd is None
                and (self._thread is None or not self._thread.is_alive())
                and (self._cancel_thread is None or not self._cancel_thread.is_alive())
            )
            if settled and self._verified and self._error is None and not self._owner.stop.is_set():
                self._completed = True
            return settled
        finally:
            self._owner.lock.release()

    def report(self) -> dict[str, object]:
        with self._owner.lock:
            settled = self.join()
            return {
                "request_sha256": self._owner.request.fingerprint,
                "expected_bytes": self._expected.bytes,
                "expected_sha256": self._expected.sha256,
                "payload_identity": [self._expected.device, self._expected.inode],
                "read_bytes": self._read_bytes,
                "delivered_bytes": self._delivered_bytes,
                "read_sha256": self._sha256,
                "stream_verified": self._verified,
                "completed": self._completed,
                "failure": self._error,
                "in_flight": self._io,
                "descriptor_retained": self._fd is not None,
                "consumer_released": self._consumer_released,
                "settled": settled,
                "reader_alive": self._thread is not None and self._thread.is_alive(),
                "cancel_alive": self._cancel_thread is not None and self._cancel_thread.is_alive(),
            }

    def _run(self) -> None:
        try:
            self._admit("reader_open")
            self._fd = os.open(
                "payload",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=self._owner.fds["directory"],
            )
            self._check_identity()
            self._admit("consumer_inspect")
            self._consumer.inspect(self._fd)
            self._check_identity()
            self._admit("reader_rewind")
            os.lseek(self._fd, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            while True:
                self._admit("reader_read")
                chunk = os.read(
                    self._fd, min(CHUNK_BYTES, self._expected.bytes - self._read_bytes + 1)
                )
                self._read_bytes += len(chunk)
                self._check_identity()
                self._owner._check_stop()  # pyright: ignore[reportPrivateUsage]
                if self._read_bytes > self._expected.bytes:
                    raise IntakeError("reader_input_oversize")
                digest.update(chunk)
                if not chunk:
                    break
                self._admit("consumer_write")
                self._consumer.write(chunk)
                self._delivered_bytes += len(chunk)
                chunk = b""
            self._sha256 = digest.hexdigest()
            if self._read_bytes != self._expected.bytes or self._sha256 != self._expected.sha256:
                raise IntakeError("reader_integrity_failed")
            self._admit("consumer_finish")
            self._verified = True
            self._consumer.finish()
            self._owner._check_stop()  # pyright: ignore[reportPrivateUsage]
        except IntakeError as error:
            self._error = str(error)
            self._owner.cancel("reader_failed")
        except Exception:
            self._error = "reader_or_consumer_failed"
            self._owner.cancel("reader_failed")
        finally:
            self._settle()

    def _settle(self) -> None:
        # The original owner retains this worker even if a consumer violates the
        # nonblocking cancel/timed join contract. No timeout releases backing.
        while not self._consumer_released:
            if self._owner.stop.is_set():
                self._cancel()
            self._io = "consumer_join"
            try:
                released = self._consumer.join(0.05)
                if type(released) is not bool:
                    raise IntakeError("invalid_consumer_join_result")
                with self._owner.lock:
                    self._consumer_released = released
            except Exception:
                self._error = self._error or "consumer_join_failed"
                self._owner.cancel("reader_failed")
            if not self._consumer_released:
                self._retry.wait(0.05)
        # Cancellation itself may still own access to the borrowed descriptor.
        # consumer_released is serialized with cancel registration above.
        while self._cancel_thread is not None and self._cancel_thread.is_alive():
            self._io = "consumer_cancel_join"
            self._cancel_thread.join(0.05)
        while self._fd is not None:
            self._io = "reader_close"
            try:
                observed_fd = os.fstat(self._fd)
                if (observed_fd.st_dev, observed_fd.st_ino) != (
                    self._expected.device,
                    self._expected.inode,
                ):
                    raise IntakeError("reader_descriptor_identity_debt")
            except OSError, IntakeError:
                # A consumer closed/replaced the borrowed descriptor. Do not close
                # a different descriptor or certify uncertain backing as released.
                self._error = "reader_descriptor_identity_debt"
                self._owner.cancel("reader_failed")
                self._retry.wait(0.05)
                continue
            try:
                os.close(self._fd)
            except OSError:
                try:
                    os.fstat(self._fd)
                except OSError as observed:
                    if observed.errno == errno.EBADF:
                        self._fd = None
                        break
                self._error = self._error or "reader_close_failed"
                self._owner.cancel("reader_failed")
                self._retry.wait(0.05)
            else:
                self._fd = None
        self._io = None
        self._finished = True
        self._owner.wake.set()


class LocalIntake:
    """One retained thread owns all blocking file work and the absolute-expiry lease.

    The caller must retain this object until join succeeds, including after the
    original cleanup deadline. It never cancels an await and abandons the I/O.
    """

    def __init__(
        self,
        directory: Path,
        source: Path | None,
        request: IntakeRequest,
        *,
        recovery: bool = False,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.directory, self.source, self.request = directory, source, request
        self.recovery, self.utc_now, self.monotonic = recovery, utc_now, monotonic
        self.owner_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.pending = threading.Event()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.phase = "not_started"
        self.failure: str | None = None
        self.cleanup_deadline: float | None = None
        # Recovery starts with an unknown old clock, including failed preflight.
        self.cleanup_deadline_unknown = recovery
        self.cleanup_started_utc: str | None = None
        self.cleanup_missed = recovery
        self.released_at: float | None = None
        self.io: str | None = None
        self.copied_bytes = 0
        self.copied_sha256: str | None = None
        self.fds: dict[str, int] = {}
        self.fd_identities: dict[str, tuple[int, int]] = {}
        self.state_identity: tuple[int, int] | None = None
        self.temp_identity: tuple[int, int] | None = None
        self.payload_identity: tuple[int, int] | None = None
        self.payload_retained = False
        self._payload_file: FileIdentity | None = None
        self._reader: OwnedRead | None = None
        self._reader_state: Literal["none", "owned", "released"] = "none"
        self.owns_slot = False
        self.state_released = False
        self.unresolved_slot = recovery
        self.expires_monotonic = monotonic() + max(
            0, (_utc(request.expires_at) - utc_now()).total_seconds()
        )

    def start(self, *, execute: bool = False) -> None:
        if not execute or sys.platform != "linux":
            raise IntakeError("explicit_linux_intake_required")
        if self.thread is not None:
            raise IntakeError("owner_already_started")
        self.request.validate(self.utc_now(), recovery=self.recovery)
        self.thread = threading.Thread(target=self._run, name="fma-local-intake", daemon=False)
        self.thread.start()

    def cancel(self, reason: str = "cancelled", *, started_at: datetime | None = None) -> None:
        with self.lock:
            if started_at is not None:
                now, mono = self.utc_now(), self.monotonic()
                if started_at.tzinfo != UTC or started_at > now:
                    raise IntakeError("invalid_original_cleanup_time")
                original = min(started_at, _utc(self.request.expires_at))
                deadline = mono + (original - now).total_seconds() + CLEANUP_SECONDS
                # A trusted enclosing evaluation may convey an earlier failure
                # from its retained helper. It can only tighten this obligation;
                # unknown recovery timing can never be repaired by a new clock.
                if not self.cleanup_deadline_unknown and (
                    self.cleanup_deadline is None or deadline < self.cleanup_deadline
                ):
                    self.cleanup_deadline = deadline
                    self.cleanup_started_utc = _timestamp(original)
            if self.cleanup_deadline is None and not self.cleanup_deadline_unknown:
                now, mono = self.utc_now(), self.monotonic()
                expires = _utc(self.request.expires_at)
                if now >= expires or mono >= self.expires_monotonic:
                    # Polling, a blocked read and a clock jump cannot renew expiry.
                    start = min(self.expires_monotonic, mono + (expires - now).total_seconds())
                    self.cleanup_deadline = start + CLEANUP_SECONDS
                    self.cleanup_started_utc = self.request.expires_at
                    reason = "expired"
                else:
                    self.cleanup_deadline = mono + CLEANUP_SECONDS
                    self.cleanup_started_utc = _timestamp(now)
            self.failure = self.failure or reason
            self.stop.set()
            self.wake.set()

    def read_into(self, consumer: ReadConsumer) -> OwnedRead:
        """Transfer one inert consumer after atomic admission; no caller-owned I/O."""
        with self.lock:
            self._check_stop()
            if not all(
                callable(getattr(consumer, name, None))
                for name in ("inspect", "write", "finish", "cancel", "join")
            ):
                raise IntakeError("invalid_reader_consumer")
            if (
                self.recovery
                or self.request.version != 2
                or not self.pending.is_set()
                or self.thread is None
                or not self.thread.is_alive()
                or self._payload_file is None
                or self._reader is not None
            ):
                raise IntakeError("reader_handoff_not_available")
            reader = OwnedRead(self, consumer, self._payload_file)
            self._reader = reader
            self._reader_state = "owned"
            self.wake.set()
            return reader

    def join(self, timeout: float = 0) -> bool:
        self._expire()
        if self.thread is not None:
            self.thread.join(timeout)
        self._expire()
        alive = self._observe_worker_exit()
        return self.thread is not None and not alive

    def _observe_worker_exit(self) -> bool:
        """Only an external observation can establish the thread's actual release."""
        with self.lock:
            alive = self.thread is not None and self.thread.is_alive()
            observed_at = self.monotonic()
            if (
                self.thread is not None
                and not alive
                and not self.fds
                and not self.payload_retained
                and not self.unresolved_slot
                and (self._reader is None or self._reader.join())
                and self.phase in ("released", "rejected")
                and self.released_at is None
            ):
                self.released_at = observed_at
            if self.cleanup_deadline is not None and self.released_at is None:
                self.cleanup_missed |= observed_at > self.cleanup_deadline
            elif self.cleanup_deadline is not None and self.released_at is not None:
                self.cleanup_missed |= self.released_at > self.cleanup_deadline
            return alive

    def report(self) -> dict[str, object]:
        self._expire()
        with self.lock:
            alive = self._observe_worker_exit()
            released = (
                self.released_at is not None
                and not alive
                and not self.fds
                and not self.payload_retained
                and not self.unresolved_slot
            )
            result: dict[str, object] = {
                "schema": f"sobafm-fma-pending-intake-{self.request.version}",
                "owner_id": self.owner_id,
                "phase": self.phase,
                "failure": self.failure,
                "request_sha256": self.request.fingerprint,
                "source_url": self.request.source_url,
                "metadata_digest": self.request.metadata_digest,
                "credit_snapshot": self.request.credit_snapshot,
                **self.request.time_fields(safe=True),
                "expires_at": self.request.expires_at,
                "notices_count": len(self.request.download_notices),
                "notices_sha256": hashlib.sha256(
                    _encoded(self.request.download_notices)
                ).hexdigest(),
                "expected_bytes": self.request.file.bytes,
                "copied_bytes": self.copied_bytes,
                "copied_sha256": self.copied_sha256,
                "payload_retained": self.payload_retained,
                "original_io_in_flight": self.io,
                "retained_handles": sorted(self.fds),
                "worker_alive": alive,
                "cleanup_deadline": self.cleanup_deadline,
                "cleanup_deadline_known": not self.cleanup_deadline_unknown,
                "pending_valid": self.pending.is_set()
                and not self.stop.is_set()
                and self.utc_now() < _utc(self.request.expires_at)
                and self.monotonic() < self.expires_monotonic,
                "unresolved_previous_slot": self.unresolved_slot,
                "cleanup_deadline_met": self.released_at is not None and not self.cleanup_missed,
                "released_at": self.released_at,
                "released": released,
                "cleanup_debt": self.stop.is_set() and not released,
                "slot_reusable": False,
                "operator_original_deleted": False,
                "admission_reviews_created": 0,
                "recording_correspondence": "unverified",
                "file_integrity_and_embedded_notices": "review_required",
            }
            if self.request.version == 2:
                result["reader"] = None if self._reader is None else self._reader.report()
            if len(_encoded(result)) > MAX_MANIFEST_BYTES:
                raise IntakeError("report_oversize")
            return result

    def _expire(self) -> None:
        if (
            self.utc_now() >= _utc(self.request.expires_at)
            or self.monotonic() >= self.expires_monotonic
        ):
            self.cancel("expired")

    def _check_stop(self) -> None:
        self._expire()
        if self.stop.is_set():
            raise IntakeError(self.failure or "cancelled")

    def _check_source(self) -> None:
        assert self.source is not None
        if not self.request.file.matches(os.fstat(self.fds["source"])):
            raise IntakeError("source_changed")
        observed = os.stat(
            self.source.name, dir_fd=self.fds["source_parent"], follow_symlinks=False
        )
        if not self.request.file.matches(observed):
            raise IntakeError("source_substituted")

    def _validate_source_hash(self) -> None:
        digest, count = hashlib.sha256(), 0
        while True:
            self._check_stop()
            self._check_source()
            self.io = "source_validation_read"
            data = os.read(
                self.fds["source"], min(CHUNK_BYTES, self.request.file.bytes - count + 1)
            )
            self.io = None
            self._check_source()
            count += len(data)
            if count > self.request.file.bytes:
                raise IntakeError("input_oversize")
            digest.update(data)
            if not data:
                break
        if count != self.request.file.bytes or digest.hexdigest() != self.request.file.sha256:
            raise IntakeError("source_integrity_failed")
        self._check_stop()
        os.lseek(self.fds["source"], 0, os.SEEK_SET)

    def _close(self, name: str) -> None:
        if name not in self.fds:
            return
        fd = self.fds[name]
        try:
            observed = os.fstat(fd)
            if (
                name in self.fd_identities
                and (observed.st_dev, observed.st_ino) != self.fd_identities[name]
            ):
                raise IntakeError("descriptor_identity_debt")
            os.close(fd)
        except OSError:
            try:
                os.fstat(fd)
            except OSError as observed:
                if observed.errno != errno.EBADF:
                    raise
            else:
                raise
        with self.lock:
            del self.fds[name]
            self.fd_identities.pop(name, None)

    def _own(self, name: str, fd: int) -> int:
        with self.lock:
            self.fds[name] = fd
        observed = os.fstat(fd)
        with self.lock:
            self.fd_identities[name] = (observed.st_dev, observed.st_ino)
        return fd

    def _open_directory(self, path: Path, name: str) -> None:
        """Retain every traversal descriptor, including a failed close."""
        if not path.is_absolute() or ".." in path.parts:
            raise IntakeError("absolute_unlinked_path_required")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current_name = "walk_0"
        current = self._own(current_name, os.open("/", flags))
        for index, part in enumerate(path.parts[1:], 1):
            following_name = f"walk_{index}"
            following = self._own(following_name, os.open(part, flags, dir_fd=current))
            self._close(current_name)
            current_name, current = following_name, following
        with self.lock:
            self.fds[name] = self.fds.pop(current_name)
            self.fd_identities[name] = self.fd_identities.pop(current_name)

    def _unlink_owned(self, name: str, identity: tuple[int, int]) -> None:
        observed = os.stat(name, dir_fd=self.fds["directory"], follow_symlinks=False)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino) != identity
        ):
            raise IntakeError("owned_file_identity_debt")
        os.unlink(name, dir_fd=self.fds["directory"])

    def _remove_temp(self) -> None:
        if self.temp_identity is not None:
            with suppress(FileNotFoundError):
                self._unlink_owned("state.tmp", self.temp_identity)
            self.temp_identity = None

    def _save(self, phase: str) -> None:
        self.io = "state_persistence"
        self.phase = phase
        state = {
            "schema": f"sobafm-fma-owned-slot-{self.request.version}",
            "owner_id": self.owner_id,
            "request_sha256": self.request.fingerprint,
            **self.request.time_fields(),
            "expires_at": self.request.expires_at,
            "phase": phase,
            "directory_identity": self.fd_identities["directory"],
            "lock_identity": self.fd_identities["lock"],
            "payload_identity": self.payload_identity,
            "copied_bytes": self.copied_bytes,
            "copied_sha256": self.copied_sha256,
            "cleanup_started_utc": self.cleanup_started_utc,
            "cleanup_missed": self.cleanup_missed,
        }
        if self.request.version == 2:
            state["reader_state"] = self._reader_state
        data = _encoded(state)
        assert len(data) <= MAX_MANIFEST_BYTES
        self._remove_temp()
        fd = os.open(
            "state.tmp",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fds["directory"],
        )
        self._own("state_write", fd)
        self.temp_identity = self.fd_identities["state_write"]
        offset = 0
        while offset < len(data):
            amount = os.write(fd, data[offset:])
            if amount <= 0:
                raise IntakeError("state_write_failed")
            offset += amount
        os.fsync(fd)
        self._close("state_write")
        if self.state_identity is not None:
            existing = os.stat("state.json", dir_fd=self.fds["directory"], follow_symlinks=False)
            if (
                (existing.st_dev, existing.st_ino) != self.state_identity
                or existing.st_nlink != 1
                or not stat.S_ISREG(existing.st_mode)
            ):
                raise IntakeError("state_identity_debt")
        os.replace(
            "state.tmp",
            "state.json",
            src_dir_fd=self.fds["directory"],
            dst_dir_fd=self.fds["directory"],
        )
        self.state_identity, self.temp_identity = self.temp_identity, None
        os.fsync(self.fds["directory"])
        self.io = None

    def _claim(self) -> None:
        self._open_directory(self.directory, "directory")
        directory = os.fstat(self.fds["directory"])
        if directory.st_uid != os.geteuid() or stat.S_IMODE(directory.st_mode) != 0o700:
            raise IntakeError("private_owned_directory_required")
        if "source_parent" in self.fds:
            original_parent = os.fstat(self.fds["source_parent"])
            if (directory.st_dev, directory.st_ino) == (
                original_parent.st_dev,
                original_parent.st_ino,
            ):
                raise IntakeError("source_destination_alias")
        fd = os.open(
            "owner.lock",
            os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | (0 if self.recovery else os.O_CREAT),
            0o600,
            dir_fd=self.fds["directory"],
        )
        self._own("lock", fd)
        lock_info = os.fstat(fd)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_nlink != 1
            or lock_info.st_uid != os.geteuid()
            or stat.S_IMODE(lock_info.st_mode) != 0o600
        ):
            raise IntakeError("invalid_lock_file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named_lock = os.stat("owner.lock", dir_fd=self.fds["directory"], follow_symlinks=False)
        if (named_lock.st_dev, named_lock.st_ino) != (lock_info.st_dev, lock_info.st_ino):
            raise IntakeError("lock_substituted")
        with os.scandir(self.fds["directory"]) as entries:
            allowed = {"owner.lock", "state.json", "payload"} if self.recovery else {"owner.lock"}
            for entry in entries:
                if entry.name not in allowed:
                    self.unresolved_slot = True
                    raise IntakeError("slot_not_empty")
        if self.recovery:
            self.unresolved_slot = True
            self._recover()
            self.unresolved_slot = False
            return
        self.owns_slot = True
        self._save("copying")
        payload_fd = os.open(
            "payload",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fds["directory"],
        )
        self.payload_retained = True
        self._own("payload", payload_fd)
        observed = os.fstat(self.fds["payload"])
        self.payload_identity = (observed.st_dev, observed.st_ino)
        self._save("copying")

    def _recover(self) -> None:
        fd = os.open(
            "state.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fds["directory"]
        )
        self._own("state_read", fd)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_MANIFEST_BYTES
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise IntakeError("invalid_recovery_state")
        time_key = "acquired_at" if self.request.version == 1 else "acquisition"
        state = _object(
            _decode(os.read(fd, MAX_MANIFEST_BYTES + 1)),
            {
                "schema",
                "owner_id",
                "request_sha256",
                time_key,
                "expires_at",
                "phase",
                "payload_identity",
                "copied_bytes",
                "copied_sha256",
                "cleanup_started_utc",
                "cleanup_missed",
                "directory_identity",
                "lock_identity",
            }
            | ({"reader_state"} if self.request.version == 2 else set()),
        )
        if (
            state["schema"] != f"sobafm-fma-owned-slot-{self.request.version}"
            or state["request_sha256"] != self.request.fingerprint
            or state[time_key] != self.request.time_fields()[time_key]
            or state["expires_at"] != self.request.expires_at
            or state["directory_identity"] != list(self.fd_identities["directory"])
            or state["lock_identity"] != list(self.fd_identities["lock"])
            or state["phase"] not in ("copying", "pending_review", "released")
            or type(state["cleanup_missed"]) is not bool
            or not isinstance(state["owner_id"], str)
            or re.fullmatch(r"[a-f0-9]{32}", state["owner_id"]) is None
        ):
            raise IntakeError("recovery_binding_mismatch")
        if self.request.version == 2 and state["reader_state"] not in ("none", "released"):
            raise IntakeError("uncertain_reader_ownership_debt")
        if self.request.version == 2:
            self._reader_state = cast(Literal["none", "released"], state["reader_state"])
        original_start = state["cleanup_started_utc"]
        original = None if original_start is None else _utc(original_start)
        if original is not None and original > self.utc_now():
            raise IntakeError("invalid_recovery_state")
        named = os.stat("state.json", dir_fd=self.fds["directory"], follow_symlinks=False)
        after = os.fstat(fd)
        if (named.st_dev, named.st_ino, named.st_mtime_ns, named.st_ctime_ns, named.st_size) != (
            info.st_dev,
            info.st_ino,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_size,
        ) or (after.st_mtime_ns, after.st_ctime_ns, after.st_size) != (
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_size,
        ):
            raise IntakeError("recovery_state_changed")
        identity = state["payload_identity"]
        if not isinstance(identity, list) or len(cast(list[object], identity)) != 2:
            raise IntakeError("unbound_payload_debt")
        pair = cast(list[object], identity)
        self.payload_identity = (_integer(pair[0]), _integer(pair[1], positive=True))
        if self.payload_identity == (self.request.file.device, self.request.file.inode):
            raise IntakeError("operator_original_identity_forbidden")
        self.state_identity = (info.st_dev, info.st_ino)
        self.owner_id = state["owner_id"]
        self.copied_bytes = _integer(state["copied_bytes"])
        if self.copied_bytes > self.request.file.bytes:
            raise IntakeError("invalid_recovery_state")
        digest = state["copied_sha256"]
        self.copied_sha256 = None if digest is None else _digest(digest)
        if state["phase"] == "pending_review" and (
            self.copied_bytes != self.request.file.bytes
            or self.copied_sha256 != self.request.file.sha256
        ):
            raise IntakeError("invalid_recovery_state")
        if state["phase"] == "released":
            try:
                os.stat("payload", dir_fd=self.fds["directory"], follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise IntakeError("released_state_has_payload_debt")
        self.payload_retained = state["phase"] != "released"
        self.owns_slot = True
        # An interrupted owner cannot retroactively prove its original cleanup deadline.
        if original is not None:
            with self.lock:
                self.cleanup_started_utc = _timestamp(original)
                self.cleanup_deadline = (
                    self.monotonic()
                    + (
                        original + timedelta(seconds=CLEANUP_SECONDS) - self.utc_now()
                    ).total_seconds()
                )
                self.cleanup_deadline_unknown = False
        self.cancel("recovered_interrupted_owner")
        self._close("state_read")

    def _copy(self) -> None:
        digest = hashlib.sha256()
        while True:
            self._check_stop()
            self._check_source()
            self.io = "source_read"
            data = os.read(
                self.fds["source"],
                min(CHUNK_BYTES, self.request.file.bytes - self.copied_bytes + 1),
            )
            self.io = None
            self._check_source()
            if not data:
                break
            if self.copied_bytes + len(data) > self.request.file.bytes:
                raise IntakeError("input_oversize")
            offset = 0
            while offset < len(data):
                self._check_stop()
                self.io = "payload_write"
                amount = os.write(self.fds["payload"], memoryview(data)[offset:])
                self.io = None
                if amount <= 0:
                    raise IntakeError("copy_stalled")
                digest.update(memoryview(data)[offset : offset + amount])
                self.copied_bytes += amount
                offset += amount
        self.copied_sha256 = digest.hexdigest()
        if (
            self.copied_bytes != self.request.file.bytes
            or self.copied_sha256 != self.request.file.sha256
        ):
            raise IntakeError("copy_integrity_failed")
        self._check_stop()
        self._close("source")
        self._close("source_parent")
        self.io = "payload_sync"
        os.fsync(self.fds["payload"])
        copied = os.stat("payload", dir_fd=self.fds["directory"], follow_symlinks=False)
        if (
            (copied.st_dev, copied.st_ino) != self.payload_identity
            or copied.st_nlink != 1
            or copied.st_size != self.copied_bytes
        ):
            raise IntakeError("payload_changed")
        self._check_stop()
        self._payload_file = FileIdentity(
            copied.st_dev,
            copied.st_ino,
            copied.st_size,
            copied.st_mtime_ns,
            copied.st_ctime_ns,
            self.copied_sha256,
        )
        self._save("pending_review")
        self._check_stop()
        self.pending.set()
        while not self.stop.is_set():
            self._check_stop()
            reader = self._reader
            if reader is not None:
                if reader._thread is None and not reader._finished:  # pyright: ignore[reportPrivateUsage]
                    # Persist ownership before any consumer callback or descriptor opens.
                    self._save("pending_review")
                    reader._start()  # pyright: ignore[reportPrivateUsage]
                elif reader.join() and self._reader_state == "owned":
                    self._reader_state = "released"
                    self._save("pending_review")
            self.wake.wait(
                min(
                    0.05 if reader is not None else 1.0,
                    max(0, self.expires_monotonic - self.monotonic()),
                )
            )
            self.wake.clear()

    def _delete_payload(self) -> None:
        if not self.payload_retained:
            return
        if self.payload_identity is None and "payload" in self.fds:
            # An fstat failure immediately after open must not lose the new file.
            bound = os.fstat(self.fds["payload"])
            self.payload_identity = (bound.st_dev, bound.st_ino)
        if self.payload_identity == (self.request.file.device, self.request.file.inode):
            raise IntakeError("operator_original_identity_forbidden")
        try:
            observed = os.stat("payload", dir_fd=self.fds["directory"], follow_symlinks=False)
        except FileNotFoundError:
            if "payload" in self.fds and os.fstat(self.fds["payload"]).st_nlink == 0:
                self.payload_retained = False
                return
            raise IntakeError("missing_owned_payload_debt") from None
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino) != self.payload_identity
        ):
            raise IntakeError("payload_identity_debt")
        os.unlink("payload", dir_fd=self.fds["directory"])
        if "payload" in self.fds and os.fstat(self.fds["payload"]).st_nlink != 0:
            raise IntakeError("moved_owned_payload_debt")
        try:
            os.stat("payload", dir_fd=self.fds["directory"], follow_symlinks=False)
        except FileNotFoundError:
            self.payload_retained = False
        else:
            raise IntakeError("payload_deletion_unsettled")

    def _release(self) -> None:
        self.cancel(self.failure or "completed")
        while True:
            try:
                self.io = "cleanup"
                if self._reader is not None:
                    self._reader._cancel()  # pyright: ignore[reportPrivateUsage]
                    if not self._reader.join(0.05):
                        raise IntakeError("reader_cleanup_debt")
                    self._reader_state = "released"
                for name in list(self.fds):
                    if name in ("lock", "directory", "payload"):
                        continue
                    self._close(name)
                if self.owns_slot and not self.state_released:
                    self._delete_payload()
                    self._close("payload")
                    if self.cleanup_deadline is not None:
                        self.cleanup_missed |= self.monotonic() > self.cleanup_deadline
                    self._save("released")
                    self.state_released = True
                self._close("lock")
                self._close("directory")
                settled_at = self.monotonic()
                if self.cleanup_deadline is not None:
                    self.cleanup_missed |= settled_at > self.cleanup_deadline
                self.phase = "released" if self.owns_slot else "rejected"
                self.io = None
                return
            except OSError, IntakeError:
                self.failure = self.failure or "cleanup_failed"
                self.phase = "cleanup_debt"
                self.io = None
                if self.cleanup_deadline is not None:
                    self.cleanup_missed |= self.monotonic() > self.cleanup_deadline
                self.wake.wait(0.05)
                self.wake.clear()

    def _run(self) -> None:
        try:
            self.phase = "validating"
            if not self.recovery:
                self._check_stop()
                if self.source is None or not self.source.name:
                    raise IntakeError("exact_source_required")
                self._open_directory(self.source.parent, "source_parent")
                self._own(
                    "source",
                    os.open(
                        self.source.name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=self.fds["source_parent"],
                    ),
                )
                self._check_source()
                self._validate_source_hash()
            self.io = "claim"
            self._claim()
            self.io = None
            if not self.recovery:
                self._copy()
        except IntakeError as error:
            self.failure = self.failure or str(error)
        except OSError:
            self.failure = self.failure or "local_io_failed"
        finally:
            self.io = None
            self._release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-reviewed-local-intake", action="store_true")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.execute_reviewed_local_intake or sys.platform != "linux":
        parser.error("explicit reviewed Linux local intake required")
    try:
        with args.manifest.open("rb") as stream:
            payload = stream.read(MAX_MANIFEST_BYTES + 1)
        with args.catalog.open("rb") as stream:
            catalog_bytes = stream.read(2_000_001)
        catalog = parse_catalog(catalog_bytes.decode("utf-8"))
        request = parse_request(payload, catalog, datetime.now(UTC), recovery=args.recover)
    except OSError, ValueError, CatalogInputError:
        print('{"failure":"invalid_intake_input","destination_mutated":false}')
        return 2
    owner = LocalIntake(args.directory, args.source, request, recovery=args.recover)

    def cancel(_signum: int, _frame: object) -> None:
        owner.cancel()

    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGINT, signal.SIGTERM)}
    output_failed = False
    reported: set[str] = set()
    try:
        try:
            owner.start(execute=True)
        except IntakeError:
            print('{"failure":"invalid_intake_input","destination_mutated":false}')
            return 2
        while not owner.join(0.1):
            phase = "pending" if owner.pending.is_set() and not owner.stop.is_set() else "debt"
            if (
                (phase == "pending" or owner.cleanup_missed)
                and phase not in reported
                and not output_failed
            ):
                try:
                    print(json.dumps(owner.report()), flush=True)
                    reported.add(phase)
                except OSError:
                    output_failed = True
                    owner.cancel("report_failed")
        if not output_failed:
            try:
                print(json.dumps(owner.report()), flush=True)
            except OSError:
                output_failed = True
        return (
            1
            if output_failed
            or owner.cleanup_missed
            or owner.failure not in ("cancelled", "expired", "completed")
            else 0
        )
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
