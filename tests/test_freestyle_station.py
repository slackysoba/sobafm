"""Offline command-to-station schedules with the real interpreter, stream and PCM pool."""

from __future__ import annotations

import asyncio
import json
from functools import partial

import pytest

from sobafm import discord_adapter
from sobafm.credits import escaped_text
from sobafm.discord_adapter import GeneratedTrack, StationSource, create_bot
from sobafm.lyria import (
    CROSSFADE_FRAMES,
    FADE_OUT_FRAMES,
    PLAY_MESSAGE,
    GeneratedStream,
)
from sobafm.station import EndReason
from tests.test_discord_adapter import (
    LISTENER_ID,
    NOT_JOINED,
    NOT_LISTENING,
    SERVER_ONLY,
    Harness,
    harness,
    inter,
    recording,
    settings_of,
    settle,
)
from tests.test_discord_adapter import (
    run as command,
)
from tests.test_freestyle_plan import (
    FakeTransport,
    HeldTransport,
    error_body,
    interaction,
    interpreter,
    output,
)
from tests.test_lyria import (
    FakeLyria,
    ManualTime,
    Peer,
    Refuse,
    Script,
    StuckClose,
    assert_faded,
    run,
    sample_of,
    until,
)


async def generate(h: Harness, request: str = "piano") -> str:
    reply = await command(lambda i: h.adapter.freestyle(i, request), inter(h.room))
    assert all(sent.options["ephemeral"] for sent in reply.sent)
    return reply.texts()[0] or ""


def pending_stream(source: StationSource) -> GeneratedStream:
    track = source._pending  # pyright: ignore[reportPrivateUsage]
    assert isinstance(track, GeneratedTrack)
    return track.stream


def music(level: int, *, bpm: int | None = None, key: str | None = None) -> Script:
    async def script(peer: Peer) -> None:
        await peer.setup()
        assert await peer.received.get() == {
            "clientContent": {"weightedPrompts": [{"text": "dark cabaret", "weight": 1.0}]}
        }
        if bpm is not None:
            assert await peer.received.get() == {
                "musicGenerationConfig": {"bpm": bpm, "scale": key}
            }
        assert await peer.received.get() == PLAY_MESSAGE
        await peer.pre_roll(level)

    return script


def connect_loopback(monkeypatch: pytest.MonkeyPatch, peer: FakeLyria) -> None:
    monkeypatch.setattr(
        discord_adapter, "GeneratedStream", partial(GeneratedStream, endpoint=peer.url)
    )


def test_command_modes_context_crossfade_and_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        first_key, next_key = "C_MAJOR_A_MINOR", "D_MAJOR_B_MINOR"
        async with FakeLyria(
            music(1000, bpm=80, key=first_key), music(3000, bpm=120, key=next_key), music(2000)
        ) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            title = "*test* @everyone"
            transport = FakeTransport(interaction(output(title=title, tempo_bpm=80, key=first_key)))
            h.adapter.interpreter = interpreter(transport)
            source = await h.join()
            assert "accepted" in (await h.play())[0]
            await settle()
            catalog_frame = source.read()
            await settle()

            assert escaped_text(title) in await generate(h)
            assert "PLAN NOW PLAYING:" not in json.loads(transport.calls[0][2])["input"]
            status = (await command(h.adapter.now, inter(None))).texts()
            assert "new request is starting" in (status[0] or "") and "Song a" in (status[1] or "")
            assert await pending_stream(source).wait_ready()
            assert sample_of(source.read()) == 1000  # catalog boundary has no mix
            await settle()
            await check_generated_status_and_skip(h, title)
            await until(lambda: h.pool.leased == 1)

            transport.payload = interaction(output(tempo_bpm=120, key=next_key))
            assert "Preparing" in await generate(h, "faster")
            assert title in json.loads(transport.calls[1][2])["input"]
            assert await pending_stream(source).wait_ready()
            assert h.pool.leased == 2
            assert_faded(
                [sample_of(source.read()) for _ in range(CROSSFADE_FRAMES)],
                1000,
                3000,
                CROSSFADE_FRAMES,
            )
            await until(lambda: h.pool.leased == 1)

            assert "accepted" in (await h.play())[0]
            await settle()
            assert source.read() == catalog_frame
            await until(lambda: h.pool.leased == 1)
            assert "Song a" in ((await command(h.adapter.now, inter(None))).texts()[1] or "")

            transport.payload = interaction(output())
            await generate(h)
            assert "PLAN NOW PLAYING:" not in json.loads(transport.calls[-1][2])["input"]
            assert await pending_stream(source).wait_ready()
            assert sample_of(source.read()) == 2000
            await settle()
            stopped = asyncio.create_task(command(h.adapter.stop, inter(None, manager=True)))
            await until(lambda: h.adapter.players[1].station.desired is None)
            assert_faded(
                [sample_of(source.read()) for _ in range(FADE_OUT_FRAMES)], 2000, 0, FADE_OUT_FRAMES
            )
            await stopped
            assert source.read() == b""
            assert await h.adapter.shutdown() and h.pool.leased == 0

    run(scenario())


async def check_generated_status_and_skip(h: Harness, title: str) -> None:
    now = await command(h.adapter.now, inter(None))
    assert escaped_text(title) in (now.texts()[0] or "")
    assert "remaining" in (now.texts()[0] or "") and len(now.sent) == 1
    skip = await command(h.adapter.skip, inter(h.room))
    assert "only for a catalog program" in (skip.texts()[0] or "")


@pytest.mark.parametrize("action", ["stop", "leave", "loss", "shutdown", "replace", "duration"])
def test_late_fallback_cannot_start_audio(action: str) -> None:
    async def scenario() -> None:
        h = harness()
        transport = HeldTransport(b"bad response")
        h.adapter.interpreter = interpreter(transport)
        await h.join()
        pending = asyncio.create_task(generate(h))
        await until(lambda: bool(transport.calls))
        assert "being interpreted" in ((await command(h.adapter.now, inter(None))).texts()[0] or "")
        if action == "replace":
            assert await generate(h, "second") == discord_adapter.MATCH_BUSY
            assert len(transport.calls) == 1
            assert "accepted" in (await h.play())[0]
        elif action == "stop":
            await command(h.adapter.stop, inter(h.room))
        elif action == "leave":
            await command(h.adapter.leave, inter(h.room, manager=True))
        elif action == "loss":
            h.adapter.channel_deleted(1, h.room.id)
        elif action == "duration":
            h.clock.now += 3601
            h.clock.expire()
        else:
            assert await h.adapter.shutdown()
        transport.release.set()
        assert "Preparing" not in await pending
        if action == "replace":
            settings_of(h).current = settings_of(h).current.changed(cooldown_seconds=30)
            assert "cooldown" in (await h.play())[0]
        assert await h.adapter.shutdown() and h.pool.leased == 0

    run(scenario())


@pytest.mark.parametrize("code", ["language", "recitation", "safety"])
def test_refused_generation_keeps_catalog_prefetch_and_releases_own_claim(code: str) -> None:
    async def scenario() -> None:
        h = harness(recordings=(recording("a"), recording("b")))
        transport = FakeTransport(error_body(code), status=400)
        h.adapter.interpreter = interpreter(transport)
        source = await h.join()
        await h.play()
        await settle()
        source.read()
        await settle()
        station = h.adapter.players[1].station
        heard, switching = station.heard, station.switching
        assert heard is not None and switching is not None
        h.voice_update(LISTENER_ID, None)
        grace = h.adapter.players[1].grace
        assert grace is not None
        settings_of(h).current = settings_of(h).current.changed(cooldown_seconds=30)
        assert "cooldown" in await generate(h)
        assert not transport.calls
        h.clock.now += 31
        assert "declined" in await generate(h)
        assert station.heard is heard and station.switching is switching
        assert h.adapter.players[1].grace is grace
        assert "Skipping" in ((await command(h.adapter.skip, inter(h.room))).texts()[0] or "")
        assert await h.adapter.shutdown()

    run(scenario())


def test_cutoff_uses_acceptance_time_and_preserves_a_newer_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with FakeLyria(music(1000), music(2000)) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            transport = HeldTransport(interaction(output()))
            h.adapter.interpreter = interpreter(transport)
            settings_of(h).current = settings_of(h).current.changed(generated_minutes=5)
            source = await h.join()
            starting = asyncio.create_task(generate(h))
            await until(lambda: bool(transport.calls))
            await until(lambda: bool(h.clock.sleeps))
            h.clock.now += 120
            transport.release.set()
            await starting
            assert await pending_stream(source).wait_ready()
            assert sample_of(source.read()) == 1000
            await settle()
            assert "3m 0s remaining" in (
                (await command(h.adapter.now, inter(None))).texts()[0] or ""
            )
            await generate(h)
            assert await pending_stream(source).wait_ready()
            first_timer = h.clock.sleeps[0][1]
            # Both the first frame and the old duration task see the same cutoff.
            h.clock.now += 180
            assert sample_of(source.read()) == 2000
            assert not first_timer.done()
            first_timer.set_result(None)
            await settle()
            assert "2m 0s remaining" in (
                (await command(h.adapter.now, inter(None))).texts()[0] or ""
            )
            # Expire the replacement at its own cutoff even when voice stops reading.
            h.clock.now += 120
            h.clock.expire()
            await until(lambda: h.adapter.players[1].station.desired is None)
            assert source.read() == b""
            assert "duration elapsed" in (
                (await command(h.adapter.now, inter(None))).texts()[0] or ""
            )
            assert await h.adapter.shutdown()

    run(scenario())


def test_fallback_filter_keeps_heard_audio_and_explains_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def filtered(peer: Peer) -> None:
        await peer.setup()
        assert await peer.received.get() == {
            "clientContent": {"weightedPrompts": [{"text": "faster", "weight": 1.0}]}
        }
        assert await peer.received.get() == PLAY_MESSAGE
        await peer.websocket.send_str('{"filteredPrompt":{}}')

    async def scenario() -> None:
        async with FakeLyria(music(1000), filtered) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            transport = FakeTransport(interaction(output()))
            h.adapter.interpreter = interpreter(transport)
            source = await h.join()
            await generate(h)
            assert await pending_stream(source).wait_ready()
            assert sample_of(source.read()) == 1000
            await settle()
            transport.payload = b"bad reply"
            reply = await generate(h, "faster")
            assert "as written, without prior context" in reply
            assert not await pending_stream(source).wait_ready()
            assert sample_of(source.read()) == 1000
            await settle()
            now = (await command(h.adapter.now, inter(None))).texts()[0] or ""
            assert "Now heard" in now and "filtered" in now
            assert len(peer.requests) == 2
            assert await h.adapter.shutdown()

    run(scenario())


def test_registered_generated_only_access_and_limit() -> None:
    async def scenario() -> None:
        h = harness()
        h.adapter.matcher = None
        transport = FakeTransport()
        h.adapter.interpreter = interpreter(transport)
        bot = create_bot(h.adapter)
        bodies = {c.name: c.body for c in bot.slash_commands}
        assert set(bodies) == {"join", "leave", "settings", "freestyle", "stop", "now"}
        body = bodies["freestyle"]
        assert body.contexts is not None and body.contexts.guild and not body.contexts.bot_dm
        assert body.default_member_permissions is None
        (option,) = body.options
        assert (option.name, option.required, option.min_length, option.max_length) == (
            "request",
            True,
            1,
            200,
        )
        assert await generate(h) == NOT_JOINED
        await h.join()
        for caller, refusal in (
            (inter(None), NOT_LISTENING),
            (inter(None, guild=False), SERVER_ONLY),
        ):
            assert (await command(lambda i: h.adapter.freestyle(i, "piano"), caller)).texts() == [
                refusal
            ]
        assert "1 to 200" in await generate(h, "x" * 201)
        assert not transport.calls
        assert await h.adapter.shutdown()

    run(scenario())


def test_shutdown_exposes_failed_cleanup_while_stream_still_owns_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with StuckClose(monkeypatch) as stuck, FakeLyria(music(1000)) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            await generate(h)
            assert await pending_stream(source).wait_ready()
            assert sample_of(source.read()) == 1000
            await settle()
            ending = asyncio.create_task(h.adapter.shutdown())
            await stuck.entered.wait()
            await until(lambda: h.adapter.cleanup_failures == 1)
            assert not ending.done() and h.pool.leased == 1 and h.adapter.retirements
            stuck.release.set()
            assert not await ending
            assert h.pool.leased == 0 and not h.adapter.tasks and h.adapter.cleanup_failures == 1

    run(scenario())


def test_filtered_audible_session_reports_expiry_before_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = ManualTime()
        filter_now = asyncio.Event()

        async def filtered(peer: Peer) -> None:
            await music(1000)(peer)
            await filter_now.wait()
            await peer.websocket.send_str('{"filteredPrompt":{}}')

        async with StuckClose(monkeypatch) as stuck, FakeLyria(filtered) as peer:
            monkeypatch.setattr(
                discord_adapter,
                "GeneratedStream",
                partial(GeneratedStream, endpoint=peer.url, clock=clock.clock, sleep=clock.sleep),
            )
            h = harness()
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            await generate(h)
            stream = pending_stream(source)
            assert await stream.wait_ready()
            source.read()
            filter_now.set()
            await stuck.entered.wait()
            await until(lambda: stream.failure == "filtered")
            clock.advance(3.1)
            await settle()
            failures_while_audible = h.adapter.cleanup_failures
            assert h.pool.leased == 1 and sample_of(source.read()) == 1000
            stuck.release.set()
            assert not await h.adapter.shutdown()
            assert failures_while_audible == 1 and h.adapter.cleanup_failures == 1
            assert h.pool.leased == 0

    run(scenario())


@pytest.mark.parametrize("action", ["grace", "loss", "leave"])
def test_generated_playback_ends_on_voice_lifecycle(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with FakeLyria(music(1000)) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            await generate(h)
            assert await pending_stream(source).wait_ready()
            source.read()
            await settle()
            player = h.adapter.players[1]
            if action == "grace":
                h.voice_update(LISTENER_ID, None)
                await until(lambda: 60 in h.clock.pending)
                (grace_timer,) = (future for seconds, future in h.clock.sleeps if seconds == 60)
                grace_timer.set_result(None)
                await until(lambda: player.station.desired is None)
                assert_faded(
                    [sample_of(source.read()) for _ in range(FADE_OUT_FRAMES)],
                    1000,
                    0,
                    FADE_OUT_FRAMES,
                )
                await until(lambda: player.station.heard is None)
                assert (
                    player.station.ended is EndReason.NO_LISTENERS and player.voice.is_connected()
                )
            elif action == "loss":
                h.adapter.channel_deleted(1, h.room.id)
                await until(lambda: not h.adapter.players)
            else:
                await command(h.adapter.leave, inter(h.room, manager=True))
                assert not h.adapter.players
            await until(lambda: h.pool.leased == 0)
            assert source.read() == b"" and await h.adapter.shutdown()

    run(scenario())


def test_terminal_access_refusal_is_visible_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with FakeLyria(Refuse(403)) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            await generate(h)
            assert not await pending_stream(source).wait_ready()
            assert source.read() == b""
            await settle()
            status = (await command(h.adapter.now, inter(None))).texts()[0] or ""
            assert "declined access" in status and "`/freestyle`" in status
            assert len(peer.requests) == 1 and await h.adapter.shutdown()

    run(scenario())


def test_full_pool_refuses_before_starting_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with FakeLyria() as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            held = [h.pool.acquire(), h.pool.acquire()]
            reply = await generate(h)
            player = h.adapter.players[1]
            playing = player.voice.is_playing()
            await settle()
            assert all(bank.release() for bank in held)
            assert await h.adapter.shutdown()
            assert not playing and "capacity" in reply
            assert source.read() == b"" and not peer.requests

    run(scenario())
