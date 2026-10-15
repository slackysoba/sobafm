import asyncio
import gzip
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any, cast

import aiohttp
import pytest
from pydantic import SecretStr, ValidationError

import sobafm.jamendo as jamendo
from sobafm.catalog import Catalog, CatalogQuery, License
from sobafm.failures import Failure
from sobafm.jamendo import CatalogError, JamendoCatalog, MetadataGate, stream_url_allowed
from tests.doubles import FakeCatalog

CLIENT = "private-client-id"
AUDIO = "https://prod-1.storage.jamendo.com/?trackid=42&client_id=private-client-id"
QUERY = CatalogQuery(
    tags=("jazz", "piano"),
    speed="low",
    vocalinstrumental="instrumental",
    acousticelectric="acoustic",
    search="quiet jazz piano",
)


def track(**changes: object) -> dict[str, object]:
    return {
        "id": "42",
        "name": "Song",
        "artist_name": "Artist",
        "duration": 120,
        "license_ccurl": "http://creativecommons.org/licenses/by-nc-nd/3.0/",
        "audio": AUDIO,
        "musicinfo": {"tags": {"genres": ["jazz"], "instruments": ["piano"], "vartags": []}},
    } | changes


def reply(*tracks: dict[str, object], code: int = 0, **headers: object) -> bytes:
    return json.dumps(
        {
            "headers": {
                "status": "failed" if code else "success",
                "code": code,
                "results_count": len(tracks),
            }
            | headers,
            "results": tracks,
        }
    ).encode()


class Response:
    def __init__(self, body: bytes, status: int = 200, retry_after: str | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = {"Retry-After": retry_after} if retry_after is not None else {}
        self.content = self
        self.closed = False

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]

    async def __aenter__(self) -> Response:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True


class HTTP:
    def __init__(self, *responses: Response | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append({"url": url} | kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> MetadataGate:
    value = MetadataGate()

    # Most tests exercise validation without real clock delays; dedicated gate tests use its timer.
    @asynccontextmanager
    async def immediate() -> AsyncGenerator[None]:
        yield

    monkeypatch.setattr(value, "call", immediate)
    return value


def client(http: HTTP, gate: MetadataGate) -> JamendoCatalog:
    return JamendoCatalog(SecretStr(CLIENT), cast(aiohttp.ClientSession, http), gate)


async def test_search_retains_explicit_filters_and_only_relaxes_valid_empty(
    gate: MetadataGate,
) -> None:
    http = HTTP(Response(reply()), Response(reply()), Response(reply(track())))
    catalog: Catalog = client(http, gate)
    tracks = await catalog.search(QUERY)
    assert len(tracks) == 1
    assert tracks[0].license is License.BY_NC_ND
    assert tracks[0].audio.get_secret_value() == AUDIO
    assert tracks[0].tags == ("jazz", "piano")
    assert tracks[0].url == "https://www.jamendo.com/track/42"
    assert not tracks[0].eligible  # Discovery cannot prove complete supplied notices.
    for call in http.calls:
        assert call["url"] == jamendo.TRACKS_URL
        assert call["allow_redirects"] is False
        assert call["params"] | {} == {
            "client_id": CLIENT,
            "format": "json",
            "limit": "10",
            "offset": "0",
            "include": "musicinfo",
            "audioformat": "mp31",
            "type": "single albumtrack",
            "durationbetween": "1_900",
            "order": "relevance",
            "speed": "low",
            "vocalinstrumental": "instrumental",
            "acousticelectric": "acoustic",
        } | {
            key: value
            for key, value in call["params"].items()
            if key in ("tags", "fuzzytags", "search")
        }
    assert [
        (key, call["params"][key])
        for key, call in zip(("tags", "fuzzytags", "search"), http.calls, strict=True)
    ] == [("tags", "jazz+piano"), ("fuzzytags", "jazz+piano"), ("search", "quiet jazz piano")]


@pytest.mark.parametrize("license_", list(License))
async def test_accepts_each_exact_license_without_narrowing(
    gate: MetadataGate, license_: License
) -> None:
    tracks = await client(HTTP(Response(reply(track(license_ccurl=license_.url)))), gate).search(
        QUERY
    )
    assert tracks[0].license is license_


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "0"},
        {"id": 42},
        {"name": " "},
        {"artist_name": None},
        {"duration": 0},
        {"duration": 901},
        {"duration": True},
        {"duration": 12.5},
        {"musicinfo": {}},
        {"musicinfo": {"tags": {"genres": "jazz", "instruments": [], "vartags": []}}},
    ],
)
async def test_invalid_metadata_is_failure_never_relaxed(
    gate: MetadataGate, changes: dict[str, object]
) -> None:
    http = HTTP(Response(reply(track(**changes))))
    with pytest.raises(CatalogError, match="invalid"):
        await client(http, gate).search(QUERY)
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"bad json",
        reply(results_count=1),
        reply(track(), track()),
        reply(code=5, status="success"),
        reply(track(), code=5),
    ],
)
async def test_invalid_envelopes_cannot_become_no_match(gate: MetadataGate, body: bytes) -> None:
    with pytest.raises(CatalogError, match="invalid"):
        await client(HTTP(Response(body)), gate).search(QUERY)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (301, None),
        (302, None),
        (307, None),
        (308, None),
        (400, None),
        (401, Failure.REJECTED),
        (403, Failure.REJECTED),
        (429, Failure.EXHAUSTED),
    ],
)
async def test_http_failure_mapping_is_terminal(
    gate: MetadataGate, status: int, expected: Failure | None
) -> None:
    response = Response(b"private-client-id provider text", status)
    http = HTTP(response)
    with pytest.raises(CatalogError) as caught:
        await client(http, gate).search(QUERY)
    assert caught.value.failure == expected
    assert caught.value.status == status
    assert len(http.calls) == 1
    assert response.closed


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (2, None),
        (3, None),
        (4, None),
        (5, Failure.REJECTED),
        (6, Failure.EXHAUSTED),
        (7, None),
        (8, None),
        (9, None),
        (10, None),
        (11, Failure.REJECTED),
        (12, Failure.REJECTED),
        (13, Failure.REJECTED),
        (21, None),
        (22, None),
        (23, None),
        (24, None),
        (101, Failure.REJECTED),
        (999, None),
    ],
)
async def test_api_failure_codes_do_not_relax(
    gate: MetadataGate, code: int, expected: Failure | None
) -> None:
    http = HTTP(Response(reply(code=code)))
    with pytest.raises(CatalogError) as caught:
        await client(http, gate).search(QUERY)
    assert (caught.value.failure, caught.value.code) == (expected, code)
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "outcome",
    [
        OSError(CLIENT),
        aiohttp.ClientPayloadError(AUDIO),
        TimeoutError(CLIENT),
        Response(reply(code=1)),
        Response(b"upstream error", 503),
    ],
)
async def test_transient_failures_retry_at_most_twice(
    monkeypatch: pytest.MonkeyPatch, gate: MetadataGate, outcome: Response | Exception
) -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(jamendo.asyncio, "sleep", sleep)
    http = HTTP(outcome, outcome, outcome)
    with pytest.raises(CatalogError) as caught:
        await client(http, gate).search(QUERY)
    assert caught.value.failure is Failure.UNAVAILABLE
    assert delays == [1, 2]
    assert len(http.calls) == 3


async def test_unsupported_candidates_do_not_trigger_relaxation(gate: MetadataGate) -> None:
    http = HTTP(
        Response(reply(track(license_ccurl="https://creativecommons.org/licenses/by/4.0/")))
    )
    assert await client(http, gate).search(QUERY) == ()
    assert len(http.calls) == 1


async def test_pages_are_bounded_and_keep_stage(gate: MetadataGate) -> None:
    page = reply(*(track(id=str(i), audio="http://unsafe.invalid/") for i in range(1, 11)))
    http = HTTP(Response(page), Response(page))
    assert await client(http, gate).search(QUERY) == ()
    assert [call["params"]["offset"] for call in http.calls] == ["0", "10"]
    assert all("tags" in call["params"] for call in http.calls)


async def test_oversized_decompressed_body_is_closed(gate: MetadataGate) -> None:
    response = Response(b" " * (jamendo.MAX_BODY + 1))
    with pytest.raises(CatalogError, match="oversized"):
        await client(HTTP(response), gate).search(QUERY)
    assert response.closed


async def test_redacts_secrets_urls_provider_text_and_invalid_input(
    gate: MetadataGate, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        tracks = await client(HTTP(Response(reply(track()))), gate).search(QUERY)
        with pytest.raises(CatalogError) as caught:
            await client(HTTP(Response(reply(track(id=CLIENT, audio=AUDIO)))), gate).search(QUERY)
    for text in (
        caplog.text,
        str(caught.value),
        repr(caught.value),
        repr(tracks),
        tracks[0].model_dump_json(),
    ):
        assert CLIENT not in text
        assert AUDIO not in text
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "url",
    [
        "http://storage.jamendo.com/",
        "https://storage.jamendo.com.evil.org/",
        "https://evil.org/storage.jamendo.com",
        "https://user:password@storage.jamendo.com/",
        "https://storage.jamendo.com:444/",
        "https://storage.jamendo.com/#fragment",
        "https://storage.jamendo.com/\n",
        "https://storage.jamendo.com/%0a",
        "https://storage.jamendo.com\\@evil.org/",
        "https://.storage.jamendo.com/",
        "https://storage.jamendo.com./",
        "https://127.0.0.1/",
        "file:///audio.mp3",
    ],
)
def test_rejects_unsafe_audio_urls(url: str) -> None:
    assert not stream_url_allowed(url)


async def test_catalog_double_has_same_discovery_contract(gate: MetadataGate) -> None:
    tracks = await client(HTTP(Response(reply(track()))), gate).search(QUERY)
    catalog: Catalog = FakeCatalog(tracks, CatalogError("invalid"))
    assert await catalog.search(QUERY) == tracks
    with pytest.raises(CatalogError):
        await catalog.search(QUERY)


async def test_constructed_invalid_query_never_reaches_http(gate: MetadataGate) -> None:
    http = HTTP()
    with pytest.raises(ValidationError):
        await client(http, gate).search(QUERY.model_copy(update={"speed": "private"}))
    assert not http.calls


async def test_retry_after_is_honored_without_extending_cycle_deadline(
    monkeypatch: pytest.MonkeyPatch, gate: MetadataGate
) -> None:
    monkeypatch.setattr(jamendo, "CYCLE_SECONDS", 0.03)
    http = HTTP(Response(b"down", 503, retry_after="120"))
    with pytest.raises(CatalogError, match="deadline"):
        await client(http, gate).search(QUERY)
    assert len(http.calls) == 1


@pytest.mark.parametrize("value", ["7", "date"])
async def test_retry_after_numbers_and_dates_apply_to_other_clients(
    gate: MetadataGate, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    delays: list[float] = []
    if value == "date":
        value = format_datetime(datetime.now(UTC) + timedelta(minutes=1))
    monkeypatch.setattr(gate, "defer", delays.append)
    with pytest.raises(CatalogError) as caught:
        await client(HTTP(Response(b"quota", 429, retry_after=value)), gate).search(QUERY)
    assert caught.value.failure is Failure.EXHAUSTED
    assert len(delays) == 1
    assert delays[0] > 0


async def test_attempt_timeout_closes_response_and_respects_cycle_backoff(
    monkeypatch: pytest.MonkeyPatch, gate: MetadataGate
) -> None:
    class Stalled(Response):
        async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
            await asyncio.Event().wait()
            yield b""

    monkeypatch.setattr(jamendo, "ATTEMPT_SECONDS", 0.01)
    monkeypatch.setattr(jamendo, "CYCLE_SECONDS", 0.05)
    response = Stalled(b"")
    http = HTTP(response)
    with pytest.raises(CatalogError, match="deadline"):
        await client(http, gate).search(QUERY)
    assert response.closed
    assert len(http.calls) == 1


async def test_cancellation_closes_response_and_releases_station_lock(gate: MetadataGate) -> None:
    entered = asyncio.Event()

    class Stalled(Response):
        async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
            entered.set()
            await asyncio.Event().wait()
            yield b""

    stalled = Stalled(b"")
    http = HTTP(stalled, Response(reply(track())))
    catalog = client(http, gate)
    pending = asyncio.create_task(catalog.search(QUERY))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert stalled.closed
    assert (await catalog.search(QUERY))[0].id == "42"


async def test_one_metadata_call_per_station(gate: MetadataGate) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Stalled(Response):
        async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
            entered.set()
            await release.wait()
            yield self.body

    http = HTTP(Stalled(reply(track())), Response(reply(track())))
    catalog = client(http, gate)
    first = asyncio.create_task(catalog.search(QUERY))
    await entered.wait()
    second = asyncio.create_task(catalog.search(QUERY))
    await asyncio.sleep(0)
    assert len(http.calls) == 1
    release.set()
    await asyncio.gather(first, second)
    assert len(http.calls) == 2


async def test_shared_gate_spaces_starts_caps_concurrency_and_releases_cancelled_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    original_sleep = asyncio.sleep

    async def advance(delay: float) -> None:
        nonlocal now
        now += delay
        await original_sleep(0)

    monkeypatch.setattr(jamendo, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(jamendo.asyncio, "sleep", advance)
    gate = MetadataGate()
    started: list[float] = []
    full, release = asyncio.Event(), asyncio.Event()

    async def call() -> None:
        async with gate.call():
            started.append(now)
            if len(started) == 2:
                full.set()
            await release.wait()

    first, second = asyncio.create_task(call()), asyncio.create_task(call())
    await full.wait()
    waiting = asyncio.create_task(call())
    await original_sleep(0)
    assert started == [0, 1]
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    gate.defer(5)
    release.set()
    await asyncio.gather(first, second)
    await call()
    assert started == [0, 1, 6]


async def test_actual_http_decompression_cannot_bypass_body_cap(
    monkeypatch: pytest.MonkeyPatch, gate: MetadataGate
) -> None:
    compressed = gzip.compress(b" " * (jamendo.MAX_BODY + 1))
    closed = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: "
                + str(len(compressed)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + compressed
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(jamendo, "TRACKS_URL", f"http://127.0.0.1:{port}/")
        async with aiohttp.ClientSession(auto_decompress=False) as session:
            with pytest.raises(CatalogError, match="oversized"):
                await JamendoCatalog(SecretStr(CLIENT), session, gate).search(QUERY)
        await closed.wait()
