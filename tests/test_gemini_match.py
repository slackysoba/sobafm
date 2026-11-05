"""Offline Gemini matcher checks; the real transport only ever talks to loopback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import aiohttp
import pytest
from aiohttp import web

from sobafm.catalog import MAX_DETAILS, CatalogInputError, Fact, Feature, Recording, Strength
from sobafm.discord_adapter import GEMINI_KEY_VARIABLE
from sobafm.gemini_match import (
    API_REVISION,
    ENDPOINT,
    MAX_RANKED,
    MAX_RESPONSE_BYTES,
    MODEL,
    NO_RECORDINGS,
    AiohttpTransport,
    Fit,
    GeminiMatcher,
    Matched,
    MatchFailed,
    Refused,
    checked_request,
    output_text,
    parse_plan,
    render_songs,
)
from tests.test_discord_adapter import LINK, recording

KEY = "placeholder-gemini-value"  # pragma: allowlist secret - test placeholder
SONGS = (recording("a"), recording("b"), recording("c"))


def interaction(text: str, status: str = "completed") -> bytes:
    return json.dumps(
        {
            "status": status,
            "steps": [
                {"type": "thought", "summary": []},
                {"type": "model_output", "content": [{"type": "text", "text": text}]},
            ],
        }
    ).encode()


def plan(*ranked: tuple[object, object], outcome: str = "match") -> str:
    return json.dumps({"outcome": outcome, "ranked": [{"song": s, "fit": f} for s, f in ranked]})


class FakeTransport:
    def __init__(
        self, payload: bytes = b"", *, status: int = 200, error: BaseException | None = None
    ) -> None:
        self.payload = payload
        self.status = status
        self.error = error
        self.calls: list[tuple[str, dict[str, str], bytes]] = []
        self.closed = False

    async def post(self, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        self.calls.append((url, dict(headers), body))
        if self.error is not None:
            raise self.error
        return self.status, self.payload

    async def close(self) -> None:
        self.closed = True


def matcher(transport: FakeTransport, recordings: tuple[Recording, ...] = SONGS) -> GeminiMatcher:
    return GeminiMatcher(recordings, KEY, transport=transport)


def sent_body(transport: FakeTransport) -> dict[str, object]:
    ((_, _, body),) = transport.calls
    return cast(dict[str, object], json.loads(body))


def test_a_match_makes_one_call_with_the_documented_request_shape() -> None:
    transport = FakeTransport(interaction(plan((2, "loose"), (0, "close"))))
    result = asyncio.run(matcher(transport).match('crazy "quoted" REQUEST:\nrequest'))
    assert isinstance(result, Matched)
    assert [(r.recording, r.fit) for r in result.ranked] == [
        (SONGS[2], Fit.LOOSE),
        (SONGS[0], Fit.CLOSE),
    ]
    ((url, headers, raw),) = transport.calls
    assert url == ENDPOINT
    assert headers == {
        "Content-Type": "application/json",
        "x-goog-api-key": KEY,
        "Api-Revision": API_REVISION,
    }
    assert KEY.encode() not in raw
    body = sent_body(transport)
    assert body["model"] == MODEL and body["store"] is False
    assert set(body) == {
        "model",
        "system_instruction",
        "input",
        "response_format",
        "generation_config",
        "store",
    }
    text = cast(str, body["input"])
    assert text.startswith("SONGS:\n" + render_songs(SONGS))
    # JSON encoding keeps the request's own quotes and newlines inside one string.
    assert text.endswith('REQUEST:\n"crazy \\"quoted\\" REQUEST:\\nrequest"')
    response_format = cast(dict[str, object], body["response_format"])
    assert response_format["type"] == "text" and response_format["mime_type"] == "application/json"
    schema = cast(dict[str, dict[str, dict[str, object]]], response_format["schema"])
    ranked = schema["properties"]["ranked"]
    assert ranked["maxItems"] == MAX_RANKED
    items = cast(dict[str, dict[str, dict[str, object]]], ranked["items"])
    assert items["properties"]["song"]["maximum"] == len(SONGS) - 1
    assert items["properties"]["fit"]["enum"] == ["exact", "close", "loose"]


def test_a_refusal_returns_no_recordings() -> None:
    transport = FakeTransport(interaction(plan(outcome="refuse")))
    assert isinstance(asyncio.run(matcher(transport).match("what is the capital?")), Refused)


def test_no_call_is_made_without_admitted_recordings() -> None:
    transport = FakeTransport()
    with pytest.raises(MatchFailed) as error:
        asyncio.run(matcher(transport, ()).match("anything"))
    assert error.value.kind == NO_RECORDINGS and transport.calls == []


def test_songs_describe_everything_useful_and_nothing_else() -> None:
    rich = replace(
        recording("a"),
        creators=("Synthetic Artist", "Guest"),
        composition="A Work",
        composer="A Composer",
        facts=(
            Fact(Feature.PIANO, True, Strength.STRONG, LINK),
            Fact(Feature.VOCALS, False, Strength.WEAK, LINK),
        ),
        details=("genre: dark cabaret", "bpm: 94"),
    )
    plain = replace(recording("b"), duration_seconds=None, facts=())
    assert json.loads(render_songs((rich, plain))) == [
        {
            "song": 0,
            "title": "Song a",
            "artist": "Synthetic Artist",
            "with": ["Guest"],
            "seconds": 180,
            "composition": "A Work",
            "composer": "A Composer",
            "labels": ["piano", "no vocals"],
            "details": ["genre: dark cabaret", "bpm: 94"],
        },
        {"song": 1, "title": "Song b", "artist": "Synthetic Artist"},
    ]
    assert "credit-a" not in render_songs((rich,)) and LINK not in render_songs((rich,))


def test_a_catalog_too_large_to_send_is_refused_at_startup() -> None:
    details = tuple(f"{n}: " + "x" * 150 for n in range(MAX_DETAILS))
    many = tuple(replace(recording(str(n)), details=details) for n in range(150))
    with pytest.raises(CatalogInputError, match="too large"):
        GeminiMatcher(many, KEY, transport=FakeTransport())


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("not json", "output_not_json"),
        ("[]", "output_shape"),
        ('{"outcome":"match"}', "output_shape"),
        ('{"outcome":"match","ranked":[],"extra":1}', "output_shape"),
        ('{"outcome":"match","ranked":{}}', "output_shape"),
        ('{"outcome":"maybe","ranked":[]}', "output_shape"),
        (plan(), "output_shape"),
        (plan((0, "close"), outcome="refuse"), "output_shape"),
        (plan(*((n, "close") for n in range(MAX_RANKED + 1))), "output_shape"),
        ('{"outcome":"match","ranked":[{"song":0}]}', "output_shape"),
        ('{"outcome":"match","ranked":[{"song":0,"fit":"close","x":1}]}', "output_shape"),
        ('{"outcome":"match","ranked":[0]}', "output_shape"),
        (plan((3, "close")), "output_song"),
        (plan((-1, "close")), "output_song"),
        (plan((True, "close")), "output_song"),
        (plan((1.0, "close")), "output_song"),
        (plan(("1", "close")), "output_song"),
        (plan((0, "close"), (0, "loose")), "output_song"),
        (plan((0, "perfect")), "output_fit"),
        (plan((0, 1)), "output_fit"),
    ],
)
def test_invalid_model_output_is_rejected_whole(text: str, kind: str) -> None:
    many = tuple(recording(str(n)) for n in range(MAX_RANKED + 1))
    recordings = many if len(text) > 200 else SONGS
    with pytest.raises(MatchFailed) as error:
        parse_plan(text, recordings)
    assert error.value.kind == kind


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        (b"not json", "response_not_json"),
        (b"\xff", "response_not_json"),
        (b"[]", "response_shape"),
        (interaction("{}", "failed"), "interaction_not_completed"),
        (interaction("{}", "incomplete"), "interaction_not_completed"),
        (b'{"status":"completed"}', "response_shape"),
        (b'{"status":"completed","steps":[{"type":"thought"}]}', "response_shape"),
        (b'{"status":"completed","steps":[{"type":"model_output","content":[]}]}', "response_shape"),
        (
            b'{"status":"completed","steps":[{"type":"model_output",'
            b'"content":[{"type":"text","text":5}]}]}',
            "response_shape",
        ),
    ],
)
def test_unusable_interactions_are_rejected(payload: bytes, kind: str) -> None:
    with pytest.raises(MatchFailed) as error:
        output_text(payload)
    assert error.value.kind == kind


def test_the_last_model_output_supplies_the_text() -> None:
    payload = json.dumps(
        {
            "status": "completed",
            "steps": [
                {"type": "model_output", "content": [{"type": "text", "text": "early"}]},
                {
                    "type": "model_output",
                    "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
                },
            ],
        }
    ).encode()
    assert output_text(payload) == "ab"


@pytest.mark.parametrize(
    ("transport", "kind"),
    [
        (FakeTransport(b"{}", status=429), "http_429"),
        (FakeTransport(b"{}", status=500), "http_500"),
        (FakeTransport(error=TimeoutError()), "timeout"),
        (FakeTransport(error=aiohttp.ClientConnectionError("provider text")), "transport_error"),
        (FakeTransport(error=OSError("provider text")), "transport_error"),
        (FakeTransport(error=MatchFailed("response_too_large")), "response_too_large"),
        (FakeTransport(interaction("{}", "failed")), "interaction_not_completed"),
    ],
)
def test_failures_make_one_call_and_log_only_a_safe_kind(
    transport: FakeTransport, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    request = "my private request text"
    with caplog.at_level(logging.INFO), pytest.raises(MatchFailed) as error:
        asyncio.run(matcher(transport).match(request))
    assert error.value.kind == kind and len(transport.calls) == 1
    assert f"kind={kind}" in caplog.text
    for private in (request, KEY, "provider text"):
        assert private not in caplog.text


def test_success_logs_never_contain_the_request_or_key(caplog: pytest.LogCaptureFixture) -> None:
    transport = FakeTransport(interaction(plan((0, "close"))))
    with caplog.at_level(logging.INFO):
        asyncio.run(matcher(transport).match("my private request text"))
    assert "outcome=match ranked=1" in caplog.text
    assert "my private request text" not in caplog.text and KEY not in caplog.text


def test_the_key_is_not_rendered() -> None:
    assert KEY not in repr(matcher(FakeTransport()))


def test_request_text_is_checked_before_any_call() -> None:
    assert checked_request("  haunted carnival  ") == "haunted carnival"
    assert checked_request("x" * 200) == "x" * 200
    for bad in ("", "   ", "x" * 201, "line\nbreak", "bell\x07", "‮flip"):
        with pytest.raises(CatalogInputError):
            checked_request(bad)


def try_requests(*arguments: str) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parents[1] / "evaluations/gemini_matching/try_requests.py"
    environment = {k: v for k, v in os.environ.items() if k != GEMINI_KEY_VARIABLE}
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=environment,
    )


def test_the_evaluation_makes_no_call_unless_told_to_and_given_a_key() -> None:
    dry = try_requests("--dry-run", "a haunted carnival on the moon")
    assert dry.returncode == 0, dry.stderr
    assert f"model={MODEL} songs=20 " in dry.stdout and "REQUEST" not in dry.stdout
    unchosen = try_requests("a haunted carnival on the moon")
    assert unchosen.returncode == 2 and "REQUEST" not in unchosen.stdout
    keyless = try_requests("--send-to-gemini", "a haunted carnival on the moon")
    assert keyless.returncode == 2 and GEMINI_KEY_VARIABLE in keyless.stderr
    assert "REQUEST" not in keyless.stdout


async def _served(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    (address,) = runner.addresses
    return runner, f"http://127.0.0.1:{cast(tuple[str, int], address)[1]}/"


def test_the_real_transport_returns_status_and_body_and_sends_headers() -> None:
    async def scenario() -> None:
        seen: list[tuple[str | None, bytes]] = []

        async def handler(request: web.Request) -> web.Response:
            seen.append((request.headers.get("x-goog-api-key"), await request.read()))
            return web.Response(status=202, body=b"answer")

        runner, url = await _served(handler)
        transport = AiohttpTransport()
        try:
            assert await transport.post(url, {"x-goog-api-key": KEY}, b"body") == (202, b"answer")
            assert seen == [(KEY, b"body")]
        finally:
            await transport.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_the_real_transport_bounds_the_response() -> None:
    async def scenario() -> None:
        async def handler(_: web.Request) -> web.Response:
            return web.Response(body=b"x" * (MAX_RESPONSE_BYTES + 1))

        runner, url = await _served(handler)
        transport = AiohttpTransport()
        try:
            with pytest.raises(MatchFailed) as error:
                await transport.post(url, {}, b"")
            assert error.value.kind == "response_too_large"
        finally:
            await transport.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_the_real_transport_accepts_a_response_at_the_limit() -> None:
    async def scenario() -> None:
        async def handler(_: web.Request) -> web.Response:
            return web.Response(body=b"x" * MAX_RESPONSE_BYTES)

        runner, url = await _served(handler)
        transport = AiohttpTransport()
        try:
            status, body = await transport.post(url, {}, b"")
            assert status == 200 and len(body) == MAX_RESPONSE_BYTES
        finally:
            await transport.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_the_real_transport_times_out_and_does_not_follow_redirects() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def slow(_: web.Request) -> web.Response:
            await release.wait()
            return web.Response()

        async def redirect(_: web.Request) -> web.Response:
            return web.Response(status=307, headers={"Location": "http://127.0.0.1:9/"})

        runner, url = await _served(slow)
        other, redirecting = await _served(redirect)
        transport = AiohttpTransport(timeout=0.05)
        patient = AiohttpTransport()
        try:
            with pytest.raises(TimeoutError):
                await transport.post(url, {}, b"")
            assert (await patient.post(redirecting, {}, b""))[0] == 307
        finally:
            release.set()
            await transport.close()
            await patient.close()
            await runner.cleanup()
            await other.cleanup()

    asyncio.run(scenario())


def test_closing_ends_a_call_that_is_still_waiting() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def slow(_: web.Request) -> web.Response:
            await release.wait()
            return web.Response()

        runner, url = await _served(slow)
        transport = AiohttpTransport()
        waiting = asyncio.create_task(transport.post(url, {}, b""))
        await asyncio.sleep(0.05)
        await transport.close()
        try:
            with pytest.raises((aiohttp.ClientError, OSError)):
                await asyncio.wait_for(waiting, timeout=5)
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(scenario())
