"""Jamendo-only HTTP ownership. No decoder, model, station wiring or live activity.

The caller reserves a station scope before interpretation and a bank before audio.
Returned metadata bytes still require SearchWindow.receive / refresh admission.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import socket
import time
from collections.abc import Awaitable, Buffer, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.client_proto import ResponseHandler
from aiohttp.connector import Connection

from sobafm.jamendo_admission import Policy, Purpose
from sobafm.jamendo_catalog import (
    MAX_OFFSET,
    MAX_RESPONSE_BYTES,
    TRACKS_ENDPOINT,
    Failure,
    Query,
    Refreshed,
    Refusal,
    Request,
    SearchWindow,
    refresh_request,
)
from sobafm.jamendo_journal import Attempt, Channel, Journal, JournalError, Quota, Reservation
from sobafm.pcm import PcmBank

READ_BYTES = 16_384
SOCKET_READ_BYTES = 32_768
RECEIVE_QUEUE_BYTES = 98_304
CONNECT_SECONDS = 3.0
GAP_SECONDS = 5.0
REQUEST_SECONDS = 10.0
CLEANUP_SECONDS = 3.0
UNCONFIGURED_QUOTA = Quota()
_JSON_LITERAL = re.compile(
    r"(?:true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
)


class Kind(StrEnum):
    CONFIGURATION = "configuration"
    CAPACITY = "capacity"
    ACCESS_DENIED = "access_denied"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    OVERSIZED = "oversized"
    MALFORMED = "malformed"
    STALE = "stale"
    QUOTA = "quota"
    CANCELLED = "cancelled"
    JOURNAL = "journal"


class TransportError(Exception):
    def __init__(self, kind: Kind) -> None:
        super().__init__(kind.value)
        self.kind = kind


@dataclass(frozen=True)
class Report:
    kind: Kind | None
    dispatches: int
    responses: int
    body_bytes: int | None
    status: int | None
    completed_at: float | None
    cleanup_expired: bool
    stopped_at: float | None = None
    settled_at: float | None = None


@dataclass(frozen=True)
class StreamLimits:
    encoded_bytes: int
    wall_seconds: float
    tolerance_seconds: float

    @classmethod
    def for_duration(cls, duration: int) -> StreamLimits:
        if type(duration) is not int or not 1 <= duration <= 86_400:
            raise TransportError(Kind.MALFORMED)
        tolerance = min(30.0, max(2.0, 0.01 * duration), 0.1 * duration)
        return cls(
            math.ceil(41_000 * (duration + tolerance)) + 262_144,
            2 * (duration + tolerance) + 60,
            tolerance,
        )


@dataclass
class Locator:
    """One refresh's ephemeral URL; never serialize or place this in a prompt."""

    refreshed: Refreshed = field(repr=False)
    completed_at: float
    used: bool = field(default=False, init=False)

    def take(self, now: float) -> str:
        if self.used:
            raise TransportError(Kind.STALE)
        self.used = True
        decision = self.refreshed.candidate.decision
        age = (datetime.now(UTC) - decision.refreshed_at).total_seconds()
        if (
            not math.isfinite(self.completed_at)
            or not 0 <= now - self.completed_at <= 5
            or not 0 <= age <= 5
            or not decision.admitted
            or decision.purpose != Purpose.REFRESH
        ):
            raise TransportError(Kind.STALE)
        return checked_audio_url(self.refreshed.stream_url, decision.key.recording_id)


def checked_audio_url(value: str, identity: str) -> str:
    try:
        if len(value.encode("utf-8")) > 2048 or any(ord(c) < 33 for c in value) or "\\" in value:
            raise ValueError
        url = urlsplit(value)
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
        params = dict(pairs)
        if (
            url.scheme != "https"
            or re.fullmatch(r"prod-[0-9]+\.storage\.jamendo\.com", url.netloc) is None
            or url.path != "/"
            or url.fragment
            or len(params) != len(pairs)
            or not params.keys() <= {"trackid", "format", "from", "client_id"}
            or params.get("trackid") != identity
            or params.get("format") != "mp32"
        ):
            raise ValueError
    except ValueError, UnicodeError:
        raise TransportError(Kind.MALFORMED) from None
    return value


def checked_request(request: Request) -> None:
    try:
        params = dict(request.parameters)
        if len(params) != len(request.parameters) or request.endpoint != TRACKS_ENDPOINT:
            raise ValueError
        if request.key is not None:
            expected = refresh_request(request.key, Policy(enabled=True))
        else:
            offset = int(params.get("offset", "-1"))
            if not 0 <= offset < MAX_OFFSET:
                raise ValueError
            query = Query(
                params.get("search", ""),
                params.get("artist_name", ""),
                params.get("name", ""),
                tuple(params["tags"].split("+")) if "tags" in params else (),
            )
            expected = SearchWindow(query, Policy(enabled=True), offset=offset).request()
        if request != expected:
            raise ValueError
    except ValueError, TypeError:
        raise TransportError(Kind.MALFORMED) from None


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def validate_json(payload: bytes) -> None:
    """Lexical caps before constructing a tree, then strict JSON syntax/duplicates."""
    try:
        if len(payload) > MAX_RESPONSE_BYTES:
            raise TransportError(Kind.OVERSIZED)
        value = payload.decode("utf-8")
        index = 0
        tokens = 0
        depth = 0
        while index < len(value):
            character = value[index]
            if character in " \t\r\n":
                index += 1
                continue
            tokens += 1
            if tokens > 32_768:
                raise ValueError
            if character in "[{":
                depth += 1
                if depth > 8:
                    raise ValueError
                index += 1
            elif character in "]}":
                depth -= 1
                if depth < 0:
                    raise ValueError
                index += 1
            elif character in ",:":
                index += 1
            elif character == '"':
                index += 1
                size = 0
                while index < len(value) and value[index] != '"':
                    character = value[index]
                    index += 1
                    if character == "\\":
                        escaped = value[index]
                        index += 1
                        if escaped == "u":
                            digits = value[index : index + 4]
                            if re.fullmatch("[0-9a-fA-F]{4}", digits) is None:
                                raise ValueError
                            code = int(digits, 16)
                            index += 4
                            if 0xD800 <= code <= 0xDBFF:
                                if value[index : index + 2] != "\\u":
                                    raise ValueError
                                low = value[index + 2 : index + 6]
                                if (
                                    re.fullmatch("[0-9a-fA-F]{4}", low) is None
                                    or not 0xDC00 <= int(low, 16) <= 0xDFFF
                                ):
                                    raise ValueError
                                index += 6
                                size += 4
                            elif 0xDC00 <= code <= 0xDFFF:
                                raise ValueError
                            else:
                                size += len(chr(code).encode("utf-8"))
                        elif escaped in '"\\/bfnrt':
                            size += 1
                        else:
                            raise ValueError
                    else:
                        if ord(character) < 32:
                            raise ValueError
                        size += len(character.encode("utf-8"))
                    if size > 65_536:
                        raise ValueError
                if index == len(value):
                    raise ValueError
                index += 1
            else:
                match = _JSON_LITERAL.match(value, index)
                if match is None:
                    raise ValueError
                token = match.group()
                if token[0] in "-0123456789" and not math.isfinite(float(token)):
                    raise ValueError
                index += len(token)
        if depth:
            raise ValueError
        json.loads(value, object_pairs_hook=_pairs)
    except TransportError:
        raise
    except ValueError, IndexError, RecursionError:
        raise TransportError(Kind.MALFORMED) from None


class _BoundedSocket(socket.socket):
    """Public socket_factory supplies a socket with explicit delivery-size bounds."""

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        return super().recv(min(bufsize, SOCKET_READ_BYTES), flags)

    def recv_into(self, buffer: Buffer, nbytes: int = 0, flags: int = 0) -> int:
        return super().recv_into(
            buffer, min(nbytes or memoryview(buffer).nbytes, SOCKET_READ_BYTES), flags
        )


def bounded_socket(address: tuple[int, int, int, str, tuple[Any, ...]]) -> socket.socket:
    sock = _BoundedSocket(address[0], address[1], address[2])
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32_768)
    return sock


class _Request(aiohttp.ClientRequest):
    async def send(self, conn: Connection) -> aiohttp.ClientResponse:
        # Configure TLS before any HTTP request/response, not after headers arrive.
        transport = conn.transport
        limit = getattr(transport, "set_read_buffer_limits", None)
        if self.is_ssl() and not callable(limit):
            raise TransportError(Kind.CONFIGURATION)
        if limit is not None:
            limit(high=32_768)
        return await super().send(conn)

    def __repr__(self) -> str:
        return "<JamendoRequest>"


class _Response(aiohttp.ClientResponse):
    def __repr__(self) -> str:
        return "<JamendoResponse>"


class CheckedResolver(AbstractResolver):
    def __init__(self) -> None:
        self._tasks: set[
            asyncio.Task[
                list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[Any, ...]]]
            ]
        ] = set()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if (
            host != "api.jamendo.com"
            and re.fullmatch(r"prod-[0-9]+\.storage\.jamendo\.com", host) is None
        ):
            raise TransportError(Kind.MALFORMED)
        task = asyncio.create_task(
            asyncio.get_running_loop().getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM
            )
        )
        self._tasks.add(task)
        try:
            answers = await asyncio.shield(task)
        finally:
            if task.done():
                self._tasks.discard(task)
        if not answers:
            raise TransportError(Kind.UNAVAILABLE)
        results: list[ResolveResult] = []
        for af, _, proto, _, address in answers:
            ip = ipaddress.ip_address(address[0])
            if (
                not ip.is_global
                or isinstance(ip, ipaddress.IPv6Address)
                and ip.ipv4_mapped is not None
            ):
                raise TransportError(Kind.MALFORMED)
            results.append(
                ResolveResult(
                    hostname=host,
                    host=str(ip),
                    port=port,
                    family=af,
                    proto=proto,
                    flags=socket.AI_NUMERICHOST,
                )
            )
        return results

    async def close(self) -> None:
        for task in tuple(self._tasks):
            with suppress(OSError, asyncio.CancelledError):
                await _join(task)
        self._tasks.clear()


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
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


def checked_headers(response: aiohttp.ClientResponse, *, audio: bool, maximum: int) -> int | None:
    # The compiled parser limits names and values separately. Validate their
    # combined raw-byte projection, including ': ' and CRLF, before body use.
    if len(response.raw_headers) > 32 or any(
        len(name) + len(value) + 4 > 1024 for name, value in response.raw_headers
    ):
        raise TransportError(Kind.MALFORMED)
    if response.status in (401, 403):
        raise TransportError(Kind.ACCESS_DENIED)
    if response.status == 429:
        raise TransportError(Kind.RATE_LIMITED)
    if response.status != 200:
        raise TransportError(Kind.UNAVAILABLE)
    headers = response.headers
    if response.version not in (aiohttp.HttpVersion10, aiohttp.HttpVersion11):
        raise TransportError(Kind.MALFORMED)
    if (
        "Transfer-Encoding" in headers
        or "Trailer" in headers
        or "Icy-Metaint" in headers
        or any(value.lower() != "identity" for value in headers.getall("Content-Encoding", []))
    ):
        raise TransportError(Kind.MALFORMED)
    lengths = headers.getall("Content-Length", [])
    if len(lengths) > 1 or audio and not lengths:
        raise TransportError(Kind.MALFORMED)
    length: int | None = None
    if lengths:
        if re.fullmatch("[0-9]{1,20}", lengths[0]) is None:
            raise TransportError(Kind.MALFORMED)
        length = int(lengths[0])
        if length > maximum:
            raise TransportError(Kind.OVERSIZED)
    expected = {"audio/mpeg", "application/octet-stream"} if audio else {"application/json"}
    if len(headers.getall("Content-Type", [])) != 1 or response.content_type not in expected:
        raise TransportError(Kind.MALFORMED)
    return length


def retry_after(value: str | None, now: datetime) -> float | None:
    if value is None or len(value) > 128:
        return None
    try:
        if re.fullmatch("[0-9]{1,10}", value):
            return float(value)
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        return max(0.0, (parsed - now).total_seconds())
    except ValueError, OverflowError:
        return None


class _Operation:
    def __init__(self, client: JamendoTransport, channel: Channel, maximum: int) -> None:
        self.client = client
        self.channel = channel
        self.maximum = maximum
        self.reservation: Reservation | None = None
        self.response: aiohttp.ClientResponse | None = None
        self.resolver = CheckedResolver()
        self.session: aiohttp.ClientSession | None = None
        self.dispatches = 0
        self.kind: Kind | None = None
        self.stopped_at: float | None = None
        self.finished_at: float | None = None
        self.closing: asyncio.Task[None] | None = None
        self.transport: asyncio.Transport | None = None
        self.protocol: ResponseHandler | None = None
        self._raw_bytes: int | None = None
        self._status: int | None = None
        self._responses = 0
        self.completed_at: float | None = None

    async def open(
        self, url: str, *, params: tuple[tuple[str, str], ...] = (), audio: bool = False
    ) -> int | None:
        if self.client.closed or self.client.clock() < self.client.not_before:
            raise TransportError(Kind.UNAVAILABLE)
        self.reservation = await self.client.journal.reserve_usage(
            self.channel, self.maximum + RECEIVE_QUEUE_BYTES, self.client.quota
        )

        async def once(
            request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
        ) -> aiohttp.ClientResponse:
            if self.dispatches:
                raise TransportError(Kind.UNAVAILABLE)
            self.dispatches = 1
            return await handler(request)

        connector = aiohttp.TCPConnector(
            limit=1,
            force_close=True,
            use_dns_cache=False,
            resolver=self.resolver,
            socket_factory=bounded_socket,
        )
        operation = self

        class OwnedRequest(_Request):
            async def send(self, conn: Connection) -> aiohttp.ClientResponse:
                operation.transport = conn.transport
                operation.protocol = conn.protocol
                return await super().send(conn)

        self.session = aiohttp.ClientSession(
            connector=connector,
            version=aiohttp.HttpVersion10,
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            auto_decompress=False,
            request_class=OwnedRequest,
            response_class=_Response,
            middlewares=(once,),
            read_bufsize=READ_BYTES,
            max_headers=32,
            max_field_size=1020,  # Per-component parser bound; checked_headers combines them.
            max_line_size=4081,  # Include "HTTP/1.x 200 " and CRLF in the status-line cap.
            timeout=aiohttp.ClientTimeout(total=None, connect=CONNECT_SECONDS, sock_read=None),
        )
        async with asyncio.timeout(5 if audio else GAP_SECONDS):
            self.response = await self.session.get(
                url,
                params=params,
                allow_redirects=False,
                read_until_eof=not audio,
                headers={"Connection": "close", "Accept-Encoding": "identity"},
            )
        response = self.response
        self._responses = 1
        self._status = response.status
        if response.status in (401, 403, 429):
            delay = retry_after(response.headers.get("Retry-After"), datetime.now(UTC))
            self.client.not_before = (
                math.inf
                if delay is None
                else max(self.client.not_before, self.client.clock() + delay)
            )
        return checked_headers(response, audio=audio, maximum=self.maximum)

    def stop(self, at: float | None = None) -> None:
        timestamp = self.client.clock() if at is None else at
        self.stopped_at = timestamp if self.stopped_at is None else min(timestamp, self.stopped_at)
        if self.transport is not None:
            self.transport.abort()
        if self.response is not None:
            self.response.close()

    async def close(self) -> None:
        self.stop()
        if self.closing is None:
            self.closing = asyncio.create_task(self._close())
        await _join(self.closing)

    async def _close(self) -> None:
        if self.finished_at is not None:
            return
        if self.session is not None:
            await self.session.close()
        if self.protocol is not None and self.protocol.closed is not None:
            await self.protocol.closed
        await self.resolver.close()
        self._raw_bytes = (
            self.response.content.total_raw_bytes
            if self.response is not None
            else 0
            if not self.dispatches
            else None
        )
        try:
            if self.reservation is not None and (self.response is not None or not self.dispatches):
                await self.client.journal.settle_usage(
                    self.reservation,
                    dispatches=self.dispatches,
                    responses=int(self.response is not None),
                    body_bytes=0
                    if self.response is None
                    else self.response.content.total_raw_bytes,
                    failed=self.kind is not None,
                )
            elif self.reservation is not None:
                # Unknown rejected bodies retain their entire reservation.
                await self.client.journal.retain_unknown(
                    self.reservation, dispatches=self.dispatches
                )
        except JournalError:
            self.kind = Kind.JOURNAL
            raise
        finally:
            # Even a failed write has settled here. Its conservative pending snapshot
            # and admission latch belong to Journal; no raw HTTP payload belongs there.
            self.response = None
            self.session = None
            self.transport = None
            self.protocol = None
            self.finished_at = self.client.clock()

    def report(self) -> Report:
        raw = (
            self.response.content.total_raw_bytes
            if self.response is not None
            else self._raw_bytes
            if self.finished_at is not None
            else (0 if not self.dispatches else None)
        )
        now = self.client.clock()
        return Report(
            self.kind,
            self.dispatches,
            self._responses,
            raw,
            self._status,
            self.completed_at,
            self.stopped_at is not None
            and (self.finished_at or now) > self.stopped_at + CLEANUP_SECONDS,
            self.stopped_at,
            self.finished_at,
        )


class OwnedAudio:
    """One reader; caller owns its bank/decoder and must close this owner on every path."""

    def __init__(
        self,
        operation: _Operation,
        length: int,
        deadline: float,
        ready_deadline: float,
        release: Callable[[], None],
    ) -> None:
        self._operation = operation
        self._length = length
        self._deadline = deadline
        self._release = release
        self._read: asyncio.Task[bytes] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._watchdog = asyncio.get_running_loop().call_at(deadline, self._expire)
        self.ready_deadline = ready_deadline
        self.stopped = asyncio.Event()
        self.consumed_bytes = 0
        self.transfer_complete = False

    def _expire(self) -> None:
        self._operation.kind = Kind.TIMEOUT
        self.cancel()

    def _begin_close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
            # Keep the failed owner in its station, without an unobserved task exception.
            self._closing.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )

    @property
    def report(self) -> Report:
        return self._operation.report()

    async def read(self) -> bytes:
        if self._closing is not None or self._read is not None:
            raise TransportError(Kind.CAPACITY)
        if self.transfer_complete:
            return b""

        async def receive() -> bytes:
            response = self._operation.response
            assert response is not None
            async with asyncio.timeout_at(
                min(self._deadline, asyncio.get_running_loop().time() + GAP_SECONDS)
            ):
                chunk = await response.content.read(READ_BYTES)
            if response.content.total_raw_bytes > self._operation.maximum:
                raise TransportError(Kind.OVERSIZED)
            self.consumed_bytes += len(chunk)
            if not chunk:
                if self.consumed_bytes != self._length or not response.content.at_eof():
                    raise TransportError(Kind.MALFORMED)
                self.transfer_complete = True
                self._operation.completed_at = self._operation.client.clock()
                self._operation.stop()
                self.stopped.set()
                self._begin_close()
            return chunk

        self._read = asyncio.create_task(receive())
        try:
            return await self._read
        except asyncio.CancelledError:
            self._operation.kind = self._operation.kind or Kind.CANCELLED
            self.cancel()
            raise
        except (TransportError, TimeoutError, aiohttp.ClientError) as exc:
            kind = (
                exc.kind
                if isinstance(exc, TransportError)
                else Kind.TIMEOUT
                if isinstance(exc, TimeoutError)
                else Kind.MALFORMED
            )
            self._operation.kind = kind
            self.cancel()
            raise TransportError(kind) from None
        finally:
            self._read = None

    def cancel(self, first_stop: float | None = None) -> None:
        self._operation.kind = self._operation.kind or Kind.CANCELLED
        self._operation.stop(first_stop)
        if self._read is not None:
            self._read.cancel()
        self.stopped.set()
        self._begin_close()

    async def close(self) -> None:
        if not self.transfer_complete:
            self._operation.kind = self._operation.kind or Kind.CANCELLED
        self._operation.stop()
        self.stopped.set()
        self._begin_close()
        assert self._closing is not None
        await _join(self._closing)

    async def _close(self) -> None:
        self._watchdog.cancel()
        read = self._read
        if read is not None:
            self.cancel()
            with suppress(asyncio.CancelledError, TransportError, aiohttp.ClientError):
                await _join(read)
        try:
            await self._operation.close()
        finally:
            if self._operation.finished_at is not None:
                self._release()


class StationTransport:
    def __init__(self, client: JamendoTransport) -> None:
        self._client = client
        self._gate = asyncio.Lock()
        self._metadata_owners = 0
        self._audio_banks: set[PcmBank] = set()
        self._audio: set[OwnedAudio] = set()
        self._work: set[asyncio.Task[Any]] = set()
        self._operations: set[_Operation] = set()
        self.closed = False
        self._closing: asyncio.Task[None] | None = None
        self.last_report: Report | None = None
        self.failed_cleanups = 0
        self.failure_counts: dict[Kind, int] = dict.fromkeys(Kind, 0)

    def _record(self, report: Report) -> None:
        self.last_report = report
        if report.cleanup_expired:
            self.failed_cleanups += 1
        if report.kind is not None:
            if self.failure_counts[report.kind] == 2**63 - 1:
                self.closed = True
            else:
                self.failure_counts[report.kind] += 1

    @property
    def settled(self) -> bool:
        return self.closed and not self._operations and not self._audio_banks

    async def fetch(self, request: Request, *, deadline: float | None = None) -> bytes | Refusal:
        start = asyncio.get_running_loop().time()
        if self.closed or self._client.closed or self._metadata_owners >= 2:
            self._record(Report(Kind.CAPACITY, 0, 0, 0, None, None, False))
            return Refusal(Failure.TRANSPORT)
        self._metadata_owners += 1
        operation = _Operation(self._client, Channel.METADATA, MAX_RESPONSE_BYTES)
        self._operations.add(operation)

        async def run() -> bytes | Refusal:
            held = False
            limit = min(start + REQUEST_SECONDS, deadline or math.inf)
            watchdog = asyncio.get_running_loop().call_at(limit, operation.stop)
            try:
                checked_request(request)
                async with asyncio.timeout_at(min(start + REQUEST_SECONDS, deadline or math.inf)):
                    await self._gate.acquire()
                    held = True
                    if self.closed or self._client.closed:
                        raise TransportError(Kind.CONFIGURATION)
                    await operation.open(
                        TRACKS_ENDPOINT,
                        params=request.parameters + (("client_id", self._client.client_id),),
                    )
                    response = operation.response
                    assert response is not None
                    body = bytearray()
                    while True:
                        async with asyncio.timeout(GAP_SECONDS):
                            chunk = await response.content.read(READ_BYTES)
                        if (
                            response.content.total_raw_bytes > MAX_RESPONSE_BYTES
                            or len(body) + len(chunk) > MAX_RESPONSE_BYTES
                        ):
                            raise TransportError(Kind.OVERSIZED)
                        if not chunk:
                            break
                        body.extend(chunk)
                    payload = bytes(body)
                    validate_json(payload)
                    if asyncio.get_running_loop().time() > min(
                        start + REQUEST_SECONDS, deadline or math.inf
                    ):
                        raise TransportError(Kind.TIMEOUT)
                    operation.completed_at = self._client.clock()
                    return payload
            except asyncio.CancelledError:
                operation.kind = Kind.CANCELLED
                raise
            except (
                TransportError,
                JournalError,
                TimeoutError,
                aiohttp.ClientError,
                OSError,
            ) as exc:
                operation.kind = (
                    exc.kind
                    if isinstance(exc, TransportError)
                    else Kind.QUOTA
                    if isinstance(exc, JournalError) and exc.kind == "quota"
                    else Kind.JOURNAL
                    if isinstance(exc, JournalError)
                    else Kind.TIMEOUT
                    if isinstance(exc, TimeoutError)
                    else Kind.UNAVAILABLE
                )
                return Refusal(Failure.TRANSPORT)
            finally:
                try:
                    await _join(asyncio.create_task(operation.close()))
                except JournalError:
                    operation.kind = Kind.JOURNAL
                    raise
                finally:
                    watchdog.cancel()
                    if held:
                        self._gate.release()

        task = asyncio.create_task(run())

        def completed(work: asyncio.Task[bytes | Refusal]) -> None:
            if work.cancelled() and operation.kind is None:
                operation.kind = Kind.CANCELLED
            self._record(operation.report())
            self._metadata_owners -= 1
            self._operations.discard(operation)

        task.add_done_callback(completed)
        self._work.add(task)
        try:
            return await _owned(task, on_cancel=operation.stop)
        except JournalError:
            return Refusal(Failure.TRANSPORT)
        finally:
            self._work.discard(task)

    async def open_audio(
        self, locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
    ) -> OwnedAudio:
        if (
            self.closed
            or self._client.closed
            or len(self._audio_banks) >= 2
            or bank in self._audio_banks
            or bank.facts().released
            or bank.facts().cancelled_at is not None
            or attempt not in self._client.journal.snapshot.entries
            or not self._client.journal.pinned(attempt.number)
            or attempt.candidate != locator.refreshed.candidate
            or attempt.settled_at is not None
        ):
            raise TransportError(Kind.CAPACITY)
        limits = StreamLimits.for_duration(
            locator.refreshed.candidate.recording.duration_seconds or 0
        )
        url = locator.take(self._client.clock())
        try:
            self._client.journal.claim_attempt(attempt)
        except JournalError:
            raise TransportError(Kind.JOURNAL) from None
        self._audio_banks.add(bank)
        operation = _Operation(self._client, Channel.AUDIO, limits.encoded_bytes)
        self._operations.add(operation)
        start = asyncio.get_running_loop().time()

        async def run() -> OwnedAudio:
            watchdog = asyncio.get_running_loop().call_at(
                min(start + 15, deadline or math.inf), operation.stop
            )
            try:
                async with asyncio.timeout_at(min(start + 15, deadline or math.inf)):
                    length = await operation.open(url, audio=True)
                assert length is not None

                def release() -> None:
                    self._record(operation.report())
                    self._audio.discard(owner)
                    self._audio_banks.discard(bank)

                owner = OwnedAudio(
                    operation,
                    length,
                    min(start + limits.wall_seconds, deadline or math.inf),
                    min(start + 15, deadline or math.inf),
                    release,
                )
                self._audio.add(owner)
                return owner
            except BaseException as exc:
                operation.kind = (
                    Kind.CANCELLED
                    if isinstance(exc, asyncio.CancelledError)
                    else exc.kind
                    if isinstance(exc, TransportError)
                    else Kind.TIMEOUT
                    if isinstance(exc, TimeoutError)
                    else Kind.JOURNAL
                    if isinstance(exc, JournalError)
                    else Kind.UNAVAILABLE
                )
                await _join(asyncio.create_task(operation.close()))
                self._audio_banks.discard(bank)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise TransportError(operation.kind) from None
            finally:
                watchdog.cancel()

        task = asyncio.create_task(run())

        def completed(work: asyncio.Task[OwnedAudio]) -> None:
            self._operations.discard(operation)
            if work.cancelled() or work.exception() is not None:
                self._audio_banks.discard(bank)

        task.add_done_callback(completed)
        self._work.add(task)
        try:
            return await _owned(
                task,
                abandon=lambda owner: owner.close(),
                on_cancel=lambda: operation.stop(bank.facts().cancelled_at),
            )
        finally:
            self._work.discard(task)

    def cancel(self, first_stop: float | None = None) -> None:
        at = self._client.clock() if first_stop is None else first_stop
        self.closed = True
        for operation in self._operations:
            operation.stop(at)
        for task in self._work:
            task.cancel()
        for audio in tuple(self._audio):
            audio.cancel(at)
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())

    async def close(self) -> None:
        self.cancel()
        assert self._closing is not None
        await _join(self._closing)

    async def _close(self) -> None:
        for audio in tuple(self._audio):
            with suppress(JournalError):
                await audio.close()
        for task in tuple(self._work):
            with suppress(asyncio.CancelledError, JournalError, TransportError):
                await _join(task)
        self._client.release_station(self)


async def _owned[T](
    task: asyncio.Task[T],
    *,
    abandon: Callable[[T], Awaitable[None]] | None = None,
    on_cancel: Callable[[], None] | None = None,
) -> T:
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await _join(task)
        if abandon is not None and not task.cancelled() and task.exception() is None:

            async def release() -> None:
                await abandon(task.result())

            with suppress(asyncio.CancelledError, Exception):
                await _join(asyncio.create_task(release()))
        raise


class JamendoTransport:
    def __init__(
        self,
        client_id: str,
        journal: Journal,
        *,
        quota: Quota = UNCONFIGURED_QUOTA,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", client_id) is None:
            raise TransportError(Kind.CONFIGURATION)
        self.client_id = client_id
        self.journal = journal
        self.quota = quota
        self.clock = clock
        self.not_before = 0.0
        self.closed = False
        self._stations: set[StationTransport] = set()
        self._closing: asyncio.Task[None] | None = None

    def reserve_station(self) -> StationTransport:
        if self.closed or len(self._stations) >= 3:
            raise TransportError(Kind.CAPACITY)
        station = StationTransport(self)
        self._stations.add(station)
        return station

    def release_station(self, station: StationTransport) -> None:
        if not station.settled:
            raise TransportError(Kind.CAPACITY)
        self._stations.discard(station)

    async def close(self) -> None:
        self.closed = True
        stations = tuple(self._stations)
        stopped_at = self.clock()
        for station in stations:
            station.cancel(stopped_at)

        async def close_all() -> None:
            for station in stations:
                await station.close()

        if self._closing is None:
            self._closing = asyncio.create_task(close_all())
        await _join(self._closing)
