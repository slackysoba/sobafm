"""Offline Discord adapter schedules with faked disnake objects; no token, gateway or voice."""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import signal
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, cast

import disnake
import pytest

from sobafm import discord_adapter
from sobafm.catalog import (
    AdmissionReview,
    Availability,
    Catalog,
    CreditSnapshot,
    Fact,
    Feature,
    Partition,
    Recording,
    RecordingKey,
    RecordingKind,
    SnapshotState,
    Source,
    Strength,
    metadata_digest,
)
from sobafm.discord_adapter import (
    NOT_JOINED,
    NOT_LISTENING,
    SERVER_ONLY,
    SILENCE,
    Adapter,
    Config,
    ConfigError,
    Inter,
    StationSource,
    Track,
    create_bot,
    intents,
)
from sobafm.pcm import FRAME_BYTES, READY_BYTES, PcmBank, PcmBankPool

LINK = "https://example.invalid/synthetic/track"
MANY_NOTICES = tuple(f"Synthetic notice {n}: " + "x" * 150 for n in range(30))


def recording(identifier: str, notices: tuple[str, ...] = ("Synthetic notice.",)) -> Recording:
    return Recording(
        key=RecordingKey(Source.JAMENDO, identifier),
        title=f"Song {identifier}",
        primary_artist="Synthetic Artist",
        creators=("Synthetic Artist",),
        provenance=LINK,
        kind=RecordingKind.COMPLETE,
        kind_evidence=LINK,
        duration_seconds=180,
        availability=Availability.AVAILABLE,
        composition=None,
        composer=None,
        facts=(Fact(Feature.PIANO, True, Strength.STRONG, LINK),),
        credits=CreditSnapshot(f"credit-{identifier}", notices, None, None),
    )


def catalog(*recordings: Recording) -> tuple[Catalog, tuple[AdmissionReview, ...]]:
    value = Catalog(True, (Partition(Source.JAMENDO, SnapshotState.COMPLETE, "", recordings),))
    reviews = tuple(
        AdmissionReview(r.key, metadata_digest(r), True, True, True, LINK) for r in recordings
    )
    return value, reviews


# Fakes for the disnake objects the adapter touches.


@dataclass(eq=False)
class FakeVoice:
    channel: FakeChannel
    connected: bool = True
    plays: list[StationSource] = field(default_factory=lambda: [])
    disconnects: int = 0

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return bool(self.plays) and self.connected

    def play(self, source: StationSource) -> None:
        self.plays.append(source)

    def stop(self) -> None:
        pass

    async def move_to(self, channel: FakeChannel) -> None:
        self.channel = channel

    async def disconnect(self, *, force: bool) -> None:
        assert force
        self.connected = False
        self.disconnects += 1


@dataclass(eq=False)
class FakeChannel:
    name: str
    voice: FakeVoice | None = None

    @property
    def mention(self) -> str:
        return f"<#{self.name}>"

    async def connect(self) -> FakeVoice:
        self.voice = FakeVoice(self)
        return self.voice


@dataclass
class FakeVoiceState:
    channel: FakeChannel | None


@dataclass
class FakeAuthor:
    voice: FakeVoiceState | None


@dataclass
class FakePermissions:
    manage_guild: bool = False


@dataclass
class FakeGuild:
    id: int = 1


@dataclass
class Sent:
    text: str | None
    options: dict[str, object]


class RejectedResponse:
    status = 404
    reason = "Not Found"


@dataclass(eq=False)
class FakeInteraction:
    guild: FakeGuild | None
    author: FakeAuthor
    permissions: FakePermissions = field(default_factory=FakePermissions)
    sent: list[Sent] = field(default_factory=lambda: [])
    deferred: float | None = None
    created_at: datetime.datetime = field(default_factory=lambda: disnake.utils.utcnow())
    token: str = "synthetic-interaction-token"
    rejected: bool = False  # Discord answers the acknowledgement with 10062

    @property
    def guild_id(self) -> int | None:
        return None if self.guild is None else self.guild.id

    @property
    def response(self) -> FakeInteraction:
        return self

    @property
    def followup(self) -> FakeInteraction:
        return self

    async def send_message(self, text: str, **options: object) -> None:
        self.deferred = self.deferred or time.monotonic()
        self.sent.append(Sent(text, options))

    async def defer(self, **options: object) -> None:
        assert self.deferred is None
        self.deferred = time.monotonic()
        if self.rejected:
            raise disnake.NotFound(
                cast(Any, RejectedResponse()), {"code": 10062, "message": "Unknown interaction"}
            )

    async def send(self, text: str, **options: object) -> None:
        assert self.deferred is not None
        self.sent.append(Sent(text, options))

    def texts(self) -> list[str | None]:
        return [s.text for s in self.sent]


def inter(
    channel: FakeChannel | None, *, manager: bool = False, guild: bool = True
) -> FakeInteraction:
    return FakeInteraction(
        FakeGuild() if guild else None,
        FakeAuthor(FakeVoiceState(channel) if channel else None),
        FakePermissions(manager),
    )


async def run(
    handler: Callable[[Inter], Awaitable[None]], fake: FakeInteraction
) -> FakeInteraction:
    """Invoke a handler and check it acknowledged or deferred within three seconds."""
    started = time.monotonic()
    await handler(cast(Inter, fake))
    assert fake.deferred is not None and fake.deferred - started < 3
    return fake


def offline(test: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], None]:
    """Run an async test on a fresh event loop; pytest has no async plugin here."""

    def wrapper() -> None:
        asyncio.run(test())

    wrapper.__name__ = test.__name__
    return wrapper


class Producer:
    """Writes ``frames`` frames (or the readiness amount) plus ``extra`` bytes and
    optionally ends the stream."""

    def __init__(
        self, frames: int = READY_BYTES // FRAME_BYTES, *, end: bool = True, extra: int = 0
    ) -> None:
        self.frames = frames
        self.end = end
        self.extra = extra
        self.release = asyncio.Event()
        self.hold = False
        self.settled: list[PcmBank] = []

    async def produce(self, recording: Recording, bank: PcmBank) -> None:
        try:
            bank.write(b"\x01" * (FRAME_BYTES * self.frames + self.extra))
            if self.end:
                bank.end_pcm()
            await asyncio.Event().wait()  # producers stay alive until cancelled
        finally:
            if self.hold:
                await self.release.wait()
            self.settled.append(bank)


@dataclass(eq=False)
class Harness:
    adapter: Adapter
    pool: PcmBankPool
    producer: Producer
    room: FakeChannel = field(default_factory=lambda: FakeChannel("room"))

    async def join(self) -> StationSource:
        await run(self.adapter.join, inter(self.room, manager=True))
        return self.adapter.players[1].source

    async def play(self, request: str = "piano", channel: FakeChannel | None = None) -> list[str]:
        fake = await run(lambda i: self.adapter.play(i, request), inter(channel or self.room))
        return [t or "" for t in fake.texts()]


def harness(
    producer: Producer | None = None, recordings: tuple[Recording, ...] | None = None
) -> Harness:
    pool = PcmBankPool()
    producer = producer or Producer()
    records = recordings if recordings is not None else (recording("a"),)
    return Harness(Adapter(catalog(*records), producer, pool=pool), pool, producer)


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# Registration, intents and configuration.


def test_intents_are_exactly_guilds_and_voice_states() -> None:
    value = intents()
    assert value.guilds and value.voice_states
    assert value.value == type(value)(guilds=True, voice_states=True).value
    assert {name for name, enabled in value if enabled} == {"guilds", "voice_states"}


@offline
async def test_command_access_rules_and_server_only_registration() -> (
    None
):  # the bot binds the running loop
    bot = create_bot(Adapter(catalog(recording("a")), Producer(), pool=PcmBankPool()))
    bodies = {c.name: c.body for c in bot.slash_commands}
    assert set(bodies) == {"join", "leave", "play", "stop", "now"}
    for name, body in bodies.items():
        assert body.contexts is not None
        assert (
            body.contexts.guild and not body.contexts.bot_dm and not body.contexts.private_channel
        )
        manage = body.default_member_permissions
        if name in ("join", "leave"):
            assert manage is not None and manage.value == type(manage)(manage_guild=True).value
        else:
            assert manage is None
    (option,) = bodies["play"].options
    assert (option.name, option.required, option.min_length, option.max_length) == (
        "request",
        True,
        1,
        200,
    )


@offline
async def test_play_is_not_registered_without_catalog() -> None:  # the bot binds the running loop
    bot = create_bot(Adapter(None, Producer(), pool=PcmBankPool()))
    assert {c.name for c in bot.slash_commands} == {"join", "leave", "stop", "now"}


def test_token_is_removed_from_environment_and_never_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placeholder = "placeholder-not-a-token"
    monkeypatch.setenv(discord_adapter.TOKEN_VARIABLE, placeholder)
    monkeypatch.delenv(discord_adapter.CATALOG_VARIABLE, raising=False)
    monkeypatch.delenv(discord_adapter.REVIEWS_VARIABLE, raising=False)
    config = Config.from_environ()
    assert config.token == placeholder and config.catalog is None
    assert discord_adapter.TOKEN_VARIABLE not in os.environ
    assert placeholder not in repr(config)

    monkeypatch.setenv(discord_adapter.TOKEN_VARIABLE, placeholder)
    monkeypatch.setenv(discord_adapter.CATALOG_VARIABLE, "/nonexistent/catalog.json")
    monkeypatch.setenv(discord_adapter.REVIEWS_VARIABLE, "/nonexistent/reviews.json")
    with pytest.raises(ConfigError) as error:
        Config.from_environ()
    assert placeholder not in str(error.value) and "nonexistent" not in str(error.value)

    with pytest.raises(ConfigError):
        Config.from_environ()  # the token was consumed by the previous attempt


# Commands.


@offline
async def test_every_command_refuses_direct_messages() -> None:
    h = harness()
    handlers: list[Callable[[Inter], Awaitable[None]]] = [
        h.adapter.join,
        h.adapter.leave,
        h.adapter.stop,
        h.adapter.now,
        lambda i: h.adapter.play(i, "piano"),
    ]
    for handler in handlers:
        fake = await run(handler, inter(h.room, manager=True, guild=False))
        assert fake.texts() == [SERVER_ONLY]
        assert fake.sent[0].options["ephemeral"] is True
    assert h.adapter.players == {}


@offline
async def test_playback_commands_explain_that_a_manager_must_join() -> None:
    h = harness()
    handlers: list[Callable[[Inter], Awaitable[None]]] = [
        lambda i: h.adapter.play(i, "piano"),
        h.adapter.stop,
    ]
    for handler in handlers:
        fake = await run(handler, inter(h.room, manager=True))
        assert fake.texts() == [NOT_JOINED]
    assert h.pool.leased == 0


@offline
async def test_join_requires_the_caller_in_voice_and_moves() -> None:
    h = harness()
    fake = await run(h.adapter.join, inter(None, manager=True))
    assert "Join a voice channel first" in (fake.texts()[0] or "")
    await h.join()
    voice = h.room.voice
    assert voice is not None and h.adapter.players[1].voice is cast(Any, voice)
    other = FakeChannel("other")
    await run(h.adapter.join, inter(other, manager=True))
    assert voice.channel is other and other.voice is None


@offline
async def test_play_and_stop_require_the_bots_voice_channel_but_managers_may_stop() -> None:
    h = harness()
    await h.join()
    elsewhere = FakeChannel("elsewhere")
    assert await h.play(channel=elsewhere) == [NOT_LISTENING]
    fake = await run(lambda i: h.adapter.play(i, "piano"), inter(None))
    assert fake.texts() == [NOT_LISTENING]
    assert h.pool.leased == 0

    assert (await run(h.adapter.stop, inter(elsewhere))).texts() == [NOT_LISTENING]
    stopped = await run(h.adapter.stop, inter(elsewhere, manager=True))
    assert "Stopped" in (stopped.texts()[0] or "")
    listener = await run(h.adapter.stop, inter(h.room))
    assert "Stopped" in (listener.texts()[0] or "")


@offline
async def test_play_without_a_match_substitutes_nothing() -> None:
    h = harness()
    source = await h.join()
    (text,) = await h.play("heavy metal")
    assert "No eligible recording" in text and "nothing was substituted" in text
    (problem,) = await h.play("anything but jazz")
    assert "cannot be matched" in problem
    station = h.adapter.players[1].station
    assert station.desired is None and station.heard is None
    assert h.pool.leased == 0 and source.read() == b""


@offline
async def test_heard_source_and_credits_change_only_at_the_actual_switch() -> None:
    first, second = recording("a"), recording("b", MANY_NOTICES)
    h = harness(recordings=(first,))
    source = await h.join()
    (accepted,) = await h.play()
    assert "Request accepted" in accepted and "audible" in accepted
    assert "is audible" not in accepted and "now playing" not in accepted.lower()
    await settle()
    player = h.adapter.players[1]
    assert player.voice.is_playing() and player.station.heard is None

    now = await run(h.adapter.now, inter(None))
    assert now.texts() == ["Nothing is audible. A new request is starting and is not audible yet."]

    frame = source.read()
    assert len(frame) == FRAME_BYTES and frame != SILENCE
    assert player.station.heard is None  # the report settles on the event loop
    await settle()
    heard = player.station.heard
    assert heard is not None and heard.recording == first

    # A replacement prepares while the heard source and its credits stay unchanged.
    h.adapter.catalog = catalog(second)
    h.producer.frames = 3
    h.producer.end = False  # not ready: fewer than six seconds and no verified end
    await h.play()
    await settle()
    assert source.read() != SILENCE  # still the first recording
    await settle()
    assert player.station.heard is heard
    pages = (await run(h.adapter.now, inter(None))).texts()
    assert (
        pages[0]
        == "Now heard (full credits follow). A new request is starting and is not audible yet."
    )
    assert "Song a" in (pages[1] or "") and all("Song b" not in (p or "") for p in pages)


@offline
async def test_now_privately_shows_complete_credits_to_ordinary_listeners() -> None:
    long = recording("a", MANY_NOTICES)
    h = harness(recordings=(long,))
    source = await h.join()
    await h.play()
    await settle()
    source.read()
    await settle()
    heard = h.adapter.players[1].station.heard
    assert heard is not None and len(heard.credits.pages) > 1

    listener = await run(h.adapter.now, inter(None))  # not in voice, not a manager
    assert listener.texts()[0] == "Now heard (full credits follow)."
    pages = listener.sent[1:]
    assert [p.text for p in pages] == [heard.credits.page(n).text for n in range(1, len(pages) + 1)]
    assert len(pages) == len(heard.credits.pages)
    joined = "".join(p.text or "" for p in pages)
    assert all(notice.split(":")[0] in joined for notice in MANY_NOTICES)
    for sent in listener.sent:
        assert sent.options["ephemeral"] is True
        assert sent.options["allowed_mentions"] is discord_adapter.NO_MENTIONS
    assert all(p.options["suppress_embeds"] is True for p in pages)


# Voice callback.


@offline
async def test_audio_source_returns_promptly_when_empty() -> None:
    reports: list[tuple[str, Track]] = []
    source = StationSource(lambda kind, track: reports.append((kind, track)))
    started = time.perf_counter()
    assert source.read() == b""  # idle: nothing current or pending
    pool = PcmBankPool()
    bank = pool.acquire()
    task = asyncio.create_task(asyncio.sleep(0))
    track = Track(cast(Any, None), bank, task)
    assert source.offer(track) is True
    for _ in range(100):
        assert source.read() == SILENCE  # not ready: never waits for the producer
    assert time.perf_counter() - started < 0.5
    with source._lock:  # pyright: ignore[reportPrivateUsage]
        assert source.read() == SILENCE  # contended lock: no waiting
    bank.cancel()
    assert source.read() == b"" and reports == [("failed", track)]
    await task
    assert bank.release() and pool.leased == 0


@offline
async def test_audio_source_counts_handed_frames_and_underruns_without_waiting() -> None:
    source = StationSource(lambda kind, track: None)
    pool = PcmBankPool()
    bank = pool.acquire()
    task = asyncio.create_task(asyncio.sleep(0))
    track = Track(cast(Any, None), bank, task)
    source.offer(track)
    assert source.read() == SILENCE and track.underruns == 0  # pending, not ready
    bank.write(b"\x01" * READY_BYTES)
    with bank._lock:  # pyright: ignore[reportPrivateUsage]
        assert source.read() == SILENCE  # pending and ready, but the bank is busy
    assert track.underruns == 1 and track.frames == 0
    for _ in range(READY_BYTES // FRAME_BYTES):
        assert source.read() != SILENCE
    assert source.read() == SILENCE  # current and ready, but nothing buffered
    assert track.frames == READY_BYTES // FRAME_BYTES and track.underruns == 2
    assert source.take_all() == [track]
    bank.cancel()
    await task
    assert bank.release() and pool.leased == 0


@offline
async def test_producer_failure_before_switch_keeps_heard_audio() -> None:
    h = harness()
    source = await h.join()
    await h.play()
    await settle()
    source.read()
    await settle()
    player = h.adapter.players[1]
    heard = player.station.heard
    assert heard is not None

    class Failing:
        async def produce(self, recording: Recording, bank: PcmBank) -> None:
            raise OSError("synthetic decoder failure")

    h.adapter._producer = Failing()  # pyright: ignore[reportPrivateUsage]
    await h.play()
    await settle()
    assert source.read() != SILENCE  # pending failed; the current frame continues
    await settle()
    for task in list(h.adapter.tasks):
        await task
    assert player.station.heard is heard and player.station.desired == heard.program
    assert h.pool.leased == 1


@offline
async def test_natural_drain_ends_the_program_and_releases_the_bank() -> None:
    h = harness(Producer(frames=2))
    source = await h.join()
    await h.play()
    await settle()
    assert source.read() != SILENCE
    await settle()
    assert source.read() != SILENCE
    assert source.read() == b""
    await settle()
    for task in list(h.adapter.tasks):
        await task
    station = h.adapter.players[1].station
    assert station.heard is None and station.desired is None
    assert h.pool.leased == 0


@offline
async def test_a_track_ending_past_a_whole_frame_plays_every_whole_frame_before_drain() -> None:
    # Night Owl's real output ended 988 bytes (whole samples) past a whole frame.
    h = harness(Producer(frames=3, extra=988))
    reports: list[str] = []
    source = await h.join()
    report = source._report  # pyright: ignore[reportPrivateUsage]

    def spy(kind: str, track: Track) -> None:
        reports.append(kind)
        report(kind, track)

    source._report = spy  # pyright: ignore[reportPrivateUsage]
    await h.play()
    await settle()
    frames = [source.read() for _ in range(3)]
    assert frames == [b"\x01" * FRAME_BYTES] * 3
    assert reports == ["switched"]  # every whole frame played, no drain yet
    assert source.read() == b""
    assert reports == ["switched", "drained"]
    await settle()
    for task in list(h.adapter.tasks):
        await task
    station = h.adapter.players[1].station
    assert station.heard is None and station.desired is None
    assert h.pool.leased == 0


def adapter_log(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "sobafm.discord_adapter"]


def test_played_tracks_are_logged_without_request_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")
    request = "piano"

    async def scenario() -> None:
        h = harness(Producer(frames=2, extra=988))
        source = await h.join()
        await h.play(request)
        await settle()
        assert source.read() != SILENCE
        await settle()
        assert source.read() != SILENCE
        assert source.read() == b""
        await settle()
        for task in list(h.adapter.tasks):
            await task
        assert await h.adapter.shutdown() is True

    asyncio.run(scenario())
    lines = adapter_log(caplog)
    retired, seconds_text = lines[3].rsplit(" cancel_to_release_seconds=", 1)
    assert [*lines[:3], retired, *lines[4:]] == [
        "voice connected",
        "heard source changed: recording=jamendo:a credit_snapshot=credit-a",
        "heard source cleared",
        "track retired: recording=jamendo:a frames_handed_to_voice=2 underruns=0 "
        "transfer_complete=False decoder_complete=False pcm_valid=True audible_drain=True "
        "failure=none bank_released=True",
        "voice disconnected",
        "shutdown: released=True leases_held=0 cleanup_failures=0",
    ]
    seconds = float(seconds_text)
    assert 0 <= seconds < discord_adapter.CLEANUP_SECONDS
    assert all(request not in line for line in lines)
    assert request not in caplog.text


def test_underruns_and_retained_banks_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")
    ready = READY_BYTES // FRAME_BYTES

    async def scenario() -> None:
        h = harness(Producer(frames=ready, end=False))  # ready, but never ends
        source = await h.join()
        await h.play()
        await settle()
        for _ in range(ready):
            assert source.read() != SILENCE
        assert source.read() == SILENCE and source.read() == SILENCE  # two underruns
        await settle()
        player = h.adapter.players[1]
        current = player.source._current  # pyright: ignore[reportPrivateUsage]
        assert current is not None
        pending = current.bank.reserve_write(1)  # a borrowed region blocks release
        assert pending is not None
        assert await h.adapter.shutdown() is False
        current.bank.commit_write(0)
        assert await h.adapter.shutdown() is True

    asyncio.run(scenario())
    lines = adapter_log(caplog)
    (retired,) = [line for line in lines if line.startswith("track retired")]
    assert f"frames_handed_to_voice={ready} underruns=2 " in retired
    assert "pcm_valid=False audible_drain=False failure=none" in retired
    assert retired.endswith("bank_released=False cancel_to_release_seconds=none")
    shutdowns = [line for line in lines if line.startswith("shutdown")]
    assert shutdowns == [
        "shutdown: released=False leases_held=1 cleanup_failures=0",
        "shutdown: released=True leases_held=0 cleanup_failures=0",
    ]


# A late acknowledgement that Discord rejects (#99).


def rejected(channel: FakeChannel, *, age_seconds: float = 0.0) -> FakeInteraction:
    fake = inter(channel, manager=True)
    fake.rejected = True
    fake.created_at = disnake.utils.utcnow() - datetime.timedelta(seconds=age_seconds)
    return fake


async def heard_track(h: Harness) -> StationSource:
    source = await h.join()
    await h.play()
    await settle()
    assert source.read() != SILENCE
    await settle()
    assert h.adapter.players[1].station.heard is not None and h.pool.leased == 1
    return source


async def join_retirements(h: Harness) -> None:
    for task in list(h.adapter.tasks):
        await task


@offline
async def test_stop_silences_and_retires_when_its_acknowledgement_is_rejected() -> None:
    h = harness()
    source = await heard_track(h)
    fake = await run(h.adapter.stop, rejected(h.room))  # does not raise
    assert fake.sent == []  # no follow-up to a gone interaction
    station = h.adapter.players[1].station
    assert station.heard is None and station.desired is None
    assert source.read() == b""  # the callback emits nothing of the stopped track
    await join_retirements(h)
    assert h.pool.leased == 0 and len(h.producer.settled) == 1
    assert h.room.voice is not None and h.room.voice.connected  # stop stays connected


@offline
async def test_leave_silences_and_disconnects_when_its_acknowledgement_is_rejected() -> None:
    h = harness()
    source = await heard_track(h)
    fake = await run(h.adapter.leave, rejected(h.room))  # does not raise
    assert fake.sent == []
    assert h.adapter.players == {}
    assert h.room.voice is not None and h.room.voice.disconnects == 1
    assert source.read() == b""
    await join_retirements(h)
    assert h.pool.leased == 0 and len(h.producer.settled) == 1


def test_a_rejected_acknowledgement_logs_command_and_age_without_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")
    fakes: list[FakeInteraction] = []

    async def scenario() -> None:
        h = harness()
        await heard_track(h)
        fakes.append(await run(h.adapter.stop, rejected(h.room, age_seconds=7.5)))
        fakes.append(await run(h.adapter.leave, rejected(h.room, age_seconds=2.25)))
        assert await h.adapter.shutdown() is True

    asyncio.run(scenario())
    lines = [
        line
        for line in adapter_log(caplog)
        if line.startswith("interaction acknowledgement rejected")
    ]
    assert len(lines) == 2
    for line, command, age in zip(lines, ("stop", "leave"), (7.5, 2.25), strict=True):
        prefix = f"interaction acknowledgement rejected: command={command} age_seconds="
        assert line.startswith(prefix)
        seconds, rest = line.removeprefix(prefix).split(" ", 1)
        assert age <= float(seconds) < age + 1
        assert rest == "status=404 code=10062"
    for fake in fakes:
        assert fake.token not in caplog.text


# Ownership and shutdown.


@offline
async def test_shutdown_disconnects_and_retains_owners_until_actual_release() -> None:
    h = harness()
    h.producer.hold = True
    source = await h.join()
    await h.play()
    await settle()
    source.read()
    await settle()
    voice = h.room.voice
    assert voice is not None and h.pool.leased == 1

    shutdown = asyncio.create_task(h.adapter.shutdown())
    await asyncio.sleep(0.01)
    assert voice.disconnects == 1 and h.adapter.players == {}
    assert not shutdown.done()  # the producer has not settled: its bank stays leased
    assert h.pool.leased == 1 and h.producer.settled == []

    h.producer.release.set()
    assert await shutdown is True
    assert h.pool.leased == 0 and len(h.producer.settled) == 1


@offline
async def test_shutdown_reports_a_bank_that_cannot_be_released() -> None:
    h = harness(Producer(frames=1, end=False))
    source = await h.join()
    await h.play()
    await settle()
    pending = h.adapter.players[1].source._pending  # pyright: ignore[reportPrivateUsage]
    assert pending is not None
    view = pending.bank.reserve_write(1)  # a borrowed region blocks actual release
    assert view is not None
    assert await h.adapter.shutdown() is False
    assert h.pool.leased == 1 and h.adapter.debt == [pending.bank]
    pending.bank.commit_write(0)
    assert await h.adapter.shutdown() is True and h.pool.leased == 0
    assert source.read() == b""


def test_shutdown_yields_while_a_finished_tasks_callback_is_queued() -> None:
    """A retirement done in the same loop iteration as shutdown must not spin it."""
    outcome: list[bool] = []

    async def scenario() -> None:
        adapter = Adapter(None, Producer(), pool=PcmBankPool())
        task = asyncio.create_task(asyncio.sleep(0))
        await task  # done; awaiting it did not run any callback
        adapter._own(task)  # pyright: ignore[reportPrivateUsage]
        assert task in adapter.tasks  # its discard callback is only queued
        outcome.append(await adapter.shutdown())

    # A spinning loop cannot honour its own timeout, so run it on a separate thread.
    thread = threading.Thread(target=asyncio.run, args=(scenario(),), daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "shutdown spun with the event loop blocked"
    assert outcome == [True]


@offline
async def test_sigterm_sets_the_stop_event() -> None:
    adapter = Adapter(None, Producer(), pool=PcmBankPool())
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, adapter.stopping.set)
    try:
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(adapter.stopping.wait(), timeout=1)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
    assert await adapter.shutdown() is True


@offline
async def test_commands_during_shutdown_are_refused_and_lease_nothing() -> None:
    h = harness()
    h.producer.hold = True
    await h.join()
    await h.play()
    await settle()
    shutdown = asyncio.create_task(h.adapter.shutdown())
    await asyncio.sleep(0.01)
    assert not shutdown.done() and h.pool.leased == 1

    joined = await run(h.adapter.join, inter(h.room, manager=True))
    assert joined.texts() == [discord_adapter.SHUTTING_DOWN]
    assert h.adapter.players == {}
    played = await run(lambda i: h.adapter.play(i, "piano"), inter(h.room))
    assert played.texts() in ([NOT_JOINED], [discord_adapter.SHUTTING_DOWN])

    h.producer.release.set()
    assert await shutdown is True
    assert h.pool.leased == 0 and h.adapter.players == {}


@offline
async def test_cleanup_deadline_counts_from_the_first_cancellation() -> None:
    # Backdate the bank clock so the original three-second deadline has nearly passed.
    pool = PcmBankPool(clock=lambda: time.monotonic() - discord_adapter.CLEANUP_SECONDS + 0.05)
    producer = Producer()
    producer.hold = True
    h = Harness(Adapter(catalog(recording("a")), producer, pool=pool), pool, producer)
    await h.join()
    await h.play()
    await settle()
    shutdown = asyncio.create_task(h.adapter.shutdown())
    await asyncio.sleep(0.2)  # past the deadline; the producer still holds its bank
    assert h.adapter.cleanup_failures == 1
    assert not shutdown.done() and pool.leased == 1 and producer.settled == []

    producer.release.set()
    assert await shutdown is False  # released, but the missed deadline stays a failed verdict
    assert pool.leased == 0 and len(producer.settled) == 1


@offline
async def test_a_newer_request_displaces_the_unheard_pending_track() -> None:
    h = harness(Producer(frames=1, end=False))
    source = await h.join()
    await h.play()
    await settle()
    player = h.adapter.players[1]
    first = player.station.switching
    assert first is not None and h.pool.leased == 1

    await h.play()
    await settle()
    second = player.station.switching
    assert second is not None and second.program != first.program
    assert player.station.desired == second.program and player.station.heard is None
    assert h.pool.leased == 1 and len(h.producer.settled) == 1  # displaced one joined
    assert source.read() == SILENCE  # the newer track is not ready either


@offline
async def test_refused_bank_lease_replies_busy_and_keeps_heard_audio() -> None:
    h = harness()
    source = await h.join()
    await h.play()
    await settle()
    source.read()
    await settle()
    player = h.adapter.players[1]
    heard = player.station.heard
    assert heard is not None
    blocker = h.pool.acquire()  # the remaining global lease is taken elsewhere

    (busy,) = await h.play()
    assert "capacity is busy" in busy and "current audio continues" in busy
    assert player.station.heard is heard and player.station.desired == heard.program
    assert player.station.switching is None and h.pool.leased == 2
    assert blocker.release()


@offline
async def test_a_player_that_cannot_start_retires_the_claimed_track() -> None:
    h = harness()
    await h.join()
    voice = h.room.voice
    assert voice is not None

    def refuse(source: StationSource) -> None:
        raise RuntimeError("synthetic: not connected")

    voice.play = refuse
    (text,) = await h.play()
    assert "could not start" in text
    for task in list(h.adapter.tasks):
        await task
    station = h.adapter.players[1].station
    assert station.switching is None and station.desired is None
    assert h.pool.leased == 0
