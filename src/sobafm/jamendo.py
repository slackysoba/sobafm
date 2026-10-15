"""Bounded Jamendo API v3 metadata discovery; never fetch audio or arbitrary credit sources."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Annotated, Literal, Self

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from sobafm.catalog import (
    CatalogQuery,
    CatalogText,
    CatalogTrack,
    License,
    TrackId,
    stream_url_allowed,
)
from sobafm.config import ApiKey
from sobafm.failures import Failure, call_failure, status_failure

TRACKS_URL = "https://api.jamendo.com/v3.0/tracks/"
MAX_BODY = 512 * 1024
ATTEMPT_SECONDS = 10
CYCLE_SECONDS = 30
MAX_ATTEMPTS = 8
PAGE_SIZE = 10
log = logging.getLogger(__name__)


class CatalogError(Exception):
    """Fixed safe metadata failure, without a provider body, URL or chained exception."""

    def __init__(
        self,
        category: Literal["http", "api", "invalid", "oversized", "connection", "deadline"],
        failure: Failure | None = None,
        *,
        status: int | None = None,
        code: int | None = None,
        retry_after: float = 0,
    ) -> None:
        super().__init__(f"Jamendo metadata {category}")
        self.category = category
        self.failure = failure
        self.status = status
        self.code = code
        self.retry_after = retry_after


class External(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)


class Headers(External):
    status: Literal["success", "succeed", "failed"]
    code: int = Field(ge=0, le=9999)
    results_count: int = Field(ge=0, le=PAGE_SIZE)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.code == 0) != (self.status != "failed"):
            raise ValueError("inconsistent response status")
        return self


class Tags(External):
    genres: list[CatalogText] = Field(max_length=100)
    instruments: list[CatalogText] = Field(max_length=100)
    vartags: list[CatalogText] = Field(max_length=100)


class MusicInfo(External):
    tags: Tags


class Track(External):
    id: TrackId
    name: CatalogText
    artist_name: CatalogText
    duration: int = Field(gt=0, le=900)
    license_ccurl: Annotated[str, Field(max_length=500)]
    audio: Annotated[str, Field(max_length=4096)] = Field(repr=False, exclude=True)
    musicinfo: MusicInfo


class Reply(External):
    headers: Headers
    results: list[Track] = Field(max_length=PAGE_SIZE)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.headers.results_count != len(self.results):
            raise ValueError("result count mismatch")
        if len({track.id for track in self.results}) != len(self.results):
            raise ValueError("duplicate response tracks")
        if self.headers.code and self.results:
            raise ValueError("failed response contains tracks")
        return self


class MetadataGate:
    """One shared gate per process: two calls in flight and one new call per second.

    Pass the same gate to every station's client. Waiting, including provider backoff, consumes
    the caller's cycle deadline; cancellation releases acquired capacity normally.
    """

    def __init__(self) -> None:
        self._slots = asyncio.Semaphore(2)
        self._starts = asyncio.Lock()
        self._next_start = 0.0

    def defer(self, seconds: float) -> None:
        self._next_start = max(self._next_start, time.monotonic() + seconds)

    @asynccontextmanager
    async def call(self) -> AsyncGenerator[None]:
        async with self._slots:
            async with self._starts:
                # Recheck after waking because another completed call may extend Retry-After.
                while (delay := self._next_start - time.monotonic()) > 0:  # noqa: ASYNC110 - timer, not polling
                    await asyncio.sleep(delay)
                self._next_start = time.monotonic() + 1
            yield


def _retry_after(value: str | None) -> float:
    if value is None or len(value) > 100:
        return 0
    if value.isascii() and value.isdecimal():
        return float(value)
    try:
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return 0
        return max(0, (stamp - datetime.now(UTC)).total_seconds())
    except ValueError, TypeError, OverflowError:
        return 0


def _steps(query: CatalogQuery) -> tuple[dict[str, str], ...]:
    filters = {
        field: value
        for field in ("speed", "vocalinstrumental", "acousticelectric")
        if (value := getattr(query, field)) is not None
    }
    return (
        filters | {"tags": "+".join(query.tags)},
        filters | {"fuzzytags": "+".join(query.tags)},
        filters | {"search": query.search or " ".join(query.tags)},
    )


async def _no_transport_replay(
    request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
) -> aiohttp.ClientResponse:
    """Keep transport retries inside the application's attempt, rate and deadline budget.

    aiohttp may replay an idempotent request once after a send/response-start disconnect. Map
    that failure at its public middleware boundary so it cannot bypass our gate and backoff.
    """
    try:
        return await handler(request)
    except (aiohttp.ClientError, OSError) as error:
        raise CatalogError("connection", call_failure(error)) from None


class JamendoCatalog:
    """One station's client, sharing the process metadata gate and an owned aiohttp session.

    The caller owns/closes the session and must not install tracing that logs requests. aiohttp
    does not log outbound URLs by default. Search logs only fixed categories/numeric codes/ids.
    """

    def __init__(
        self, client_id: ApiKey, session: aiohttp.ClientSession, gate: MetadataGate
    ) -> None:
        self._client_id = client_id
        self._session = session
        self._gate = gate
        self._search_lock = asyncio.Lock()

    async def search(self, query: CatalogQuery) -> tuple[CatalogTrack, ...]:
        # Revalidate even constructed/copied models before they trigger I/O.
        query = CatalogQuery.model_validate(query.model_dump())
        try:
            async with asyncio.timeout(CYCLE_SECONDS), self._search_lock:
                return await self._search(query)
        except TimeoutError:
            raise CatalogError("deadline", Failure.UNAVAILABLE) from None

    async def _search(self, query: CatalogQuery) -> tuple[CatalogTrack, ...]:
        attempts, retries = 0, 0
        for step in _steps(query):
            had_results = False
            for offset in (0, PAGE_SIZE):
                while True:
                    if attempts >= MAX_ATTEMPTS:
                        raise CatalogError("deadline", Failure.UNAVAILABLE)
                    attempts += 1
                    try:
                        reply = await self._fetch(step | {"offset": str(offset)})
                        break
                    except CatalogError as error:
                        log.warning(
                            "Jamendo metadata failure category=%s failure=%s status=%s code=%s",
                            error.category,
                            error.failure,
                            error.status,
                            error.code,
                        )
                        if error.failure != Failure.UNAVAILABLE or retries >= 2:
                            raise
                        retries += 1
                        await asyncio.sleep(max(retries, error.retry_after))
                if not reply.results:
                    break
                had_results = True
                tracks: list[CatalogTrack] = []
                for track in reply.results:
                    try:
                        license_ = License.from_url(track.license_ccurl)
                    except ValueError:
                        log.info("Jamendo candidate %s ineligible: license", track.id)
                        continue
                    if not stream_url_allowed(track.audio):
                        log.info("Jamendo candidate %s ineligible: audio_url", track.id)
                        continue
                    tags = track.musicinfo.tags
                    tracks.append(
                        CatalogTrack(
                            id=track.id,
                            title=track.name,
                            artist=track.artist_name,
                            duration=track.duration,
                            license=license_,
                            audio=SecretStr(track.audio),
                            tags=tuple(
                                dict.fromkeys((*tags.genres, *tags.instruments, *tags.vartags))
                            ),
                        )
                    )
                if tracks:
                    return tuple(tracks)
                if len(reply.results) < PAGE_SIZE:
                    break
            # Filtering an unsafe/unsupported candidate is not a successful empty provider reply.
            if had_results:
                return ()
        return ()

    async def _fetch(self, params: dict[str, str]) -> Reply:
        params = {
            "client_id": self._client_id.get_secret_value(),
            "format": "json",
            "limit": str(PAGE_SIZE),
            "include": "musicinfo",
            "order": "relevance",
            "audioformat": "mp31",
            "type": "single albumtrack",
            "durationbetween": "1_900",
        } | params
        try:
            async with self._gate.call(), asyncio.timeout(ATTEMPT_SECONDS):  # noqa: SIM117 - timeout starts after admission
                async with self._session.get(
                    TRACKS_URL,
                    params=params,
                    allow_redirects=False,
                    auto_decompress=True,
                    middlewares=(_no_transport_replay,),
                    timeout=aiohttp.ClientTimeout(total=ATTEMPT_SECONDS),
                ) as response:
                    delay = _retry_after(response.headers.get("Retry-After"))
                    if delay:
                        self._gate.defer(delay)
                    if response.status != 200:
                        raise CatalogError(
                            "http",
                            status_failure(response.status),
                            status=response.status,
                            retry_after=delay,
                        )
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(16_384):
                        if len(body) + len(chunk) > MAX_BODY:
                            raise CatalogError("oversized")
                        body.extend(chunk)
                    try:
                        reply = Reply.model_validate_json(body)
                    except ValidationError:
                        raise CatalogError("invalid") from None
                    if reply.headers.code:
                        code = reply.headers.code
                        failure = (
                            Failure.REJECTED
                            if code in (5, 11, 12, 13, 101)
                            else Failure.EXHAUSTED
                            if code == 6
                            else Failure.UNAVAILABLE
                            if code == 1
                            else None
                        )
                        raise CatalogError("api", failure, code=code, retry_after=delay)
                    return reply
        except (aiohttp.ClientError, OSError) as error:
            raise CatalogError("connection", call_failure(error)) from None
