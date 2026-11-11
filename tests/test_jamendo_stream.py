"""Owned synthetic HTTP and Python-child streaming; no real MP3/native/source runs."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sobafm.jamendo_admission import Candidate, Policy, Purpose, evaluate, parse_listing
from sobafm.jamendo_catalog import Refreshed
from sobafm.jamendo_journal import Cleanup, Journal, Outcome, Quota
from sobafm.jamendo_stream import JamendoStream
from sobafm.jamendo_transport import JamendoTransport, Locator
from sobafm.pcm import BANK_BYTES, FRAME_BYTES, PcmBankPool
from tests.test_decode import child, echo_frames
from tests.test_jamendo_transport import peer, response_script, tick, wire
from tests.test_stream_checks import mp3, tag


def selected(duration: int) -> tuple[Candidate, str]:
    listing = parse_listing(
        {
            "id": "123",
            "name": "Synthetic",
            "artist_id": "77",
            "artist_name": "Artist",
            "duration": duration,
            "shareurl": "https://www.jamendo.com/track/123",
            "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
            "audio": "https://prod-1.storage.jamendo.com/?trackid=123&format=mp32",
        }
    )
    return Candidate(
        listing.recording,
        evaluate(listing, Policy(enabled=True), datetime.now(UTC), Purpose.REFRESH),
        listing.content_digest,
    ), listing.stream_url


@pytest.mark.parametrize(
    "duration,status,extra,expected",
    [
        (1, 0, 0, None),
        (5, 0, 4, None),
        (8, 0, 0, None),
        (1, 1, 0, Outcome.INTEGRITY),
        (1, 0, 1, Outcome.INTEGRITY),
        (1, 0, 19204, Outcome.INTEGRITY),
    ],
)
def test_complete_or_invalid_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duration: int,
    status: int,
    extra: int,
    expected: Outcome | None,
) -> None:
    async def run() -> None:
        payload = mp3() * 3
        async with peer(
            monkeypatch,
            response_script(
                wire(payload, f"Content-Length: {len(payload)}\r\n".encode(), audio=True)
            ),
        ):
            journal = await Journal.open(tmp_path / "journal.json")
            transport = JamendoTransport("placeholder", journal, quota=Quota(10, 100_000_000))
            scope = transport.reserve_station()
            choice, url = selected(duration)

            async def refresh_choice(candidate: Candidate) -> Locator:
                return Locator(Refreshed(choice, False, url), asyncio.get_running_loop().time())

            bank = PcmBankPool().acquire()
            stream = JamendoStream(
                choice,
                bank,
                refresh_candidate=refresh_choice,
                journal=journal,
                open_audio=scope.open_audio,
                command=echo_frames(duration * 50, status=status, extra=extra),
            )
            result = await asyncio.wait_for(stream.run(), 10)
            assert result.failure == expected
            assert result.transfer_complete and result.envelope_complete
            assert result.encoded_hash == hashlib.sha256(payload).hexdigest()
            assert result.io_settled and not result.settled
            assert not await stream.release()
            if expected is None:
                assert result.pcm.pcm_valid and result.prepared is not None
                assert result.prepared.candidate == choice
            else:
                assert bank.read_frame() == b""
            assert bank.release()
            assert await stream.release()
            assert not journal.pinned(
                result.prepared.attempt
                if result.prepared is not None
                else journal.snapshot.entries[0].number
            )
            await transport.close()
            await journal.close()

    asyncio.run(run())


def test_clean_http_eof_does_not_cancel_full_bank_decoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        payload = mp3()
        async with peer(
            monkeypatch,
            response_script(
                wire(payload, f"Content-Length: {len(payload)}\r\n".encode(), audio=True)
            ),
        ):
            journal = await Journal.open(tmp_path / "journal.json")
            transport = JamendoTransport("placeholder", journal, quota=Quota(10, 100_000_000))
            scope = transport.reserve_station()
            choice, url = selected(20)

            async def refresh_choice(candidate: Candidate) -> Locator:
                return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

            bank = PcmBankPool().acquire()
            stream = JamendoStream(
                choice,
                bank,
                refresh_candidate=refresh_choice,
                journal=journal,
                open_audio=scope.open_audio,
                command=echo_frames(1000),
            )
            work = asyncio.create_task(stream.run())
            assert await stream.prepare() is not None
            async with asyncio.timeout(10):
                while bank.facts().buffered_bytes < BANK_BYTES:
                    await tick()
                assert stream.report().transfer_complete
                assert bank.facts().cancelled_at is None and not work.done()
                frames = 0
                while not work.done():
                    if bank.read_frame():
                        frames += 1
                    await tick()
                result = await work
                while bank.read_frame():
                    frames += 1
            assert result.failure is None and frames == 1000
            assert bank.release() and await stream.release()
            await transport.close()
            await journal.close()

    asyncio.run(run())


def test_late_notice_discards_prepared_pcm_and_quarantines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        continuation = asyncio.Event()
        frame = mp3()
        late = tag(b"TCOP", b"\3new required credit")

        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(
                wire(frame, f"Content-Length: {len(frame) + len(late)}\r\n".encode(), audio=True)
            )
            await writer.drain()
            await continuation.wait()
            writer.write(late)
            await writer.drain()

        async with peer(monkeypatch, serve):
            journal = await Journal.open(tmp_path / "journal.json")
            transport = JamendoTransport("placeholder", journal, quota=Quota(10, 100_000_000))
            scope = transport.reserve_station()
            choice, url = selected(8)

            async def refresh_choice(candidate: Candidate) -> Locator:
                return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

            bank = PcmBankPool().acquire()
            command = child(
                f"import sys\nsys.stdin.buffer.read({len(frame)})\n"
                f"sys.stdout.buffer.write(b'\\x01' * {300 * FRAME_BYTES})\n"
                "sys.stdout.buffer.flush()\nsys.stdin.buffer.read()\n"
            )
            stream = JamendoStream(
                choice,
                bank,
                refresh_candidate=refresh_choice,
                journal=journal,
                open_audio=scope.open_audio,
                command=command,
            )
            prepared = await asyncio.wait_for(stream.prepare(), 10)
            assert prepared is not None and bank.read_frame()
            continuation.set()
            result = await asyncio.wait_for(stream.run(), 10)
            assert result.failure == Outcome.NOTICE
            assert bank.read_frame() == b""
            assert result.prepared == prepared
            entry = journal.snapshot.entries[0]
            assert entry.conflict_digest is not None and journal.blocked(choice.recording.key)
            assert bank.release() and await stream.release()
            assert journal.blocked(choice.recording.key)
            await transport.close()
            await journal.close()

    asyncio.run(run())


def test_short_duration_holds_readiness_even_at_six_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        payload = mp3()
        async with peer(
            monkeypatch,
            response_script(
                wire(payload, f"Content-Length: {len(payload)}\r\n".encode(), audio=True)
            ),
        ):
            journal = await Journal.open(tmp_path / "journal.json")
            transport = JamendoTransport("placeholder", journal, quota=Quota(10, 100_000_000))
            scope = transport.reserve_station()
            choice, url = selected(5)

            async def refresh_choice(candidate: Candidate) -> Locator:
                return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

            bank = PcmBankPool().acquire()
            stream = JamendoStream(
                choice,
                bank,
                refresh_candidate=refresh_choice,
                journal=journal,
                open_audio=scope.open_audio,
                command=echo_frames(300),
            )
            assert await asyncio.wait_for(stream.prepare(), 10) is None
            result = await stream.run()
            assert result.failure is not None and not bank.ready_now()
            assert bank.release() and await stream.release()
            await transport.close()
            await journal.close()

    asyncio.run(run())


def test_stopped_read_repeated_cancel_keeps_original_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(wire(b"", b"Content-Length: 417\r\n", audio=True))
            await writer.drain()
            await reader.read()

        async with peer(monkeypatch, serve):
            journal = await Journal.open(tmp_path / "journal.json")
            transport = JamendoTransport("placeholder", journal, quota=Quota(10, 100_000_000))
            scope = transport.reserve_station()
            choice, url = selected(1)

            async def refresh_choice(candidate: Candidate) -> Locator:
                return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

            bank = PcmBankPool().acquire()
            stream = JamendoStream(
                choice,
                bank,
                refresh_candidate=refresh_choice,
                journal=journal,
                open_audio=scope.open_audio,
                command=echo_frames(50),
            )
            work = asyncio.create_task(stream.run())
            async with asyncio.timeout(10):
                while stream.report().transport is None:
                    await tick()
                bank.cancel()
                first = bank.facts().cancelled_at
                stream.cancel()
                stream.cancel()
                work.cancel()
                await tick()
                work.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await work
            result = stream.report()
            assert result.failure == Outcome.CANCELLED and result.io_settled
            assert result.cleanup_started_at == first and result.cleanup == Cleanup.RELEASED
            assert result.decoder is not None and not result.decoder.decoder_complete
            assert bank.read_frame() == b""
            assert bank.release() and await stream.release()
            await transport.close()
            await journal.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "headers,body",
    [
        (b"Content-Length: 1000\r\n", mp3()),
        (b"Transfer-Encoding: chunked\r\n", b"0\r\n\r\n"),
        (b"", mp3()),
    ],
)
def test_framing_failure_never_prepares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headers: bytes, body: bytes
) -> None:
    async def run() -> None:
        async with peer(monkeypatch, response_script(wire(body, headers, audio=True))):
            journal = await Journal.open(tmp_path / "journal.json")
            transport = JamendoTransport("placeholder", journal, quota=Quota(10, 100_000_000))
            scope = transport.reserve_station()
            choice, url = selected(1)

            async def refresh_choice(candidate: Candidate) -> Locator:
                return Locator(Refreshed(choice, True, url), asyncio.get_running_loop().time())

            bank = PcmBankPool().acquire()
            stream = JamendoStream(
                choice,
                bank,
                refresh_candidate=refresh_choice,
                journal=journal,
                open_audio=scope.open_audio,
                command=echo_frames(50),
            )
            result = await asyncio.wait_for(stream.run(), 10)
            assert result.failure == Outcome.TRANSPORT and result.prepared is None
            assert bank.release() and await stream.release()
            await transport.close()
            await journal.close()

    asyncio.run(run())
