"""Deterministic owned stage schedules, separate from real transport integration."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from sobafm.jamendo_admission import Candidate, Policy, Purpose
from sobafm.jamendo_catalog import Refreshed, Refusal
from sobafm.jamendo_journal import (
    Attempt,
    Cleanup,
    Journal,
    JournalError,
    Outcome,
    Quota,
    atomic_write,
)
from sobafm.jamendo_stream import JamendoStream, RefreshCandidate
from sobafm.jamendo_transport import JamendoTransport, Kind, Locator, Report
from sobafm.pcm import FRAME_BYTES, PcmBank, PcmBankPool
from tests.test_decode import child, echo_frames
from tests.test_jamendo_catalog import candidate as ranked_candidate
from tests.test_jamendo_catalog import response, row
from tests.test_jamendo_stream import selected
from tests.test_jamendo_transport import peer, response_script, tick, wire
from tests.test_stream_checks import mp3, tag


class Audio:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0
        self.ready_deadline = asyncio.get_running_loop().time() + 15
        self.stopped = asyncio.Event()
        self.transfer_complete = False
        self.report = Report(None, 1, 1, len(payload), 200, None, False)
        self.close_allowed = asyncio.Event()
        self.close_allowed.set()
        self.closing = asyncio.Event()

    async def read(self) -> bytes:
        part = self.payload[self.offset : self.offset + 16_384]
        self.offset += len(part)
        if not part:
            self.transfer_complete = True
            now = asyncio.get_running_loop().time()
            self.report = replace(self.report, stopped_at=now, completed_at=now)
            self.stopped.set()
        return part

    def cancel(self, first_stop: float | None = None) -> None:
        now = asyncio.get_running_loop().time() if first_stop is None else first_stop
        first = self.report.stopped_at
        self.report = replace(
            self.report, kind=Kind.CANCELLED, stopped_at=now if first is None else min(now, first)
        )
        self.stopped.set()

    async def close(self) -> None:
        if self.report.settled_at is not None:
            return
        self.closing.set()
        await self.close_allowed.wait()
        now = asyncio.get_running_loop().time()
        first = self.report.stopped_at
        self.report = replace(
            self.report, settled_at=now, cleanup_expired=first is not None and now > first + 3
        )


def test_owned_success_without_shared_transport(tmp_path: Path) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        choice, url = selected(1)
        audio = Audio(mp3())

        async def fresh(candidate: Candidate) -> Locator:
            return Locator(Refreshed(choice, False, url), asyncio.get_running_loop().time())

        async def open_audio(
            locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
        ) -> Audio:
            locator.take(asyncio.get_running_loop().time())
            journal.claim_attempt(attempt)
            return audio

        bank = PcmBankPool().acquire()
        stream = JamendoStream(
            choice,
            bank,
            refresh_candidate=fresh,
            journal=journal,
            open_audio=open_audio,
            command=echo_frames(50),
        )
        result = await stream.run()
        assert result.failure is None and result.prepared is not None
        assert result.pcm.pcm_valid and result.io_settled
        assert result.cleanup == Cleanup.RELEASED
        assert bank.release() and await stream.release()
        assert stream.report().settled
        await journal.close()

    asyncio.run(run())


def test_late_close_keeps_bank_pin_and_failed_verdict(tmp_path: Path) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        choice, url = selected(1)
        audio = Audio(mp3())
        audio.close_allowed.clear()

        async def fresh(candidate: Candidate) -> Locator:
            return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

        async def open_audio(
            locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
        ) -> Audio:
            journal.claim_attempt(attempt)
            return audio

        bank = PcmBankPool().acquire()
        stream = JamendoStream(
            choice,
            bank,
            refresh_candidate=fresh,
            journal=journal,
            open_audio=open_audio,
            command=echo_frames(50),
        )
        work = asyncio.create_task(stream.run())
        await audio.closing.wait()
        # Move the owned HTTP failure timestamp into the past; no sleep races.
        first = asyncio.get_running_loop().time() - 4
        audio.report = replace(audio.report, stopped_at=first, kind=Kind.CANCELLED)
        stream.cancel()
        stream.cancel()
        assert not work.done() and journal.pinned(1)
        assert not await stream.release()
        audio.close_allowed.set()
        result = await work
        assert result.cleanup == Cleanup.EXPIRED and result.io_settled
        assert bank.release() and await stream.release()
        assert stream.report().cleanup == Cleanup.EXPIRED
        assert journal.snapshot.entries[0].facts.cleanup == Cleanup.EXPIRED
        await journal.close()

    asyncio.run(run())


def test_failed_conflict_write_latches_before_blocked_close(tmp_path: Path) -> None:
    async def run() -> None:
        fail = threading.Event()
        attempted = threading.Event()

        def writer(path: Path, payload: bytes) -> None:
            if fail.is_set():
                attempted.set()
                raise OSError("synthetic write failure")
            atomic_write(path, payload)

        journal = await Journal.open(tmp_path / "journal.json", writer=writer)
        choice, url = selected(1)
        audio = Audio(tag(b"TCOP", b"\3new required credit") + mp3())
        audio.close_allowed.clear()

        async def fresh(candidate: Candidate) -> Locator:
            return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

        async def open_audio(
            locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
        ) -> Audio:
            journal.claim_attempt(attempt)
            fail.set()
            return audio

        bank = PcmBankPool().acquire()
        stream = JamendoStream(
            choice,
            bank,
            refresh_candidate=fresh,
            journal=journal,
            open_audio=open_audio,
            command=echo_frames(50),
        )
        work = asyncio.create_task(stream.run())
        async with asyncio.timeout(10):
            while not journal.latched:
                await tick()
        assert attempted.is_set() and not work.done()
        assert bank.read_frame() == b""
        audio.close_allowed.set()
        result = await work
        assert result.failure == Outcome.NOTICE
        assert bank.release()
        with pytest.raises(JournalError):
            await stream.release()
        fail.clear()
        await journal.restore()
        assert await stream.release()
        assert journal.blocked(choice.recording.key)
        await journal.close()

    asyncio.run(run())


def test_partial_pcm_reads_are_screened_before_commit(tmp_path: Path) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        choice, url = selected(1)
        audio = Audio(mp3())

        async def fresh(candidate: Candidate) -> Locator:
            return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

        async def open_audio(
            locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
        ) -> Audio:
            journal.claim_attempt(attempt)
            return audio

        command = child(
            "import os,sys\nsys.stdin.buffer.read()\n"
            + f"for i in range({50 * FRAME_BYTES} // 3): os.write(1, b'\\x01'*3)\n"
        )
        bank = PcmBankPool().acquire()
        stream = JamendoStream(
            choice,
            bank,
            refresh_candidate=fresh,
            journal=journal,
            open_audio=open_audio,
            command=command,
        )
        result = await stream.run()
        assert result.failure is None and result.pcm.written_bytes == 50 * FRAME_BYTES
        assert bank.release() and await stream.release()
        await journal.close()

    asyncio.run(run())


def test_blocked_feeder_write_cancellation_preserves_other_bank(tmp_path: Path) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        choice, url = selected(8)
        audio = Audio(mp3() * 700)

        async def fresh(candidate: Candidate) -> Locator:
            return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

        async def open_audio(
            locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
        ) -> Audio:
            journal.claim_attempt(attempt)
            return audio

        command = child(
            "import sys,signal\n"
            f"sys.stdout.buffer.write(b'\\x01'*{300 * FRAME_BYTES})\n"
            "sys.stdout.buffer.flush()\nsignal.pause()\n"
        )
        pool = PcmBankPool()
        current, bank = pool.acquire(), pool.acquire()
        current.write(b"\x02" * FRAME_BYTES)
        current.end_pcm()
        stream = JamendoStream(
            choice,
            bank,
            refresh_candidate=fresh,
            journal=journal,
            open_audio=open_audio,
            command=command,
        )
        assert await asyncio.wait_for(stream.prepare(), 10) is not None
        stream.cancel()
        first = stream.report().cleanup_started_at
        stream.cancel()
        result = await asyncio.wait_for(stream.run(), 10)
        assert result.cleanup_started_at == first and result.cleanup == Cleanup.RELEASED
        assert result.decoder is not None and result.decoder.input_bytes < len(audio.payload)
        assert current.read_frame() == b"\x02" * FRAME_BYTES
        assert current.release() and bank.release() and await stream.release()
        await journal.close()

    asyncio.run(run())


def test_absolute_deadline_keeps_running_after_http_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        loop = asyncio.get_running_loop()
        real_clock = loop.time
        offset = 0.0

        def clock() -> float:
            return real_clock() + offset

        monkeypatch.setattr(loop, "time", clock)
        journal = await Journal.open(tmp_path / "journal.json")
        choice, url = selected(20)
        audio = Audio(mp3())

        async def fresh(candidate: Candidate) -> Locator:
            return Locator(Refreshed(choice, True, url), loop.time())

        async def open_audio(
            locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
        ) -> Audio:
            journal.claim_attempt(attempt)
            return audio

        bank = PcmBankPool().acquire()
        stream = JamendoStream(
            choice,
            bank,
            refresh_candidate=fresh,
            journal=journal,
            open_audio=open_audio,
            command=echo_frames(1000),
            clock=clock,
        )
        work = asyncio.create_task(stream.run())
        assert await stream.prepare() is not None
        while bank.facts().buffered_bytes < 2_304_000:
            await tick()
        assert stream.report().transfer_complete and not work.done()
        offset = 200
        result = await work
        assert result.failure == Outcome.TRANSPORT and bank.read_frame() == b""
        assert bank.release() and await stream.release()
        await journal.close()
        monkeypatch.setattr(loop, "time", real_clock)

    asyncio.run(run())


def test_refresh_uses_new_credits_and_disabled_policy_makes_no_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        payload = response([row(notices=["New coherent notice"])])
        async with peer(
            monkeypatch,
            response_script(wire(payload, f"Content-Length: {len(payload)}\r\n".encode())),
        ) as requests:
            journal = await Journal.open(tmp_path / "journal.json")
            transport = JamendoTransport("placeholder", journal, quota=Quota(10, 100_000_000))
            scope = transport.reserve_station()
            ranked = ranked_candidate()
            assert isinstance(await RefreshCandidate(scope, Policy())(ranked), Refusal)
            assert requests == []
            locator = await RefreshCandidate(scope, Policy(enabled=True))(ranked)
            assert isinstance(locator, Locator)
            fresh = locator.refreshed.candidate
            assert fresh != ranked and not locator.refreshed.previous_decision_matches
            assert fresh.decision.purpose == Purpose.REFRESH
            assert fresh.recording.credits is not None
            assert "New coherent notice" in fresh.recording.credits.notices
            assert len(requests) == 1 and not journal.snapshot.entries
            assert scope.last_report is not None
            assert locator.completed_at == scope.last_report.completed_at
            await transport.close()
            await journal.close()

    asyncio.run(run())


def test_initial_journal_failure_opens_no_audio(tmp_path: Path) -> None:
    async def run() -> None:
        def writer(path: Path, payload: bytes) -> None:
            raise OSError("synthetic failure")

        journal = await Journal.open(tmp_path / "journal.json", writer=writer)
        choice, url = selected(1)
        opens = 0

        async def fresh(candidate: Candidate) -> Locator:
            return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

        async def open_audio(
            locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
        ) -> Audio:
            nonlocal opens
            opens += 1
            return Audio(mp3())

        bank = PcmBankPool().acquire()
        stream = JamendoStream(
            choice,
            bank,
            refresh_candidate=fresh,
            journal=journal,
            open_audio=open_audio,
            command=echo_frames(50),
        )
        result = await stream.run()
        assert result.failure == Outcome.JOURNAL and result.prepared is None
        assert opens == 0 and journal.latched
        assert bank.release() and await stream.release()
        await journal.close()

    asyncio.run(run())


def test_rejected_pcm_still_counts_observed_samples(tmp_path: Path) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        choice, url = selected(1)
        audio = Audio(mp3())

        async def fresh(candidate: Candidate) -> Locator:
            return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

        async def open_audio(
            locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
        ) -> Audio:
            journal.claim_attempt(attempt)
            return audio

        command = child(
            "import sys\nsys.stdin.buffer.read()\n"
            f"sys.stdout.buffer.write(b'\\xff\\x7f' * {51 * FRAME_BYTES // 2})\n"
        )
        bank = PcmBankPool().acquire()
        stream = JamendoStream(
            choice,
            bank,
            refresh_candidate=fresh,
            journal=journal,
            open_audio=open_audio,
            command=command,
        )
        result = await stream.run()
        assert result.failure == Outcome.DEFECT
        assert result.decoded_bytes == 51 * FRAME_BYTES
        assert result.pcm.written_bytes == 50 * FRAME_BYTES
        assert journal.snapshot.entries[0].facts.decoded_samples == 51 * FRAME_BYTES // 4
        assert bank.read_frame() == b""
        assert bank.release() and await stream.release()
        await journal.close()

    asyncio.run(run())


def test_delayed_terminal_write_retains_original_failed_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        loop = asyncio.get_running_loop()
        original_clock = loop.time
        offset = 0.0

        def clock() -> float:
            return original_clock() + offset

        monkeypatch.setattr(loop, "time", clock)
        writing = threading.Event()
        allowed = threading.Event()

        def writer(path: Path, payload: bytes) -> None:
            if b'"encoded_bytes":417' in payload and not writing.is_set():
                writing.set()
                assert allowed.wait(30)
            atomic_write(path, payload)

        journal = await Journal.open(tmp_path / "journal.json", writer=writer)
        choice, url = selected(1)
        audio = Audio(mp3())

        async def fresh(candidate: Candidate) -> Locator:
            return Locator(Refreshed(choice, True, url), loop.time())

        async def open_audio(
            locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
        ) -> Audio:
            journal.claim_attempt(attempt)
            return audio

        bank = PcmBankPool(clock=clock).acquire()
        stream = JamendoStream(
            choice,
            bank,
            refresh_candidate=fresh,
            journal=journal,
            open_audio=open_audio,
            command=echo_frames(50),
            clock=clock,
        )
        work = asyncio.create_task(stream.run())
        async with asyncio.timeout(10):
            while not writing.is_set():
                await tick()
        assert not stream.report().io_settled
        offset = 4
        while stream.report().cleanup != Cleanup.EXPIRED:
            await tick()
        assert not work.done() and journal.pinned(1)
        allowed.set()
        result = await work
        assert result.cleanup == Cleanup.EXPIRED and result.io_settled
        assert journal.snapshot.entries[0].facts.cleanup == Cleanup.EXPIRED
        assert bank.release() and await stream.release()
        await journal.close()
        monkeypatch.setattr(loop, "time", original_clock)

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["refresh", "open"])
def test_cancelled_completed_grant_never_launches_decoder(tmp_path: Path, stage: str) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        choice, url = selected(1)
        audio = Audio(mp3())
        opens = 0

        async def fresh(candidate: Candidate) -> Locator:
            if stage == "refresh":
                asyncio.get_running_loop().call_soon(stream.cancel)
            return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

        async def open_audio(
            locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
        ) -> Audio:
            nonlocal opens
            opens += 1
            journal.claim_attempt(attempt)
            asyncio.get_running_loop().call_soon(stream.cancel)
            return audio

        bank = PcmBankPool().acquire()
        stream = JamendoStream(
            choice,
            bank,
            refresh_candidate=fresh,
            journal=journal,
            open_audio=open_audio,
            command=echo_frames(50),
        )
        result = await stream.run()
        assert result.failure == Outcome.CANCELLED and result.decoder is None
        assert opens == (1 if stage == "open" else 0)
        assert bank.release() and await stream.release()
        await journal.close()

    asyncio.run(run())
