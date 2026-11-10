"""Offline listener replies and safe operator signals from both playback paths."""

import asyncio
import json
import logging
from typing import cast

import aiohttp
import disnake
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from disnake.http import Route
from disnake.webhook.async_ import AsyncWebhookAdapter

from sobafm import discord_adapter
from sobafm.__main__ import configure_logging
from sobafm.discord_adapter import NO_MENTIONS, Inter
from sobafm.gemini_match import GeminiMatcher
from sobafm.lyria import PLAY_MESSAGE
from sobafm.media import MediaProducer
from tests.test_discord_adapter import FakeMatcher, harness, inter, recording, settle
from tests.test_discord_adapter import run as command
from tests.test_freestyle_plan import KEY, FakeTransport, interaction, interpreter, output
from tests.test_freestyle_station import generate, pending_stream, timed_harness
from tests.test_gemini_match import plan
from tests.test_lyria import (
    PRE_ROLL_FRAMES,
    FakeLyria,
    ManualTime,
    Peer,
    Refuse,
    advancing_until,
    run,
    sample_of,
    until,
)

REQUEST = "synthetic private listener direction"
DETAIL = "https://example.invalid/?token=synthetic-private-detail"


@pytest.mark.parametrize("stage", ["defer", "follow-up"])
@pytest.mark.parametrize("retry_after", [DETAIL, 0])
def test_disnake_rate_limit_warning_is_safe_before_formatting(
    stage: str,
    retry_after: str | int,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger("disnake.webhook.async_")
    monkeypatch.setattr(logger, "filters", [])
    configure_logging()
    calls = 0

    async def discord_response(_: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.Response(
                status=429,
                headers={"Via": "fake", "Content-Type": "application/json"},
                body=json.dumps({"retry_after": retry_after}).encode(),
            )
        return web.json_response({})

    async def scenario() -> None:
        app = web.Application()
        app.router.add_post("/", discord_response)
        h = harness()
        await h.join()
        fake = inter(h.room)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            route = Route("POST", "/", webhook_id=987654321, webhook_token=fake.token)
            route.url = str(server.make_url("/"))
            sdk = AsyncWebhookAdapter()
            original = fake.defer if stage == "defer" else fake.send

            async def send(*args: str, **kwargs: object) -> None:
                await sdk.request(route, session)
                await original(*args, **kwargs)

            monkeypatch.setattr(fake, "defer" if stage == "defer" else "send", send)
            await h.adapter.play(cast(Inter, fake), REQUEST)
            assert calls == (2 if retry_after == 0 else 1)
            assert bool(fake.sent) == (retry_after == 0)
            assert await h.adapter.shutdown()

    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        run(scenario())
        logger.warning("unrelated SDK diagnostic")
    finally:
        logger.removeHandler(handler)
    output = caplog.text + capsys.readouterr().err
    assert "Discord rate limited an interaction response" in output
    assert "unrelated SDK diagnostic" in output
    assert "987654321" not in output and DETAIL not in output and "Logging error" not in output


@pytest.mark.parametrize("mode", ["play", "freestyle"])
@pytest.mark.parametrize("stage", ["refusal", "defer", "follow-up"])
def test_discord_delivery_failure_never_exposes_the_error_or_retries(
    mode: str, stage: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        h = harness()
        transport = FakeTransport(interaction('{"outcome":"refuse"}'))
        h.adapter.interpreter = interpreter(transport)
        await h.join()
        fake = inter(None if stage == "refusal" else h.room)

        async def failed_send(*args: object, **kwargs: object) -> None:
            raise OSError(f"{DETAIL} {fake.token}")

        if stage == "defer":
            fake.transport_failure = OSError(f"{DETAIL} {fake.token}")
        elif stage == "refusal":
            monkeypatch.setattr(fake, "send_message", failed_send)
        else:
            monkeypatch.setattr(fake, "send", failed_send)
        handler = h.adapter.play if mode == "play" else h.adapter.freestyle
        await handler(cast(Inter, fake), REQUEST)
        expected = "follow-up" if stage == "follow-up" else "acknowledgement"
        assert f"interaction {expected} failed: command={mode} " in caplog.text
        assert "error=OSError" in caplog.text
        assert not fake.sent and not any(
            value in caplog.text for value in (DETAIL, fake.token, REQUEST)
        )
        assert len(transport.calls) == int(mode == "freestyle" and stage == "follow-up")
        assert (h.adapter.players[1].station.desired is not None) == (
            mode == "play" and stage == "follow-up"
        )
        assert await h.adapter.shutdown()

    run(scenario())


@pytest.mark.parametrize(
    ("status", "code", "wording", "kind"),
    [
        (401, "", "denied access", "http_401"),
        (402, "", "intended free-tier project", "http_402"),
        (403, "", "denied access", "http_403"),
        (404, "", "configured model and endpoint", "http_404"),
        (429, "safety", "rate or quota limit", "http_429"),
        (503, "", "server error", "http_503"),
        (408, "", "timed out", "http_408"),
        (400, "language", "interpreter declined", "http_400"),
        (400, "unknown", "usable interpretation", "http_400"),
        (200, "", "usable interpretation", "interaction_not_completed"),
    ],
)
def test_catalog_match_failure_preserves_heard_audio(
    status: int, code: str, wording: str, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        h = harness()
        source = await h.join()
        await h.play()
        await settle()
        frame = source.read()
        await settle()
        station = h.adapter.players[1].station
        heard = station.heard
        assert heard is not None
        body = json.dumps({"error": {"code": code, "message": DETAIL}}).encode()
        transport = FakeTransport(body, status=status)
        h.adapter.matcher = GeminiMatcher((recording("a"),), KEY, transport=transport)
        reply = await command(lambda i: h.adapter.play(i, REQUEST), inter(h.room))
        (sent,) = reply.sent
        assert wording in (sent.text or "")
        assert sent.options == {
            "ephemeral": True,
            "suppress_embeds": True,
            "allowed_mentions": NO_MENTIONS,
        }
        assert f"request matching failed: kind={kind} " in caplog.text
        assert f"blocked={code == 'language'}" in caplog.text
        for private in (KEY, REQUEST, DETAIL, reply.token):
            assert private not in (sent.text or "") + caplog.text
        assert len(transport.calls) == 1
        assert station.heard is heard and station.desired is heard.program
        assert source.read() == frame
        assert await h.adapter.shutdown()

    run(scenario())


def test_loose_fit_and_empty_catalog_have_distinct_replies() -> None:
    async def scenario() -> None:
        h = harness()
        transport = FakeTransport(interaction(plan((0, "loose"))))
        h.adapter.matcher = GeminiMatcher((), KEY, transport=transport)
        source = await h.join()
        assert "no recordings ready" in (await h.play())[0]
        assert not transport.calls and source.read() == b""
        h.adapter.matcher = GeminiMatcher((recording("a"),), KEY, transport=transport)
        (reply,) = await h.play()
        assert "nearest thing" in reply and "Request accepted" in reply
        assert h.adapter.players[1].station.heard is None
        assert await h.adapter.shutdown()

    run(scenario())


@pytest.mark.parametrize("mode", ["play", "freestyle"])
@pytest.mark.parametrize("failure", ["capacity", "voice"])
def test_deferred_failure_names_the_request_and_preserves_heard_audio(
    mode: str, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with FakeLyria() as peer:
            h = timed_harness(monkeypatch, peer, ManualTime())
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            await h.play()
            await settle()
            frame = source.read()  # the real first-frame report is still queued
            station = h.adapter.players[1].station
            first = station.switching
            assert first is not None and frame != discord_adapter.SILENCE
            h.adapter.matcher = FakeMatcher(recording("b"))
            if mode == "play":
                assert "Request accepted" in (await h.play())[0]
            else:
                assert "Preparing" in await generate(h)
            assert station.ready is not None and station.switching is first

            def refuse_voice(_: disnake.AudioSource) -> None:
                raise RuntimeError(DETAIL)

            bank = None
            if failure == "capacity":
                bank = h.pool.acquire()
                monkeypatch.setattr(discord_adapter, "CAPACITY_WAIT_SECONDS", 0)
                reason = "both audio banks were in use"
            else:
                voice = h.room.voice
                assert voice is not None
                voice.plays.clear()
                monkeypatch.setattr(voice, "play", refuse_voice)
                reason = "voice player could not start"
            await until(lambda: station.desired is first.program)
            assert station.heard is first and source.read() == frame
            assert station.failed_start is not None and station.failed_start.command == f"/{mode}"
            status = (await command(h.adapter.now, inter(h.room))).texts()[0] or ""
            assert reason in status and f"`/{mode}`" in status and "Now heard" in status
            assert DETAIL not in status
            if bank is not None:
                assert bank.release()
            await command(h.adapter.stop, inter(h.room))
            assert reason not in ((await command(h.adapter.now, inter(h.room))).texts()[0] or "")
            assert await h.adapter.shutdown() and not peer.requests

    run(scenario())


def test_missing_first_media_entry_is_visible_until_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        h = harness()
        source = await h.join()
        with monkeypatch.context() as missing:
            missing.setattr(h.adapter, "_producer", MediaProducer({}))
            assert "Request accepted" in (await h.play())[0]
            await settle()
            assert source.read() == b""
            await settle()
        status = (await command(h.adapter.now, inter(h.room))).texts()[0] or ""
        assert "Nothing is audible" in status and "no media manifest entry" in status
        assert "`/play`" in status
        assert "Request accepted" in (await h.play())[0]
        status = (await command(h.adapter.now, inter(h.room))).texts()[0] or ""
        assert "starting" in status and "manifest" not in status
        assert await h.adapter.shutdown()

    run(scenario())


def test_superseded_first_switch_failure_cannot_mark_the_new_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        h = harness()
        source = await h.join()
        with monkeypatch.context() as missing:
            missing.setattr(h.adapter, "_producer", MediaProducer({}))
            assert "Request accepted" in (await h.play())[0]
            await settle()
            assert source.read() == b""  # failure queued on the loop; not yet settled
        assert "Request accepted" in (await h.play("new request"))[0]
        station = h.adapter.players[1].station
        assert station.switching is not None and station.ready is not None
        await until(lambda: station.ready is None)
        await settle()
        assert source.read() not in (b"", discord_adapter.SILENCE)
        await settle()
        assert station.heard is not None and station.heard.program is station.desired
        assert station.failed_start is None
        status = (await command(h.adapter.now, inter(h.room))).texts()[0] or ""
        assert "Now heard" in status and "manifest" not in status
        assert await h.adapter.shutdown()

    run(scenario())


@pytest.mark.parametrize(
    ("statuses", "ending", "kind"),
    [
        ((403,), "declined access", "access_denied"),
        ((404,), "declined the session", "rejected"),
        ((503, 429, 429), "last generation attempt hit a rate or quota limit", "rate_limited"),
        ((429, 429, 503), "server error on the last attempt", "service_error"),
    ],
)
def test_fallback_then_generation_failure_keeps_the_heard_recording(
    statuses: tuple[int, ...],
    ending: str,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        clock = ManualTime()
        async with FakeLyria(*(Refuse(status) for status in statuses)) as peer:
            h = timed_harness(monkeypatch, peer, clock)
            h.adapter.interpreter = interpreter(FakeTransport(DETAIL.encode(), status=429))
            source = await h.join()
            await h.play()
            await settle()
            frame = source.read()
            await settle()
            station = h.adapter.players[1].station
            heard = station.heard
            assert heard is not None
            reply = await generate(h, REQUEST)
            assert "Preparing" in reply and "interpreter hit a rate or quota limit" in reply
            assert "as written, without prior context" in reply
            await advancing_until(clock, lambda: station.switching is None)
            now = (await command(h.adapter.now, inter(None))).texts()[0] or ""
            assert "Now heard" in now and ending in now
            assert station.heard is heard and source.read() == frame
            assert len(peer.requests) == len(statuses)
            assert f"lyria stream cannot continue: failure={kind}" in caplog.text
            assert "freestyle interpretation fell back: kind=http_429 " in caplog.text
            for private in (KEY, REQUEST, DETAIL):
                assert private not in reply + now + caplog.text
            assert await h.adapter.shutdown() and h.pool.leased == 0

    caplog.set_level(logging.INFO, logger="sobafm")
    run(scenario())


@pytest.mark.parametrize(
    ("status", "problem"), [(402, "requested payment"), (404, "requested resource")]
)
def test_configuration_fallback_and_1011_after_heard_audio(
    status: int, problem: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        end_healthy_session = asyncio.Event()
        clock = ManualTime()

        async def playing(peer: Peer) -> None:
            await peer.setup()
            assert await peer.received.get() == {
                "clientContent": {"weightedPrompts": [{"text": REQUEST, "weight": 1.0}]}
            }
            assert await peer.received.get() == PLAY_MESSAGE
            await peer.pre_roll()
            await end_healthy_session.wait()
            await peer.websocket.close(code=1000)

        async def failed(peer: Peer) -> None:
            await peer.setup()
            await peer.websocket.close(code=1011, message=DETAIL.encode())

        async with FakeLyria(playing, Refuse(503), Refuse(429), failed) as peer:
            h = timed_harness(monkeypatch, peer, clock)
            h.adapter.interpreter = interpreter(FakeTransport(DETAIL.encode(), status=status))
            source = await h.join()
            reply = await generate(h, REQUEST)
            assert problem in reply and "as written, without prior context" in reply
            stream = pending_stream(source)
            assert await stream.wait_ready() and sample_of(source.read()) == 1_000
            await settle()
            end_healthy_session.set()
            await advancing_until(clock, lambda: len(peer.requests) == 4)
            await until(lambda: stream.failure == "service_error" and not peer.scripts)
            await peer.peers[-1].disconnected.wait()
            assert {sample_of(source.read()) for _ in range(PRE_ROLL_FRAMES - 1)} == {1_000}
            await until(lambda: source.read() == b"")
            await settle()
            now = (await command(h.adapter.now, inter(h.room))).texts()[0] or ""
            assert "Nothing is audible" in now and "server error on the last attempt" in now
            assert len(peer.requests) == 4
            assert (
                DETAIL not in reply + now + caplog.text and REQUEST not in reply + now + caplog.text
            )
            assert await h.adapter.shutdown()

    run(scenario())
