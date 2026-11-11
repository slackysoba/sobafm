"""Explicit offline regressions; never collected by ordinary pytest/CI."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import cast

import pytest

from evaluations.lyria_live import probe as evaluator
from sobafm import lyria
from sobafm.lyria import LyriaSession, SessionReport
from sobafm.pcm import PcmBank
from tests.test_lyria import FakeLyria, Peer, Refuse, audio_message, until


def test_default_never_reads_key_or_starts_session(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("default or refused invocation performed live work")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(evaluator, "live", forbidden)
    assert evaluator.main([]) == 0
    assert evaluator.main(["--send-to-lyria"]) == 2
    assert evaluator.main(["--confirm-free-tier"]) == 2


def test_actual_client_pressure_consumption_and_release() -> None:
    async def script(peer: Peer) -> None:
        await peer.setup()
        assert await peer.received.get() == evaluator.REQUEST.prompts_message()
        assert await peer.received.get() == evaluator.REQUEST.config_message()
        assert await peer.received.get() == {"playbackControl": "PLAY"}
        for _ in range(8):
            await peer.websocket.send_str(audio_message())
        await peer.disconnected.wait()

    async def scenario() -> None:
        async with FakeLyria(script) as server:
            result = await evaluator.probe("placeholder", endpoint=server.url)
            assert result.result == "pass", result
            assert result.frames == 200
            assert result.resumed
            assert result.pressure_bytes == result.held_bytes
            assert 0 < result.pcm_bytes <= evaluator.MAX_PCM_BYTES
            assert result.first_pcm_ms is not None
            assert result.ready_ms is not None
            assert result.cleanup == "clean"
            assert result.leased == 0
            assert len(server.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_refusal_never_retries(status: int) -> None:
    async def scenario() -> None:
        async with FakeLyria(Refuse(status)) as server:
            result = await evaluator.probe("placeholder", endpoint=server.url)
            assert result.result == "failed"
            assert result.reason in {"access_denied", "rate_limited", "service_error"}
            assert result.frames == result.pcm_bytes == result.leased == 0
            assert result.cleanup == "clean"
            assert len(server.requests) == 1

    asyncio.run(scenario())


def test_disconnect_during_upgrade_cannot_emit_a_second_request() -> None:
    async def scenario() -> None:
        connections = 0
        requests = 0
        handlers: set[asyncio.Task[None]] = set()

        async def disconnect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal connections, requests
            task = asyncio.current_task()
            assert task is not None
            handlers.add(task)
            connections += 1
            try:
                await reader.readuntil(b"\r\n\r\n")
                requests += 1
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.remove(task)

        server = await asyncio.start_server(disconnect, "127.0.0.1", 0, limit=16384)
        port = cast(tuple[str, int], server.sockets[0].getsockname())[1]
        endpoint = f"ws://127.0.0.1:{port}/"
        original = lyria.receive_bounded_socket
        try:
            # Positive control: the pinned production client retries this disconnect.
            control = await evaluator._probe("placeholder", endpoint=endpoint)  # pyright: ignore[reportPrivateUsage]
            assert connections == requests == 2
            assert control.reason == "connect" and control.leased == 0
            connections = requests = 0
            result = await evaluator.probe("placeholder", endpoint=endpoint)
            assert connections == requests == 1
            assert result.reason == "connect"
            assert result.cleanup == "clean" and result.leased == 0
            assert lyria.receive_bounded_socket is original
        finally:
            server.close()
            await server.wait_closed()
            await asyncio.gather(*handlers)

    asyncio.run(scenario())


def test_failed_first_socket_does_not_permit_address_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allocations = 0

    def refuse_first(_address: object) -> socket.socket:
        nonlocal allocations
        allocations += 1
        raise OSError("synthetic address failure")

    monkeypatch.setattr(lyria, "receive_bounded_socket", refuse_first)
    with evaluator.one_socket():
        with pytest.raises(OSError, match="synthetic address failure"):
            lyria.receive_bounded_socket((socket.AF_INET, socket.SOCK_STREAM, 6, "", ()))
        with pytest.raises(OSError, match="evaluation connection limit"):
            lyria.receive_bounded_socket((socket.AF_INET6, socket.SOCK_STREAM, 6, "", ()))
    assert allocations == 1
    assert lyria.receive_bounded_socket is refuse_first


def test_redirect_cannot_emit_another_upgrade_on_the_same_or_a_new_socket() -> None:
    async def scenario() -> None:
        connections = 0
        requests = 0
        closed = asyncio.Event()
        endpoint = ""

        async def redirect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal connections, requests
            connections += 1
            try:
                while await reader.read(1):
                    await reader.readuntil(b"\r\n\r\n")
                    requests += 1
                    writer.write(
                        (
                            f"HTTP/1.1 302 Found\r\nLocation: {endpoint}\r\n"
                            "Content-Length: 0\r\nConnection: keep-alive\r\n\r\n"
                        ).encode()
                    )
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        server = await asyncio.start_server(redirect, "127.0.0.1", 0, limit=16384)
        port = cast(tuple[str, int], server.sockets[0].getsockname())[1]
        endpoint = f"ws://127.0.0.1:{port}/redirect"
        try:
            async with asyncio.timeout(5):
                result = await evaluator.probe("placeholder", endpoint=endpoint)
                await closed.wait()
            assert connections == requests == 1
            assert result.reason == "rejected"
            assert result.cleanup == "clean" and result.leased == 0
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_malformed_payload_is_not_output(capsys: pytest.CaptureFixture[str]) -> None:
    async def script(peer: Peer) -> None:
        await peer.setup()
        await peer.received.get()
        await peer.received.get()
        await peer.received.get()
        await peer.websocket.send_str('{"secret-provider-body":"do-not-print"}')
        await peer.websocket.send_str("malformed-private-body")
        await peer.disconnected.wait()

    async def scenario() -> None:
        async with FakeLyria(script) as server:
            result = await evaluator.probe("placeholder", endpoint=server.url)
            assert result.result == "failed"
            assert result.reason == "malformed"
            assert result.leased == 0

    asyncio.run(scenario())
    assert "private-body" not in capsys.readouterr().out


def test_repeated_cancel_keeps_owner_until_settlement(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        settling = asyncio.Event()
        release = asyncio.Event()
        banks: list[PcmBank] = []
        reports: list[SessionReport] = []

        class Delayed(LyriaSession):
            async def finish(self) -> SessionReport:
                report = await super().finish()
                # Simulate a previously missed original deadline without wall-clock sleeps.
                report = SessionReport(
                    report.failure, report.pcm_bytes, report.close_code, "expired"
                )
                reports.append(report)
                return report

            async def settle(self) -> None:
                settling.set()
                await release.wait()
                await super().settle()

        original = evaluator.PcmBankPool.acquire

        def acquire(pool: evaluator.PcmBankPool) -> PcmBank:
            bank = original(pool)
            banks.append(bank)
            return bank

        monkeypatch.setattr(evaluator.PcmBankPool, "acquire", acquire)
        monkeypatch.setattr(evaluator, "LyriaSession", Delayed)
        async with FakeLyria(Refuse(429)) as server:
            original_factory = lyria.receive_bounded_socket
            task = asyncio.create_task(evaluator.probe("placeholder", endpoint=server.url))
            await settling.wait()
            cancelled = banks[0].facts().cancelled_at
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not banks[0].facts().released
                assert lyria.receive_bounded_socket is not original_factory
                assert banks[0].facts().cancelled_at == cancelled
                assert not task.done()
            release.set()
            result = await task
            assert result.result == "failed"
            assert result.cleanup == "expired"
            assert result.leased == 0
            assert len(reports) == 1
            assert lyria.receive_bounded_socket is original_factory

    asyncio.run(scenario())


def test_work_timeout_stops_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(peer: Peer) -> None:
            await peer.disconnected.wait()

        async with FakeLyria(script) as server:
            # Immediate timeout deterministically precedes preparation. No race sleeps.
            monkeypatch.setattr(evaluator, "WORK_SECONDS", 0)
            result = await evaluator.probe("placeholder", endpoint=server.url)
            assert result.reason == "work_timeout"
            assert result.cleanup == "clean"
            assert result.leased == 0

    asyncio.run(scenario())


def test_hard_stop_cannot_claim_settlement(monkeypatch: pytest.MonkeyPatch) -> None:
    output: list[bytes] = []

    def write(_descriptor: int, value: bytes) -> int:
        output.append(value)
        return len(value)

    def exit_process(code: int) -> None:
        raise SystemExit(code)

    monkeypatch.setattr(evaluator.os, "write", write)
    monkeypatch.setattr(evaluator.os, "_exit", exit_process)
    with pytest.raises(SystemExit) as stopped:
        evaluator.hard_stop(0, None)
    assert stopped.value.code == 124
    assert b'"settlement":"unconfirmed"' in output[0]
    assert b"pass" not in output[0]


def test_cancel_during_prepare_releases() -> None:
    async def script(peer: Peer) -> None:
        await peer.disconnected.wait()

    async def scenario() -> None:
        async with FakeLyria(script) as server:
            task = asyncio.create_task(evaluator.probe("placeholder", endpoint=server.url))
            await until(lambda: bool(server.peers))
            task.cancel()
            result = await task
            assert result.reason == "cancelled"
            assert result.cleanup == "clean"
            assert result.leased == 0

    asyncio.run(scenario())
