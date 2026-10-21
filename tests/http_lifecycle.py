"""Real locked aiohttp response lifecycle, with no socket or scheduled close callback."""

import asyncio
from typing import cast

import aiohttp
from aiohttp.client_proto import ResponseHandler
from aiohttp.connector import Connection
from yarl import URL

from sobafm.catalog_audio import AudioStream
from tests.test_catalog import credit, discovered


class HeldTransport(asyncio.Transport):
    def __init__(self, protocol: ResponseHandler) -> None:
        super().__init__()
        self.protocol = protocol
        self.closing = False
        self.close_calls = 0
        self.lost = False

    def get_extra_info(self, name: str, default: object = None) -> object:
        return default

    def is_closing(self) -> bool:
        return self.closing

    def close(self) -> None:
        self.close_calls += 1
        self.closing = True

    def write(self, data: bytes | bytearray | memoryview[int]) -> None:
        pass

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass

    def finish(self, error: BaseException | None = None) -> None:
        if not self.lost:
            self.lost = True
            self.protocol.connection_lost(error)


class LifecycleSession:
    def __init__(
        self,
        response_class: type[aiohttp.ClientResponse],
        *,
        body: bytes = b"mp3",
        status: int = 200,
        immediate_eof: bool = True,
    ) -> None:
        self.response_class = response_class
        self.connector = aiohttp.TCPConnector()
        self.session = aiohttp.ClientSession(
            connector=self.connector,
            response_class=response_class,
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        self.protocol = ResponseHandler(asyncio.get_running_loop())
        self.transport = HeldTransport(self.protocol)
        self.protocol.connection_made(self.transport)
        self.protocol.set_response_params(auto_decompress=False)
        self.body, self.status, self.immediate_eof = body, status, immediate_eof
        self.response: aiohttp.ClientResponse | None = None
        self.connection: Connection | None = None
        self.returned = asyncio.Event()
        self.starting = asyncio.Event()
        self.close_calls = 0
        self.close_error = False
        self.closed_event = asyncio.Event()
        self.before_start_error = False
        self.start_error = False
        self.hold_headers = False
        self.hold_writer = False
        self.writer: asyncio.Task[None] | None = None
        self.writer_started = asyncio.Event()
        self.writer_cancelled = asyncio.Event()
        self.writer_release = asyncio.Event()

    async def delayed_writer(self) -> None:
        self.writer_started.set()
        while not self.writer_release.is_set():
            try:
                await self.writer_release.wait()
            except asyncio.CancelledError:
                self.writer_cancelled.set()

    @property
    def closed(self) -> bool:
        return self.session.closed

    async def get(self, url: str, **kwargs: object) -> aiohttp.ClientResponse:
        request = aiohttp.ClientRequest(
            "GET", URL(url), session=self.session, response_class=self.response_class
        )
        connection = self.connection = Connection(
            self.connector, request.connection_key, self.protocol, asyncio.get_running_loop()
        )
        # Only fixture setup touches connector ownership; production uses its
        # supported response_class injection and observes the original protocol.
        self.connector._acquired.add(self.protocol)  # pyright: ignore[reportPrivateUsage]
        try:
            response = self.response = await request.send(connection)
        except BaseException:
            connection.close()
            raise
        if self.hold_writer:
            self.writer = asyncio.create_task(self.delayed_writer())
            response._writer = self.writer  # pyright: ignore[reportPrivateUsage] - owned writer via real response lifecycle
            await self.writer_started.wait()
        headers = (
            f"HTTP/1.1 {self.status} Fixture\r\nContent-Length: {len(self.body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        if not self.start_error and not self.hold_headers:
            self.protocol.data_received(headers + (self.body if self.immediate_eof else b""))
        self.starting.set()
        try:
            if self.before_start_error:
                raise aiohttp.ClientConnectionError("private pre-hook diagnostic")
            if self.start_error:
                self.protocol.set_exception(
                    aiohttp.ClientConnectionError("private start diagnostic")
                )
            await response.start(connection)
        except BaseException:
            # Match ClientSession's original get/start failure cleanup order.
            response.close()
            connection.close()
            raise
        self.returned.set()
        return response

    async def close(self) -> None:
        self.close_calls += 1
        await self.session.close()
        self.closed_event.set()
        if self.close_error:
            raise OSError("private session diagnostic")


class HTTPRig:
    def __init__(
        self, *, body: bytes = b"mp3", status: int = 200, immediate_eof: bool = True
    ) -> None:
        self.http: LifecycleSession

        def factory(response_class: type[aiohttp.ClientResponse]) -> aiohttp.ClientSession:
            self.http = LifecycleSession(
                response_class, body=body, status=status, immediate_eof=immediate_eof
            )
            return cast(aiohttp.ClientSession, self.http)

        self.source = AudioStream(
            discovered(duration=1).with_credits(credit()), session_factory=factory
        )
