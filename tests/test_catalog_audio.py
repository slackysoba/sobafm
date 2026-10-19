import asyncio
from typing import Any, cast

import aiohttp
import pytest

import sobafm.catalog_audio as audio
from sobafm.catalog_audio import AudioStream, TrackError
from tests.test_catalog import credit, discovered


class Response:
    def __init__(self, body: bytes = b"mp3", *, headers: dict[str, str] | None = None) -> None:
        self.body, self.headers = body, headers if headers is not None else {}
        self.status = 200
        self.content = self
        self.closed = False
        self.offset = 0
        self.error: Exception | None = None
        self.eof = True

    async def read(self, size: int) -> bytes:
        if self.error is not None:
            raise self.error
        value = self.body[self.offset : self.offset + size]
        self.offset += len(value)
        return value

    def at_eof(self) -> bool:
        return self.eof and self.offset == len(self.body)

    def close(self) -> None:
        self.closed = True


class Session:
    def __init__(self, response: Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.gate: asyncio.Event | None = None
        self.error: Exception | None = None

    async def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append(dict(url=url, **kwargs))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self) -> None:
        self.closed = True


def stream(response: Response) -> tuple[AudioStream, Session]:
    session = Session(response)
    track = discovered(duration=1).with_credits(credit())
    return AudioStream(cast(aiohttp.ClientSession, session), track), session


async def collect(source: AudioStream) -> bytes:
    return b"".join([part async for part in source.chunks()])


async def test_incremental_owned_http_has_explicit_clean_eof() -> None:
    response = Response(b"mp3" * 10_000, headers={"Content-Length": "30000"})
    source, session = stream(response)
    chunks = [chunk async for chunk in source.chunks()]
    assert max(map(len, chunks)) <= 16384
    assert b"".join(chunks) == response.body
    assert source.complete
    assert response.closed
    assert source.received == 30000
    call = session.calls[0]
    assert call["allow_redirects"] is False
    assert call["auto_decompress"] is False
    assert call["headers"] == {"Accept-Encoding": "identity"}
    assert len(call["middlewares"]) == 1
    assert call["timeout"].total is None
    await source.aclose()
    assert source.closed


@pytest.mark.parametrize("status", [201, 206, 301, 302, 307, 308, 401, 403, 404, 429, 500])
async def test_non_200_never_reads_or_follows(status: int) -> None:
    response = Response()
    response.status = status
    source, session = stream(response)
    with pytest.raises(TrackError, match=r"^http$"):
        await collect(source)
    assert not source.complete
    assert source.received == 0
    assert response.closed
    assert len(session.calls) == 1
    await source.aclose()


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "unknown"])
async def test_no_hidden_http_decompression(encoding: str) -> None:
    source, _ = stream(Response(headers={"Content-Encoding": encoding}))
    with pytest.raises(TrackError, match=r"^http$"):
        await collect(source)
    await source.aclose()


@pytest.mark.parametrize("length", ["-1", "3.0", "+3", " 3", "٣", "x", "99999999999"])
async def test_malformed_length_fails_before_body(length: str) -> None:
    source, _ = stream(Response(headers={"Content-Length": length}))
    with pytest.raises(TrackError, match=r"^transfer$"):
        await collect(source)
    assert source.received == 0
    await source.aclose()


@pytest.mark.parametrize("length", ["2", "4"])
async def test_length_disagreement_is_never_complete(length: str) -> None:
    source, _ = stream(Response(headers={"Content-Length": length}))
    with pytest.raises(TrackError, match=r"^transfer$"):
        await collect(source)
    assert not source.complete
    await source.aclose()


async def test_advertised_oversize_rejected_without_body() -> None:
    source, _ = stream(Response(headers={"Content-Length": str(audio.ENCODED_LIMIT + 1)}))
    with pytest.raises(TrackError, match=r"^encoded_limit$"):
        await collect(source)
    assert source.received == 0
    await source.aclose()


@pytest.mark.parametrize("count", [10, 11])
async def test_actual_counted_cap_with_no_length(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    monkeypatch.setattr(audio, "ENCODED_LIMIT", 10)
    source, _ = stream(Response(b"x" * count))
    if count == 10:
        assert await collect(source) == b"x" * 10
        assert source.complete
    else:
        with pytest.raises(TrackError, match=r"^encoded_limit$"):
            await collect(source)
        assert not source.complete
        assert source.received == 0
    await source.aclose()


@pytest.mark.parametrize(
    "error",
    [
        OSError("secret-url"),
        aiohttp.ClientPayloadError("secret-body"),
        TimeoutError("secret-timeout"),
    ],
)
async def test_failure_fixed_category_not_private_text(error: Exception) -> None:
    response = Response()
    response.error = error
    source, _ = stream(response)
    expected = "deadline" if isinstance(error, TimeoutError) else "transfer"
    with pytest.raises(TrackError, match=r"^" + expected + "$"):
        await collect(source)
    assert not source.complete
    assert response.closed
    await source.aclose()


async def test_short_read_without_clean_eof_is_failure() -> None:
    response = Response()
    response.eof = False
    source, _ = stream(response)
    with pytest.raises(TrackError, match=r"^transfer$"):
        await collect(source)
    await source.aclose()


async def test_one_shot_is_claimed_before_get_await_and_survives_cancel() -> None:
    source, session = stream(Response())
    session.gate = asyncio.Event()
    first = asyncio.create_task(collect(source))
    await asyncio.sleep(0)
    with pytest.raises(TrackError, match=r"^transfer$"):
        await collect(source)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(TrackError, match=r"^transfer$"):
        await collect(source)
    assert len(session.calls) == 1
    await source.aclose()


async def test_consumer_backpressure_does_not_use_network_idle_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio, "IDLE_SECONDS", 0.001)
    source, _ = stream(Response(b"x" * 20000))
    iterator = source.chunks()
    await anext(iterator)
    await asyncio.sleep(0.005)
    assert await anext(iterator) == b"x" * (20000 - 16384)
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert source.complete
    await source.aclose()


def test_uncredited_track_never_triggers_get() -> None:
    session = Session(Response())
    with pytest.raises(TrackError, match=r"^ineligible$"):
        AudioStream(cast(aiohttp.ClientSession, session), discovered())
    assert not session.calls
