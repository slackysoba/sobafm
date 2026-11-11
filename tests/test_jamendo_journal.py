"""Owned temporary JSON snapshots only; no providers, credentials or media."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from sobafm.jamendo_admission import Candidate, Policy, Purpose, evaluate, parse_listing
from sobafm.jamendo_journal import (
    MAX_COUNTER,
    MAX_ENTRIES,
    Attempt,
    Channel,
    Cleanup,
    Counters,
    Facts,
    Journal,
    JournalError,
    Outcome,
    Quota,
    Snapshot,
    atomic_write,
    decode,
    encode,
)

NOW = datetime(2026, 11, 11, tzinfo=UTC)


def candidate(identity: str = "123", *, purpose: Purpose = Purpose.REFRESH) -> Candidate:
    """Small complete safe fixture for future journal/stream consumers."""
    listing = parse_listing(
        {
            "id": identity,
            "name": "夜の音楽",
            "artist_id": "77",
            "artist_name": "Créateur",
            "creators": ["演奏者"],
            "duration": 180,
            "shareurl": f"https://www.jamendo.com/track/{identity}",
            "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
            "audio": f"https://prod-1.storage.jamendo.com/?trackid={identity}&format=mp32&from=opaque",
            "notices": ["原作の表示 — 保持する"],
        }
    )
    return Candidate(
        listing.recording,
        evaluate(listing, Policy(enabled=True), NOW, purpose),
        listing.content_digest,
    )


def settled(entry: Attempt, *, at: datetime = NOW) -> Attempt:
    return replace(
        entry,
        facts=Facts(True, True, True, "a" * 64, 12, 48_000, cleanup=Cleanup.RELEASED),
        settled_at=at,
        last_used=at,
    )


def test_unicode_projection_round_trip_excludes_ephemeral_fields(tmp_path: Path) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json", clock=lambda: NOW)
        original = candidate()
        attempt = await journal.reserve_attempt(original)
        assert attempt.candidate == original
        raw = (tmp_path / "journal.json").read_bytes()
        assert "原作の表示".encode() in raw
        for secret in (b"opaque", b"prod-1", b"stream_url", b"client_id", b"audio"):
            # The fixed audio usage counter is allowed, never an audio URL/payload.
            if secret != b"audio":
                assert secret not in raw
        restarted = await Journal.open(tmp_path / "journal.json", clock=lambda: NOW)
        assert restarted.blocked(original.recording.key)
        assert restarted.snapshot.entries[0] == attempt
        with pytest.raises(JournalError):
            await restarted.reserve_attempt(original)
        await journal.close()
        await restarted.close()

    asyncio.run(run())


def test_ranking_and_changed_metadata_never_authorize_attempt(tmp_path: Path) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json", clock=lambda: NOW)
        original = candidate()
        for bad in (
            candidate(purpose=Purpose.RANKING),
            replace(original, recording=replace(original.recording, title="Different")),
            replace(original, decision=replace(original.decision, policy_version="other")),
        ):
            with pytest.raises(JournalError):
                await journal.reserve_attempt(bad)
        assert not (tmp_path / "journal.json").exists()

    asyncio.run(run())


def test_projection_digest_catches_details_change_and_duplicate_keys() -> None:
    entry = Attempt(1, candidate(), Facts(), None, None, NOW)
    payload = encode(Snapshot(next_id=2, entries=(entry,)))
    assert decode(payload).entries == (entry,)
    raw = cast(dict[str, object], json.loads(payload))
    entries = cast(list[dict[str, object]], raw["entries"])
    projection = cast(dict[str, object], entries[0]["candidate"])
    recording = cast(dict[str, object], projection["recording"])
    recording["details"] = ["new descriptive evidence"]
    with pytest.raises(JournalError, match="journal_invalid"):
        decode(json.dumps(raw).encode())
    with pytest.raises(JournalError):
        decode(payload.replace(b'"next_id":2', b'"next_id":2,"next_id":2'))


def test_entry_count_and_byte_envelopes(monkeypatch: pytest.MonkeyPatch) -> None:
    from sobafm import jamendo_journal as module

    base = Attempt(1, candidate(), Facts(), None, None, NOW)
    entries = tuple(replace(base, number=n) for n in range(1, MAX_ENTRIES + 1))
    assert len(decode(encode(Snapshot(MAX_ENTRIES + 1, entries))).entries) == MAX_ENTRIES
    with pytest.raises(JournalError, match="journal_capacity"):
        encode(Snapshot(MAX_ENTRIES + 2, entries + (replace(base, number=257),)))
    snapshot = Snapshot(2, (base,))
    payload = encode(snapshot)
    monkeypatch.setattr(module, "MAX_JOURNAL_BYTES", len(payload))
    assert encode(snapshot) == payload
    monkeypatch.setattr(module, "MAX_JOURNAL_BYTES", len(payload) - 1)
    with pytest.raises(JournalError, match="journal_capacity"):
        encode(snapshot)
    monkeypatch.setattr(module, "MAX_JOURNAL_BYTES", 4_194_304)
    raw = cast(dict[str, object], json.loads(payload))
    entry = cast(list[object], raw["entries"])[0]
    size = len(
        json.dumps(entry, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    )
    monkeypatch.setattr(module, "MAX_ENTRY_BYTES", size)
    assert encode(snapshot) == payload
    monkeypatch.setattr(module, "MAX_ENTRY_BYTES", size - 1)
    with pytest.raises(JournalError, match="journal_capacity"):
        encode(snapshot)


def test_quarantine_and_pins_survive_ttl_and_specific_resolution(tmp_path: Path) -> None:
    async def run() -> None:
        now = NOW
        journal = await Journal.open(tmp_path / "journal.json", clock=lambda: now)
        entry = await journal.reserve_attempt(candidate())
        await journal.commit(
            entry.number,
            Facts(failure=Outcome.NOTICE, cleanup=Cleanup.EXPIRED),
            settled=True,
            conflict_digest="b" * 64,
        )
        journal.release_pin(entry.number)
        now += timedelta(days=2)
        await journal.reserve_attempt(candidate("124"))
        assert journal.blocked(entry.candidate.recording.key)
        with pytest.raises(JournalError):
            await journal.resolve(
                entry.number,
                expected_evidence_digest="c" * 64,
                expected_conflict_digest="b" * 64,
                operator_review=True,
            )
        await journal.resolve(
            entry.number,
            expected_evidence_digest=entry.candidate.decision.evidence_digest,
            expected_conflict_digest="b" * 64,
            operator_review=True,
        )
        assert not journal.blocked(entry.candidate.recording.key)
        fresh = await journal.reserve_attempt(entry.candidate)
        assert fresh.number != entry.number
        assert journal.snapshot.entries[0].facts.cleanup == Cleanup.EXPIRED

    asyncio.run(run())


def test_six_pins_and_oldest_settled_eviction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sobafm import jamendo_journal as module

    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json", clock=lambda: NOW)
        attempts = [await journal.reserve_attempt(candidate(str(n))) for n in range(1, 7)]
        with pytest.raises(JournalError, match="journal_capacity"):
            await journal.reserve_attempt(candidate("7"))
        first = attempts[0]
        await journal.commit(first.number, Facts(cleanup=Cleanup.RELEASED), settled=True)
        journal.release_pin(first.number)
        monkeypatch.setattr(module, "MAX_ENTRIES", 6)
        await journal.reserve_attempt(candidate("7"))
        assert first.number not in {e.number for e in journal.snapshot.entries}
        assert len(journal.snapshot.entries) == 6

    asyncio.run(run())


def test_quota_reserved_before_work_restart_does_not_refund(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "journal.json"
        journal = await Journal.open(path)
        reservation = await journal.reserve_usage(Channel.METADATA, 100, Quota(1, 100))
        with pytest.raises(JournalError, match="quota"):
            await journal.reserve_usage(Channel.AUDIO, 1, Quota(1, 100))
        restarted = await Journal.open(path)
        assert restarted.snapshot.reservations == (reservation,)
        with pytest.raises(JournalError):
            await restarted.settle_usage(
                reservation, dispatches=0, responses=0, body_bytes=0, failed=True
            )
        await journal.settle_usage(
            reservation, dispatches=1, responses=1, body_bytes=37, failed=False
        )
        assert journal.snapshot.metadata == Counters(1, 1, 1, 0, 37)
        assert not journal.snapshot.reservations
        with pytest.raises(JournalError, match="quota"):
            await journal.reserve_usage(Channel.AUDIO, 1, Quota(1, 100))

    asyncio.run(run())


def test_counter_overflow_fails_without_write(tmp_path: Path) -> None:
    async def run() -> None:
        journal = Journal(
            tmp_path / "journal.json", Snapshot(metadata=Counters(admitted=MAX_COUNTER))
        )
        with pytest.raises(JournalError):
            await journal.reserve_usage(Channel.METADATA, 0, Quota())
        assert not (tmp_path / "journal.json").exists()

    asyncio.run(run())


def test_cancelled_write_remains_owned_and_publishes_once(tmp_path: Path) -> None:
    async def run() -> None:
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        writes = 0

        def writer(path: Path, payload: bytes) -> None:
            nonlocal writes
            writes += 1
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            atomic_write(path, payload)

        journal = await Journal.open(tmp_path / "journal.json", clock=lambda: NOW, writer=writer)
        task = asyncio.create_task(journal.reserve_attempt(candidate()))
        await started.wait()
        task.cancel()
        task.cancel()
        assert not task.done()
        assert not journal.snapshot.entries
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert writes == 1 and len(journal.snapshot.entries) == 1
        assert journal.blocked(candidate().recording.key)
        assert decode((tmp_path / "journal.json").read_bytes()) == journal.snapshot

    asyncio.run(run())


def test_initial_and_late_write_failure_latch_restore_preserves_conflict(tmp_path: Path) -> None:
    async def run() -> None:
        fail = True

        def writer(path: Path, payload: bytes) -> None:
            if fail:
                raise OSError("must not escape")
            atomic_write(path, payload)

        journal = await Journal.open(tmp_path / "journal.json", clock=lambda: NOW, writer=writer)
        with pytest.raises(JournalError, match="journal_io") as caught:
            await journal.reserve_attempt(candidate())
        assert "must not escape" not in str(caught.value)
        assert journal.latched and not journal.snapshot.entries
        fail = False
        await journal.restore()
        assert journal.blocked(candidate().recording.key)
        other = await journal.reserve_attempt(candidate("124"))
        fail = True
        with pytest.raises(JournalError):
            await journal.commit(
                other.number,
                Facts(failure=Outcome.RIGHTS, cleanup=Cleanup.EXPIRED),
                settled=True,
                conflict_digest="d" * 64,
            )
        assert journal.latched
        fail = False
        await journal.restore()
        assert journal.snapshot.entries[-1].conflict_digest == "d" * 64
        assert journal.blocked(other.candidate.recording.key)

    asyncio.run(run())


def test_atomic_fault_and_restart_never_silently_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sobafm import jamendo_journal as module

    path = tmp_path / "journal.json"
    original = encode(Snapshot())
    atomic_write(path, original)

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError):
        atomic_write(path, encode(Snapshot(next_id=2)))
    assert path.read_bytes() == original
    assert path.with_name(path.name + ".tmp").exists()
    with pytest.raises(JournalError, match="journal_recovery"):
        asyncio.run(Journal.open(path))


def test_writer_has_only_six_waiters(tmp_path: Path) -> None:
    async def run() -> None:
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def writer(path: Path, payload: bytes) -> None:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            atomic_write(path, payload)

        journal = await Journal.open(tmp_path / "journal.json", writer=writer)
        first = asyncio.create_task(journal.reserve_usage(Channel.METADATA, 1, Quota()))
        await started.wait()
        waiting = [
            asyncio.create_task(journal.reserve_usage(Channel.AUDIO, 1, Quota())) for _ in range(6)
        ]
        barrier = loop.create_future()
        loop.call_soon(barrier.set_result, None)
        await barrier
        with pytest.raises(JournalError, match="journal_capacity"):
            await journal.reserve_usage(Channel.METADATA, 1, Quota())
        release.set()
        await asyncio.gather(first, *waiting)
        assert len(journal.snapshot.reservations) == 7
        await journal.close()

    asyncio.run(run())


def test_expiry_on_restart_preserves_unsettled_and_quarantine(tmp_path: Path) -> None:
    base = Attempt(1, candidate(), Facts(), None, None, NOW)
    expired = settled(base)
    unsettled = replace(base, number=2, candidate=candidate("124"))
    conflict = replace(
        settled(base),
        number=3,
        candidate=candidate("125"),
        facts=Facts(failure=Outcome.RIGHTS, cleanup=Cleanup.RELEASED),
        conflict_digest="f" * 64,
    )
    path = tmp_path / "journal.json"
    atomic_write(path, encode(Snapshot(4, (expired, unsettled, conflict))))

    async def run() -> None:
        journal = await Journal.open(path, clock=lambda: NOW + timedelta(hours=24))
        assert {e.number for e in journal.snapshot.entries} == {2, 3}
        assert decode(path.read_bytes()) == journal.snapshot
        await journal.close()

    asyncio.run(run())


def test_directory_fsync_failure_keeps_unpublished_snapshot_for_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sobafm import jamendo_journal as module

    async def run() -> None:
        original = module.os.fsync
        count = 0
        fail = True

        def fsync(fd: int) -> None:
            nonlocal count
            count += 1
            if count == 2 and fail:
                raise OSError("directory fsync failed")
            original(fd)

        monkeypatch.setattr(module.os, "fsync", fsync)
        journal = await Journal.open(tmp_path / "journal.json", clock=lambda: NOW)
        with pytest.raises(JournalError, match="journal_io"):
            await journal.reserve_attempt(candidate())
        assert journal.latched and not journal.snapshot.entries
        assert decode((tmp_path / "journal.json").read_bytes()).entries
        fail = False
        await journal.restore()
        assert journal.blocked(candidate().recording.key)
        assert journal.snapshot == decode((tmp_path / "journal.json").read_bytes())
        await journal.close()

    asyncio.run(run())
