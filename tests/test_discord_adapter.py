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

import aiohttp
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
from sobafm.catalog_prompt import Interpretation
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


BOT_ID = 1
LISTENER_ID = 100


@dataclass(frozen=True)
class FakeUser:
    id: int = BOT_ID


@dataclass(eq=False)
class FakeVoice:
    channel: FakeChannel
    user: FakeUser = field(default_factory=FakeUser)
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
    guild: FakeGuild = field(default_factory=lambda: FakeGuild())
    # Voice states in this channel by user ID; one uncached human listener by default.
    voice_states: dict[int, FakeVoiceState] = field(
        default_factory=lambda: {LISTENER_ID: FakeVoiceState(None)}
    )

    @property
    def mention(self) -> str:
        return f"<#{self.name}>"

    async def connect(self) -> FakeVoice:
        self.voice = FakeVoice(self)
        return self.voice


@dataclass
class FakeVoiceState:
    channel: FakeChannel | None
    deaf: bool = False
    self_deaf: bool = False


@dataclass
class FakeAuthor:
    voice: FakeVoiceState | None


@dataclass
class FakePermissions:
    manage_guild: bool = False


@dataclass(frozen=True)
class FakeMember:
    id: int
    bot: bool


@dataclass
class FakeGuild:
    id: int = 1
    members: dict[int, FakeMember] = field(default_factory=lambda: {})

    def get_member(self, user_id: int) -> FakeMember | None:
        return self.members.get(user_id)


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
    transport_failure: BaseException | None = None  # the acknowledgement request fails

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
        if self.transport_failure is not None:
            raise self.transport_failure
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


class FakeClock:
    """The grace timer's sleep: it returns only when the test calls ``expire``."""

    def __init__(self) -> None:
        self.sleeps: list[tuple[float, asyncio.Future[None]]] = []

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.sleeps.append((seconds, future))
        await future

    @property
    def pending(self) -> list[float]:
        return [seconds for seconds, future in self.sleeps if not future.done()]

    def expire(self) -> None:
        for _, future in self.sleeps:
            if not future.done():
                future.set_result(None)


@dataclass(eq=False)
class Harness:
    adapter: Adapter
    pool: PcmBankPool
    producer: Producer
    # Wired to the adapter by `harness()`; other constructions keep a listener present.
    clock: FakeClock = field(default_factory=FakeClock)
    room: FakeChannel = field(default_factory=lambda: FakeChannel("room"))

    def voice_update(
        self, user_id: int, state: FakeVoiceState | None, channel: FakeChannel | None = None
    ) -> None:
        """Apply a member's voice state in ``channel`` (the room by default) and dispatch it."""
        channel = channel or self.room
        if state is None:
            channel.voice_states.pop(user_id, None)
        else:
            state.channel = channel
            channel.voice_states[user_id] = state
        target = None if state is None else channel
        self.adapter.voice_state_changed(1, user_id, cast(Any, target))

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
    clock = FakeClock()
    adapter = Adapter(catalog(*records), producer, pool=pool, sleep=clock.sleep)
    return Harness(adapter, pool, producer, clock)


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
    assert set(bodies) == {"join", "leave", "play", "skip", "stop", "now"}
    assert len(bot.extra_events["on_voice_state_update"]) == 1  # listener grace
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
    assert listener.texts()[0] == (
        "Now heard (full credits follow). This is the program's last recording: every "
        "eligible recording matching its request has been played. Use `/play` to start "
        "a new program."
    )  # the one-recording catalog has nothing to continue with
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
    retired, seconds_text = lines[5].rsplit(" cancel_to_release_seconds=", 1)
    assert [*lines[:5], retired, *lines[6:]] == [
        "voice connected",
        "heard source changed: recording=jamendo:a credit_snapshot=credit-a",
        "program has no further eligible recording",
        "heard source cleared",
        "program ended: reason=exhausted",
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


def test_stop_silences_and_retires_when_its_acknowledgement_request_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")
    fake = inter(FakeChannel("unused"), manager=True)
    fake.transport_failure = aiohttp.ClientConnectionError("synthetic transport failure")

    async def scenario() -> None:
        h = harness()
        source = await heard_track(h)
        await run(h.adapter.stop, fake)  # does not raise
        assert fake.sent == []
        station = h.adapter.players[1].station
        assert station.heard is None and station.desired is None
        assert source.read() == b""
        await join_retirements(h)
        assert h.pool.leased == 0 and len(h.producer.settled) == 1
        assert h.room.voice is not None and h.room.voice.connected

    asyncio.run(scenario())
    (line,) = [m for m in adapter_log(caplog) if m.startswith("interaction acknowledgement")]
    assert line.startswith("interaction acknowledgement failed: command=stop age_seconds=")
    assert line.endswith(" error=ClientConnectionError")
    assert fake.token not in caplog.text


def test_leave_silences_and_disconnects_when_its_acknowledgement_request_times_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")
    fake = inter(FakeChannel("unused"), manager=True)
    fake.transport_failure = TimeoutError()

    async def scenario() -> None:
        h = harness()
        source = await heard_track(h)
        await run(h.adapter.leave, fake)  # does not raise
        assert fake.sent == []
        assert h.adapter.players == {}
        assert h.room.voice is not None and h.room.voice.disconnects == 1
        assert source.read() == b""
        await join_retirements(h)
        assert h.pool.leased == 0 and len(h.producer.settled) == 1

    asyncio.run(scenario())
    (line,) = [m for m in adapter_log(caplog) if m.startswith("interaction acknowledgement")]
    assert line.startswith("interaction acknowledgement failed: command=leave age_seconds=")
    assert line.endswith(" error=TimeoutError")
    assert fake.token not in caplog.text


def test_a_cancelled_acknowledgement_propagates_from_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sobafm")
    fake = inter(FakeChannel("unused"), manager=True)
    fake.transport_failure = asyncio.CancelledError()

    async def scenario() -> None:
        h = harness()
        await heard_track(h)
        with pytest.raises(asyncio.CancelledError):
            await h.adapter.stop(cast(Inter, fake))
        assert fake.sent == []
        assert await h.adapter.shutdown() is True  # the track stays owned until shutdown

    asyncio.run(scenario())
    assert not [m for m in adapter_log(caplog) if m.startswith("interaction acknowledgement")]


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
        if line.startswith("interaction acknowledgement failed")
    ]
    assert len(lines) == 2
    for line, command, age in zip(lines, ("stop", "leave"), (7.5, 2.25), strict=True):
        prefix = f"interaction acknowledgement failed: command={command} age_seconds="
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


# Continuous catalog programs and /skip (#103), with a synthetic multi-recording catalog.


def tag(identifier: str) -> bytes:
    """One frame filled with the recording identifier's byte, to tell tracks apart."""
    return bytes([ord(identifier[0])]) * FRAME_BYTES


class Tagged(Producer):
    """Writes each recording's frames tagged with its identifier, under per-recording plans."""

    def __init__(self, frames: int = 2, *, end: bool = True) -> None:
        super().__init__(frames, end=end)
        self.plans: dict[str, tuple[int, bool]] = {}
        self.failing: set[str] = set()
        self.holding: dict[str, asyncio.Event] = {}
        self.started: list[str] = []
        self.banks: dict[str, PcmBank] = {}
        self.finished: list[str] = []

    async def produce(self, recording: Recording, bank: PcmBank) -> None:
        identifier = recording.key.recording_id
        self.started.append(identifier)
        self.banks[identifier] = bank
        if identifier in self.failing:
            raise OSError("synthetic decoder failure")
        frames, end = self.plans.get(identifier, (self.frames, self.end))
        try:
            bank.write(tag(identifier) * frames)
            if end:
                bank.end_pcm()
            await asyncio.Event().wait()  # producers stay alive until cancelled
        finally:
            if (gate := self.holding.get(identifier)) is not None:
                # Bounded, so a failed assertion cannot hang the loop's final cleanup.
                await asyncio.wait_for(gate.wait(), timeout=5)
            self.finished.append(identifier)


def program_harness(
    *identifiers: str, producer: Tagged | None = None, pool: PcmBankPool | None = None
) -> tuple[Harness, Tagged]:
    tagged = producer or Tagged()
    pool = pool or PcmBankPool()
    records = tuple(recording(i) for i in identifiers)
    clock = FakeClock()
    adapter = Adapter(catalog(*records), tagged, pool=pool, sleep=clock.sleep)
    return Harness(adapter, pool, tagged, clock), tagged


async def until(condition: Callable[[], bool]) -> None:
    """Run the loop until ``condition`` holds, without sleeping for a race."""
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def heard_id(h: Harness) -> str | None:
    heard = h.adapter.players[1].station.heard
    return None if heard is None else heard.recording.key.recording_id


def next_id(h: Harness) -> str | None:
    switching = h.adapter.players[1].station.switching
    return None if switching is None else switching.recording.key.recording_id


async def prepared_next(h: Harness, identifier: str) -> None:
    """Wait until ``identifier`` is the claimed next track and its producer has written."""
    producer = cast(Tagged, h.producer)
    await until(lambda: next_id(h) == identifier and identifier in producer.banks)


async def now_texts(h: Harness) -> list[str]:
    return [t or "" for t in (await run(h.adapter.now, inter(None))).texts()]


async def skip(h: Harness, channel: FakeChannel | None = None) -> str:
    (text,) = (await run(h.adapter.skip, inter(channel or h.room))).texts()
    return text or ""


async def start_program(h: Harness) -> StationSource:
    """Join, play and hear the first recording; its successor is then prepared."""
    source = await h.join()
    await h.play()
    await settle()
    assert source.read() == tag("a")
    await until(lambda: heard_id(h) == "a")
    return source


@offline
async def test_a_program_continues_successively_with_heard_credits_at_each_boundary() -> None:
    h, tagged = program_harness("a", "b", "c")
    interpretations: list[str] = []
    original = discord_adapter.normalize_prompt

    def counted(request: str) -> Interpretation:
        interpretations.append(request)
        return original(request)

    discord_adapter.normalize_prompt = counted
    try:
        source = await start_program(h)
        await prepared_next(h, "b")  # one prepared next track, in the second bank
        assert h.pool.leased == 2 and tagged.started == ["a", "b"]

        assert source.read() == tag("a")  # the heard track plays on; no overlap or cut
        await settle()
        pages = await now_texts(h)
        assert pages[0] == "Now heard (full credits follow)."
        assert "Song a" in pages[1] and all("Song b" not in p for p in pages)

        # The last whole frame of a drains; b's first frame follows in the same callback.
        assert source.read() == tag("b")
        await until(lambda: heard_id(h) == "b")
        pages = await now_texts(h)
        assert "Song b" in pages[1] and all("Song a" not in p for p in pages)
        await prepared_next(h, "c")  # after a's retirement released its bank
        assert h.pool.leased == 2 and tagged.finished == ["a"]

        assert [source.read(), source.read(), source.read()] == [tag("b"), tag("c"), tag("c")]
        await until(lambda: heard_id(h) == "c")
        assert interpretations == ["piano"]  # the request was interpreted exactly once
        station = h.adapter.players[1].station
        assert station.desired is not None and station.desired.request == "piano"
        assert station.history(station.desired) == {RecordingKey(Source.JAMENDO, i) for i in "abc"}
    finally:
        discord_adapter.normalize_prompt = original
    assert await h.adapter.shutdown() is True


@offline
async def test_exhaustion_ends_the_program_without_repeating_or_looping() -> None:
    h, tagged = program_harness("a", "b", producer=Tagged(frames=1))
    source = await start_program(h)
    await prepared_next(h, "b")
    assert source.read() == tag("b")
    await until(lambda: heard_id(h) == "b")
    await join_retirements(h)
    station = h.adapter.players[1].station
    assert station.ending is discord_adapter.EndReason.EXHAUSTED and station.switching is None
    assert h.pool.leased == 1  # nothing further was leased
    (status, *_) = await now_texts(h)
    assert "This is the program's last recording" in status and "`/play`" in status

    assert source.read() == b""  # b drained; nothing follows and the player goes idle
    await settle()
    await join_retirements(h)
    for _ in range(20):
        await asyncio.sleep(0)
    assert tagged.started == ["a", "b"] and h.adapter.tasks == set()  # no repeat, no loop
    assert station.desired is None and station.heard is None and h.pool.leased == 0
    assert station.ended is discord_adapter.EndReason.EXHAUSTED
    assert await now_texts(h) == [
        "Nothing is audible. The last program ended: every eligible recording matching its "
        "request has been played. Use `/play` to start a new one."
    ]
    assert source.read() == b""

    # A new /play starts a new program with fresh history.
    await h.play()
    await settle()
    assert source.read() == tag("a")
    await until(lambda: heard_id(h) == "a")
    assert station.ended is None
    assert await h.adapter.shutdown() is True


@offline
async def test_a_one_recording_catalog_ends_after_that_recording() -> None:
    h, tagged = program_harness("a", producer=Tagged(frames=1))
    source = await start_program(h)
    await join_retirements(h)
    assert source.read() == b""
    await settle()
    await join_retirements(h)
    station = h.adapter.players[1].station
    assert station.desired is None and station.ended is discord_adapter.EndReason.EXHAUSTED
    assert tagged.started == ["a"] and h.pool.leased == 0
    assert await skip(h) == "Nothing from the current program is audible to skip."
    assert await h.adapter.shutdown() is True


@offline
async def test_skip_switches_to_the_prepared_next_track() -> None:
    h, tagged = program_harness("a", "b", "c", producer=Tagged(frames=50))
    source = await start_program(h)
    await prepared_next(h, "b")
    assert source.read() == tag("a")  # continuation alone would wait for a's end

    assert await skip(h) == discord_adapter.SKIPPING
    assert source.read() == tag("b")  # b was ready: the skip cuts at once
    await until(lambda: heard_id(h) == "b")
    await prepared_next(h, "c")
    assert tagged.finished == ["a"] and h.pool.leased == 2
    assert await h.adapter.shutdown() is True


@offline
async def test_skip_while_the_next_track_is_still_preparing() -> None:
    tagged = Tagged(frames=50)
    tagged.plans["b"] = (1, False)  # one frame and no verified end: not ready yet
    h, _ = program_harness("a", "b", producer=tagged)
    source = await start_program(h)
    await prepared_next(h, "b")

    assert await skip(h) == discord_adapter.SKIPPING
    for _ in range(3):
        assert source.read() == tag("a")  # the current audio continues meanwhile
    await settle()
    assert heard_id(h) == "a"

    tagged.banks["b"].end_pcm()  # b's preparation completes
    assert source.read() == tag("b")
    await until(lambda: heard_id(h) == "b")
    assert await h.adapter.shutdown() is True


@offline
async def test_skip_failure_keeps_the_current_audio() -> None:
    tagged = Tagged(frames=50)
    tagged.failing.add("b")
    h, _ = program_harness("a", "b", "c", producer=tagged)
    source = await start_program(h)
    await prepared_next(h, "b")
    await settle()  # b's producer failed and stopped its bank

    assert await skip(h) == discord_adapter.SKIPPING
    assert source.read() == tag("a")  # b failed; a keeps playing
    await settle()
    assert heard_id(h) == "a"
    pages = await now_texts(h)
    assert "Song a" in pages[1]
    await prepared_next(h, "c")  # the program continues after a, not cutting it
    assert source.read() == tag("a")
    assert tagged.started == ["a", "b", "c"]

    # With nothing left to follow, /skip says so and the current audio continues.
    h2, _ = program_harness("a", producer=Tagged(frames=50))
    source2 = await start_program(h2)
    await join_retirements(h2)
    text = await skip(h2)
    assert text.startswith("Nothing follows: every eligible recording") and "continues" in text
    assert source2.read() == tag("a")
    assert await h.adapter.shutdown() is True and await h2.adapter.shutdown() is True


@offline
async def test_skip_requires_the_bots_voice_channel() -> None:
    h, _ = program_harness("a", "b")
    assert await skip(h) == NOT_JOINED
    await start_program(h)
    assert await skip(h, FakeChannel("elsewhere")) == NOT_LISTENING
    (text,) = (await run(h.adapter.skip, inter(None))).texts()
    assert text == NOT_LISTENING
    assert await h.adapter.shutdown() is True


@pytest.mark.parametrize("ending", ["replacement", "stop", "leave", "disconnect", "shutdown"])
def test_ending_or_replacing_discards_the_prepared_next_track(ending: str) -> None:
    async def scenario() -> None:
        tagged = Tagged(frames=50)
        tagged.holding["b"] = asyncio.Event()  # b's producer settles only when allowed
        h, _ = program_harness("a", "b", producer=tagged)
        source = await start_program(h)
        await prepared_next(h, "b")
        player = h.adapter.players[1]
        pending = player.source._pending  # pyright: ignore[reportPrivateUsage]
        assert pending is not None and pending.audio.recording.key.recording_id == "b"
        bank = pending.bank

        match ending:
            case "replacement":
                h.adapter.catalog = catalog(recording("d"))
                replaced = asyncio.create_task(h.play())
            case "stop":
                replaced = asyncio.create_task(run(h.adapter.stop, inter(h.room)))
            case "leave" | "disconnect":
                if ending == "disconnect":
                    cast(FakeVoice, player.voice).connected = False  # lost; settled at /leave
                replaced = asyncio.create_task(run(h.adapter.leave, inter(h.room, manager=True)))
            case _:
                replaced = asyncio.create_task(h.adapter.shutdown())
        await until(lambda: bank.facts().cancelled_at is not None)
        first_cancel = bank.facts().cancelled_at
        assert pending.retirement is not None and h.pool.leased == 2  # still owned
        assert h.adapter._retire(pending) is pending.retirement  # pyright: ignore[reportPrivateUsage]
        bank.cancel()  # a repeated cancellation keeps the original cleanup clock
        assert bank.facts().cancelled_at == first_cancel

        tagged.holding["b"].set()
        await replaced
        await join_retirements(h)
        assert "b" in tagged.finished and bank.facts().cancelled_at == first_cancel
        assert h.adapter.cleanup_failures == 0 and h.adapter.debt == []
        if ending == "replacement":
            assert source.read() == tag("d")  # the new program's first track cuts in
            await until(lambda: heard_id(h) == "d")
            await join_retirements(h)
            assert h.pool.leased == 1
        else:
            assert source.read() == b"" and h.pool.leased == 0
        assert await h.adapter.shutdown() is True and h.pool.leased == 0

    asyncio.run(scenario())


@offline
async def test_a_prepared_next_track_retained_past_its_deadline_stays_charged() -> None:
    # Backdate the bank clock so the original three-second deadline has nearly passed.
    pool = PcmBankPool(clock=lambda: time.monotonic() - discord_adapter.CLEANUP_SECONDS + 0.05)
    tagged = Tagged(frames=50)
    tagged.holding["b"] = asyncio.Event()
    h = Harness(Adapter(catalog(recording("a"), recording("b")), tagged, pool=pool), pool, tagged)
    await start_program(h)
    await prepared_next(h, "b")
    tagged.holding["a"] = asyncio.Event()
    tagged.holding["a"].set()
    shutdown = asyncio.create_task(h.adapter.shutdown())
    await asyncio.sleep(0.2)  # past the original deadline; b's producer still holds its bank
    assert h.adapter.cleanup_failures == 1 and not shutdown.done() and pool.leased == 1
    tagged.holding["b"].set()
    assert await shutdown is False  # released, but the missed deadline stays a failed verdict
    assert pool.leased == 0


@offline
async def test_stale_completions_after_a_replacement_change_nothing() -> None:
    h, tagged = program_harness("a", "b", producer=Tagged(frames=50))
    source = await start_program(h)
    await prepared_next(h, "b")
    player = h.adapter.players[1]
    old = player.station.desired
    stale = player.source._pending  # pyright: ignore[reportPrivateUsage]
    assert old is not None and stale is not None

    h.adapter.catalog = catalog(recording("d"))
    await h.play()
    await prepared_next(h, "d")
    await join_retirements(h)
    station = player.station
    new = station.desired
    assert new is not None and new != old
    heard = station.heard
    assert heard is not None and heard.recording.key.recording_id == "a"

    # Late reports for the displaced track, and the old program's continuation, are stale.
    for kind in ("switched", "failed", "drained"):
        h.adapter._settle(1, kind, stale)  # pyright: ignore[reportPrivateUsage]
    await h.adapter._continue(player, old)  # pyright: ignore[reportPrivateUsage]
    await join_retirements(h)
    assert station.desired == new and station.heard is heard and next_id(h) == "d"
    assert tagged.started == ["a", "b", "d"] and h.pool.leased == 2
    pages = await now_texts(h)
    assert "Song a" in pages[1] and all("Song b" not in p and "Song d" not in p for p in pages)

    assert source.read() == tag("d")
    await until(lambda: heard_id(h) == "d")
    assert await h.adapter.shutdown() is True


@offline
async def test_the_next_track_waits_for_capacity_held_by_a_retiring_track() -> None:
    tagged = Tagged(frames=1)
    tagged.holding["a"] = asyncio.Event()  # a's retirement keeps its bank until allowed
    h, _ = program_harness("a", "b", "c", producer=tagged)
    source = await start_program(h)
    await prepared_next(h, "b")

    assert source.read() == tag("b")  # a drains, b follows; a now retires
    await until(lambda: heard_id(h) == "b")
    for _ in range(50):
        await asyncio.sleep(0)
    # Both banks are charged: b audible and a retiring. c is neither leased nor attempted.
    assert h.pool.leased == 2 and tagged.started == ["a", "b"] and next_id(h) is None
    assert h.adapter.players[1].attempts == 0

    tagged.holding["a"].set()
    await prepared_next(h, "c")
    assert tagged.finished == ["a"] and h.pool.leased == 2
    assert source.read() == tag("c")
    await until(lambda: heard_id(h) == "c")
    assert await h.adapter.shutdown() is True


# Continuation bounds (#108): stuck retirements, /skip retries and busy capacity.


def flaky_voice(h: Harness, failures: int | None) -> FakeVoice:
    """Make the voice player report it stopped and refuse ``failures`` restarts (all if None)."""
    voice = h.room.voice
    assert voice is not None
    play = voice.play
    refused = [0]

    def refusing(source: StationSource) -> None:
        if failures is None or refused[0] < failures:
            refused[0] += 1
            raise RuntimeError("synthetic: voice player could not start")
        play(source)

    voice.play = refusing
    voice.is_playing = lambda: False
    return voice


def current(h: Harness, guild: int = 1) -> Track:
    track = h.adapter.players[guild].source._current  # pyright: ignore[reportPrivateUsage]
    assert track is not None
    return track


def test_a_stuck_retirement_does_not_stall_continuation_while_a_bank_is_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discord_adapter, "CLEANUP_SECONDS", 0.05)

    async def scenario() -> None:
        tagged = Tagged(frames=1)
        tagged.holding["a"] = asyncio.Event()  # a's producer does not settle until allowed
        h, _ = program_harness("a", "b", "c", producer=tagged)
        source = await start_program(h)
        a = current(h)
        await prepared_next(h, "b")
        assert source.read() == tag("b")  # a drains and retires; its producer stays stuck
        await until(lambda: heard_id(h) == "b")
        await asyncio.sleep(0.1)  # past a's original cleanup deadline
        retirement = a.retirement
        assert retirement is not None and not retirement.done()
        first_cancel = a.bank.facts().cancelled_at
        assert h.adapter.cleanup_failures == 1 and h.pool.leased == 2 and next_id(h) is None

        assert source.read() == b""  # b drains; its retirement frees a bank
        await prepared_next(h, "c")  # proceeds although a's retirement never settled
        assert source.read() == tag("c")
        await until(lambda: heard_id(h) == "c")
        # a's retirement keeps its owner, original clock, failed verdict and bank.
        assert retirement in h.adapter.retirements and not retirement.done()
        assert a.bank.facts().cancelled_at == first_cancel and h.adapter.debt == []
        assert h.adapter.cleanup_failures == 1 and h.pool.leased == 2

        tagged.holding["a"].set()
        assert await h.adapter.shutdown() is False  # the missed deadline stays failed
        assert h.pool.leased == 0

    asyncio.run(scenario())


async def both_banks_stuck(tagged: Tagged) -> tuple[Harness, StationSource]:
    """a and b drained, and both retirements hold their banks: nothing is free."""
    tagged.holding["a"] = asyncio.Event()
    tagged.holding["b"] = asyncio.Event()
    h, _ = program_harness("a", "b", "c", producer=tagged)
    source = await start_program(h)
    await prepared_next(h, "b")
    assert source.read() == tag("b")
    await until(lambda: heard_id(h) == "b")
    assert source.read() == b""
    await until(lambda: heard_id(h) is None)
    return h, source


def test_a_stuck_retirement_without_a_free_bank_ends_the_program_at_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discord_adapter, "CLEANUP_SECONDS", 0.05)
    monkeypatch.setattr(discord_adapter, "CAPACITY_WAIT_SECONDS", 0.2)

    async def scenario() -> None:
        tagged = Tagged(frames=1)
        h, _ = await both_banks_stuck(tagged)
        station = h.adapter.players[1].station
        assert await now_texts(h) == [
            "Nothing is audible. The program's next recording is preparing and is not audible yet."
        ]
        await asyncio.sleep(0.3)  # past the capacity bound
        await until(lambda: station.desired is None)
        assert station.ended is discord_adapter.EndReason.UNAVAILABLE
        assert await now_texts(h) == [
            "Nothing is audible. The last program ended: the next recording could not be "
            "prepared. Use `/play` to start a new one."
        ]
        assert tagged.started == ["a", "b"] and h.adapter.players[1].attempts == 0
        assert h.pool.leased == 2 and h.adapter.cleanup_failures == 2

        tagged.holding["a"].set()
        tagged.holding["b"].set()
        assert await h.adapter.shutdown() is False
        assert h.pool.leased == 0

    asyncio.run(scenario())


def test_shutdown_finishes_while_a_continuation_waits_on_a_stuck_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discord_adapter, "CLEANUP_SECONDS", 0.05)

    async def scenario() -> None:
        tagged = Tagged(frames=1)
        h, _ = await both_banks_stuck(tagged)
        continuation = h.adapter.players[1].continuation
        assert continuation is not None and not continuation.done()  # within its 10 s bound

        started = time.monotonic()
        shutdown = asyncio.create_task(h.adapter.shutdown())
        await until(continuation.done)  # it does not hold shutdown up
        await asyncio.sleep(0.1)
        assert not shutdown.done() and h.pool.leased == 2  # the retirements still own banks

        tagged.holding["a"].set()
        tagged.holding["b"].set()
        assert await asyncio.wait_for(shutdown, timeout=2) is False  # missed deadlines
        assert time.monotonic() - started < discord_adapter.CAPACITY_WAIT_SECONDS
        assert h.pool.leased == 0 and h.adapter.tasks == set()

    asyncio.run(scenario())


@offline
async def test_skip_survives_a_failed_start_of_its_next_track() -> None:
    h, tagged = program_harness("a", "b", "c", producer=Tagged(frames=50))
    blocker = h.pool.acquire()  # the next track waits for a bank
    source = await start_program(h)
    player = h.adapter.players[1]
    flaky_voice(h, failures=1)  # the first start of the next track fails

    skipping = asyncio.create_task(skip(h))
    await until(lambda: player.skip_requested)
    assert blocker.release()
    h.adapter._capacity_changed()  # pyright: ignore[reportPrivateUsage]
    assert await skipping == discord_adapter.SKIPPING
    assert tagged.started == ["a", "b", "c"] and player.attempts == 2
    assert source.read() == tag("c")  # still a cut: it does not wait for a's 50 frames
    await until(lambda: heard_id(h) == "c")
    assert not player.skip_requested
    assert await h.adapter.shutdown() is True


@offline
async def test_failed_next_track_attempts_are_bounded_and_end_the_program() -> None:
    h, tagged = program_harness("a", "b", "c", "d", "e", producer=Tagged(frames=50))
    source = await start_program(h)
    flaky_voice(h, failures=None)  # every next track fails to start
    station = h.adapter.players[1].station
    await until(lambda: station.ending is not None)
    assert station.ending is discord_adapter.EndReason.UNAVAILABLE
    assert h.adapter.players[1].attempts == discord_adapter.MAX_CONTINUATION_ATTEMPTS + 1
    assert tagged.started == ["a", "b", "c", "d"]  # no repeat
    assert source.read() == tag("a")  # the heard track continues to its end
    assert await h.adapter.shutdown() is True


@offline
async def test_a_program_continues_after_its_own_track_when_two_servers_hold_both_banks() -> None:
    h, tagged = program_harness("a", "b", producer=Tagged(frames=1))
    room2 = FakeChannel("room2")

    def second(manager: bool = False) -> FakeInteraction:
        fake = inter(room2, manager=manager)
        fake.guild = FakeGuild(2)
        return fake

    source1 = await h.join()
    await h.play()
    await run(h.adapter.join, second(manager=True))
    await run(lambda i: h.adapter.play(i, "piano"), second())
    source2 = h.adapter.players[2].source
    await settle()
    assert source1.read() == tag("a") and source2.read() == tag("a")
    one, two = h.adapter.players[1], h.adapter.players[2]
    await until(lambda: one.station.heard is not None and two.station.heard is not None)
    for _ in range(50):
        await asyncio.sleep(0)
    # Both servers play their current tracks; neither next track can lease.
    assert h.pool.leased == 2 and tagged.started == ["a", "a"]
    assert one.attempts == two.attempts == 0 and one.station.needs_next

    assert source1.read() == b""  # server 1's track ends and frees its bank
    await prepared_next(h, "b")  # server 1 gets it, although server 2 also waits
    assert two.station.switching is None and h.pool.leased == 2
    assert source1.read() == tag("b")  # after a gap
    await until(lambda: heard_id(h) == "b")
    assert two.station.heard is not None and two.station.needs_next
    assert await h.adapter.shutdown() is True


def test_capacity_that_never_frees_ends_the_program_without_a_hot_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discord_adapter, "CLEANUP_SECONDS", 0.05)
    monkeypatch.setattr(discord_adapter, "CAPACITY_WAIT_SECONDS", 0.2)
    checks = [0]

    class Counting(PcmBankPool):
        @property
        def leased(self) -> int:
            checks[0] += 1
            return super().leased

    async def scenario() -> None:
        tagged = Tagged(frames=1)
        tagged.holding["a"] = asyncio.Event()  # a's retirement keeps its bank
        pool = Counting()
        adapter = Adapter(catalog(recording("a"), recording("b")), tagged, pool=pool)
        h = Harness(adapter, pool, tagged)
        blocker = pool.acquire()  # held elsewhere throughout
        source = await start_program(h)
        station = h.adapter.players[1].station
        assert source.read() == b""  # a drains; no bank frees
        checks[0] = 0
        started = time.monotonic()
        while station.desired is not None and time.monotonic() - started < 2:
            await asyncio.sleep(0.01)  # the test's own clock, not the adapter's
        waited = time.monotonic() - started
        assert 0.2 <= waited < 2 and checks[0] < 20  # woken by events and the bound only
        assert station.ended is discord_adapter.EndReason.UNAVAILABLE
        assert tagged.started == ["a"] and h.adapter.players[1].attempts == 0
        (status,) = await now_texts(h)
        assert "the next recording could not be prepared" in status

        assert blocker.release()
        tagged.holding["a"].set()
        assert await h.adapter.shutdown() is False  # a missed its cleanup deadline
        assert pool.leased == 0

    asyncio.run(scenario())


# Listener grace.

GRACE_ENDED = (
    "Nothing is audible. The last program ended: no listeners remained. "
    "Use `/play` to start a new one."
)


def grace_program(h: Harness) -> str | None:
    grace = h.adapter.players[1].grace
    return None if grace is None else grace.program.request


async def grace_started(h: Harness) -> None:
    assert h.adapter.players[1].grace is not None
    await settle()
    assert h.clock.pending == [discord_adapter.LISTENER_GRACE_SECONDS]


async def grace_cancelled(h: Harness) -> None:
    assert h.adapter.players[1].grace is None
    await settle()
    assert h.clock.pending == []  # the timer task was cancelled, not left waiting


@offline
async def test_the_program_ends_when_the_grace_period_after_the_last_listener_expires() -> None:
    h, tagged = program_harness("a", "b", "c")
    source = await start_program(h)
    await prepared_next(h, "b")
    player = h.adapter.players[1]
    tracks = [t for t in (player.source._current, player.source._pending) if t]  # pyright: ignore[reportPrivateUsage]
    assert len(tracks) == 2 and h.pool.leased == 2

    h.voice_update(LISTENER_ID, None)  # the last listener leaves
    await grace_started(h)
    assert source.read() == tag("a")  # the program continues during the grace period
    h.clock.expire()
    await until(lambda: player.station.desired is None)

    station = player.station
    assert station.heard is None and station.ended is discord_adapter.EndReason.NO_LISTENERS
    assert source.read() == b""  # silent, through the same path as /stop
    assert player.voice.is_connected() and h.room.voice is not None
    assert h.room.voice.disconnects == 0 and h.adapter.players[1] is player
    # Both tracks retire through the owned path and release their banks.
    assert all(t.retirement is not None for t in tracks)
    await asyncio.gather(*(t.retirement for t in tracks if t.retirement))
    assert h.pool.leased == 0 and h.adapter.debt == [] and h.adapter.cleanup_failures == 0
    assert sorted(tagged.finished) == ["a", "b"]
    assert await now_texts(h) == [GRACE_ENDED]

    # A new program starts normally afterwards and clears the reason.
    h.voice_update(LISTENER_ID, FakeVoiceState(None))
    await h.play()
    assert station.ended is None and station.desired is not None
    assert player.grace is None
    assert await h.adapter.shutdown() is True


@offline
async def test_expiry_keeps_the_original_cleanup_clock_failed_verdicts_and_debt() -> None:
    # Backdate the bank clock so the original three-second deadline has nearly passed.
    pool = PcmBankPool(clock=lambda: time.monotonic() - discord_adapter.CLEANUP_SECONDS + 0.05)
    h, tagged = program_harness("a", pool=pool)
    tagged.holding["a"] = asyncio.Event()
    await start_program(h)
    h.voice_update(LISTENER_ID, None)
    await grace_started(h)
    h.clock.expire()
    await until(lambda: h.adapter.players[1].station.desired is None)
    await asyncio.sleep(0.2)  # past the original deadline; the producer still holds its bank
    assert h.adapter.cleanup_failures == 1 and pool.leased == 1 and tagged.finished == []
    tagged.holding["a"].set()
    assert await h.adapter.shutdown() is False  # released, but the failed verdict stays
    assert pool.leased == 0 and tagged.finished == ["a"]


@offline
async def test_a_listener_returning_before_expiry_cancels_the_grace_period() -> None:
    h, _ = program_harness("a", "b", "c")
    source = await start_program(h)
    player = h.adapter.players[1]
    program = player.station.desired
    h.voice_update(LISTENER_ID, None)
    await grace_started(h)
    h.voice_update(LISTENER_ID, FakeVoiceState(None))
    await grace_cancelled(h)
    h.clock.expire()  # nothing is waiting any more
    await settle()
    assert player.station.desired is program and source.read() == tag("a")
    assert player.station.ended is None
    assert await h.adapter.shutdown() is True


@offline
async def test_deafened_members_are_not_listeners() -> None:
    h, _ = program_harness("a", "b", "c")
    await start_program(h)
    player = h.adapter.players[1]
    h.voice_update(LISTENER_ID, FakeVoiceState(None, self_deaf=True))
    await grace_started(h)
    h.voice_update(LISTENER_ID, FakeVoiceState(None))  # undeafens
    await grace_cancelled(h)
    h.voice_update(LISTENER_ID, FakeVoiceState(None, deaf=True))  # deafened by the server
    await grace_started(h)
    h.voice_update(LISTENER_ID, FakeVoiceState(None, deaf=True, self_deaf=False))
    assert len(h.clock.sleeps) == 2  # an unrelated change does not restart the period
    h.clock.expire()
    await until(lambda: player.station.desired is None)
    assert player.station.ended is discord_adapter.EndReason.NO_LISTENERS
    assert await h.adapter.shutdown() is True


@offline
async def test_bots_do_not_count_but_uncached_members_do() -> None:
    h, _ = program_harness("a", "b", "c")
    await start_program(h)
    h.room.guild.members[200] = FakeMember(200, bot=True)
    h.room.guild.members[BOT_ID] = FakeMember(BOT_ID, bot=True)
    h.voice_update(200, FakeVoiceState(None))
    h.voice_update(BOT_ID, FakeVoiceState(None))
    await grace_cancelled(h)  # the uncached human is still there
    h.voice_update(LISTENER_ID, None)
    await grace_started(h)  # only bots, including SobaFM itself, remain
    # A member whose bot flag is unknown is not cut off.
    h.voice_update(300, FakeVoiceState(None))
    await grace_cancelled(h)
    # A cached human counts too.
    h.room.guild.members[301] = FakeMember(301, bot=False)
    h.voice_update(300, None)
    await grace_started(h)
    h.voice_update(301, FakeVoiceState(None))
    await grace_cancelled(h)
    assert await h.adapter.shutdown() is True


@offline
async def test_moves_to_another_channel_start_the_grace_period() -> None:
    h, _ = program_harness("a", "b", "c")
    await start_program(h)
    other = FakeChannel("other", voice_states={})
    h.room.voice_states.pop(LISTENER_ID)
    h.voice_update(LISTENER_ID, FakeVoiceState(None), channel=other)  # the listener moves away
    await grace_started(h)
    other.voice_states.pop(LISTENER_ID)
    h.voice_update(LISTENER_ID, FakeVoiceState(None))  # and back
    await grace_cancelled(h)

    # A manager moves the bot to a channel whose only member is deafened.
    other.voice_states[LISTENER_ID + 1] = FakeVoiceState(other, self_deaf=True)
    await run(h.adapter.join, inter(other, manager=True))
    assert h.adapter.players[1].voice.channel is cast(Any, other)
    await grace_started(h)
    await run(h.adapter.join, inter(h.room, manager=True))  # back to the listener
    await grace_cancelled(h)
    assert await h.adapter.shutdown() is True


@offline
async def test_stop_leave_voice_loss_and_shutdown_cancel_the_grace_period() -> None:
    h, _ = program_harness("a", "b", "c")
    await start_program(h)
    h.voice_update(LISTENER_ID, None)
    await grace_started(h)
    await run(h.adapter.stop, inter(None, manager=True))
    await grace_cancelled(h)
    assert h.adapter.players[1].station.ended is None  # stopped, not a grace expiry

    h.voice_update(LISTENER_ID, FakeVoiceState(None))
    await h.play()
    h.voice_update(LISTENER_ID, None)
    await grace_started(h)
    h.voice_update(BOT_ID, None)  # SobaFM's own voice connection is lost
    await grace_cancelled(h)

    h.voice_update(LISTENER_ID, FakeVoiceState(None))
    h.voice_update(LISTENER_ID, None)
    await grace_started(h)
    player = h.adapter.players[1]
    await run(h.adapter.leave, inter(None, manager=True))
    assert player.grace is None and 1 not in h.adapter.players
    await settle()
    assert h.clock.pending == []

    await h.join()
    await h.play()
    h.voice_update(LISTENER_ID, None)
    await grace_started(h)
    grace = h.adapter.players[1].grace
    assert grace is not None and grace.task in h.adapter.tasks  # owned by the adapter
    assert await h.adapter.shutdown() is True  # returns without waiting for the timer
    assert grace.task.cancelled() and h.adapter.tasks == set()


@offline
async def test_a_replacement_ends_the_old_grace_period() -> None:
    h, _ = program_harness("a", "b", "c")
    await start_program(h)
    h.voice_update(LISTENER_ID, None)
    await grace_started(h)
    assert grace_program(h) == "piano"
    first = h.adapter.players[1].grace
    assert first is not None

    # A deafened caller replaces the program: its own grace period starts afresh.
    await h.play("quiet piano")
    assert first.task.cancelled() or first.task.cancelling()
    assert grace_program(h) == "quiet piano"
    await grace_started(h)

    # A listener's replacement leaves no grace period.
    h.voice_update(LISTENER_ID, FakeVoiceState(None))
    await h.play("solo piano")
    await grace_cancelled(h)
    assert await h.adapter.shutdown() is True


@offline
async def test_a_stale_timer_never_ends_a_newer_program() -> None:
    h, _ = program_harness("a", "b", "c")
    await start_program(h)
    player = h.adapter.players[1]
    h.voice_update(LISTENER_ID, None)
    await grace_started(h)
    async with player.lock:
        h.clock.expire()  # the timer fires and waits for the lock
        await settle()
        # A newer program is accepted after the timer was armed, before it runs.
        newer = player.station.request("quiet piano").program
    await settle()
    assert player.station.desired is newer and player.station.ended is None
    assert player.grace is None  # the stale timer cleared only its own registration
    assert await h.adapter.shutdown() is True
