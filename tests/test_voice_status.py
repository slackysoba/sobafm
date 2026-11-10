"""Heard-source and connection schedules with fake voice and a loopback-only status route."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import aiohttp
import disnake
import pytest
from aiohttp import web
from aiohttp.payload import JsonPayload
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
    client._global_over = asyncio.Event()
    client._global_over.set()
    send = AsyncMock()

    async def acknowledged(route: Route, **kwargs: object) -> None:
        await send(route, json=json.loads(cast(JsonPayload, kwargs["data"]).decode()))
        check = cast(
            Callable[[aiohttp.ClientResponse], Awaitable[None]], kwargs["raise_for_status"]
        )
        await check(Mock(spec=aiohttp.ClientResponse, status=204))

    client.request.side_effect = acknowledged
    h.adapter.voice_status = VoiceStatuses(client)
    return send


def updates(request: AsyncMock) -> list[tuple[int, str | None]]:
    return [(c.args[0].channel_id, c.kwargs["json"]["status"]) for c in request.await_args_list]


@asynccontextmanager
async def loopback(
    h: Harness, app: web.Application, monkeypatch: pytest.MonkeyPatch, *, connections: int = 100
) -> AsyncGenerator[HTTPClient]:
    h.room = room(1001)
    bot = create_bot(h.adapter)
    async with (
        TestServer(app) as server,
        aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=connections)) as session,
    ):
        monkeypatch.setattr(Route, "BASE", str(server.make_url("/")).rstrip("/"))
        monkeypatch.setattr(bot.http, "_HTTPClient__session", session)
        bot.http.token = "synthetic-placeholder"
        try:
            yield bot.http
        finally:
            await bot.close()


def test_dispatched_title_settles_before_stop_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        accepted = asyncio.Event()
        release = asyncio.Event()
        applied: list[str | None] = []

        async def write(request: web.Request) -> web.Response:
            text = (await request.json())["status"]
            if text is not None:
                accepted.set()
                await release.wait()
            applied.append(text)
            return web.Response(status=204)

        app = web.Application()
        app.router.add_put("/channels/1001/voice-status", write)
        h, _ = program_harness("a")
        monkeypatch.setattr(voice_status, "WRITE_SECONDS", 0.05)
        async with loopback(h, app, monkeypatch):
            await start_program(h)
            await asyncio.wait_for(accepted.wait(), 1)
            await run(h.adapter.stop, inter(h.room))
            try:
                await asyncio.sleep(0.1)
                assert not applied and h.pool.leased == 0
            finally:
                release.set()
            assert await h.adapter.shutdown()
            assert applied == ["Song a · jamendo", None]

    asyncio.run(scenario())


def test_lost_first_ack_leaves_remote_outcome_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        calls: list[str | None] = []
        received = asyncio.Event()

        async def lose_ack(request: web.Request) -> web.Response:
            calls.append((await request.json())["status"])
            received.set()
            assert request.transport is not None
            request.transport.close()
            return web.Response(status=204)

        app = web.Application()
        app.router.add_put("/channels/1001/voice-status", lose_ack)
        h, _ = program_harness("a")
        async with loopback(h, app, monkeypatch):
            await start_program(h)
            await asyncio.wait_for(received.wait(), 1)
            await run(h.adapter.stop, inter(h.room))
            assert await h.adapter.shutdown()
            # The peer received the PUT. The client can neither claim nor clear it,
            # and a persistent-connection retry must not replay the uncertain PUT.
            assert calls == ["Song a · jamendo"]

    asyncio.run(scenario())


@pytest.mark.parametrize("gate", ["global", "bucket", "connector"])
@pytest.mark.parametrize("timeout_first", [True, False])
def test_undispatched_title_does_not_own_a_clear(
    gate: str, timeout_first: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        calls: list[str | None] = []

        async def write(request: web.Request) -> web.Response:
            calls.append((await request.json())["status"])
            return web.Response(status=204)

        app = web.Application()
        app.router.add_put("/channels/1001/voice-status", write)
        occupied = asyncio.Event()
        release = asyncio.Event()

        async def hold(_: web.Request) -> web.Response:
            occupied.set()
            await release.wait()
            return web.Response(status=204)

        app.router.add_get("/hold", hold)
        h, _ = program_harness("a")
        monkeypatch.setattr(voice_status, "WRITE_SECONDS", 0.05)
        async with loopback(h, app, monkeypatch, connections=1) as http:
            route = Route("PUT", "/channels/{channel_id}/voice-status", channel_id=1001)
            lock = asyncio.Lock()
            other = None
            if gate == "global":
                http._global_over.clear()  # pyright: ignore[reportPrivateUsage]
            elif gate == "bucket":
                await lock.acquire()
                http._locks[route.bucket] = lock  # pyright: ignore[reportPrivateUsage]
            else:
                other = asyncio.create_task(http.request(Route("GET", "/hold")))
                await asyncio.wait_for(occupied.wait(), 1)
            await start_program(h)
            if timeout_first:
                await asyncio.sleep(0.1)
            else:
                await run(h.adapter.stop, inter(h.room))
            if gate == "global":
                http._global_over.set()  # pyright: ignore[reportPrivateUsage]
            elif gate == "bucket":
                lock.release()
            else:
                release.set()
                assert other is not None
                await other
            if timeout_first:
                await run(h.adapter.stop, inter(h.room))
            assert await h.adapter.shutdown() and not calls

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["global", "shared"])
def test_rate_limit_scope_across_admin_move(scope: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        calls: list[tuple[str, str | None]] = []
        first_received = asyncio.Event()
        sent_limit = asyncio.Event()
        limited_at = 0.0
        gateway_at = 0.0
        moved_at = 0.0
        cleared_at = 0.0

        async def write(request: web.Request) -> web.Response:
            nonlocal limited_at, moved_at, cleared_at
            channel = request.match_info["channel"]
            text = (await request.json())["status"]
            calls.append((channel, text))
            if channel == "1001" and text == "Song a · jamendo":
                first_received.set()
            if channel == "1001" and text == "Song b · jamendo":
                limited_at = time.monotonic()
                sent_limit.set()
                return web.json_response(
                    {"global": scope == "global", "retry_after": 0.25},
                    status=429,
                    headers={"Retry-After": "0.25", "X-RateLimit-Scope": scope, "Via": "test"},
                )
            if channel == "1002" and text:
                moved_at = time.monotonic()
            if channel == "1001" and text is None:
                cleared_at = time.monotonic()
            return web.Response(status=204)

        async def gateway(_: web.Request) -> web.Response:
            nonlocal gateway_at
            gateway_at = time.monotonic()
            return web.Response(
                body=b'{"url":"wss://unopened.invalid"}', content_type="application/json"
            )

        app = web.Application()
        app.router.add_put("/channels/{channel}/voice-status", write)
        app.router.add_get("/gateway", gateway)
        h, _ = program_harness("a", "b")
        async with loopback(h, app, monkeypatch) as http:
            source = await start_program(h)
            await asyncio.wait_for(first_received.wait(), 1)
            await prepared_next(h, "b")
            assert source.read() == tag("a") and source.read() == tag("b")
            await asyncio.wait_for(sent_limit.wait(), 1)
            await settle()
            destination = room(1002)
            await run(h.adapter.join, inter(destination, manager=True))
            await asyncio.wait_for(http.get_gateway(), 1)
            assert (gateway_at >= limited_at + 0.25) == (scope == "global")
            async with asyncio.timeout(1):
                while ("1001", None) not in calls or not moved_at:
                    await asyncio.sleep(0.005)
            assert (moved_at >= limited_at + 0.25) == (scope == "global")
            assert cleared_at >= limited_at + 0.25
            assert calls.count(("1001", "Song b · jamendo")) == 1
            assert await h.adapter.shutdown()

    asyncio.run(scenario())


def test_late_last_clear_before_shutdown_observer_stays_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        h, _ = program_harness("a")
        request = with_status(h)
        clearing = asyncio.Event()
        release = asyncio.Event()
        now = time.monotonic()
        monkeypatch.setattr(voice_status, "time", SimpleNamespace(monotonic=lambda: now))

        async def write(route: Route, **kwargs: object) -> None:
            nonlocal now
            if cast(dict[str, str | None], kwargs["json"])["status"] is None:
                clearing.set()
                await release.wait()
                now = time.monotonic() + discord_adapter.CLEANUP_SECONDS + 0.001

        request.side_effect = write
        await start_program(h)
        await until(lambda: bool(updates(request)))
        await run(h.adapter.leave, inter(h.room, manager=True))
        await asyncio.wait_for(clearing.wait(), 1)
        shutdown = asyncio.create_task(h.adapter.shutdown())
        release.set()  # shutdown captures cutoff; the clear settles before its observer runs
        assert not await shutdown and h.adapter.cleanup_failures == 1
        assert not await h.adapter.shutdown()

    asyncio.run(scenario())


def test_global_retry_release_survives_shutdown_and_overlapping_sdk_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        limited = asyncio.Event()

        async def limited_write(_: web.Request) -> web.Response:
            limited.set()
            return web.json_response({"global": True, "retry_after": 0.2}, status=429)

        app = web.Application()
        app.router.add_put("/channels/1001/voice-status", limited_write)
        h, _ = program_harness("a")
        async with loopback(h, app, monkeypatch) as http:
            gate = http._global_over  # pyright: ignore[reportPrivateUsage]
            await start_program(h)
            await asyncio.wait_for(limited.wait(), 1)
            await until(lambda: not gate.is_set())
            # A writer cancelled during backoff neither releases the advertised
            # global cooldown nor leaves the rest of the client stuck afterwards.
            assert await h.adapter.shutdown()
            gate.clear()
            gate.set()
            assert not gate.is_set()
            gate.clear()
            await asyncio.sleep(0.25)
            assert not gate.is_set()
            gate.set()
            assert gate.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("last_status", [204, 429])
def test_unchanged_heard_title_retries_once_after_channel_limit(
    last_status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        writes: list[str | None] = []
        retried = asyncio.Event()

        async def respond(request: web.Request) -> web.Response:
            writes.append((await request.json())["status"])
            if len(writes) >= 2:
                retried.set()
                if last_status == 204:
                    return web.Response(status=204)
            return web.Response(
                status=429, headers={"X-RateLimit-Scope": "user", "Retry-After": "0"}
            )

        app = web.Application()
        app.router.add_put("/channels/1001/voice-status", respond)
        h, _ = program_harness("a")
        async with loopback(h, app, monkeypatch):
            await start_program(h)
            await asyncio.wait_for(retried.wait(), 1)
            await settle()
            assert await h.adapter.shutdown()
            expected = ["Song a · jamendo"] * 2
            assert writes == (expected + [None] if last_status == 204 else expected)

    asyncio.run(scenario())


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
        async with loopback(h, app, monkeypatch) as http:
            await start_program(h)
            await asyncio.wait_for(completed.wait(), timeout=1)
            assert await h.adapter.shutdown()
            assert calls == (["Song a · jamendo", None] if code == 204 else ["Song a · jamendo"])
            # A global refusal outlives the status writer and releases on its timer.
            gate = http._global_over  # pyright: ignore[reportPrivateUsage]
            assert gate.is_set() == (code != 429)

    caplog.set_level(logging.DEBUG, logger="disnake.http")
    asyncio.run(scenario())
    diagnostics = "\n".join(
        r.getMessage() for r in caplog.records if r.name in {"disnake.http", voice_status.__name__}
    )
    assert not any(value in diagnostics for value in ("private-", "synthetic-", "1001", "Song a"))
    if code != 204:
        assert f"status={code}" in diagnostics
