"""Owned, incremental audio HTTP; discovery never implies playback eligibility."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from enum import StrEnum
from typing import cast
from weakref import ref

import aiohttp
from aiohttp.client_proto import ResponseHandler
from aiohttp.connector import Connection

from sobafm.catalog import CatalogTrack

ENCODED_LIMIT = 16 * 1024**2
INPUT_CHUNK = 16 * 1024
IDLE_SECONDS = 15.0
type SessionFactory = Callable[[type[aiohttp.ClientResponse]], aiohttp.ClientSession]


class TrackFailure(StrEnum):
    INELIGIBLE = "ineligible"
    HTTP = "http"
    ENCODED_LIMIT = "encoded_limit"
    TRANSFER = "transfer"
    OUTPUT = "output"
    DURATION = "duration"
    DECODER = "decoder"
    DEADLINE = "deadline"
    CAPACITY = "capacity"
    CLEANUP = "cleanup"


class TrackError(Exception):
    """Only a fixed category survives; provider/native exceptions may retain audio."""

    def __init__(self, category: TrackFailure) -> None:
        super().__init__(category.value)
        self.category = category


async def _no_replay(
    request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
) -> aiohttp.ClientResponse:
    # Translate at the middleware boundary so aiohttp cannot silently replay a GET.
    try:
        return await handler(request)
    except TimeoutError:
        raise TrackError(TrackFailure.DEADLINE) from None
    except aiohttp.ClientError, OSError:
        raise TrackError(TrackFailure.TRANSFER) from None


def audio_session(response_class: type[aiohttp.ClientResponse]) -> aiohttp.ClientSession:
    """No inherited credentials/cookies, proxies, decompression or request logging."""
    return aiohttp.ClientSession(
        trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=False,
        read_bufsize=16 * 1024,
        response_class=response_class,
    )


class AudioStream:
    """One response, no redirect/HTTP compression, and an explicit verified EOF.

    The factory receives the observing response class before creating a dedicated
    credential-free session with no URL/header logging, default auth, cookies or
    proxy settings. One retained close task survives borrower cancellation; only
    successful owned closure observations satisfy closed. TrackDeck supplies the
    original cleanup deadline. Transport/audio storage remains separately charged
    and qualified; neither read size nor connection_lost proves its release.
    """

    def __init__(
        self,
        track: CatalogTrack,
        *,
        session_factory: SessionFactory = audio_session,
    ) -> None:
        self._track = CatalogTrack.model_validate(track)
        if not self._track.eligible:
            raise TrackError(TrackFailure.INELIGIBLE)
        self._responses: list[aiohttp.ClientResponse] = []
        self._observations: list[asyncio.Future[object]] = []
        self._unknown_closure = False
        self._starts = 0
        self._close_task: asyncio.Task[None] | None = None
        owner_ref = ref(self)

        class ObservedResponse(aiohttp.ClientResponse):
            async def start(self, connection: Connection) -> aiohttp.ClientResponse:
                # start installs an EOF hook that can release even before get
                # returns. Registration must precede that first superclass await.
                owner = owner_ref()
                if owner is None:
                    raise TrackError(TrackFailure.CLEANUP)
                owner._observe(self, connection)
                return await super().start(connection)

        self._session = session_factory(ObservedResponse)
        self.received = 0
        self.complete = False
        self._started = False

    def _observe(self, response: aiohttp.ClientResponse, connection: Connection) -> None:
        """Observe only the locked aiohttp 3.14.4 internal lifecycle signal.

        This is not a public transport guarantee or proof of TLS/audio storage
        release. Unknown shapes and extra starts retain debt, including failures
        before get has assigned this input's response.
        """
        self._starts += 1
        self._responses.append(response)
        try:
            protocol = connection.protocol
            if aiohttp.__version__ != "3.14.4" or type(protocol) is not ResponseHandler:
                self._unknown_closure = True
                return
            observation: object = protocol.closed
            if (
                not isinstance(observation, asyncio.Future)
                or observation.get_loop() is not asyncio.get_running_loop()
            ):
                self._unknown_closure = True
                return
            future = cast(asyncio.Future[object], observation)
            self._observations.append(future)
            future.add_done_callback(_retrieve_close_error)
        except Exception as error:  # noqa: BLE001 - unknown library shape cannot prove release
            _detach(error)
            self._unknown_closure = True

    def _observed_closed(self) -> bool:
        return (
            not self._unknown_closure
            and self._starts == len(self._observations) == 1
            and all(
                future.done()
                and not future.cancelled()
                and future.exception() is None
                and future.result() is None
                for future in self._observations
            )
        )

    async def chunks(self) -> AsyncGenerator[bytes]:
        if self._started or self._close_task is not None:
            raise TrackError(TrackFailure.TRANSFER)
        self._started = True
        try:
            async with asyncio.timeout(IDLE_SECONDS):
                response = await self._session.get(
                    self._track.audio.get_secret_value(),
                    allow_redirects=False,
                    auto_decompress=False,
                    headers={"Accept-Encoding": "identity"},
                    middlewares=(_no_replay,),
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=IDLE_SECONDS),
                )
            if all(original is not response for original in self._responses):
                self._responses.append(response)
            if (
                response.status != 200
                or response.headers.get("Content-Encoding", "identity") != "identity"
            ):
                raise TrackError(TrackFailure.HTTP)
            length_text = response.headers.get("Content-Length")
            declared: int | None = None
            if length_text is not None:
                if (
                    not length_text.isascii()
                    or not length_text.isdecimal()
                    or len(length_text) > 10
                ):
                    raise TrackError(TrackFailure.TRANSFER)
                declared = int(length_text)
                if declared > ENCODED_LIMIT:
                    raise TrackError(TrackFailure.ENCODED_LIMIT)
            while True:
                # The timeout covers expected network progress, not the consumer's
                # deliberate pipe/PCM backpressure while this generator is suspended.
                async with asyncio.timeout(IDLE_SECONDS):
                    chunk = await response.content.read(INPUT_CHUNK)
                if not chunk:
                    break
                if len(chunk) > INPUT_CHUNK or self.received + len(chunk) > ENCODED_LIMIT:
                    raise TrackError(TrackFailure.ENCODED_LIMIT)
                self.received += len(chunk)
                yield chunk
            if not response.content.at_eof() or (
                declared is not None and declared != self.received
            ):
                raise TrackError(TrackFailure.TRANSFER)
            self.complete = True
        except TimeoutError:
            raise TrackError(TrackFailure.DEADLINE) from None
        except aiohttp.ClientError, OSError:
            raise TrackError(TrackFailure.TRANSFER) from None
        finally:
            self.close()

    def close(self) -> None:
        if self._close_task is None:
            success = True
            for response in self._responses:
                try:
                    response.close()
                except Exception as error:  # noqa: BLE001 - still close the session and retain failure
                    _detach(error)
                    success = False
            self._close_task = asyncio.create_task(self._close(success), name="catalog HTTP close")
            self._close_task.add_done_callback(_retrieve_close_error)

    async def _close(self, success: bool) -> None:
        success &= await _finish_close(self._session.close())
        for response in self._responses:
            success &= await _finish_close(response.wait_for_close())
        # Even unexpected additional observations remain owned. Never cancel a
        # shared library future when an independently cancelled borrower leaves.
        for future in self._observations:
            success &= await _finish_close(asyncio.shield(future))
        if not success or not self._observed_closed():
            raise TrackError(TrackFailure.CLEANUP)

    async def aclose(self) -> None:
        self.close()
        await asyncio.shield(cast(asyncio.Task[None], self._close_task))

    @property
    def closed(self) -> bool:
        task = self._close_task
        return (
            task is not None
            and task.done()
            and not task.cancelled()
            and task.exception() is None
            and self._observed_closed()
        )


def _detach(error: BaseException) -> None:
    error.__traceback__ = error.__context__ = error.__cause__ = None


def _retrieve_close_error[T](future: asyncio.Future[T]) -> None:
    if not future.cancelled() and (error := future.exception()) is not None:
        _detach(error)


async def _finish_close(operation: Awaitable[object]) -> bool:
    try:
        await operation
    except (Exception, asyncio.CancelledError) as error:
        _detach(error)
        task = asyncio.current_task()
        if isinstance(error, asyncio.CancelledError) and task is not None and task.cancelling():
            raise
        return False
    return True
