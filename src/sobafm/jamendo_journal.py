"""Bounded durable attempt provenance and quota ownership, never playback approval.

One Journal instance owns one path. Callers must retain it through shutdown;
cancelling an await never detaches the executor write or refunds a reservation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

from sobafm.catalog import RecordingKey, metadata_digest, parse_catalog
from sobafm.credits import render_credits
from sobafm.jamendo_admission import (
    POLICY_VERSION,
    Candidate,
    Decision,
    Purpose,
    Reason,
    checked_time,
    text,
    track_id,
)

MAX_ENTRIES = 256
MAX_ENTRY_BYTES = 131_072
MAX_JOURNAL_BYTES = 4_194_304
MAX_ENVELOPE_BYTES = 4_096
MAX_PINS = 6
MAX_RESERVATIONS = 9  # Three metadata owners plus six exclusively leased audio owners.
MAX_COUNTER = 2**63 - 1
RETENTION = timedelta(hours=24)
SCHEMA = "sobafm-jamendo-journal-1"


class JournalError(Exception):
    """Fixed safe reason only; filesystem/provider exceptions never escape."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class Channel(StrEnum):
    METADATA = "metadata"
    AUDIO = "audio"


class Cleanup(StrEnum):
    PENDING = "pending"
    RELEASED = "released"
    EXPIRED = "expired"


class Outcome(StrEnum):
    CANCELLED = "cancelled"
    TRANSPORT = "transport"
    FRAMING = "framing"
    INTEGRITY = "integrity"
    DURATION = "duration"
    DEFECT = "defect"
    RIGHTS = "rights"
    NOTICE = "notice"
    JOURNAL = "journal"


@dataclass(frozen=True)
class Facts:
    transfer: bool | None = None
    decode: bool | None = None
    pcm: bool | None = None
    encoded_hash: str | None = None
    encoded_bytes: int = 0
    decoded_samples: int = 0
    failure: Outcome | None = None
    cleanup: Cleanup = Cleanup.PENDING


@dataclass(frozen=True)
class Attempt:
    number: int
    candidate: Candidate
    facts: Facts
    conflict_digest: str | None
    settled_at: datetime | None
    last_used: datetime
    resolved: bool = False

    @property
    def blocked(self) -> bool:
        return not self.resolved and (self.settled_at is None or self.conflict_digest is not None)

    @property
    def expires_at(self) -> datetime | None:
        return None if self.blocked else self.last_used + RETENTION


@dataclass(frozen=True)
class Reservation:
    number: int
    channel: Channel
    body_bytes: int
    dispatches: int | None = None


@dataclass(frozen=True)
class Counters:
    admitted: int = 0
    dispatches: int = 0
    responses: int = 0
    failures: int = 0
    body_bytes: int = 0


@dataclass(frozen=True)
class Quota:
    """Configured aggregate limits for this journal; None invents no allowance."""

    requests: int | None = None
    body_bytes: int | None = None

    def __post_init__(self) -> None:
        for value in (self.requests, self.body_bytes):
            if value is not None:
                _integer(value)


@dataclass(frozen=True)
class Snapshot:
    next_id: int = 1
    entries: tuple[Attempt, ...] = ()
    reservations: tuple[Reservation, ...] = ()
    metadata: Counters = Counters()
    audio: Counters = Counters()


def _integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise JournalError("journal_invalid")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{64}", value) is None:
        raise JournalError("journal_invalid")
    return value


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise JournalError("journal_invalid")
    data = cast(dict[str, object], value)
    if data.keys() != keys:
        raise JournalError("journal_invalid")
    return data


def _json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise JournalError("journal_invalid")
    return checked_time(datetime.fromisoformat(value))


def _candidate_data(candidate: Candidate) -> dict[str, object]:
    record = asdict(candidate.recording)
    record.pop("key")
    record["recording_id"] = candidate.recording.key.recording_id
    decision = asdict(candidate.decision)
    decision["refreshed_at"] = candidate.decision.refreshed_at.isoformat()
    return {"recording": record, "decision": decision, "content_digest": candidate.content_digest}


def _candidate(value: object) -> Candidate:
    data = _object(value, {"recording", "decision", "content_digest"})
    # Reuse the existing recording schema, not a second admission parser.
    catalog = parse_catalog(
        _json(
            {
                "schema": "sobafm-catalog-1",
                "synthetic": False,
                "partitions": [
                    {
                        "source": "jamendo",
                        "state": "partial",
                        "note": "Provenance only",
                        "recordings": [data["recording"]],
                    }
                ],
            }
        ).decode()
    )
    recording = catalog.partitions[0].recordings[0]
    track_id(recording.key.recording_id)
    decision = _object(
        data["decision"],
        {
            "key",
            "metadata_digest",
            "evidence_digest",
            "policy_version",
            "reason",
            "refreshed_at",
            "purpose",
        },
    )
    if (
        decision["key"] != asdict(recording.key)
        or decision["policy_version"] != POLICY_VERSION
        or decision["reason"] != Reason.PROVISIONAL
        or decision["purpose"] != Purpose.REFRESH
        or decision["metadata_digest"] != metadata_digest(recording)
        or recording.credits is None
    ):
        raise JournalError("journal_invalid")
    # The prepared-catalog schema permits plain notices; impose the live boundary's
    # credential/link rules as well before anything can reach durable storage.
    for notice in recording.credits.notices:
        text(notice, 1024, links=True)
    for detail in recording.details:
        text(detail, 160, links=True)
    for item in (recording.title, recording.primary_artist, *recording.creators):
        text(item, links=True)
    for item in (recording.composition, recording.composer):
        if item is not None:
            text(item, links=True)
    render_credits(recording)
    return Candidate(
        recording,
        Decision(
            recording.key,
            _digest(decision["metadata_digest"]),
            _digest(decision["evidence_digest"]),
            POLICY_VERSION,
            Reason.PROVISIONAL,
            _time(decision["refreshed_at"]),
            Purpose.REFRESH,
        ),
        _digest(data["content_digest"]),
    )


def _entry_data(entry: Attempt) -> dict[str, object]:
    return {
        "number": entry.number,
        "candidate": _candidate_data(entry.candidate),
        "projection_digest": hashlib.sha256(_json(_candidate_data(entry.candidate))).hexdigest(),
        "facts": asdict(entry.facts),
        "conflict_digest": entry.conflict_digest,
        "settled_at": None if entry.settled_at is None else entry.settled_at.isoformat(),
        "last_used": entry.last_used.isoformat(),
        "resolved": entry.resolved,
        "expires_at": None if entry.expires_at is None else entry.expires_at.isoformat(),
    }


def _entry(value: object) -> Attempt:
    data = _object(
        value,
        {
            "number",
            "candidate",
            "projection_digest",
            "facts",
            "conflict_digest",
            "settled_at",
            "last_used",
            "resolved",
            "expires_at",
        },
    )
    if _digest(data["projection_digest"]) != hashlib.sha256(_json(data["candidate"])).hexdigest():
        raise JournalError("journal_invalid")
    facts = _object(
        data["facts"],
        {
            "transfer",
            "decode",
            "pcm",
            "encoded_hash",
            "encoded_bytes",
            "decoded_samples",
            "failure",
            "cleanup",
        },
    )
    for key in ("transfer", "decode", "pcm"):
        if facts[key] is not None and type(facts[key]) is not bool:
            raise JournalError("journal_invalid")
    if type(data["resolved"]) is not bool:
        raise JournalError("journal_invalid")
    result = Attempt(
        _integer(data["number"]),
        _candidate(data["candidate"]),
        Facts(
            cast(bool | None, facts["transfer"]),
            cast(bool | None, facts["decode"]),
            cast(bool | None, facts["pcm"]),
            None if facts["encoded_hash"] is None else _digest(facts["encoded_hash"]),
            _integer(facts["encoded_bytes"]),
            _integer(facts["decoded_samples"]),
            None if facts["failure"] is None else Outcome(cast(str, facts["failure"])),
            Cleanup(cast(str, facts["cleanup"])),
        ),
        None if data["conflict_digest"] is None else _digest(data["conflict_digest"]),
        None if data["settled_at"] is None else _time(data["settled_at"]),
        _time(data["last_used"]),
        data["resolved"],
    )
    if (
        result.settled_at is not None
        and result.facts.cleanup == Cleanup.PENDING
        or result.conflict_digest is not None
        and result.facts.failure not in (Outcome.RIGHTS, Outcome.NOTICE)
    ):
        raise JournalError("journal_invalid")
    if data["expires_at"] != (None if result.expires_at is None else result.expires_at.isoformat()):
        raise JournalError("journal_invalid")
    return result


def encode(snapshot: Snapshot) -> bytes:
    """Validate complete safe snapshots and exact serialized limits before writing."""
    entries: list[dict[str, object]] = []
    if len(snapshot.entries) > MAX_ENTRIES or len(snapshot.reservations) > MAX_RESERVATIONS:
        raise JournalError("journal_capacity")
    for entry in snapshot.entries:
        value = _entry_data(entry)
        if len(_json(value)) > MAX_ENTRY_BYTES:
            raise JournalError("journal_capacity")
        entries.append(value)
    envelope = {
        "schema": SCHEMA,
        "next_id": snapshot.next_id,
        "metadata": asdict(snapshot.metadata),
        "audio": asdict(snapshot.audio),
        "reservations": [asdict(r) for r in snapshot.reservations],
    }
    if len(_json(envelope)) > MAX_ENVELOPE_BYTES:
        raise JournalError("journal_capacity")
    payload = _json({**envelope, "entries": entries})
    if len(payload) > MAX_JOURNAL_BYTES:
        raise JournalError("journal_capacity")
    return payload


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in items:
        if key in result:
            raise JournalError("journal_invalid")
        result[key] = item
    return result


def decode(payload: bytes) -> Snapshot:
    try:
        if len(payload) > MAX_JOURNAL_BYTES:
            raise JournalError("journal_capacity")
        data = _object(
            json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs),
            {"schema", "next_id", "entries", "reservations", "metadata", "audio"},
        )
        if (
            data["schema"] != SCHEMA
            or not isinstance(data["entries"], list)
            or not isinstance(data["reservations"], list)
        ):
            raise JournalError("journal_invalid")
        raw_entries = cast(list[object], data["entries"])
        raw_reservations = cast(list[object], data["reservations"])
        if len(raw_entries) > MAX_ENTRIES or len(raw_reservations) > MAX_RESERVATIONS:
            raise JournalError("journal_capacity")
        entries = tuple(_entry(v) for v in raw_entries)
        reservations: list[Reservation] = []
        for value in raw_reservations:
            item = _object(value, {"number", "channel", "body_bytes", "dispatches"})
            if item["dispatches"] is not None and (
                type(item["dispatches"]) is not int or item["dispatches"] not in (0, 1)
            ):
                raise JournalError("journal_invalid")
            reservations.append(
                Reservation(
                    _integer(item["number"]),
                    Channel(cast(str, item["channel"])),
                    _integer(item["body_bytes"]),
                    cast(int | None, item["dispatches"]),
                )
            )
        counters: list[Counters] = []
        for channel in ("metadata", "audio"):
            item = _object(
                data[channel], {"admitted", "dispatches", "responses", "failures", "body_bytes"}
            )
            counters.append(Counters(**{key: _integer(value) for key, value in item.items()}))
        snapshot = Snapshot(_integer(data["next_id"]), entries, tuple(reservations), *counters)
        identifiers = [e.number for e in entries] + [r.number for r in reservations]
        if len(set(identifiers)) != len(identifiers) or any(
            not 0 < n < snapshot.next_id for n in identifiers
        ):
            raise JournalError("journal_invalid")
        if encode(snapshot) != payload:
            raise JournalError("journal_invalid")
        return snapshot
    except JournalError:
        raise
    except ValueError, TypeError, KeyError, RecursionError:
        raise JournalError("journal_invalid") from None


def atomic_write(path: Path, payload: bytes) -> None:
    """One current plus one temp; no backups, unbounded archive, or WAL."""
    if len(payload) > MAX_JOURNAL_BYTES:
        raise JournalError("journal_capacity")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _load(path: Path) -> Snapshot:
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        # A failed write is not silently discarded/replayed as a committed version.
        raise JournalError("journal_recovery")
    if not path.exists():
        return Snapshot()
    with path.open("rb") as stream:
        return decode(stream.read(MAX_JOURNAL_BYTES + 1))


async def _join[T](task: asyncio.Task[T]) -> T:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        # Retrieve errors even when cancellation takes precedence.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


class Journal:
    def __init__(
        self,
        path: Path,
        snapshot: Snapshot,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        writer: Callable[[Path, bytes], None] = atomic_write,
    ) -> None:
        self._path = path
        self._snapshot = snapshot
        self._clock = clock
        self._writer = writer
        self._lock = asyncio.Lock()
        self._waiting = 0
        self._pins: set[int] = set()
        self._claimed: set[int] = set()
        self._live_reservations: set[int] = set()
        self._latched = False
        self._pending: Snapshot | None = None
        self._closed = False
        self._expiry_timer: asyncio.TimerHandle | None = None
        self._expiry_task: asyncio.Task[None] | None = None

    @classmethod
    async def open(
        cls,
        path: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        writer: Callable[[Path, bytes], None] = atomic_write,
    ) -> Journal:
        try:
            snapshot = await _join(asyncio.create_task(asyncio.to_thread(_load, path)))
        except OSError:
            raise JournalError("journal_io") from None
        journal = cls(path, snapshot, clock=clock, writer=writer)
        if journal._prune(snapshot) != snapshot:
            await journal._change(journal._prune)
        else:
            journal._schedule_expiry()
        return journal

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def latched(self) -> bool:
        return self._latched

    def blocked(self, key: RecordingKey) -> bool:
        return any(e.candidate.recording.key == key and e.blocked for e in self._snapshot.entries)

    def _prune(self, snapshot: Snapshot, *, make_room: bool = False) -> Snapshot:
        now = checked_time(self._clock())
        eligible = sorted(
            (e for e in snapshot.entries if e.number not in self._pins and not e.blocked),
            key=lambda e: e.last_used,
        )

        evicted = {e.number for e in eligible if now - e.last_used >= RETENTION}
        entries = tuple(e for e in snapshot.entries if e.number not in evicted)
        snapshot = replace(snapshot, entries=entries)
        if make_room:
            while True:
                try:
                    encode(snapshot)
                    return snapshot
                except JournalError as exc:
                    if exc.kind != "journal_capacity":
                        raise
                    victim = next((e for e in eligible if e.number not in evicted), None)
                    if victim is None:
                        raise
                    evicted.add(victim.number)
                    snapshot = replace(
                        snapshot,
                        entries=tuple(e for e in snapshot.entries if e.number != victim.number),
                    )
        return snapshot

    def _schedule_expiry(self) -> None:
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
        if self._closed or self._latched:
            return
        times = [
            e.expires_at
            for e in self._snapshot.entries
            if e.number not in self._pins and e.expires_at is not None
        ]
        if times:
            delay = max(0.0, (min(times) - checked_time(self._clock())).total_seconds())
            self._expiry_timer = asyncio.get_running_loop().call_later(delay, self._start_expiry)

    def _start_expiry(self) -> None:
        async def expire() -> None:
            try:
                await self._change(self._prune)
            except JournalError:
                self._latched = True

        self._expiry_task = asyncio.create_task(expire())

    async def _change(
        self, change: Callable[[Snapshot], Snapshot], *, restore: bool = False
    ) -> None:
        if self._closed or self._latched and not restore:
            raise JournalError("journal_latched")
        if self._waiting >= 7:
            raise JournalError("journal_capacity")
        self._waiting += 1
        try:
            async with self._lock:
                if self._closed or self._latched and not restore:
                    raise JournalError("journal_latched")
                updated = change(self._snapshot)
                payload = encode(updated)

                async def write() -> None:
                    try:
                        await asyncio.to_thread(self._writer, self._path, payload)
                    except Exception:
                        self._pending = updated
                        self._latched = True
                        raise JournalError("journal_io") from None
                    # Publication belongs to the owned job, even if its caller was cancelled.
                    self._snapshot = updated
                    if restore:
                        self._latched = False
                        self._pending = None
                    self._schedule_expiry()

                await _join(asyncio.create_task(write()))
        finally:
            self._waiting -= 1

    async def reserve_attempt(self, candidate: Candidate) -> Attempt:
        try:
            checked = _candidate(_candidate_data(candidate))
        except ValueError, TypeError:
            raise JournalError("journal_invalid") from None
        result: list[Attempt] = []

        def change(snapshot: Snapshot) -> Snapshot:
            if len(self._pins) >= MAX_PINS or self.blocked(checked.recording.key):
                raise JournalError("journal_capacity")
            entry = Attempt(
                snapshot.next_id, checked, Facts(), None, None, checked_time(self._clock())
            )
            if len(_json(_entry_data(entry))) > MAX_ENTRY_BYTES:
                raise JournalError("journal_capacity")
            result.append(entry)
            return self._prune(
                replace(
                    snapshot,
                    next_id=_integer(snapshot.next_id + 1),
                    entries=snapshot.entries + (entry,),
                ),
                make_room=True,
            )

        # A cancelled successful write remains an unsettled durable attempt,
        # but never leaves an inaccessible runtime pin behind.
        await self._change(change)
        entry = result[0]
        self._pins.add(entry.number)
        return entry

    async def commit(
        self, number: int, facts: Facts, *, settled: bool, conflict_digest: str | None = None
    ) -> None:
        def change(snapshot: Snapshot) -> Snapshot:
            old = next((e for e in snapshot.entries if e.number == number), None)
            if old is None or number not in self._pins:
                raise JournalError("journal_invalid")
            if conflict_digest is not None:
                _digest(conflict_digest)
                if facts.failure not in (Outcome.RIGHTS, Outcome.NOTICE):
                    raise JournalError("journal_invalid")
            retained = (
                replace(facts, cleanup=Cleanup.EXPIRED)
                if old.facts.cleanup == Cleanup.EXPIRED
                else facts
            )
            if old.facts.failure is not None and retained.failure is None:
                raise JournalError("journal_invalid")
            now = checked_time(self._clock())
            new = replace(
                old,
                facts=retained,
                conflict_digest=conflict_digest or old.conflict_digest,
                settled_at=now if settled else old.settled_at,
                last_used=now,
            )
            _entry(_entry_data(new))
            return replace(
                snapshot, entries=tuple(new if e.number == number else e for e in snapshot.entries)
            )

        try:
            await self._change(change)
        except BaseException:
            if conflict_digest is not None:
                self._latched = True
            raise

    def release_pin(self, number: int) -> None:
        entry = next((e for e in self._snapshot.entries if e.number == number), None)
        if entry is None or entry.settled_at is None:
            raise JournalError("journal_invalid")
        self._pins.discard(number)
        self._claimed.discard(number)
        self._schedule_expiry()

    def pinned(self, number: int) -> bool:
        return number in self._pins

    def claim_attempt(self, attempt: Attempt) -> None:
        """Consume one durable, pinned initial attempt before an audio dispatch."""
        if (
            self._closed
            or self._latched
            or attempt.number not in self._pins
            or attempt.number in self._claimed
            or attempt not in self._snapshot.entries
            or attempt.facts != Facts()
            or attempt.conflict_digest is not None
            or attempt.settled_at is not None
            or attempt.resolved
        ):
            raise JournalError("journal_invalid")
        self._claimed.add(attempt.number)

    async def credit_used(self, number: int) -> None:
        def change(snapshot: Snapshot) -> Snapshot:
            if not any(e.number == number for e in snapshot.entries):
                raise JournalError("journal_invalid")
            return replace(
                snapshot,
                entries=tuple(
                    replace(e, last_used=checked_time(self._clock())) if e.number == number else e
                    for e in snapshot.entries
                ),
            )

        await self._change(change)

    async def resolve(
        self,
        number: int,
        *,
        expected_evidence_digest: str,
        expected_conflict_digest: str | None,
        operator_review: bool,
    ) -> None:
        """Explicit operator resolution of this exact conflict; metadata changes do nothing."""

        def change(snapshot: Snapshot) -> Snapshot:
            old = next((e for e in snapshot.entries if e.number == number), None)
            if (
                operator_review is not True
                or old is None
                or number in self._pins
                or old.candidate.decision.evidence_digest != expected_evidence_digest
                or old.conflict_digest != expected_conflict_digest
            ):
                raise JournalError("journal_invalid")
            now = checked_time(self._clock())
            new = replace(old, resolved=True, last_used=now)
            return replace(
                snapshot, entries=tuple(new if e.number == number else e for e in snapshot.entries)
            )

        await self._change(change)

    async def reserve_usage(self, channel: Channel, body_bytes: int, quota: Quota) -> Reservation:
        _integer(body_bytes)
        result: list[Reservation] = []

        def change(snapshot: Snapshot) -> Snapshot:
            outstanding = snapshot.reservations
            if len(outstanding) >= MAX_RESERVATIONS:
                raise JournalError("journal_capacity")
            calls = (
                snapshot.metadata.dispatches
                + snapshot.audio.dispatches
                + sum(r.dispatches is None for r in outstanding)
                + 1
            )
            total = (
                snapshot.metadata.body_bytes
                + snapshot.audio.body_bytes
                + sum(r.body_bytes for r in outstanding)
                + body_bytes
            )
            if (
                quota.requests is not None
                and calls > quota.requests
                or quota.body_bytes is not None
                and total > quota.body_bytes
            ):
                raise JournalError("quota")
            _integer(calls)
            _integer(total)
            reservation = Reservation(snapshot.next_id, channel, body_bytes)
            result.append(reservation)
            counters = snapshot.metadata if channel == Channel.METADATA else snapshot.audio
            counters = replace(counters, admitted=_integer(counters.admitted + 1))
            return replace(
                snapshot,
                next_id=_integer(snapshot.next_id + 1),
                reservations=outstanding + (reservation,),
                **{channel.value: counters},
            )

        await self._change(change)
        reservation = result[0]
        self._live_reservations.add(reservation.number)
        return reservation

    async def settle_usage(
        self,
        reservation: Reservation,
        *,
        dispatches: int,
        responses: int,
        body_bytes: int,
        failed: bool,
    ) -> None:
        def change(snapshot: Snapshot) -> Snapshot:
            if (
                reservation.number not in self._live_reservations
                or reservation not in snapshot.reservations
                or dispatches not in (0, 1)
                or responses not in (0, 1)
                or responses > dispatches
                or body_bytes > reservation.body_bytes
            ):
                raise JournalError("journal_invalid")
            _integer(body_bytes)
            counters = (
                snapshot.metadata if reservation.channel == Channel.METADATA else snapshot.audio
            )
            counters = replace(
                counters,
                dispatches=_integer(counters.dispatches + dispatches),
                responses=_integer(counters.responses + responses),
                failures=_integer(counters.failures + int(failed)),
                body_bytes=_integer(counters.body_bytes + body_bytes),
            )
            return replace(
                snapshot,
                reservations=tuple(r for r in snapshot.reservations if r != reservation),
                **{reservation.channel.value: counters},
            )

        await self._change(change)
        self._live_reservations.discard(reservation.number)

    async def retain_unknown(self, reservation: Reservation, *, dispatches: int) -> None:
        """Publish known calls but permanently charge unobservable rejected-body usage."""

        def change(snapshot: Snapshot) -> Snapshot:
            if (
                reservation.number not in self._live_reservations
                or reservation not in snapshot.reservations
                or type(dispatches) is not int
                or dispatches not in (0, 1)
            ):
                raise JournalError("journal_invalid")
            counters = (
                snapshot.metadata if reservation.channel == Channel.METADATA else snapshot.audio
            )
            counters = replace(
                counters,
                dispatches=_integer(counters.dispatches + dispatches),
                failures=_integer(counters.failures + 1),
            )
            return replace(
                snapshot,
                reservations=tuple(
                    replace(r, dispatches=dispatches) if r == reservation else r
                    for r in snapshot.reservations
                ),
                **{reservation.channel.value: counters},
            )

        await self._change(change)
        self._live_reservations.discard(reservation.number)

    async def restore(self) -> None:
        """Retry the last published conservative snapshot; never clear unresolved entries."""
        await self._change(lambda snapshot: self._prune(self._pending or snapshot), restore=True)

    async def close(self) -> None:
        self._closed = True
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()

        # Existing owners finish; queued mutations see closed before starting writes.
        async def wait() -> None:
            async with self._lock:
                pass
            if self._expiry_task is not None:
                await self._expiry_task

        await _join(asyncio.create_task(wait()))
