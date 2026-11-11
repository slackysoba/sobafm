"""Synthetic loopback HTTP; production host/DNS policy is tested separately."""

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp.client_proto import ResponseHandler

from sobafm import jamendo_transport as module
from sobafm.jamendo_admission import Policy
from sobafm.jamendo_catalog import Query, Refreshed, Refusal, Request, SearchWindow
from sobafm.jamendo_journal import Journal, Quota
from sobafm.jamendo_transport import (
    CheckedResolver,
    JamendoTransport,
    Kind,
    Locator,
    StreamLimits,
    TransportError,
    checked_audio_url,
    checked_request,
    retry_after,
    validate_json,
)
from sobafm.pcm import PcmBankPool
from tests.test_jamendo_journal import candidate
from tests.test_lyria import tls_pair as tls_pair

BODY = (
    b'{"headers":{"status":"success","code":0,"warnings":"",'
    b'"error_message":"","results_count":0},"results":[]}'
)
Script = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def request() -> Request:
    value = SearchWindow(Query(search="synthetic"), Policy(enabled=True)).request()
    assert isinstance(value, Request)
    return value


def wire(
    body: bytes, headers: bytes = b"", *, status: bytes = b"200 OK", audio: bool = False
) -> bytes:
    return (
        b"HTTP/1.0 "
        + status
        + b"\r\nContent-Type: "
        + (b"audio/mpeg" if audio else b"application/json")
        + b"\r\n"
        + headers
        + b"\r\n"
        + body
    )


@asynccontextmanager
async def peer(
    monkeypatch: pytest.MonkeyPatch,
    script: Script,
    *,
    tls: tuple[ssl.SSLContext, ssl.SSLContext] | None = None,
    trusted: bool = True,
) -> AsyncGenerator[list[bytes]]:
    requests: list[bytes] = []
    tasks: set[asyncio.Task[None]] = set()
    errors: list[BaseException] = []
    original = cast(Callable[..., object], aiohttp.ClientSession.get)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            await script(reader, writer)
        except ConnectionError, asyncio.IncompleteReadError:
            pass
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                errors.append(exc)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            tasks.discard(task)

    async with await asyncio.start_server(
        serve, "127.0.0.1", 0, ssl=None if tls is None else tls[0]
    ) as server:
        port = server.sockets[0].getsockname()[1]
        endpoint = f"{'http' if tls is None else 'https'}://127.0.0.1:{port}/"

        def redirect(session: aiohttp.ClientSession, url: object, **kwargs: object) -> object:
            assert str(url).startswith(
                ("https://api.jamendo.com/", "https://prod-1.storage.jamendo.com/")
            )
            if tls is not None and trusted:
                kwargs["ssl"] = tls[1]
            query = urlsplit(str(url)).query
            return original(session, endpoint + ("?" + query if query else ""), **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(aiohttp.ClientSession, "get", redirect)
            try:
                yield requests
            finally:
                for task in tuple(tasks):
                    task.cancel()
                await asyncio.gather(*tuple(tasks), return_exceptions=True)
        assert not errors


def response_script(payload: bytes) -> Script:
    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(payload)
        await writer.drain()

    return script


async def tick() -> None:
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        (wire(BODY), None),
        (wire(BODY, f"Content-Length: {len(BODY)}\r\n".encode()), None),
        (wire(BODY[:-1]), Kind.MALFORMED),
        (wire(BODY + b"{}"), Kind.MALFORMED),
        (wire(b"x", b"Content-Encoding: gzip\r\nContent-Length: 1\r\n"), Kind.MALFORMED),
        (
            wire(b"1\r\nx\r\n0\r\nX-Notice: hidden\r\n\r\n", b"Transfer-Encoding: chunked\r\n"),
            Kind.MALFORMED,
        ),
        (wire(BODY, b"Trailer: X-Notice\r\n"), Kind.MALFORMED),
        (wire(BODY, b"Content-Length: 1048577\r\n"), Kind.OVERSIZED),
        (
            wire(b"", b"Location: https://untrusted.invalid/\r\n", status=b"302 Found"),
            Kind.UNAVAILABLE,
        ),
        (wire(b"", b"Retry-After: 30\r\n", status=b"429 Refused"), Kind.RATE_LIMITED),
        (wire(b"", status=b"403 Refused"), Kind.ACCESS_DENIED),
        (wire(BODY, b"Content-Length: 1\r\nContent-Length: 2\r\n"), Kind.UNAVAILABLE),
        (wire(BODY, b"X-A: b\r\n" * 32), Kind.UNAVAILABLE),
    ],
)
def test_real_http_metadata_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes, kind: Kind | None
) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        client = JamendoTransport("placeholder-id", journal)
        scope = client.reserve_station()
        async with peer(monkeypatch, response_script(payload)) as requests:
            result = await scope.fetch(request())
            assert scope.last_report is not None
            assert scope.last_report.kind == kind
            assert isinstance(result, bytes) if kind is None else isinstance(result, Refusal)
            assert len(requests) == 1
            assert requests[0].startswith(b"GET /?") and b"HTTP/1.0\r\n" in requests[0]
            assert b"Connection: close\r\n" in requests[0]
            assert b"client_id=placeholder-id" in requests[0]
            if kind in (Kind.ACCESS_DENIED, Kind.RATE_LIMITED):
                assert client.not_before > client.clock()
        await client.close()
        assert b"placeholder-id" not in (tmp_path / "journal.json").read_bytes()

    asyncio.run(run())


def test_byte_cap_at_boundary_and_one_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        for size in (1_048_576, 1_048_577):
            journal = await Journal.open(tmp_path / f"journal-{size}.json")
            client = JamendoTransport("placeholder", journal)
            scope = client.reserve_station()
            body = BODY + b" " * (size - len(BODY))
            async with peer(monkeypatch, response_script(wire(body))):
                result = await scope.fetch(request())
            if size == 1_048_576:
                assert result == body
                assert journal.snapshot.metadata.body_bytes == size
            else:
                assert isinstance(result, Refusal)
                assert scope.last_report is not None and scope.last_report.kind == Kind.OVERSIZED
                assert (
                    size
                    <= journal.snapshot.metadata.body_bytes
                    <= 1_048_576 + module.RECEIVE_QUEUE_BYTES
                )
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":1e999}',
        b'{"a":"\\ud800"}',
        b'{"a":"\\udc00"}',
        b'{"a":"\\u000x"}',
        b"[" * 9 + b"0" + b"]" * 9,
        b'["' + b"x" * 65_537 + b'"]',
        b"[" + b"0," * 16_384 + b"0]",
        b'"\xff"',
    ],
)
def test_preparse_json_rejects_structural_and_encoding_limits(payload: bytes) -> None:
    with pytest.raises(TransportError, match="malformed"):
        validate_json(payload)


def test_json_exact_depth_string_tokens_and_unicode() -> None:
    validate_json(b"[" * 8 + b"0" + b"]" * 8)
    validate_json(b'["' + b"x" * 65_536 + b'"]')
    validate_json(b'["' + b"\\ud834\\udd1e" * 16_384 + b'"]')
    validate_json(b"[" + b"0," * 16_382 + b"0]")


def test_request_and_audio_route_revalidation() -> None:
    good = request()
    checked_request(good)
    with pytest.raises(TransportError):
        checked_request(replace(good, parameters=good.parameters + (("client_id", "forbidden"),)))
    base = "https://prod-1.storage.jamendo.com/?trackid=123&format=mp32"
    assert checked_audio_url(base, "123") == base
    for url in (
        base.replace("prod-1", "evil"),
        base + "&format=mp32",
        base + "#x",
        base.replace("/?", "/download?"),
        base.replace("mp32", "mp31"),
        base.replace("https://", "https://user:secret@"),
        base.replace(".com/", ".com:444/"),
        base + "&extra=x",
        base + "\\",
    ):
        with pytest.raises(TransportError):
            checked_audio_url(url, "123")


def test_dropped_get_guard_and_uncertain_usage_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        client = JamendoTransport("placeholder", journal, quota=Quota(requests=1))
        scope = client.reserve_station()
        async with peer(monkeypatch, response_script(b"")) as requests:
            assert isinstance(await scope.fetch(request()), Refusal)
            assert len(requests) == 1
            assert scope.last_report is not None and scope.last_report.body_bytes is None
            assert len(journal.snapshot.reservations) == 1
            assert isinstance(await scope.fetch(request()), Refusal)
            assert scope.last_report.kind == Kind.QUOTA
            assert len(requests) == 1
        await client.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.1"],
        ["169.254.1.1"],
        ["::1"],
        ["::ffff:8.8.8.8"],
        ["8.8.8.8", "192.168.0.1"],
    ],
)
def test_dns_refuses_every_private_mapped_or_mixed_answer(
    monkeypatch: pytest.MonkeyPatch, addresses: list[str]
) -> None:
    async def run() -> None:
        async def lookup(
            *args: object, **kwargs: object
        ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            return [
                (
                    socket.AF_INET6 if ":" in address else socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, 443),
                )
                for address in addresses
            ]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
        resolver = CheckedResolver()
        with pytest.raises(TransportError, match="malformed"):
            await resolver.resolve("api.jamendo.com", 443)
        await resolver.close()

    asyncio.run(run())


def test_dns_late_completion_is_owned_after_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def lookup(
            *args: object, **kwargs: object
        ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            started.set()
            await release.wait()
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
        resolver = CheckedResolver()
        task = asyncio.create_task(resolver.resolve("api.jamendo.com", 443))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        close = asyncio.create_task(resolver.close())
        await tick()
        assert not close.done()
        release.set()
        await close

    asyncio.run(run())


def test_fourth_scope_and_closed_client_make_no_work(tmp_path: Path) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        client = JamendoTransport("placeholder", journal)
        scopes = [client.reserve_station() for _ in range(3)]
        with pytest.raises(TransportError, match="capacity"):
            client.reserve_station()
        await client.close()
        assert isinstance(await scopes[0].fetch(request()), Refusal)
        with pytest.raises(TransportError):
            client.reserve_station()
        assert not journal.snapshot.reservations

    asyncio.run(run())


def test_repeated_cancel_holds_gate_and_failed_cleanup_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        begun, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        now = 10.0
        closes = 0

        class Resolver(CheckedResolver):
            async def close(self) -> None:
                nonlocal closes
                closes += 1
                if closes == 1:
                    closing.set()
                    await release.wait()
                await super().close()

        monkeypatch.setattr(module, "CheckedResolver", Resolver)

        async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(wire(b"{", b"Content-Length: 100\r\n"))
            await writer.drain()
            begun.set()
            await reader.read(1)

        journal = await Journal.open(tmp_path / "journal.json")
        client = JamendoTransport("placeholder", journal, clock=lambda: now)
        scope = client.reserve_station()
        async with peer(monkeypatch, script) as requests:
            first = asyncio.create_task(scope.fetch(request()))
            await begun.wait()
            first.cancel()
            await closing.wait()
            first.cancel()
            second = asyncio.create_task(scope.fetch(request()))
            await tick()
            assert isinstance(await scope.fetch(request()), Refusal)
            assert len(requests) == 1 and not first.done()
            now += 4
            second.cancel()
            release.set()
            for task in (first, second):
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert scope.last_report is not None
            assert scope.failed_cleanups == 1
        await client.close()

    asyncio.run(run())


def test_audio_owned_length_stream_and_no_api_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        client = JamendoTransport("api-placeholder", journal)
        scope = client.reserve_station()
        original = candidate()
        fresh = replace(
            original, decision=replace(original.decision, refreshed_at=datetime.now(UTC))
        )
        attempt = await journal.reserve_attempt(fresh)
        locator = Locator(
            Refreshed(
                fresh,
                True,
                "https://prod-1.storage.jamendo.com/?trackid=123&format=mp32&from=opaque",
            ),
            client.clock(),
        )
        bank = PcmBankPool().acquire()
        async with peer(
            monkeypatch, response_script(wire(b"synthetic", b"Content-Length: 9\r\n", audio=True))
        ) as requests:
            owner = await scope.open_audio(locator, attempt=attempt, bank=bank)
            assert await owner.read() == b"synthetic"
            assert await owner.read() == b"" and owner.transfer_complete
            await owner.close()
            assert owner.report.body_bytes == 9
            assert journal.snapshot.audio.body_bytes == 9
            assert b"api-placeholder" not in requests[0]
            assert b"from=opaque" in requests[0]
            with pytest.raises(TransportError, match="stale"):
                await scope.open_audio(locator, attempt=attempt, bank=bank)
            another = Locator(locator.refreshed, client.clock())
            with pytest.raises(TransportError, match="journal"):
                await scope.open_audio(another, attempt=attempt, bank=bank)
        await client.close()
        bank.cancel()
        assert bank.release()

    asyncio.run(run())


def test_duration_and_retry_after_bounds() -> None:
    assert StreamLimits.for_duration(86_400).encoded_bytes == 3_543_892_144
    assert StreamLimits.for_duration(1).tolerance_seconds == 0.1
    for duration in (0, 86_401, True):
        with pytest.raises(TransportError):
            StreamLimits.for_duration(duration)
    now = datetime.now(UTC)
    assert retry_after("30", now) == 30
    assert retry_after("nonsense", now) is None
    assert (
        retry_after((now + timedelta(seconds=60)).strftime("%a, %d %b %Y %H:%M:%S GMT"), now)
        is not None
    )


def test_tls_receive_bounds_and_raw_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tls_pair: tuple[ssl.SSLContext, ssl.SSLContext]
) -> None:
    deliveries: list[int] = []
    original = ResponseHandler.data_received

    def received(protocol: ResponseHandler, data: bytes) -> None:
        deliveries.append(len(data))
        original(protocol, data)

    monkeypatch.setattr(ResponseHandler, "data_received", received)

    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        client = JamendoTransport("placeholder", journal)
        scope = client.reserve_station()
        body = BODY + b" " * (1_048_576 - len(BODY))
        async with peer(monkeypatch, response_script(wire(body)), tls=tls_pair):
            assert await scope.fetch(request()) == body
        assert deliveries and max(deliveries) <= 65_536
        assert journal.snapshot.metadata.body_bytes == len(body)
        await client.close()

    asyncio.run(run())


def test_invalid_certificate_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tls_pair: tuple[ssl.SSLContext, ssl.SSLContext]
) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        client = JamendoTransport("placeholder", journal)
        scope = client.reserve_station()
        async with peer(
            monkeypatch, response_script(wire(BODY)), tls=tls_pair, trusted=False
        ) as requests:
            assert isinstance(await scope.fetch(request()), Refusal)
            assert not requests
        await client.close()

    asyncio.run(run())


def test_tls_read_ahead_is_reserved_metered_and_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tls_pair: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    async def run() -> None:
        buffered = asyncio.Event()
        queues: list[int] = []
        feed = aiohttp.StreamReader.feed_data

        def observe(reader: aiohttp.StreamReader, data: bytes, size: int = 0) -> bool:
            result = feed(reader, data, size)
            queues.append(int(reader._size))  # pyright: ignore[reportPrivateUsage] -- measure actual buffered payload
            if reader.total_raw_bytes > 32_768:
                buffered.set()
            return result

        monkeypatch.setattr(aiohttp.StreamReader, "feed_data", observe)
        journal = await Journal.open(tmp_path / "journal.json")
        client = JamendoTransport("placeholder", journal)
        scope = client.reserve_station()
        original = candidate()
        fresh = replace(
            original, decision=replace(original.decision, refreshed_at=datetime.now(UTC))
        )
        attempt = await journal.reserve_attempt(fresh)
        locator = Locator(
            Refreshed(fresh, True, "https://prod-1.storage.jamendo.com/?trackid=123&format=mp32"),
            client.clock(),
        )
        bank = PcmBankPool().acquire()
        body = b"x" * 524_288
        async with peer(
            monkeypatch,
            response_script(wire(body, b"Content-Length: 524288\r\n", audio=True)),
            tls=tls_pair,
        ):
            owner = await scope.open_audio(locator, attempt=attempt, bank=bank)
            await buffered.wait()
            assert owner.consumed_bytes == 0
            assert queues and max(queues) <= module.RECEIVE_QUEUE_BYTES
            reservation = journal.snapshot.reservations[0]
            assert (
                reservation.body_bytes
                == StreamLimits.for_duration(180).encoded_bytes + module.RECEIVE_QUEUE_BYTES
            )
            await owner.close()
            assert owner.report.body_bytes is not None
            assert 32_768 < owner.report.body_bytes <= module.RECEIVE_QUEUE_BYTES
            assert journal.snapshot.audio.body_bytes == owner.report.body_bytes
            assert not journal.snapshot.reservations
            assert journal.snapshot.audio.failures == 1
        await client.close()
        bank.cancel()
        assert bank.release()

    asyncio.run(run())


def test_queue_deadline_and_stale_locator_make_no_dispatch(tmp_path: Path) -> None:
    async def run() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        client = JamendoTransport("placeholder", journal)
        scope = client.reserve_station()
        result = await scope.fetch(request(), deadline=asyncio.get_running_loop().time() - 1)
        assert isinstance(result, Refusal)
        assert scope.last_report is not None and scope.last_report.dispatches == 0
        assert scope.last_report.kind == Kind.TIMEOUT
        original = candidate()
        locator = Locator(
            Refreshed(
                original,
                True,
                "https://prod-1.storage.jamendo.com/?trackid=123&format=mp32&from=secret-synthetic",
            ),
            client.clock() - 6,
        )
        assert "secret-synthetic" not in repr(locator)
        with pytest.raises(TransportError, match="stale"):
            locator.take(client.clock())
        await client.close()

    asyncio.run(run())
