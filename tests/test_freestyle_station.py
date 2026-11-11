"""Offline command-to-station schedules with the real interpreter, stream and PCM pool."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from functools import partial
from types import SimpleNamespace
from typing import cast

import disnake
import pytest

from sobafm import discord_adapter
from sobafm.credits import escaped_text
from sobafm.discord_adapter import GeneratedTrack, PlayingTrack, StationSource, create_bot
from sobafm.gemini_match import GeminiMatcher
from sobafm.lyria import (
    CROSSFADE_FRAMES,
    FADE_OUT_FRAMES,
    PLAY_MESSAGE,
    RENEW_AFTER_SECONDS,
    GeneratedStream,
    LyriaSession,
)
from sobafm.pcm import PcmBankPool
from sobafm.station import EndReason
from tests.test_discord_adapter import (
    LISTENER_ID,
    NOT_JOINED,
    NOT_LISTENING,
    SERVER_ONLY,
    FakeChannel,
    FakeGuild,
    FakeInteraction,
    FakeMatcher,
    FakeVoice,
    Harness,
    Tagged,
    harness,
    inter,
    matcher_of,
    program_harness,
    recording,
    settings_of,
    settle,
    tag,
)
from tests.test_discord_adapter import (
    run as command,
)
from tests.test_freestyle_plan import (
    KEY,
    FakeTransport,
    HeldTransport,
    error_body,
    interaction,
    interpreter,
    output,
)
from tests.test_gemini_match import plan as catalog_plan
from tests.test_lyria import (
    PRE_ROLL_FRAMES,
    FakeLyria,
    ManualTime,
    Peer,
    Refuse,
    Script,
    StuckClose,
    assert_faded,
    feed_bank,
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


def timed_harness(monkeypatch: pytest.MonkeyPatch, peer: FakeLyria, clock: ManualTime) -> Harness:
    # Session and bank cancellation timestamps are compared against the same deadline.
    monkeypatch.setattr(
        discord_adapter,
        "GeneratedStream",
        partial(GeneratedStream, endpoint=peer.url, clock=clock.clock, sleep=clock.sleep),
    )
    return harness(pool=PcmBankPool(clock=clock.clock))


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


@pytest.mark.parametrize("timer_first", [True, False], ids=["timer-first", "report-first"])
def test_generated_cutoff_starts_the_waiting_catalog_track(
    monkeypatch: pytest.MonkeyPatch, timer_first: bool
) -> None:
    async def scenario() -> None:
        async with FakeLyria(music(1000)) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness(recordings=(recording("b"),))
            settings_of(h).current = settings_of(h).current.changed(generated_minutes=5)
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            await generate(h)
            voice = h.room.voice
            assert voice is not None
            voice.connected = False
            assert await pending_stream(source).wait_ready()
            h.clock.now += 299
            voice.connected = True

            player = h.adapter.players[1]
            report_switch = await hold_generated_switch(h, source, monkeypatch)
            assert "accepted" in (await h.play())[0]
            expected = player.station.ready
            assert expected is not None and player.station.heard is None

            expire_around_report(h, report_switch, timer_first)
            await settle()
            assert player.output.read() not in (b"", discord_adapter.SILENCE)
            await settle()
            assert player.station.heard is expected and player.station.desired is expected.program
            assert "Song b" in ((await command(h.adapter.now, inter(None))).texts()[1] or "")
            assert await h.adapter.shutdown()

    run(scenario())


async def prepare_over_heard_catalog(
    h: Harness, monkeypatch: pytest.MonkeyPatch, peer: FakeLyria
) -> StationSource:
    connect_loopback(monkeypatch, peer)
    settings_of(h).current = settings_of(h).current.changed(generated_minutes=5)
    h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
    source = await h.join()
    assert "accepted" in (await h.play())[0]
    await settle()
    assert sample_of(h.adapter.players[1].output.read()) == sample_of(tag("a")) // 2
    await settle()
    assert "Song a" in ((await command(h.adapter.now, inter(None))).texts()[1] or "")
    await generate(h)
    voice = h.room.voice
    assert voice is not None
    voice.connected = False
    assert await pending_stream(source).wait_ready()
    h.clock.now += 299
    voice.connected = True
    return source


async def hold_generated_switch(
    h: Harness, source: StationSource, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], None]:
    """Run the real player for one frame. Only its first-frame report is held."""
    report = source._report  # pyright: ignore[reportPrivateUsage]
    switches: list[Callable[[], None]] = []
    samples: list[int] = []

    def hold_switch(kind: str, track: PlayingTrack) -> None:
        if kind == "switched":
            switches.append(partial(report, kind, track))
        else:
            report(kind, track)

    def receive(data: bytes, *, encode: bool) -> None:
        assert encode
        samples.append(sample_of(data))
        thread.stop()

    with monkeypatch.context() as callback:
        callback.setattr(source, "_report", hold_switch)
        sink = local_voice_sink(callback, receive)
        thread = disnake.player.AudioPlayer(h.adapter.players[1].output, sink)
        thread.start()
        await asyncio.to_thread(thread.join, 2)
        assert not thread.is_alive()
    assert samples == [500]
    (deliver,) = switches
    return deliver


def expire_around_report(h: Harness, report: Callable[[], None], timer_first: bool) -> None:
    h.clock.now += 1
    if timer_first:
        h.clock.expire()
    report()
    if not timer_first:
        h.clock.expire()


@pytest.mark.parametrize("order", ["zero-frames", "timer-first", "report-first"])
def test_generated_expiry_settles_what_displaced_the_heard_catalog(
    monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    async def scenario() -> None:
        async with FakeLyria(music(1000)) as peer:
            h, _ = program_harness("a")
            source = await prepare_over_heard_catalog(h, monkeypatch, peer)
            if order == "zero-frames":
                h.clock.now += 1
                h.clock.expire()
            else:
                report = await hold_generated_switch(h, source, monkeypatch)
                expire_around_report(h, report, order == "timer-first")
            await until(lambda: h.pool.leased == int(order == "zero-frames"))
            player = h.adapter.players[1]
            now = (await command(h.adapter.now, inter(None))).texts()
            if order == "zero-frames":
                assert "Song a" in (now[1] or "")
                assert sample_of(player.output.read()) == sample_of(tag("a")) // 2
            else:
                assert len(now) == 1 and player.station.heard is None
                assert player.output.read() == b""
            assert await h.adapter.shutdown()

    run(scenario())


@pytest.mark.parametrize(
    ("timer_first", "action"), [(True, "play"), (False, "play"), (True, "stop"), (False, "replace")]
)
def test_accepted_catalog_waits_for_two_retiring_banks(
    monkeypatch: pytest.MonkeyPatch, timer_first: bool, action: str
) -> None:
    async def scenario() -> None:
        async with FakeLyria(music(1000)) as peer, StuckClose(monkeypatch) as close:
            h, decoder = program_harness("a")
            source = await prepare_over_heard_catalog(h, monkeypatch, peer)
            player = h.adapter.players[1]
            release_a = decoder.holding["a"] = asyncio.Event()
            late = action == "play" and timer_first
            try:
                report = await hold_generated_switch(h, source, monkeypatch)
                h.adapter.matcher = FakeMatcher(recording("b"))
                settings_of(h).current = settings_of(h).current.changed(cooldown_seconds=60)
                assert "accepted" in (await h.play())[0]
                expected = player.station.ready
                claim = player.waiting_claim
                assert expected is not None and claim is not None
                expire_around_report(h, report, timer_first)
                await close.entered.wait()
                await settle()
                assert h.pool.leased == 2 and player.station.ready is expected
                assert player.waiting_claim is claim and "cooldown" in (await h.play())[0].lower()
                assert player.station.heard is None
                if late:
                    await asyncio.sleep(3.12)
                    assert h.adapter.cleanup_failures == 2 and h.pool.leased == 2
                if action == "stop":
                    await command(h.adapter.stop, inter(h.room))
                elif action == "replace":
                    # Queue the genuine replacement under the lock before A's release
                    # wakes B. The stale waiter must not start or refund C.
                    async with player.lock:
                        h.clock.now += 60
                        matcher = FakeMatcher(recording("c"))
                        h.adapter.matcher = matcher
                        replacing = asyncio.create_task(h.play("newer"))
                        await until(lambda: bool(matcher.requests))
                        release_a.set()
                        await until(lambda: h.pool.leased == 1)
                    assert "accepted" in (await replacing)[0]
                release_a.set()
                if action == "stop":
                    await until(lambda: h.pool.leased == 1)
                    assert player.output.read() == b"" and player.station.desired is None
                    assert decoder.started == ["a"]
                else:
                    song = "c" if action == "replace" else "b"
                    await until(lambda: song in decoder.started)
                    assert h.pool.leased == 2  # generation still owns its closing socket
                    assert sample_of(player.output.read()) == sample_of(tag(song)) // 2
                    await settle()
                    assert f"Song {song}" in (
                        (await command(h.adapter.now, inter(None))).texts()[1] or ""
                    )
                    assert "cooldown" in (await h.play())[0].lower()
                    assert decoder.started == ["a", song]
                    if action == "play":
                        assert player.station.heard is expected
            finally:
                release_a.set()
                close.release.set()
            assert await h.adapter.shutdown() is (not late)
            assert h.pool.leased == 0 and h.adapter.cleanup_failures == (2 if late else 0)

    run(scenario())


def test_waiting_generated_successor_keeps_its_original_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with FakeLyria(music(1000)) as peer, StuckClose(monkeypatch) as close:
            h, decoder = program_harness("a")
            source = await prepare_over_heard_catalog(h, monkeypatch, peer)
            player = h.adapter.players[1]
            ((_, old_timer),) = [entry for entry in h.clock.sleeps if not entry[1].done()]
            release_a = decoder.holding["a"] = asyncio.Event()
            try:
                report = await hold_generated_switch(h, source, monkeypatch)
                assert "Preparing" in await generate(h)
                settings_of(h).current = settings_of(h).current.changed(cooldown_seconds=600)
                waiting = player.station.ready
                assert waiting is not None and player.waiting_claim is not None
                h.clock.now += 1
                old_timer.set_result(None)
                report()
                await close.entered.wait()
                await settle()
                assert player.station.ready is waiting and h.pool.leased == 2
                h.clock.now += 299
                h.clock.expire()
                await settle()
                assert player.station.ended is EndReason.DURATION and player.waiting_claim is None
                assert len(peer.requests) == 1
            finally:
                release_a.set()
                close.release.set()
            await until(lambda: h.pool.leased == 0)
            assert "accepted" in (await h.play())[0]
            assert await h.adapter.shutdown()

    run(scenario())


@pytest.mark.parametrize("competing_guild", [False, True])
def test_internal_renewal_wakes_the_accepted_catalog_track(
    monkeypatch: pytest.MonkeyPatch, competing_guild: bool
) -> None:
    async def scenario() -> None:
        renewals = asyncio.Queue[None]()

        async def sleep(delay: float) -> None:
            if delay == RENEW_AFTER_SECONDS:
                await renewals.get()
            else:
                await asyncio.sleep(delay)

        async with FakeLyria(music(1000), music(3000)) as peer:
            monkeypatch.setattr(
                discord_adapter,
                "GeneratedStream",
                partial(GeneratedStream, endpoint=peer.url, sleep=sleep),
            )
            h, decoder = program_harness("b")
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            await generate(h)
            try:
                stream = pending_stream(source)
                assert await stream.wait_ready()
                old = feed_bank(stream)
                report = await hold_generated_switch(h, source, monkeypatch)
                renewals.put_nowait(None)
                await until(lambda: stream.crossfading)
                h.clock.now += 60
                settings_of(h).current = settings_of(h).current.changed(cooldown_seconds=60)
                assert "accepted" in (await h.play())[0]
                player = h.adapter.players[1]
                expected, claim = player.station.ready, player.waiting_claim
                assert expected is not None and claim is not None
                report()
                await settle()
                assert player.station.ready is expected and h.pool.leased == 2
                levels = [sample_of(player.output.read()) for _ in range(CROSSFADE_FRAMES)]
                assert_faded(levels, 500, 1500, CROSSFADE_FRAMES)
                await until(lambda: old.facts().released)
                await settle()
                assert not peer.peers[1].disconnected.is_set()  # renewal's new feed is still live
                if competing_guild:
                    await asyncio.sleep(0.2)
                    other = FakeChannel("other", guild=FakeGuild(2))
                    join = inter(other, manager=True)
                    join.guild = other.guild
                    await command(h.adapter.join, join)
                    listener = inter(other)
                    listener.guild = other.guild
                    reply = await command(lambda i: h.adapter.play(i, "other"), listener)
                    assert "accepted" in (reply.texts()[0] or "")
                    await settle()
                assert player.station.switching is expected
                assert decoder.started == ["b"] * (2 if competing_guild else 1)
                assert sample_of(player.output.read()) == sample_of(tag("b")) // 2
                await settle()
                assert (
                    player.station.heard is expected and "cooldown" in (await h.play())[0].lower()
                )
            finally:
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
            h = timed_harness(monkeypatch, peer, clock)
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
            held = [h.pool.reserve(1).acquire(), h.pool.reserve(1).acquire()]
            reply = await generate(h)
            player = h.adapter.players[1]
            playing = player.voice.is_playing()
            await settle()
            assert all(bank.release() for bank in held)
            assert await h.adapter.shutdown()
            assert not playing and "capacity" in reply
            assert source.read() == b"" and not peer.requests

    run(scenario())


@pytest.mark.parametrize("outcome", ["refused", "failed", "capacity"])
def test_unsuccessful_catalog_match_preserves_generation(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with FakeLyria(music(1000)) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            held = HeldTransport(interaction(output()))
            h.adapter.interpreter = interpreter(held)
            source = await h.join()
            pending = asyncio.create_task(generate(h))
            await until(lambda: bool(held.calls))
            catalog = FakeTransport(
                interaction(
                    catalog_plan(outcome="refuse")
                    if outcome == "refused"
                    else catalog_plan((0, "close"))
                ),
                status=503 if outcome == "failed" else 200,
            )
            h.adapter.matcher = GeminiMatcher((recording("a"),), KEY, transport=catalog)
            banks = []
            if outcome == "capacity":
                banks = [h.pool.reserve(1).acquire(), h.pool.reserve(1).acquire()]
            assert "accepted" not in (await h.play())[0]
            assert len(catalog.calls) == 1
            for bank in banks:
                assert bank.release()
            held.release.set()
            assert "Preparing" in await pending
            assert await pending_stream(source).wait_ready()
            assert sample_of(source.read()) == 1000
            assert await h.adapter.shutdown()

    run(scenario())


def test_pending_direction_preserves_catalog_skip() -> None:
    async def scenario() -> None:
        h = harness(recordings=(recording("a"), recording("b")))
        held = HeldTransport(interaction('{"outcome":"refuse"}'))
        h.adapter.interpreter = interpreter(held)
        source = await h.join()
        await h.play()
        await settle()
        source.read()
        await settle()
        pending = asyncio.create_task(generate(h))
        await until(lambda: bool(held.calls))
        reply = (await command(h.adapter.skip, inter(h.room))).texts()[0] or ""
        held.release.set()
        assert "declined" in await pending
        assert reply.startswith("Skipping")
        source.read()
        await settle()
        assert "Song b" in ((await command(h.adapter.now, inter(None))).texts()[1] or "")
        assert await h.adapter.shutdown()

    run(scenario())


def test_unread_refusal_settles_before_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with FakeLyria(Refuse(403)) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            settings_of(h).current = settings_of(h).current.changed(
                cooldown_seconds=600, generated_minutes=5
            )
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            await generate(h)
            stream = pending_stream(source)
            assert not await stream.wait_ready()
            station = h.adapter.players[1].station
            await until(lambda: station.ended is EndReason.GENERATION_ACCESS_DENIED)
            h.clock.now += 300
            h.clock.expire()
            await settle()
            assert station.ended is EndReason.GENERATION_ACCESS_DENIED
            assert source.read() == b"" and station.desired is None
            assert "accepted" in (await h.play())[0]
            assert await h.adapter.shutdown()

    run(scenario())


@pytest.mark.parametrize("order", ["timer_only", "callback_first", "report_after_timer"])
def test_refusal_at_cutoff_preserves_reason_and_own_claim(
    monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    async def scenario() -> None:
        failed = asyncio.Event()
        release = asyncio.Event()
        wait_prepared = LyriaSession.wait_prepared

        async def hold_observation(session: LyriaSession) -> None:
            await wait_prepared(session)
            assert session.failure == "access_denied"
            failed.set()
            await release.wait()

        monkeypatch.setattr(LyriaSession, "wait_prepared", hold_observation)
        async with FakeLyria(Refuse(403)) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            settings_of(h).current = settings_of(h).current.changed(
                cooldown_seconds=600, generated_minutes=5
            )
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            await generate(h)
            await failed.wait()
            station = h.adapter.players[1].station
            assert station.switching is not None and h.clock.pending == [300]
            h.clock.now += 300
            if order == "callback_first":
                assert source.read() == b""
            h.clock.expire()
            if order == "report_after_timer":
                assert source.read() == b""  # detached before the timer, report queued behind it
            await until(lambda: station.ended is not None)
            assert station.ended is EndReason.GENERATION_ACCESS_DENIED
            assert "accepted" in (await h.play())[0]
            release.set()
            await settle()
            assert "cooldown" in (await h.play())[0].lower()
            assert len(peer.requests) == 1
            assert await h.adapter.shutdown()

    run(scenario())


def test_now_recovers_only_on_emitted_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        allow_pcm = asyncio.Event()

        async def first(peer: Peer) -> None:
            await music(1000)(peer)
            await peer.websocket.close(code=1000)

        async def next_feed(peer: Peer) -> None:
            await allow_pcm.wait()
            await music(2000)(peer)

        async with FakeLyria(first, next_feed) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            await generate(h)
            stream = pending_stream(source)
            voice_output = h.adapter.players[1].output
            assert await stream.wait_ready()
            for _ in range(PRE_ROLL_FRAMES):
                assert sample_of(voice_output.read()) == 500
            await until(lambda: len(peer.requests) == 2)
            for _ in range(52):
                assert voice_output.read() is discord_adapter.SILENCE
            assert h.pool.voice_readers == 0
            await settle()

            async def status() -> str:
                return (await command(h.adapter.now, inter(None))).texts()[0] or ""

            gap = await status()
            assert "recovering" in gap and "Now heard" not in gap and "remaining" in gap
            allow_pcm.set()
            await until(lambda: not stream.crossfading and pending_recovery_ready(stream))
            assert "recovering" in await status()
            assert sample_of(voice_output.read()) == 1000
            assert "Now heard" in await status()
            assert await h.adapter.shutdown()

    run(scenario())


def pending_recovery_ready(stream: GeneratedStream) -> bool:
    voice = stream._voice  # pyright: ignore[reportPrivateUsage]
    return voice.audible is not None and voice.audible.bank.ready_now()


def test_volume_output_reserves_through_frame_handoff() -> None:
    async def scenario() -> None:
        h = harness()
        previous: list[bytes] = []
        for _ in range(2):
            await h.join()
            await h.play()
            await settle()
            output = h.adapter.players[1].output
            previous.append(output.read())
            in_handoff = output.read()
            assert in_handoff is not discord_adapter.SILENCE
            assert output.read() is discord_adapter.SILENCE
            del in_handoff
            await command(h.adapter.leave, inter(h.room, manager=True))
            assert h.pool.leased == 0
        assert all(frame != discord_adapter.SILENCE for frame in previous)
        await h.join()
        await h.play()
        await settle()
        player = h.adapter.players[1]
        assert player.output.read() is discord_adapter.SILENCE
        await settle()
        assert player.station.heard is None
        previous.pop()  # actual release by one sink admits this reader's first frame
        assert player.output.read() != discord_adapter.SILENCE
        previous.clear()
        assert await h.adapter.shutdown()

    run(scenario())


@pytest.mark.parametrize("return_before_observer", [True, False])
def test_shutdown_reports_voice_output_held_after_bank_release(
    monkeypatch: pytest.MonkeyPatch,
    return_before_observer: bool,
) -> None:
    async def scenario() -> None:
        h = harness()
        await h.join()
        await h.play()
        await settle()
        frames = [h.adapter.players[1].output.read()]
        await command(h.adapter.leave, inter(h.room, manager=True))
        assert h.pool.leased == 0 and h.pool.voice_readers == 1
        # All producers have settled. Advance only the wait for a retained voice frame.
        monkeypatch.setattr(discord_adapter, "CLEANUP_SECONDS", 0)
        ending = asyncio.create_task(h.adapter.shutdown())
        await asyncio.sleep(0)  # deadline set, observer queued on the loop
        if return_before_observer:
            frames.clear()
        assert not await ending
        frames.clear()
        assert h.pool.voice_readers == 0 and not await h.adapter.shutdown()
        assert h.adapter.cleanup_failures == 1

    run(scenario())


def local_voice_sink(
    monkeypatch: pytest.MonkeyPatch, send: Callable[..., None]
) -> disnake.VoiceClient:
    # The pinned player reads the real StationOutput. Only the two outbound edges are fake.
    def mute_speaking(*args: object) -> None:
        pass

    monkeypatch.setattr(disnake.player.AudioPlayer, "_speak", mute_speaking)
    connected = threading.Event()
    connected.set()
    return cast(disnake.VoiceClient, SimpleNamespace(_connected=connected, send_audio_packet=send))


def test_three_real_voice_readers_in_startup_backoff_share_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = ManualTime()
        # Distinct origins expose an accidental comparison with raw host uptime.
        clock.now = time.monotonic() + 3600
        release = threading.Event()
        outputs: list[bytes] = []
        threads: list[disnake.player.AudioPlayer] = []
        async with FakeLyria(Refuse(429), Refuse(429), Refuse(429)) as peer:
            h = timed_harness(monkeypatch, peer, clock)
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))

            def hold_packet(data: bytes, *, encode: bool) -> None:
                assert encode
                outputs.append(data)
                release.wait()

            sink = local_voice_sink(monkeypatch, hold_packet)
            try:
                for guild in range(1, 4):
                    room = FakeChannel(str(guild))
                    join = inter(room, manager=True)
                    join.guild = FakeGuild(guild)
                    await command(h.adapter.join, join)
                    listener = inter(room)
                    listener.guild = join.guild
                    await command(lambda i: h.adapter.freestyle(i, "piano"), listener)
                    await until(lambda: h.pool.leased == 0)
                    thread = disnake.player.AudioPlayer(h.adapter.players[guild].output, sink)
                    threads.append(thread)
                    thread.start()
                await until(lambda: len(outputs) == 3)
                assert h.pool.leased == 0 and len(peer.requests) == 3
                assert all(frame is discord_adapter.SILENCE for frame in outputs)
            finally:
                for thread in threads:
                    thread.stop()
                release.set()
                for thread in threads:
                    await asyncio.to_thread(thread.join)
                assert await h.adapter.shutdown()

    run(scenario())


def test_duplicate_preroll_refusal_preserves_accepted_catalog_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        failed = asyncio.Event()
        watch = asyncio.Event()
        wait_ready = GeneratedStream.wait_ready

        async def held_watcher(stream: GeneratedStream) -> bool:
            ready = await wait_ready(stream)
            assert not ready
            failed.set()
            await watch.wait()
            return ready

        monkeypatch.setattr(GeneratedStream, "wait_ready", held_watcher)
        async with FakeLyria(Refuse(403)) as peer:
            connect_loopback(monkeypatch, peer)
            h = harness()
            h.adapter.interpreter = interpreter(FakeTransport(interaction(output())))
            source = await h.join()
            await generate(h)
            await failed.wait()
            matcher = matcher_of(h)
            matcher.gate = asyncio.Event()
            catalog = asyncio.create_task(h.play())
            await until(lambda: bool(matcher.requests))
            # Queue the catalog match, then the watcher, then the real callback report.
            # The callback detached the refused track before the catalog command resumes.
            matcher.gate.set()
            watch.set()
            assert source.read() == b""
            assert "accepted" in (await catalog)[0]
            await settle()
            station = h.adapter.players[1].station
            assert source.read() not in (b"", discord_adapter.SILENCE)
            await settle()
            assert station.heard is not None and station.heard.program is station.desired
            assert "Song a" in ((await command(h.adapter.now, inter(None))).texts()[1] or "")
            settings_of(h).current = settings_of(h).current.changed(cooldown_seconds=600)
            assert "cooldown" in (await h.play())[0].lower()
            assert await h.adapter.shutdown()

    run(scenario())


def test_deferred_catalog_start_cannot_displace_a_newer_ready_track() -> None:
    async def scenario() -> None:
        h = harness()
        source = await h.join()
        await h.play()
        await settle()
        assert source.read() not in (b"", discord_adapter.SILENCE)
        h.adapter.matcher = FakeMatcher(recording("b"))
        assert "accepted" in (await h.play("second"))[0]
        station = h.adapter.players[1].station
        assert station.ready is not None  # B waits for A's queued switch report
        # The A report schedules B after this third command is already runnable.
        h.adapter.matcher = FakeMatcher(recording("c"))
        latest = asyncio.create_task(h.play("third"))
        assert "accepted" in (await latest)[0]
        await settle()
        assert source.read() not in (b"", discord_adapter.SILENCE)
        await settle()
        assert "Song c" in ((await command(h.adapter.now, inter(None))).texts()[1] or "")
        assert station.heard is not None and station.heard.program is station.desired
        assert await h.adapter.shutdown()

    run(scenario())


@pytest.mark.parametrize("held_past_deadline", [True, False])
def test_real_voice_frame_deadline_runs_during_disconnect(
    monkeypatch: pytest.MonkeyPatch, held_past_deadline: bool
) -> None:
    async def scenario() -> None:
        h = harness()
        await h.join()
        await h.play()
        await settle()
        sending = threading.Event()
        return_frame = threading.Event()
        disconnecting = asyncio.Event()
        finish_disconnect = asyncio.Event()

        def hold_packet(data: bytes, *, encode: bool) -> None:
            assert encode and data != discord_adapter.SILENCE
            sending.set()
            return_frame.wait()

        async def held_disconnect(voice: FakeVoice, *, force: bool) -> None:
            assert force
            disconnecting.set()
            await finish_disconnect.wait()
            voice.connected = False

        monkeypatch.setattr(FakeVoice, "disconnect", held_disconnect)
        sink = local_voice_sink(monkeypatch, hold_packet)
        thread = disnake.player.AudioPlayer(h.adapter.players[1].output, sink)
        thread.start()
        await until(sending.is_set)
        thread.stop()  # the real player still owns the frame until send returns
        ending = asyncio.create_task(h.adapter.shutdown())
        try:
            await disconnecting.wait()
            await until(lambda: h.pool.leased == 0)
            if not held_past_deadline:
                return_frame.set()
                await asyncio.to_thread(thread.join)
            await asyncio.sleep(3.12)
            assert not ending.done() and h.pool.voice_readers == int(held_past_deadline)
            failures_at_deadline = h.adapter.cleanup_failures
        finally:
            return_frame.set()
            await asyncio.to_thread(thread.join)
            finish_disconnect.set()
        assert await ending is (not held_past_deadline)
        assert failures_at_deadline == int(held_past_deadline)
        assert h.pool.voice_readers == 0
        assert await h.adapter.shutdown() is (not held_past_deadline)
        assert h.adapter.cleanup_failures == failures_at_deadline

    run(scenario())


@pytest.mark.parametrize("catalog_stations", [0, 1, 2])
def test_three_stations_reserve_simultaneous_prefetch_and_renewal(
    monkeypatch: pytest.MonkeyPatch, catalog_stations: int
) -> None:
    async def scenario() -> None:
        renewals = asyncio.Queue[None]()

        async def sleep(delay: float) -> None:
            if delay == RENEW_AFTER_SECONDS:
                await renewals.get()
            else:
                await asyncio.sleep(delay)

        generated = 3 - catalog_stations
        async with FakeLyria(*([music(1000)] * generated + [music(3000)] * generated)) as peer:
            monkeypatch.setattr(
                discord_adapter,
                "GeneratedStream",
                partial(GeneratedStream, endpoint=peer.url, sleep=sleep),
            )
            h, producer = program_harness("a", "b", producer=Tagged(frames=2))
            transport = FakeTransport(interaction(output()))
            h.adapter.interpreter = interpreter(transport)
            streams: list[GeneratedStream] = []

            def listener(guild: int, *, manager: bool = False) -> FakeInteraction:
                room = FakeChannel(str(guild), guild=FakeGuild(guild))
                if guild in h.adapter.players:
                    room = cast(FakeChannel, h.adapter.players[guild].voice.channel)
                fake = inter(room, manager=manager)
                fake.guild = room.guild
                return fake

            for guild in range(1, 4):
                await command(h.adapter.join, listener(guild, manager=True))
                if guild <= catalog_stations:
                    await command(lambda i: h.adapter.play(i, "piano"), listener(guild))
                else:
                    await command(lambda i: h.adapter.freestyle(i, "piano"), listener(guild))
                    stream = pending_stream(h.adapter.players[guild].source)
                    streams.append(stream)
                    assert await stream.wait_ready()
            await settle()
            for player in h.adapter.players.values():
                assert player.output.read()
            await until(lambda: producer.started.count("b") == catalog_stations)
            for _ in streams:
                renewals.put_nowait(None)
            await until(lambda: all(stream.crossfading for stream in streams))
            assert h.pool.leased == 6 and len(peer.requests) == generated * 2
            for player in h.adapter.players.values():
                assert player.reservation is not None and player.reservation.leased == 2

            await command(h.adapter.join, listener(4, manager=True))
            calls = len(transport.calls)
            refused = await command(lambda i: h.adapter.freestyle(i, "piano"), listener(4))
            assert "Three stations" in str(refused.texts())
            assert len(transport.calls) == calls
            assert len(peer.requests) == generated * 2
            for guild in range(catalog_stations + 1, 4):
                player = h.adapter.players[guild]
                levels = [sample_of(player.output.read()) for _ in range(CROSSFADE_FRAMES)]
                assert_faded(levels, 500, 1500, CROSSFADE_FRAMES)
            await until(lambda: h.pool.leased == 3 + catalog_stations)
            assert await h.adapter.shutdown()

    run(scenario())
