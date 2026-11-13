"""Runtime composition with real search, admission, stream, bank and journal owners.

Model/source/Discord are scripted locally; Python children stand in for mpg123.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sobafm import __main__ as app
from sobafm.catalog import Catalog, eligible
from sobafm.catalog_search import CatalogSearch
from sobafm.discord_adapter import CatalogRuntime, CatalogSession, Config, ConfigError, Track
from sobafm.gemini_match import GeminiMatcher
from sobafm.jamendo_admission import Candidate
from sobafm.jamendo_catalog import Refreshed, Refusal, Request, refresh, refresh_request
from sobafm.jamendo_journal import Attempt, Journal
from sobafm.jamendo_stream import JamendoStream
from sobafm.jamendo_transport import Locator
from sobafm.pcm import PcmBank
from sobafm.station import HISTORY_LIMIT, Audio, EndReason, Outcome, Station
from tests.test_catalog import catalog, partition, recording, reviews
from tests.test_catalog_intent import KEY, intent
from tests.test_catalog_search import EMPTY_CATALOG, Metadata, Model
from tests.test_decode import echo_frames
from tests.test_discord_adapter import (
    FakeChannel,
    FakeGuild,
    FakeInteraction,
    Harness,
    Producer,
    harness,
    inter,
    run,
    settle,
)
from tests.test_gemini_match import plan
from tests.test_jamendo_catalog import NOW, ON, response, row
from tests.test_jamendo_stream_ownership import Audio as SourceAudio
from tests.test_stream_checks import mp3


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(10):
        while not condition():
            await asyncio.sleep(0)


class Runtime(CatalogRuntime):
    def __init__(
        self, model: Model, metadata: Metadata, journal: Journal, local: Catalog = EMPTY_CATALOG
    ) -> None:
        matcher = GeminiMatcher(eligible(local, reviews(local)), KEY, transport=model)
        # The model and source are fakes, but the shared client and all validation are real.
        super().__init__(
            Config(
                "placeholder", matcher, catalog=local, reviews=reviews(local), jamendo_enabled=True
            ),
            None,
        )
        self.metadata = metadata
        self.journal = journal
        self.streams: list[JamendoStream] = []
        self.refresh_gate: asyncio.Event | None = None
        self.refresh_entered = asyncio.Event()
        self.close_gate: asyncio.Event | None = None
        self.fail_refresh = False
        self.fresh_rows: dict[str, dict[str, object]] = {}
        self.opens = 0

    def session(self, guild_id: int) -> CatalogSession:
        if guild_id not in self.sessions:
            assert self.config.catalog is not None and self.config.matcher is not None
            self.sessions[guild_id] = CatalogSession(
                CatalogSearch(
                    self.config.catalog,
                    self.config.reviews,
                    client=self.config.matcher.client,
                    metadata=self.metadata,
                    policy=ON,
                    observed_at=lambda: NOW,
                ),
                None,
            )
        return self.sessions[guild_id]

    def stream(self, guild_id: int, candidate: Candidate, bank: PcmBank) -> JamendoStream:
        session = self.session(guild_id)

        async def fresh(candidate: Candidate) -> Locator | Refusal:
            async with session.metadata_lock:
                self.refresh_entered.set()
                if self.refresh_gate is not None:
                    await self.refresh_gate.wait()
                value = self.fresh_rows.get(
                    candidate.recording.key.recording_id,
                    row(
                        candidate.recording.key.recording_id,
                        duration=candidate.recording.duration_seconds,
                    ),
                )
                if self.fail_refresh:
                    value = row(
                        candidate.recording.key.recording_id, name="Changed musical identity"
                    )
                request = refresh_request(candidate.recording.key, ON)
                assert isinstance(request, Request)
                result = refresh(request, response([value]), candidate, ON, datetime.now(UTC))
                if isinstance(result, Refusal):
                    return result
                assert isinstance(result, Refreshed)
                return Locator(result, asyncio.get_running_loop().time())

        async def open_audio(
            locator: Locator, *, attempt: Attempt, bank: PcmBank, deadline: float | None = None
        ) -> SourceAudio:
            locator.take(asyncio.get_running_loop().time())
            self.journal.claim_attempt(attempt)
            self.opens += 1
            audio = SourceAudio(mp3())
            if self.close_gate is not None:
                audio.close_allowed = self.close_gate
            return audio

        duration = candidate.recording.duration_seconds
        assert duration is not None
        stream = JamendoStream(
            candidate,
            bank,
            refresh_candidate=fresh,
            journal=self.journal,
            open_audio=open_audio,
            command=echo_frames(duration * 50),
        )
        self.streams.append(stream)
        return stream


def configure(h: Harness, runtime: Runtime) -> None:
    h.adapter.matcher = runtime.config.matcher
    h.adapter.catalog_runtime = runtime


def local_catalog() -> Catalog:
    return catalog(partition("fma", [recording("local", duration_seconds=180)]))


def test_configuration_defaults_off_and_jamendo_only_requires_no_local_media(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(os, "environ", {"SOBAFM_DISCORD_TOKEN": "placeholder"})
    config = Config.from_environ()
    assert not config.jamendo_enabled and config.matcher is None
    monkeypatch.setattr(
        os,
        "environ",
        {
            "SOBAFM_DISCORD_TOKEN": "placeholder",
            "SOBAFM_GEMINI_API_KEY": KEY,
            "SOBAFM_DATABASE": str(tmp_path / "settings.db"),
            "SOBAFM_JAMENDO_ENABLED": "true",
            "SOBAFM_JAMENDO_CLIENT_ID": "dedicated-placeholder",
            "SOBAFM_JAMENDO_JOURNAL": str(tmp_path / "journal.json"),
            "SOBAFM_JAMENDO_MAX_REQUESTS": "0",
            "SOBAFM_JAMENDO_MAX_BODY_BYTES": "1000",
        },
    )
    startup = app.load_startup()
    assert startup.media == {} and startup.config.jamendo_enabled
    assert startup.config.jamendo_quota.requests == 0
    assert "dedicated-placeholder" not in repr(startup.config)
    assert "SOBAFM_JAMENDO_CLIENT_ID" not in os.environ


@pytest.mark.parametrize(
    "values",
    [
        {"SOBAFM_JAMENDO_ENABLED": "yes"},
        {"SOBAFM_JAMENDO_ENABLED": "true"},
        {"SOBAFM_JAMENDO_CLIENT_ID": "credential?bad"},
        {"SOBAFM_JAMENDO_CLIENT_SECRET": "forbidden-placeholder"},  # pragma: allowlist secret
        {"SOBAFM_JAMENDO_MAX_REQUESTS": "-1"},
        {"SOBAFM_JAMENDO_MAX_BODY_BYTES": "1.2"},
    ],
)
def test_malformed_configuration_is_safe(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, str]
) -> None:
    monkeypatch.setattr(os, "environ", {"SOBAFM_DISCORD_TOKEN": "placeholder", **values})
    with pytest.raises(ConfigError) as caught:
        Config.from_environ()
    assert "credential?bad" not in str(caught.value)
    assert "forbidden-placeholder" not in str(caught.value)
    assert "SOBAFM_JAMENDO_CLIENT_SECRET" not in os.environ


def test_pooled_selection_late_prefetch_skip_and_refreshed_switch(tmp_path: Path) -> None:
    async def check() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        model = Model(intent(), plan((0, "close"), (1, "close")))
        metadata = Metadata(response([row("123", duration=1)]), response([]))
        runtime = Runtime(model, metadata, journal, local_catalog())
        runtime.fresh_rows["123"] = row(
            "123",
            duration=1,
            license_ccurl="https://creativecommons.org/licenses/by/4.0/",
            licenses={"ccnc": False, "ccnd": False, "ccsa": False},
        )
        h = harness(Producer(end=False))
        configure(h, runtime)
        source = await h.join()
        replies = await h.play()
        assert "partial" in str(replies)
        await settle()
        assert source.read()
        await settle()
        player = h.adapter.players[1]
        old = player.station.heard
        assert isinstance(old, Audio) and old.recording.key.source.value == "fma"
        assert not runtime.streams and len(model.bodies) == 2
        # Long current recording: metadata is retained, no refresh/open/bank lease.
        assert h.pool.leased == 1
        await run(h.adapter.skip, inter(h.room))
        await until(lambda: bool(runtime.streams))
        await until(
            lambda: (
                runtime.streams[0].report().prepared is not None
                or runtime.streams[0].report().io_settled
            )
        )
        assert runtime.streams[0].report().prepared is not None, (
            runtime.streams[0].report().failure,
            runtime.streams[0].report().refusal,
        )
        await until(lambda: player.preparing_stream is None)
        assert player.station.heard is old
        prepared = runtime.streams[0].report().prepared
        assert prepared is not None
        assert source.read()
        await settle()
        heard = player.station.heard
        assert isinstance(heard, Audio)
        assert heard.recording is prepared.candidate.recording and heard.credits is prepared.credits
        assert runtime.opens == 1 and len(model.bodies) == 2
        assert await h.adapter.shutdown()
        assert not journal.pinned(prepared.attempt)
        await journal.close()

    asyncio.run(check())


def test_stop_joins_search_owner_and_fourth_station_makes_zero_calls(tmp_path: Path) -> None:
    async def check() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        model = Model(intent(), intent(), intent())
        metadata = Metadata()
        metadata.gate = asyncio.Event()
        metadata.settle = asyncio.Event()
        runtime = Runtime(model, metadata, journal)
        h = harness()
        configure(h, runtime)
        rooms = [FakeChannel(f"room-{i}") for i in range(1, 5)]
        for i, room in enumerate(rooms, 1):
            room.guild = FakeGuild(i)
            fake = inter(room, manager=True)
            fake.guild = room.guild
            await run(h.adapter.join, fake)

        async def play(room: FakeChannel) -> None:
            fake = inter(room)
            fake.guild = room.guild
            await run(lambda i: h.adapter.play(i, "piano"), fake)

        requests = [asyncio.create_task(play(room)) for room in rooms[:3]]
        await until(lambda: len(metadata.requests) == 3)
        await play(rooms[3])
        assert len(model.bodies) == len(metadata.requests) == 3 and h.pool.leased == 0
        stop = asyncio.create_task(run(h.adapter.stop, inter(rooms[0])))
        await metadata.stopping.wait()
        await play(rooms[3])
        assert len(model.bodies) == 3 and h.pool.owners == (1, 2, 3)
        # Stop owns the cancelled source until its original work actually settles.
        closing = asyncio.create_task(h.adapter.shutdown())
        await settle()
        assert not closing.done()
        metadata.settle.set()
        await asyncio.gather(*requests)
        await stop
        assert await closing
        assert not h.pool.owners
        await journal.close()

    asyncio.run(check())


def test_failed_refresh_preserves_heard_credits_and_never_opens(tmp_path: Path) -> None:
    async def check() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        model = Model(intent(), plan((0, "close"), (1, "close")), intent(), plan((1, "close")))
        metadata = Metadata(response([row("123", duration=1)]), response([row("123", duration=1)]))
        runtime = Runtime(model, metadata, journal, local_catalog())
        h = harness(Producer(end=False))
        configure(h, runtime)
        source = await h.join()
        await h.play()
        await settle()
        assert source.read()
        await settle()
        old = h.adapter.players[1].station.heard
        # Stop the autonomous refill while this replacement is controlled.
        player = h.adapter.players[1]
        if player.refill is not None:
            player.refill.task.cancel()
        runtime.fail_refresh = True
        await h.play("another direction")
        await until(lambda: bool(runtime.streams))
        await until(lambda: runtime.streams[0].report().settled)
        assert player.station.heard is old and source.read()
        assert runtime.opens == 0
        await h.adapter.shutdown()
        await journal.close()

    asyncio.run(check())


def test_refill_reuses_intent_and_stops_after_three_empty_windows(tmp_path: Path) -> None:
    async def check() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        model = Model(intent(), plan((0, "close")))
        metadata = Metadata(*(response([]) for _ in range(4)))
        runtime = Runtime(model, metadata, journal, local_catalog())
        h = harness(Producer(end=False))
        configure(h, runtime)
        source = await h.join()
        await h.play()
        await settle()
        assert source.read()
        player = h.adapter.players[1]
        await until(lambda: len(metadata.requests) == 2)
        program = player.station.desired
        assert program is not None
        discovery = player.discoveries[program]
        original_intent = discovery.selection.intent
        for count in (3, 4):
            await until(lambda: bool(h.clock.pending))
            h.clock.now += 60
            h.clock.expire()
            await until(lambda count=count: len(metadata.requests) == count)
        await until(lambda: player.station.ending is not None)
        assert player.station.ending is EndReason.NO_PROGRESS
        assert discovery.budget.cycles == 4 and discovery.refill.attempts == 3
        assert discovery.selection.intent is original_intent
        assert len(model.bodies) == 2 and len(discovery.budget.tried) == 1
        assert source.read()  # ending discovery leaves valid current audio alone
        assert await h.adapter.shutdown()
        await journal.close()

    asyncio.run(check())


@pytest.mark.parametrize("wake", ["drain", "threshold"])
def test_early_drain_or_thirty_second_threshold_wakes_single_preparation(
    tmp_path: Path,
    wake: str,
) -> None:
    async def check() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        model = Model(intent(), plan((0, "close"), (1, "close")))
        metadata = Metadata(response([row("123", duration=1)]), response([]))
        runtime = Runtime(model, metadata, journal, local_catalog())
        h = harness(Producer(end=False))
        configure(h, runtime)
        source = await h.join()
        await h.play()
        await settle()
        assert source.read()
        await settle()
        assert not runtime.streams
        current = source._current  # pyright: ignore[reportPrivateUsage]
        assert isinstance(current, Track)
        if wake == "drain":
            current.bank.cancel()  # early integrity/drain path, independent of metadata duration
            source.read()
        else:
            current.frames = 150 * 50
            h.adapter._capacity_changed()  # pyright: ignore[reportPrivateUsage]
        await until(lambda: len(runtime.streams) == 1)
        assert h.pool.leased <= 2
        assert await h.adapter.shutdown()
        await journal.close()

    asyncio.run(check())


def test_station_history_refuses_the_769th_key_without_eviction() -> None:
    from tests.test_discord_adapter import recording as track_recording

    station = Station()
    program = station.request("piano").program
    for index in range(HISTORY_LIMIT):
        assert station.prepared(program, track_recording(str(index))) is Outcome.ACCEPTED
        ready = station.claim_ready()
        assert ready is not None
        assert station.switched(ready) is Outcome.ACCEPTED
        assert station.drained(ready) is Outcome.ACCEPTED
        assert station.continue_program(program) is Outcome.ACCEPTED
    before = station.history(program)
    assert station.prepared(program, track_recording("overflow")) is Outcome.REJECTED
    assert station.history(program) == before and len(before) == 768


def test_stream_bank_view_keeps_journal_pin_and_same_station_reservation(tmp_path: Path) -> None:
    async def check() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        model = Model(intent(), plan((0, "close")))
        metadata = Metadata(response([row("123", duration=1)]))
        runtime = Runtime(model, metadata, journal)
        runtime.refresh_gate = asyncio.Event()
        h = harness()
        configure(h, runtime)
        await h.join()
        await h.play()
        await runtime.refresh_entered.wait()
        stream = runtime.streams[0]
        region = stream.bank.reserve_write(4)
        assert region is not None
        view = region[:]
        stream.bank.commit_write(0)
        runtime.refresh_gate.set()
        await until(lambda: stream.report().io_settled)
        prepared = stream.report().prepared
        assert prepared is not None
        owner = h.pool.reserve(1)
        h.pool.reserve(2)
        h.pool.reserve(3)
        await run(h.adapter.stop, inter(h.room))
        assert journal.pinned(prepared.attempt) and h.adapter.stream_debt
        assert h.pool.reserve(1) is owner and owner.leased == 1
        first = stream.report().cleanup_started_at
        stream.cancel()
        assert stream.report().cleanup_started_at == first
        view.release()
        h.adapter._capacity_changed()  # pyright: ignore[reportPrivateUsage]
        await until(lambda: not journal.pinned(prepared.attempt))
        assert not h.adapter.stream_debt
        assert await h.adapter.shutdown()
        await journal.close()

    asyncio.run(check())


def test_three_mixed_stations_keep_stream_debt_and_refuse_fourth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_freestyle_plan import FakeTransport, interaction, interpreter, output
    from tests.test_freestyle_station import connect_loopback, music, pending_stream
    from tests.test_lyria import FakeLyria

    async def check() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        model = Model(intent(), plan((0, "close"), (1, "close")), intent(), plan((1, "close")))
        metadata = Metadata(response([row("123", duration=1)]), response([row("123", duration=1)]))
        runtime = Runtime(model, metadata, journal, local_catalog())
        runtime.refresh_gate = asyncio.Event()
        h = harness(Producer(end=False))
        configure(h, runtime)
        rooms = [FakeChannel(f"room-{i}") for i in range(1, 5)]

        def command(room: FakeChannel, *, manager: bool = False) -> FakeInteraction:
            fake = inter(room, manager=manager)
            fake.guild = room.guild
            return fake

        for number, room in enumerate(rooms, 1):
            room.guild = FakeGuild(number)
            await run(h.adapter.join, command(room, manager=True))
        await run(lambda i: h.adapter.play(i, "piano"), command(rooms[0]))
        await settle()
        assert h.adapter.players[1].source.read()
        await settle()
        await run(lambda i: h.adapter.play(i, "another"), command(rooms[1]))
        await runtime.refresh_entered.wait()
        stream = runtime.streams[0]
        region = stream.bank.reserve_write(4)
        assert region is not None
        view = region[:]
        stream.bank.commit_write(0)
        runtime.refresh_gate.set()
        await until(lambda: stream.report().io_settled)
        async with FakeLyria(music(1000)) as peer:
            connect_loopback(monkeypatch, peer)
            generated = FakeTransport(interaction(output()))
            h.adapter.interpreter = interpreter(generated)
            await run(lambda i: h.adapter.freestyle(i, "piano"), command(rooms[2]))
            assert await pending_stream(h.adapter.players[3].source).wait_ready()
            assert h.pool.owners == (1, 2, 3) and h.pool.leased == 3
            calls = len(model.bodies), len(metadata.requests), len(generated.calls), runtime.opens
            refused = await run(lambda i: h.adapter.play(i, "piano"), command(rooms[3]))
            assert "Three stations" in str(refused.texts())
            await run(h.adapter.stop, command(rooms[1]))
            assert h.adapter.stream_debt and h.pool.reserve(2).leased == 1
            refused = await run(lambda i: h.adapter.freestyle(i, "piano"), command(rooms[3]))
            assert "Three stations" in str(refused.texts())
            assert calls == (
                len(model.bodies),
                len(metadata.requests),
                len(generated.calls),
                runtime.opens,
            )
            view.release()
            h.adapter._capacity_changed()  # pyright: ignore[reportPrivateUsage]
            await until(lambda: not h.adapter.stream_debt)
            assert await h.adapter.shutdown()
        await journal.close()

    asyncio.run(check())


@pytest.mark.parametrize("limit", ["cycles", "history"])
def test_discovery_lifetime_ending_makes_no_further_source_or_model_call(
    tmp_path: Path,
    limit: str,
) -> None:
    async def check() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        model = Model(intent(), plan((0, "close")))
        metadata = Metadata(response([]))
        runtime = Runtime(model, metadata, journal, local_catalog())
        h = harness(Producer(end=False))
        configure(h, runtime)
        source = await h.join()
        await h.play()
        player = h.adapter.players[1]
        program = player.station.desired
        assert program is not None
        budget = player.discoveries[program].budget
        if limit == "cycles":
            budget.cycles = 768
        else:
            from sobafm.catalog import RecordingKey, Source

            for index in range(767):
                budget.mark_tried(RecordingKey(Source.FMA, str(index)))
        await settle()
        assert source.read()
        await until(lambda: player.station.ending is not None)
        assert player.station.ending is (
            EndReason.CYCLES if limit == "cycles" else EndReason.HISTORY
        )
        assert len(model.bodies) == 2 and len(metadata.requests) == 1
        assert await h.adapter.shutdown()
        await journal.close()

    asyncio.run(check())


def test_refused_window_preserves_safe_histogram_without_source_text(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def check() -> None:
        journal = await Journal.open(tmp_path / "journal.json")
        model = Model(intent())
        metadata = Metadata(
            response(
                [
                    row(
                        "123",
                        name="Unlogged source title",
                        license_ccurl="https://example.invalid/unsupported",
                    )
                ]
            )
        )
        runtime = Runtime(model, metadata, journal)
        h = harness()
        configure(h, runtime)
        await h.join()
        caplog.set_level(logging.INFO)
        replies = await h.play("Unlogged musical request")
        assert "No usable match" in str(replies)
        assert "unsupported_license" in caplog.text and "status=200" in caplog.text
        assert "Unlogged source title" not in caplog.text
        assert "Unlogged musical request" not in caplog.text
        assert not runtime.streams and runtime.opens == 0
        assert await h.adapter.shutdown()
        await journal.close()

    asyncio.run(check())
