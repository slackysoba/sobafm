"""Heard-source and connection schedules with fake voice and a loopback-only status route."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, Mock

import aiohttp
import disnake
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from disnake.http import HTTPClient, Route

from sobafm import discord_adapter, voice_status
from sobafm.discord_adapter import create_bot
from sobafm.gemini_match import MatchFailed
from sobafm.voice_status import MAX_CHANNELS, SET_VOICE_CHANNEL_STATUS, VoiceStatuses
from tests.test_discord_adapter import (
    BOT_ID,
    FakeChannel,
    FakeGuild,
    FakeVoice,
    Harness,
    harness,
    inter,
    matcher_of,
    offline,
    prepared_next,
    program_harness,
    recording,
    run,
    settle,
    start_program,
    tag,
    until,
)
from tests.test_freestyle_plan import FakeTransport, interaction, interpreter, output
from tests.test_freestyle_station import connect_loopback, generate, music, pending_stream
from tests.test_lyria import FakeLyria


def room(channel_id: int, *, permission: int = SET_VOICE_CHANNEL_STATUS) -> FakeChannel:
    channel = Mock(spec=disnake.VoiceChannel)
    channel.id = channel_id
    channel.mention = "test room"
    channel.guild = FakeGuild()
    channel.voice_states = FakeChannel("room").voice_states
    channel.permissions_for.return_value = disnake.Permissions(permission, connect=True, speak=True)
    fake = cast(FakeChannel, channel)
    channel.connect = AsyncMock(return_value=FakeVoice(fake))
    return fake


def with_status(h: Harness, *, permission: int = SET_VOICE_CHANNEL_STATUS) -> AsyncMock:
    h.room = room(1001, permission=permission)
    client = Mock(spec=HTTPClient)
    h.adapter.voice_status = VoiceStatuses(client)
    return client.request


def updates(request: AsyncMock) -> list[tuple[int, str | None]]:
    return [(c.args[0].channel_id, c.kwargs["json"]["status"]) for c in request.await_args_list]


@offline
async def test_status_follows_only_heard_catalog_and_drain() -> None:
    h, _ = program_harness("a", "b")
    request = with_status(h)
    source = await h.join()
    await h.play()
    await settle()
    assert not updates(request)
    assert source.read() == tag("a")
    await prepared_next(h, "b")
    assert updates(request) == [(1001, "Song a · jamendo")]
    matcher_of(h).outcome = MatchFailed("timeout")
    await h.play("a private request")
    assert source.read() == tag("a")
    await settle()
    assert len(updates(request)) == 1
    assert source.read() == tag("b")
    await until(lambda: len(updates(request)) == 2)
    assert source.read() == tag("b")
    source.read()
    await settle()
    assert updates(request) == [
        (1001, "Song a · jamendo"),
        (1001, "Song b · jamendo"),
        (1001, None),
    ]
    assert await h.adapter.shutdown()


@offline
async def test_delayed_status_across_move_leave_and_rejoin() -> None:
    h, _ = program_harness("a", "b")
    request = with_status(h)
    release = asyncio.Event()

    async def delayed(route: Route, **kwargs: object) -> None:
        if route.channel_id == 1001 and request.await_count == 1:
            await release.wait()

    request.side_effect = delayed
    source = await start_program(h)
    await prepared_next(h, "b")
    moved = room(1002)
    voice = cast(FakeVoice, h.adapter.players[1].voice)
    voice.channel = moved
    h.adapter.voice_state_changed(1, BOT_ID, cast(disnake.VoiceChannel, moved))
    await settle()
    assert source.read() == tag("a") and source.read() == tag("b")
    await settle()
    await run(h.adapter.leave, inter(moved, manager=True))
    assert updates(request) == [
        (1001, "Song a · jamendo"),
        (1002, "Song a · jamendo"),
        (1002, "Song b · jamendo"),
        (1002, None),
    ]
    # A new player in the original room still uses that channel's one writer.
    h.room = room(1001)
    matcher_of(h).recordings = (recording("c"),)
    source = await h.join()
    await h.play()
    await settle()
    assert source.read() == tag("c")
    await settle()
    assert len(updates(request)) == 4
    release.set()
    await settle()
    assert updates(request)[-1] == (1001, "Song c · jamendo")
    await run(h.adapter.stop, inter(h.room))
    assert updates(request)[-1] == (1001, None)
    assert await h.adapter.shutdown()


@pytest.mark.parametrize("stage", [False, True])
def test_optional_permission_and_unsupported_channel(stage: bool) -> None:
    async def scenario() -> None:
        h, _ = program_harness("a")
        request = with_status(h, permission=0)
        if stage:
            # A stage can have all the permissions and still has its own topic.
            ch = Mock(spec=disnake.StageChannel, wraps=h.room)
            ch.guild = h.room.guild
            ch.id = 1002
            ch.voice_states = h.room.voice_states
            ch.permissions_for.return_value = disnake.Permissions.all()
            ch.connect = AsyncMock(return_value=FakeVoice(cast(FakeChannel, ch)))
            h.room = cast(FakeChannel, ch)
        await start_program(h)
        await run(h.adapter.stop, inter(h.room))
        assert await h.adapter.shutdown()
        assert not updates(request)

    asyncio.run(scenario())


@offline
async def test_manager_move_and_terminal_voice_loss() -> None:
    h, _ = program_harness("a")
    request = with_status(h, permission=disnake.Permissions(administrator=True).value)
    await start_program(h)
    await until(lambda: bool(updates(request)))
    destination = room(1002)
    await run(h.adapter.join, inter(destination, manager=True))
    await settle()
    assert updates(request) == [
        (1001, "Song a · jamendo"),
        (1001, None),
        (1002, "Song a · jamendo"),
    ]
    h.adapter.channel_deleted(1, 1002)
    await until(lambda: not h.adapter.players)
    await settle()
    assert updates(request)[-1] == (1002, None)
    assert await h.adapter.shutdown() and not h.pool.leased


def test_status_timeout_releases_the_writer_for_the_current_track(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        h, _ = program_harness("a", "b")
        request = with_status(h)
        interrupted = asyncio.Event()

        async def stalled(route: Route, **kwargs: object) -> None:
            if request.await_count == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    interrupted.set()

        request.side_effect = stalled
        monkeypatch.setattr(voice_status, "WRITE_SECONDS", 0.15)
        source = await start_program(h)
        await prepared_next(h, "b")
        assert source.read() == tag("a") and source.read() == tag("b")
        await settle()
        assert len(updates(request)) == 1
        await asyncio.wait_for(interrupted.wait(), timeout=1)
        await settle()
        assert updates(request)[-1] == (1001, "Song b · jamendo")
        assert await h.adapter.shutdown() and h.adapter.cleanup_failures == 0

    asyncio.run(scenario())
    assert "voice status update unavailable" in caplog.text


def test_public_status_omits_request_model_text_and_links(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with FakeLyria(music(1000)) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness(recordings=(replace(recording("a"), title="https://fake.invalid/key"),))
            request = with_status(h)
            h.adapter.interpreter = interpreter(
                FakeTransport(interaction(output(title="@everyone")))
            )
            source = await h.join()
            await h.play()
            await settle()
            source.read()
            await settle()
            await generate(h, "private prompt")
            assert await pending_stream(source).wait_ready()
            assert updates(request) == [(1001, "Catalog music · jamendo")]
            source.read()
            await settle()
            assert updates(request)[-1] == (1001, "Generated music")
            assert await h.adapter.shutdown()
            assert updates(request)[-1] == (1001, None)

    asyncio.run(scenario())


def test_all_writers_busy_and_cancelled_shutdown_keeps_original_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        h, _ = program_harness("a")
        request = with_status(h)
        release = asyncio.Event()
        ended: list[int] = []

        async def held(route: Route, **kwargs: object) -> None:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            ended.append(int(str(route.channel_id)))

        request.side_effect = held
        monkeypatch.setattr(discord_adapter, "CLEANUP_SECONDS", 0.15)
        await start_program(h)
        voice = cast(FakeVoice, h.adapter.players[1].voice)
        for i in range(2, MAX_CHANNELS * 3):
            voice.channel = room(1000 + i)
            h.adapter.voice_state_changed(1, BOT_ID, cast(disnake.VoiceChannel, voice.channel))
            await settle()
        assert len(updates(request)) == MAX_CHANNELS
        waiter = asyncio.create_task(h.adapter.shutdown())
        await settle()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        try:
            assert not await h.adapter.shutdown()
            assert h.adapter.cleanup_failures == 1 and not ended and not h.pool.leased
        finally:
            release.set()
        await until(lambda: len(ended) == MAX_CHANNELS)
        assert len(updates(request)) == MAX_CHANNELS
        assert not await h.adapter.shutdown() and h.adapter.cleanup_failures == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("code", [204, 403, 429, 503])
def test_sdk_route_and_safe_failures(
    code: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        calls: list[str | None] = []
        completed = asyncio.Event()

        async def respond(request: web.Request) -> web.Response:
            assert request.headers["Authorization"] == "Bot synthetic-placeholder"
            calls.append((await request.json())["status"])
            completed.set()
            if code == 204:
                return web.Response(status=204)
            return web.json_response(
                {
                    "message": "private-error-marker",
                    "global": True,
                    "retry_after": "private-marker",
                },
                status=code,
                headers={"Retry-After": "private-marker", "Via": "test"},
            )

        app = web.Application()
        app.router.add_put("/channels/1001/voice-status", respond)
        h, _ = program_harness("a")
        h.room = room(1001)
        bot = create_bot(h.adapter)  # real registration, HTTP client and route
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            monkeypatch.setattr(Route, "BASE", str(server.make_url("/")).rstrip("/"))
            monkeypatch.setattr(bot.http, "_HTTPClient__session", session)
            bot.http.token = "synthetic-placeholder"
            await start_program(h)
            await asyncio.wait_for(completed.wait(), timeout=1)
            assert await h.adapter.shutdown()
            assert calls == (["Song a · jamendo", None] if code == 204 else ["Song a · jamendo"])
            assert bot.http._global_over.is_set()  # pyright: ignore[reportPrivateUsage]
        await bot.close()

    caplog.set_level(logging.DEBUG, logger="disnake.http")
    asyncio.run(scenario())
    diagnostics = "\n".join(
        r.getMessage() for r in caplog.records if r.name in {"disnake.http", voice_status.__name__}
    )
    assert not any(value in diagnostics for value in ("private-", "synthetic-", "1001", "Song a"))
    if code != 204:
        assert f"status={code}" in diagnostics
