"""Offline listener replies and safe operator signals from both playback paths."""

import json
import logging
from typing import cast

import pytest

from sobafm.discord_adapter import NO_MENTIONS, Inter
from sobafm.gemini_match import GeminiMatcher
from tests.test_discord_adapter import harness, inter, recording, settle
from tests.test_discord_adapter import run as command
from tests.test_freestyle_plan import KEY, FakeTransport, interaction, interpreter
from tests.test_freestyle_station import generate, timed_harness
from tests.test_gemini_match import plan
from tests.test_lyria import FakeLyria, ManualTime, Refuse, advancing_until, run

REQUEST = "synthetic private listener direction"
DETAIL = "https://example.invalid/?token=synthetic-private-detail"


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
        (403, "", "denied access", "http_403"),
        (429, "safety", "rate or quota limit", "http_429"),
        (503, "", "server error", "http_503"),
        (408, "", "timed out", "http_408"),
        (400, "language", "interpreter declined", "blocked"),
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
