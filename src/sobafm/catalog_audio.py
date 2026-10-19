"""Owned, incremental audio HTTP; discovery never implies playback eligibility."""

import asyncio
from collections.abc import AsyncGenerator
from enum import StrEnum

import aiohttp

from sobafm.catalog import CatalogTrack

ENCODED_LIMIT = 16 * 1024**2
INPUT_CHUNK = 16 * 1024
IDLE_SECONDS = 15.0


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


class AudioStream:
    """One response, no redirect/HTTP compression, and an explicit verified EOF.

    The owner supplies a dedicated credential-free session with no URL/header
    logging, default auth, cookies or proxy settings. Its transport allocation is
    part of the source's separately qualified reservation, not proven by read size.
    """

    def __init__(self, session: aiohttp.ClientSession, track: CatalogTrack) -> None:
        self._session = session
        self._track = CatalogTrack.model_validate(track)
        if not self._track.eligible:
            raise TrackError(TrackFailure.INELIGIBLE)
        self._response: aiohttp.ClientResponse | None = None
        self.received = 0
        self.complete = False
        self._started = False

    async def chunks(self) -> AsyncGenerator[bytes]:
        if self._started:
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
            self._response = response
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
        if self._response is not None:
            self._response.close()

    async def aclose(self) -> None:
        self.close()
        await self._session.close()

    @property
    def closed(self) -> bool:
        return self._session.closed and (self._response is None or self._response.closed)


def audio_session() -> aiohttp.ClientSession:
    """No inherited credentials/cookies, proxies, decompression or request logging."""
    return aiohttp.ClientSession(
        trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=False,
        read_bufsize=16 * 1024,
    )
