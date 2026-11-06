"""Discord adapter for the first playback slice: slash commands, voice and shutdown.

Commands work only in servers. `/join` and `/leave` require Manage Server by default
through Discord command permissions; `/play`, `/skip` and `/stop` require the caller to
share the bot's voice channel, except that a manager may stop from elsewhere. `/now` is
private and open to ordinary listeners.

`/play` asks an injected `Matcher` for a validated plan. A refusal or failure changes
nothing, one request is matched at a time per server, and a stop during that wait wins.
When the last recording on a program's list starts, the `Matcher` is asked in the
background for more, leaving out what the program has played (ADR 0011). Music, `/skip`
and `/stop` never wait on that call, and it takes no change cooldown.

Each server's `Station` settles desired, prepared and heard facts on the event loop.
Audio comes from an injected `Producer` that fills a leased `pcm.py` bank. The voice
callback (`StationSource.read`) only copies prepared frames: it never waits on the
network, a process, a database or cleanup, and hands every report back to the loop.
The heard source changes only when the callback emits the first frame of new audio.
A catalog program continues: once a track is heard, the next one is taken from the
program's current list and history and prepared in the second bank, and the
callback plays it successively after the heard track's last frame. Without a free
bank the next track waits, bounded and woken by capacity changes, through its heard
track and briefly after it. `/skip` and a replacement instead switch as soon as the
new audio is ready.
Superseded audio is cancelled, its producer joined and its bank released on the loop;
a bank that cannot yet be released stays leased as retained debt.

Safe INFO lines record what was played: heard-source changes by recording key and
credit snapshot ID, each track's settled retirement (frames handed to the voice
player, underruns and the bank's facts), voice connection changes and the shutdown
outcome. They never contain request text, tokens or interaction data.

A program ends `LISTENER_GRACE_SECONDS` after the last listener leaves the bot's voice
channel, through the same path as `/stop`; a listener who returns or undeafens first
cancels the grace period. Listeners are counted from the channel's voice states, which
the Voice States intent keeps complete, not from the member cache, which lacks members
already in voice when the bot connected; such a member counts as a listener unless
deafened, so an unknown member never cuts off a real listener.

`/stop` and `/leave` take effect even when their acknowledgement fails, whether Discord
rejects it (for example `10062 Unknown interaction`) or the request does not complete;
they then send no follow-up and log one WARNING line with the command name and the
interaction's age, never its token.

`/settings` shows or changes a server's persisted settings. Reading them never waits,
and a change is written off the event loop before anything takes effect. Volume is a
linear gain of at most 1.0 that disnake's `PCMVolumeTransformer` applies to each frame
after the callback's copy, so changing it reaches current audio at the next frame and
can't clip.

The change cooldown spans `/play` and `/skip` and is checked before interpretation or
selection. Each accepted request holds its own claim. It gives that claim back when it
fails or changes nothing, including when its track later fails, and a late report from
an older request can't free a newer request's claim.

`/join` remembers the voice channel and `/leave` forgets it; a failed save leaves the
connection and the saved channel as they were. When Discord first reports ready, each
remembered channel is rejoined once, with no program, or forgotten with a safe reason.
A move by an administrator is adopted and current audio carries on there. A real
disconnection or a deleted channel ends the program and forgets the channel;
`WatchedVoiceClient` separates that from a reconnect the library handles itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import signal
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

import disnake
from disnake.ext import commands

from sobafm.catalog import (
    MAX_CATALOG_BYTES,
    Catalog,
    CatalogInputError,
    Recording,
    RecordingKey,
    Source,
    eligible,
    parse_catalog,
    parse_reviews,
)
from sobafm.cooldown import Claim, Cooldown
from sobafm.credits import escaped_text
from sobafm.gemini_interactions import TIMEOUT_SECONDS
from sobafm.gemini_match import (
    NO_RECORDINGS,
    Fit,
    GeminiMatcher,
    Matched,
    MatchFailed,
    Ranked,
    Refused,
    checked_request,
    valid_api_key,
)
from sobafm.pcm import (
    BANK_COUNT,
    FRAME_BYTES,
    FRAME_MILLISECONDS,
    GLOBAL_PCM_BANKS,
    READY_SECONDS,
    PcmBank,
    PcmBankPool,
    PcmCapacityError,
)
from sobafm.settings import (
    MAX_COOLDOWN_SECONDS,
    MAX_GENERATED_MINUTES,
    MAX_VOLUME_PERCENT,
    MIN_COOLDOWN_SECONDS,
    MIN_GENERATED_MINUTES,
    MIN_VOLUME_PERCENT,
    ServerSettings,
    SettingsError,
    SettingsStore,
)
from sobafm.station import (
    MAX_REQUEST_CHARACTERS,
    Audio,
    EndReason,
    Outcome,
    Program,
    Station,
    select_next,
)

log = logging.getLogger(__name__)

TOKEN_VARIABLE = "SOBAFM_DISCORD_TOKEN"
GEMINI_KEY_VARIABLE = "SOBAFM_GEMINI_API_KEY"
CATALOG_VARIABLE = "SOBAFM_CATALOG"
REVIEWS_VARIABLE = "SOBAFM_REVIEWS"
MAX_REVIEWS_BYTES = 256_000
# Bound on how long a command reply waits for retirements before answering.
SETTLE_SECONDS = 5.0
# One original cleanup deadline, counted from the bank's first cancellation (ADR 0004).
CLEANUP_SECONDS = 3.0
# Bounds on continuing a program: next-track preparations per heard track (a failed
# lease, voice error or decoder failure), and credit-rejected candidates per preparation.
MAX_CONTINUATION_ATTEMPTS = 3
MAX_SELECTION_ATTEMPTS = 3
# How long a next track waits for a free bank once its program has nothing audible,
# past the three-second cleanup deadline of the program's own drained track. While the
# program's track is still audible, it waits for that track's end, which frees a bank.
CAPACITY_WAIT_SECONDS = 10.0
# Bounds on a refill (ADR 0011): the waits after its first and second failed calls. Three
# fast failures then span 75 seconds, so the last call can outlast a per-minute rate limit.
REFILL_WAIT_SECONDS = (15.0, 60.0)
MAX_REFILL_ATTEMPTS = len(REFILL_WAIT_SECONDS) + 1
# What a wait leaves before the heard track ends: the last call's timeout and the pre-roll
# of the next track. It fits even the shortest admitted recording, now 74 seconds.
REFILL_RESERVE_SECONDS = TIMEOUT_SECONDS + READY_SECONDS
# How long a program continues after the last listener leaves (docs/requirements.md).
LISTENER_GRACE_SECONDS = 60.0
# disnake's voice connect timeout. It also bounds how long disnake waits after a forced
# close before giving up, so how long a removed bot takes to notice (see WatchedVoiceClient).
VOICE_TIMEOUT_SECONDS = 20.0
# Bounds on rejoining remembered channels at startup: attempts per channel, seconds per
# attempt (disnake can retry within one connect) and rejoins running at once.
REJOIN_ATTEMPTS = 2
REJOIN_ATTEMPT_SECONDS = 45.0
MAX_CONCURRENT_REJOINS = 4
VOCAL_CHANNEL_TYPES = (disnake.ChannelType.voice, disnake.ChannelType.stage_voice)
SILENCE = bytes(FRAME_BYTES)
NO_MENTIONS = disnake.AllowedMentions.none()
Inter = disnake.ApplicationCommandInteraction[commands.InteractionBot]

NOT_JOINED = "SobaFM is not in a voice channel. A manager must use `/join` first."
NOT_LISTENING = "Join SobaFM's voice channel to use this command."
SERVER_ONLY = "SobaFM commands work in servers, not direct messages."
SHUTTING_DOWN = "SobaFM is shutting down."
MATCH_BUSY = "SobaFM is still matching an earlier request. Try again in a moment."
MATCH_UNAVAILABLE = (
    "SobaFM couldn't interpret that request just now; current audio continues. Try again shortly."
)
NOT_A_MUSIC_REQUEST = "That doesn't look like a music request, so current audio continues."
NOTHING_ADMITTED = "The catalog has no recordings ready to play."
STOPPED_WHILE_MATCHING = "Playback stopped while your request was being matched; nothing started."
FIT_WORDING = {
    Fit.EXACT: "This is the recording you asked for",
    Fit.CLOSE: "Closest match in the catalog",
    Fit.LOOSE: "Nothing in the catalog really fits, so this is the nearest thing",
}
SKIPPING = (
    "Skipping. The next recording becomes audible once enough of it is buffered; "
    "the current one continues until then."
)
COOLING = "This server's change cooldown is running, so nothing changed. Try again in {}."
SETTINGS_INVALID = (
    f"Volume must be {MIN_VOLUME_PERCENT} to {MAX_VOLUME_PERCENT}, cooldown "
    f"{MIN_COOLDOWN_SECONDS} to {MAX_COOLDOWN_SECONDS} and duration "
    f"{MIN_GENERATED_MINUTES} to {MAX_GENERATED_MINUTES}. Nothing was changed."
)
SETTINGS_UNAVAILABLE = "The settings could not be saved, so nothing was changed. Try again shortly."
VOICE_BUSY = "SobaFM is still changing its voice connection. Try again in a moment."
JOIN_NOT_SAVED = "SobaFM couldn't save that channel, so nothing changed. Try again shortly."
JOIN_FAILED = (
    "SobaFM couldn't connect to that channel, so it won't rejoin it after a restart. "
    "Check that it can connect and speak there, then try again."
)
LEAVE_NOT_SAVED = (
    "SobaFM couldn't save that it left, so it stayed connected and current audio continues. "
    "`/stop` still ends the program. Try again shortly."
)
NOT_CONNECTED = "SobaFM is not in a voice channel."
ENDED = {
    EndReason.EXHAUSTED: "every recording in the catalog has been played or tried",
    EndReason.REFILL_FAILED: "Gemini did not supply more recordings",
    EndReason.UNAVAILABLE: "the next recording could not be prepared",
    EndReason.NO_LISTENERS: "no listeners remained",
}


class ConfigError(Exception):
    """Configuration is missing or invalid; messages never contain values."""


class Producer(Protocol):
    """Fills one bank with a recording's PCM, away from the event loop and voice callback.

    It calls ``end_pcm`` after its completion policy is satisfied, and returns or
    raises only once every borrowed region is committed and its work has settled,
    including after cancellation.
    """

    async def produce(self, recording: Recording, bank: PcmBank) -> None: ...


class Matcher(Protocol):
    """Turns one request into a validated plan, or raises `MatchFailed`.

    ``played`` recordings are left out of the call. With none left to send it makes no
    call, and the failure's kind is `NO_RECORDINGS`.
    """

    async def match(
        self, request: str, played: frozenset[RecordingKey] = frozenset()
    ) -> Matched | Refused: ...

    async def close(self) -> None: ...


class Settings(Protocol):
    """Each server's persisted settings: reading never waits; a write waits off the loop."""

    def get(self, guild_id: int) -> ServerSettings: ...

    async def update(
        self,
        guild_id: int,
        *,
        generated_minutes: int | None = None,
        volume_percent: int | None = None,
        cooldown_seconds: int | None = None,
    ) -> ServerSettings: ...

    def channel(self, guild_id: int) -> int | None: ...

    def channels(self) -> dict[int, int]: ...

    async def remember_channel(self, guild_id: int, channel_id: int) -> None: ...

    async def forget_channel(self, guild_id: int) -> None: ...


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    matcher: GeminiMatcher | None

    @classmethod
    def from_environ(cls) -> Config:
        """Read configuration, removing credentials so child processes cannot inherit them."""
        token = os.environ.pop(TOKEN_VARIABLE, "").strip()
        api_key = os.environ.pop(GEMINI_KEY_VARIABLE, "").strip()
        if not token:
            raise ConfigError(f"{TOKEN_VARIABLE} is required")
        paths = [os.environ.get(name, "") for name in (CATALOG_VARIABLE, REVIEWS_VARIABLE)]
        if not any(paths):
            return cls(token, None)
        if not all(paths):
            raise ConfigError(f"{CATALOG_VARIABLE} and {REVIEWS_VARIABLE} must be set together")
        if not api_key:
            raise ConfigError(f"{GEMINI_KEY_VARIABLE} is required with {CATALOG_VARIABLE}")
        if not valid_api_key(api_key):
            raise ConfigError(f"{GEMINI_KEY_VARIABLE} contains unsupported characters")
        try:
            catalog = parse_catalog(_read(Path(paths[0]), MAX_CATALOG_BYTES))
            reviews = parse_reviews(_read(Path(paths[1]), MAX_REVIEWS_BYTES))
            admitted = eligible(catalog, reviews)
            matcher = GeminiMatcher(admitted, api_key)
        except (CatalogInputError, OSError, UnicodeError) as exc:
            raise ConfigError("catalog snapshot or reviews could not be loaded") from exc
        _log_coverage(catalog, admitted)
        return cls(token, matcher)


def _log_coverage(catalog: Catalog, admitted: tuple[Recording, ...]) -> None:
    """Report every source, so a missing or empty one is visible to the operator."""
    partitions = {p.source: p for p in catalog.partitions}
    counts = Counter(r.key.source for r in admitted)
    for source in Source:
        partition = partitions.get(source)
        log.info(
            "catalog source: source=%s state=%s provided=%d admitted=%d",
            source.value,
            "missing" if partition is None else partition.state.value,
            0 if partition is None else len(partition.recordings),
            counts[source],
        )


def _read(path: Path, maximum: int) -> str:
    with path.open("rb") as file:
        payload = file.read(maximum + 1)
    if len(payload) > maximum:
        raise CatalogInputError("input file exceeds byte limit")
    return payload.decode("utf-8")


def intents() -> disnake.Intents:
    """Exactly the Guilds and Voice States gateway intents."""
    return disnake.Intents(guilds=True, voice_states=True)


@dataclass(eq=False)
class Track:
    """Claimed audio with the bank and producer task it owns until actual release."""

    audio: Audio
    bank: PcmBank
    task: asyncio.Task[None]
    retirement: asyncio.Task[None] | None = None
    # Whether the callback switches to this track as soon as it is ready (a new program
    # or `/skip`) rather than after the current track's last frame (continuation).
    # Read and written under the callback's lock.
    cut: bool = True
    # Counted by the voice callback under its own non-blocking lock. ``frames`` are
    # frames handed to the voice player, not packets confirmed delivered; ``underruns``
    # include silence returned while the bank's lock was momentarily held.
    frames: int = 0
    underruns: int = 0
    # The cooldown claim of the request this track serves, given back if it fails to
    # start. Only the event loop reads or writes it; the callback ignores it.
    claim: Claim | None = None


class StationSource(disnake.AudioSource):
    """Voice callback source: plays the current track, switching when new audio is ready."""

    def __init__(self, report: Callable[[str, Track], None]) -> None:
        self._lock = threading.Lock()
        self._report = report
        self._current: Track | None = None
        self._pending: Track | None = None
        self._idle = True

    def read(self) -> bytes:
        if not self._lock.acquire(blocking=False):
            return SILENCE
        try:
            pending = self._pending
            if (
                pending is not None
                and (pending.cut or self._current is None)
                and (frame := self._switch(pending))
            ):
                return frame
            if (current := self._current) is not None:
                frame = current.bank.read_frame()
                if frame is None:
                    current.underruns += 1
                    return SILENCE
                if frame:
                    current.frames += 1
                    return frame
                self._current = None
                self._report("drained", current)
                # Successive playback: a prepared next track follows the last frame.
                if (pending := self._pending) is not None and (frame := self._switch(pending)):
                    return frame
            if (pending := self._pending) is not None:
                if pending.bank.ready_now():
                    pending.underruns += 1
                return SILENCE
            self._idle = True
            return b""
        finally:
            self._lock.release()

    def _switch(self, pending: Track) -> bytes | None:
        """Emit the pending track's first frame, making it current; call under the lock."""
        frame = pending.bank.read_frame()
        if frame:
            pending.frames += 1
            retired, self._current, self._pending = self._current, pending, None
            self._report("switched", pending)
            if retired is not None:
                self._report("retired", retired)
            return frame
        if frame == b"":
            self._pending = None
            self._report("failed", pending)
        return None

    def offer(self, track: Track) -> bool:
        """Make ``track`` pending; return whether the callback had gone idle."""
        with self._lock:
            assert self._pending is None
            self._pending = track
            idle, self._idle = self._idle, False
            return idle

    def current_seconds(self) -> float:
        """How much of the current track has been handed to the voice player."""
        with self._lock:
            frames = 0 if self._current is None else self._current.frames
        return frames * FRAME_MILLISECONDS / 1000

    def cut_pending(self, claim: Claim) -> bool:
        """Make a pending track switch as soon as it is ready, for the request holding
        ``claim``; report whether one exists."""
        with self._lock:
            if self._pending is None:
                return False
            self._pending.cut = True
            self._pending.claim = claim
            return True

    def take_pending(self) -> Track | None:
        """Remove the unheard pending track, if the callback has not yet switched to it."""
        with self._lock:
            pending, self._pending = self._pending, None
            return pending

    def take_all(self) -> list[Track]:
        """Remove every track; the callback emits none of their frames afterwards."""
        with self._lock:
            tracks = [t for t in (self._pending, self._current) if t is not None]
            self._pending = self._current = None
            return tracks

    def cleanup(self) -> None:
        """Ownership stays with the adapter, which releases banks on the event loop."""


@dataclass(eq=False)
class Grace:
    """A listener grace period armed for ``program``; its timer task is adapter-owned."""

    program: Program
    task: asyncio.Task[None]


@dataclass(eq=False)
class Refill:
    """A background call for more recordings for ``program``; its task is adapter-owned."""

    program: Program
    task: asyncio.Task[None]


@dataclass(eq=False)
class GuildPlayer:
    guild_id: int
    voice: disnake.VoiceClient
    source: StationSource
    # What the voice player reads: the source's frames at this server's volume.
    output: disnake.PCMVolumeTransformer[StationSource]
    station: Station = field(default_factory=Station)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # One request is matched at a time per server. A stop during the wait must win, so
    # the wait compares this count before starting anything.
    matching: bool = False
    silenced: int = 0
    continuation: asyncio.Task[None] | None = None
    # The program ``continuation`` serves; a newer desired program gets its own task.
    continuation_program: Program | None = None
    # Next-track preparations since the last heard switch, bounded per heard track.
    attempts: int = 0
    # A `/skip` waiting for its next track, which then switches as soon as it is ready.
    skip_requested: bool = False
    # At most one listener grace period per server.
    grace: Grace | None = None
    # At most one refill per server, for its desired program.
    refill: Refill | None = None
    cooldown: Cooldown = field(default_factory=Cooldown)
    # The claim of a request whose track has not been offered to the callback yet.
    waiting_claim: Claim | None = None
    # The adapter is ending this connection, so the library's report of it isn't a loss.
    closing: bool = False
    # `_disconnect` has run; a second call must not touch a newer player or voice client.
    disconnected: bool = False


class WatchedVoiceClient(disnake.VoiceClient):
    """Reports when a connection comes up and when it has ended for good.

    A member's voice state can't tell a disconnection from a reconnect. For a transient
    failure, `poll_voice_ws` calls `voice_disconnect` and then `connect` itself (lines
    506-512), so SobaFM's own state briefly has no channel while the library recovers.
    The library's own answer is `VoiceProtocol.cleanup` (166-179), which removes the
    client from its table of live connections. Only `disconnect` calls it, in a `finally`
    (522-541), and the reconnect path never does. So `cleanup` is the signal of a real
    disconnection, covering being kicked, a removed bot and a reconnect that gave up.

    Close codes 4017 and 4021 end `poll_voice_ws` with no `disconnect` at all (463-467),
    leaving a dead client still registered and reporting connected, so a polling loop
    that ends is treated as the same event.

    Lines are in disnake 2.12.2's `voice_client.py`:
    https://github.com/DisnakeDev/disnake/blob/v2.12.2/disnake/voice_client.py
    """

    def __init__(
        self,
        client: disnake.Client,
        channel: disnake.abc.Connectable,
        ready: Callable[[disnake.VoiceClient], None],
        gone: Callable[[disnake.VoiceClient], None],
    ) -> None:
        super().__init__(client, channel)
        self._report_ready = ready
        self._report_gone = gone
        self._ended = False

    async def connect_websocket(self, *, resume: bool = False) -> None:
        await super().connect_websocket(resume=resume)
        self._report_ready(self)

    async def poll_voice_ws(self, reconnect: bool) -> None:
        try:
            await super().poll_voice_ws(reconnect)
        except Exception:
            await self._end_if_left_open()
            raise
        await self._end_if_left_open()

    async def _end_if_left_open(self) -> None:
        if not self._ended:
            await self.disconnect(force=True)

    def cleanup(self) -> None:
        self._ended = True
        try:
            super().cleanup()
        finally:
            self._report_gone(self)


class Adapter:
    """Command handlers and per-server players; all state is owned by the event loop."""

    def __init__(
        self,
        matcher: Matcher | None,
        producer: Producer,
        settings: Settings,
        *,
        pool: PcmBankPool = GLOBAL_PCM_BANKS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.matcher = matcher
        self._producer = producer
        self._settings = settings
        self._pool = pool
        self._sleep = sleep  # the grace and refill timers' clock; tests inject one
        self._clock = clock  # the cooldown's clock, injected the same way
        self.players: dict[int, GuildPlayer] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.retirements: set[asyncio.Task[None]] = set()
        self.debt: list[PcmBank] = []
        self.cleanup_failures = 0
        self.stopping = asyncio.Event()
        # Continuations waiting for a bank; woken when a retirement or report may have
        # changed capacity, or at shutdown.
        self._capacity_waiters: set[asyncio.Future[None]] = set()
        # Servers whose voice connection and remembered channel are changing together. A
        # `/join`, `/leave` or rejoin holds this across its awaits, so two can't interleave
        # and leave the connection and the saved channel disagreeing.
        self._voice_busy: set[int] = set()
        self._rejoins_started = False
        self._rejoins: asyncio.Task[None] | None = None

    # Access.

    async def _refuse(self, inter: Inter, text: str) -> None:
        await inter.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @staticmethod
    async def _defer(inter: Inter, command: str) -> bool:
        """Defer privately; report whether the acknowledgement succeeded.

        Used where the command must take effect even if the acknowledgement fails,
        whether Discord rejects it or the request does not complete. Cancellation still
        propagates. A failed interaction gets no follow-up. The log line names the
        command, the interaction's age from its creation time and Discord's status and
        code when present, otherwise the exception type; never the token.
        """
        try:
            await inter.response.defer(ephemeral=True)
        except Exception as exc:
            age = (disnake.utils.utcnow() - inter.created_at).total_seconds()
            if isinstance(exc, disnake.HTTPException):
                detail = f"status={exc.status} code={exc.code}"
            else:
                detail = f"error={type(exc).__name__}"
            log.warning(
                "interaction acknowledgement failed: command=%s age_seconds=%.3f %s",
                command,
                age,
                detail,
            )
            return False
        return True

    def _player(self, inter: Inter) -> GuildPlayer | None:
        player = self.players.get(inter.guild_id or 0)
        return player if player is not None and player.voice.is_connected() else None

    @staticmethod
    def _voice_channel(
        inter: Inter,
    ) -> disnake.VoiceChannel | disnake.StageChannel | None:
        state = getattr(inter.author, "voice", None)  # a Member in servers
        return None if state is None else state.channel

    def _listening(self, inter: Inter, player: GuildPlayer) -> bool:
        channel = self._voice_channel(inter)
        return channel is not None and channel == player.voice.channel

    # Commands.

    async def join(self, inter: Inter) -> None:
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        if self.stopping.is_set():
            return await self._refuse(inter, SHUTTING_DOWN)
        channel = self._voice_channel(inter)
        if channel is None:
            return await self._refuse(inter, "Join a voice channel first, then use `/join`.")
        guild_id = inter.guild.id
        if guild_id in self._voice_busy:
            return await self._refuse(inter, VOICE_BUSY)
        self._voice_busy.add(guild_id)
        try:
            await inter.response.defer(ephemeral=True)
            reply = await self._join(guild_id, channel)
        finally:
            self._voice_busy.discard(guild_id)
        await inter.followup.send(reply, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def _join(
        self, guild_id: int, channel: disnake.VoiceChannel | disnake.StageChannel
    ) -> str:
        """Save the channel, then connect or move there, undoing the save if that fails.

        Saving first means a failed save changes nothing at all, with no voice action.
        """
        previous = self._settings.channel(guild_id)
        if previous != channel.id:
            try:
                await self._settings.remember_channel(guild_id, channel.id)
            except SettingsError as exc:
                log.warning("settings write failed: kind=%s", exc)
                return JOIN_NOT_SAVED
        player = self.players.get(guild_id)
        if player is not None and not player.voice.is_connected():
            await self._disconnect(guild_id, player)  # settle a lost connection's owners
            player = None
        try:
            if player is not None:
                await player.voice.move_to(channel)
                self._review_listeners(player)  # the new channel may have no listener
            elif await self._connect(guild_id, channel) is None:
                return SHUTTING_DOWN  # shutdown began while connecting
        except Exception as exc:
            log.warning("voice connection failed: %s", type(exc).__name__)
            if previous != channel.id:
                await self._restore_channel(guild_id, previous)
            return JOIN_FAILED
        return f"Joined {channel.mention}."

    async def _connect(
        self, guild_id: int, channel: disnake.VoiceChannel | disnake.StageChannel
    ) -> GuildPlayer | None:
        """Connect and register an idle player, or return None if shutdown began meanwhile."""
        voice = await channel.connect(
            timeout=VOICE_TIMEOUT_SECONDS, cls=self._voice_client_factory(guild_id)
        )
        if self.stopping.is_set():
            await voice.disconnect(force=True)
            return None
        log.info("voice connected")
        source = StationSource(self._reporter(guild_id))
        gain = self._settings.get(guild_id).volume_percent / 100
        output = disnake.PCMVolumeTransformer(source, gain)
        player = GuildPlayer(guild_id, voice, source, output)
        self.players[guild_id] = player
        return player

    def _voice_client_factory(
        self, guild_id: int
    ) -> Callable[[disnake.Client, disnake.abc.Connectable], WatchedVoiceClient]:
        def create(client: disnake.Client, channel: disnake.abc.Connectable) -> WatchedVoiceClient:
            return WatchedVoiceClient(
                client,
                channel,
                lambda voice: self._voice_ready(guild_id, voice),
                lambda voice: self._voice_gone(guild_id, voice),
            )

        return create

    async def _restore_channel(self, guild_id: int, channel_id: int | None) -> None:
        """Put back what was remembered before a connection attempt that failed."""
        try:
            if channel_id is None:
                await self._settings.forget_channel(guild_id)
            else:
                await self._settings.remember_channel(guild_id, channel_id)
        except SettingsError as exc:
            log.warning("settings write failed: kind=%s", exc)

    async def play(self, inter: Inter, request: str) -> None:
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        if self.matcher is None:
            return await self._refuse(inter, "Catalog playback is not configured.")
        if self.stopping.is_set():
            return await self._refuse(inter, SHUTTING_DOWN)
        if (player := self._player(inter)) is None:
            return await self._refuse(inter, NOT_JOINED)
        if not self._listening(inter, player):
            return await self._refuse(inter, NOT_LISTENING)
        await inter.response.defer(ephemeral=True)
        reply = await self._prepare(player, request)
        self._review_listeners(player)  # e.g. a deafened caller started a program
        await inter.followup.send(reply, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def _prepare(self, player: GuildPlayer, request: str) -> str:
        assert self.matcher is not None
        try:
            text = checked_request(request)
        except CatalogInputError:
            return f"Requests must be 1 to {MAX_REQUEST_CHARACTERS} printable characters."
        if player.matching:
            return MATCH_BUSY
        claim = self._claim(player)
        if isinstance(claim, str):
            return claim
        accepted = False
        player.matching = True
        silenced = player.silenced
        try:
            try:
                plan = await self.matcher.match(text)
            except MatchFailed as exc:
                return NOTHING_ADMITTED if exc.kind == NO_RECORDINGS else MATCH_UNAVAILABLE
            finally:
                player.matching = False
            if isinstance(plan, Refused):
                return NOT_A_MUSIC_REQUEST
            async with player.lock:
                if self.stopping.is_set():
                    return SHUTTING_DOWN
                if player.silenced != silenced:
                    return STOPPED_WHILE_MATCHING
                station = player.station
                # Later tracks come from this list, and from more of the same when it runs out.
                program = station.request(text, tuple(c.recording for c in plan.ranked)).program
                self._cancel_refill(player)  # the replaced program's; `_resume` covers a failure
                player.attempts = 0
                player.skip_requested = False
                player.waiting_claim = claim
                chosen = next(
                    (
                        choice
                        for choice in plan.ranked
                        if station.prepared(program, choice.recording) is Outcome.ACCEPTED
                    ),
                    None,
                )
                if chosen is None:
                    station.preparation_failed(program)
                    return (
                        "The matching recordings' credits are incomplete; nothing was substituted."
                    )
                if not await self._start(player):
                    return (
                        "Playback could not start (capacity is busy or voice is unavailable); "
                        "current audio continues. Try again shortly."
                    )
                accepted = True
            return _accepted(chosen)
        finally:
            if not accepted:
                self._give_back(player, claim)
                self._resume(player)  # a failed replacement hands the place back to the program

    def _claim(self, player: GuildPlayer) -> Claim | str:
        """Take the server's cooldown for one request, or return the reply refusing it."""
        seconds = self._settings.get(player.guild_id).cooldown_seconds
        now = self._clock()
        claim = player.cooldown.claim(now, seconds)
        if claim is not None:
            return claim
        wait = math.ceil(player.cooldown.remaining(now, seconds))
        return COOLING.format(f"{wait} second{'' if wait == 1 else 's'}")

    @staticmethod
    def _give_back(player: GuildPlayer, claim: Claim | None) -> None:
        """A failed or no-op request releases its own claim, and only that."""
        player.cooldown.release(claim)
        if player.waiting_claim is claim:
            player.waiting_claim = None

    async def _start(self, player: GuildPlayer, *, cut: bool = True) -> bool:
        """Claim ready audio, displacing an unheard pending track, and hand it to the player.

        ``cut`` audio switches as soon as it is ready; otherwise it follows the
        current track's last frame. The track takes over ``player.waiting_claim`` once
        it is offered; after a failure that claim stays for the caller to settle.
        """
        station = player.station
        if self.stopping.is_set():
            return False  # shutdown leases nothing new
        if station.switching is not None:
            displaced = player.source.take_pending()
            if displaced is not None:
                station.switch_failed(displaced.audio)
                # Join the displaced producer before its bank's capacity is reused.
                await asyncio.wait({self._retire(displaced)}, timeout=SETTLE_SECONDS)
        audio = station.claim_ready()
        if audio is None:
            return True  # a switch report is in flight; it claims ready audio when settled
        try:
            bank = self._pool.acquire()
        except PcmCapacityError:
            station.switch_failed(audio)
            return False
        task = asyncio.create_task(self._produce(audio.recording, bank))
        track = Track(audio, bank, task, cut=cut, claim=player.waiting_claim)
        idle = player.source.offer(track)
        if idle or not player.voice.is_playing():  # also restarts after a player error
            try:
                player.voice.stop()
                player.voice.play(player.output)
            except Exception as exc:  # e.g. the voice connection dropped meanwhile
                log.warning("voice player could not start: %s", type(exc).__name__)
                if player.source.take_pending() is track:
                    station.switch_failed(audio)
                    self._retire(track)
                return False
        player.waiting_claim = None
        return True

    async def _produce(self, recording: Recording, bank: PcmBank) -> None:
        try:
            await self._producer.produce(recording, bank)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("audio producer failed: %s", type(exc).__name__)
            bank.cancel()  # the voice callback then reports the failure

    def _resume(self, player: GuildPlayer) -> None:
        """Keep the desired program going if it has no next track in progress."""
        if not self.stopping.is_set() and self.matcher is not None and player.station.needs_next:
            self._schedule_continue(player)

    def _schedule_continue(self, player: GuildPlayer) -> asyncio.Task[None]:
        """Start (once at a time per program) preparing the desired program's next track.

        A task still running for a superseded program, for example while it waits for
        capacity, is not reused: the desired program gets its own task. The old task
        stays owned and exits through its stale check without touching the newer program.
        """
        program = player.station.desired
        assert program is not None
        task = player.continuation
        if task is None or task.done() or player.continuation_program != program:
            task = self._own(asyncio.create_task(self._continue(player, program)))
            player.continuation = task
            player.continuation_program = program
        return task

    async def _continue(self, player: GuildPlayer, program: Program) -> None:
        """Prepare ``program``'s next track in a free bank, within bounded attempts.

        A retiring track still holds its bank, and the pool counts it, so a next
        track leases only while a bank is free; it never waits on retirements as
        such. Without a free bank it waits, woken by capacity changes rather than
        polling: through its program's audible track, whose end frees that bank,
        then at most ``CAPACITY_WAIT_SECONDS``. Exhaustion of either bound ends the
        program after the heard track instead of repeating a recording. With nothing
        left on its list it starts a refill and returns at once, whatever the capacity,
        so a `/skip` waiting on this task never waits on the model.
        """
        deadline: float | None = None  # set once the program has nothing audible
        try:
            while not self.stopping.is_set():
                waiter: asyncio.Future[None] | None = None
                timeout = CAPACITY_WAIT_SECONDS
                async with player.lock:
                    station = player.station
                    if (
                        self.stopping.is_set()
                        or station.desired != program
                        or not station.needs_next
                    ):
                        return  # replaced, stopped or already continued: this work is stale
                    if select_next(station.plan(program), station.history(program)) is None:
                        self._start_refill(player, program)
                        return
                    if not self._bank_free(player):
                        if not self._audible(player):
                            now = time.monotonic()
                            deadline = now + timeout if deadline is None else deadline
                            if now >= deadline:
                                station.exhausted(program, EndReason.UNAVAILABLE)
                                log.warning("next track could not be prepared; the program ends")
                                return
                            timeout = deadline - now
                        waiter = self._capacity_waiter()
                    else:
                        player.attempts += 1
                        if player.attempts > MAX_CONTINUATION_ATTEMPTS:
                            station.exhausted(program, EndReason.UNAVAILABLE)
                            log.warning("next track could not be prepared; the program ends")
                            return
                        station.continue_program(program)
                        for _ in range(MAX_SELECTION_ATTEMPTS):
                            recording = select_next(station.plan(program), station.history(program))
                            if recording is None:  # credits rejected the rest of the list
                                station.preparation_failed(program)
                                self._start_refill(player, program)
                                return
                            if station.prepared(program, recording) is Outcome.ACCEPTED:
                                break
                        else:
                            station.preparation_failed(program)
                            continue
                        # A `/skip` stays requested until its track is handed to the player.
                        if await self._start(player, cut=player.skip_requested):
                            player.skip_requested = False
                            return
                    # Otherwise the lease or voice failed; the next attempt is bounded above.
                if waiter is not None:
                    await self._await_capacity(waiter, timeout)
        finally:
            # A claim still waiting belongs to a `/skip` of this program that got no track.
            # A newer `/play`'s claim is left alone: that program is then the desired one.
            if player.station.desired in (program, None):
                self._give_back(player, player.waiting_claim)
            self._capacity_changed()  # a bank this program yielded may now go elsewhere
            self._review_listeners(player)  # exhaustion can end the program

    def _start_refill(self, player: GuildPlayer, program: Program) -> None:
        """Ask for more recordings for ``program``, unless that is already under way."""
        refill = player.refill
        if refill is not None and refill.program == program and not refill.task.done():
            return
        self._cancel_refill(player)
        task = self._own(asyncio.create_task(self._refill(player, program)))
        player.refill = Refill(program, task)

    @staticmethod
    def _cancel_refill(player: GuildPlayer) -> None:
        refill, player.refill = player.refill, None
        if refill is not None:
            refill.task.cancel()

    async def _refill(self, player: GuildPlayer, program: Program) -> None:
        """Get more recordings for ``program``, whose list has run out (ADR 0011).

        This runs beside the music and takes the player lock only to settle a result,
        which is discarded if the program is no longer desired. A failed call, a refusal
        and a list with nothing new each use up one of ``MAX_REFILL_ATTEMPTS``, with a
        `refill_wait` between them, so none can loop. Once they are used up the program
        ends after its heard track, or at once if nothing is heard. A refill isn't a
        member action, so it takes no change cooldown.
        """
        assert self.matcher is not None
        station = player.station
        try:
            for attempt in range(1, MAX_REFILL_ATTEMPTS + 1):
                if self.stopping.is_set() or station.desired != program:
                    return
                recordings: tuple[Recording, ...] = ()
                nothing_left = False
                try:
                    plan = await self.matcher.match(program.request, station.history(program))
                except MatchFailed as exc:
                    nothing_left = exc.kind == NO_RECORDINGS
                else:
                    if isinstance(plan, Matched):
                        recordings = tuple(c.recording for c in plan.ranked)
                async with player.lock:
                    if self.stopping.is_set() or station.desired != program:
                        return  # replaced or stopped meanwhile: the result changes nothing
                    if nothing_left:
                        log.info("program has no recording left in the catalog")
                        self._end_after_heard(player, program, EndReason.EXHAUSTED)
                        return
                    if station.refilled(program, recordings) is Outcome.ACCEPTED:
                        log.info(
                            "program refilled: recordings=%d attempt=%d",
                            len(station.plan(program)),
                            attempt,
                        )
                        self._schedule_continue(player)
                        return
                    log.warning("program refill failed: attempt=%d", attempt)
                    if attempt == MAX_REFILL_ATTEMPTS:
                        self._end_after_heard(player, program, EndReason.REFILL_FAILED)
                        return
                await self._sleep(refill_wait(attempt, self._seconds_left(player, program)))
        finally:
            self._capacity_changed()  # a bank this program yielded may now go elsewhere
            self._review_listeners(player)  # the program may have ended

    @staticmethod
    def _seconds_left(player: GuildPlayer, program: Program) -> float:
        """About how long ``program``'s heard track has left, or 0 if nothing says.

        The catalog's duration is only a timing hint here: a wrong one moves a refill's
        next call, and nothing else depends on it.
        """
        heard = player.station.heard
        if heard is None or heard.program != program or heard.recording.duration_seconds is None:
            return 0.0
        return heard.recording.duration_seconds - player.source.current_seconds()

    @staticmethod
    def _end_after_heard(player: GuildPlayer, program: Program, reason: EndReason) -> None:
        player.station.exhausted(program, reason)
        if player.station.desired is None:  # nothing was heard, so no drain will report it
            log.info("program ended: reason=%s", reason.value)

    @staticmethod
    def _audible(player: GuildPlayer) -> bool:
        """Whether the desired program's own track is still heard on ``player``."""
        heard = player.station.heard
        return heard is not None and heard.program == player.station.desired

    def _bank_free(self, player: GuildPlayer) -> bool:
        """Whether ``player``'s next track may lease a bank now.

        A program that is still audible leaves a free bank to another server's
        program whose drained track left it silent, so a server whose track ended
        is not starved by one still playing.
        """
        if self._pool.leased >= BANK_COUNT:
            return False
        return not self._audible(player) or not any(
            other is not player and other.station.needs_next and not self._audible(other)
            for other in self.players.values()
        )

    def _capacity_waiter(self) -> asyncio.Future[None]:
        """Register for the next capacity change; created under the player lock, so
        a change between the check and the wait is not missed."""
        waiter = asyncio.get_running_loop().create_future()
        self._capacity_waiters.add(waiter)
        return waiter

    async def _await_capacity(self, waiter: asyncio.Future[None], timeout: float) -> None:
        try:
            await asyncio.wait({waiter}, timeout=timeout)
        finally:
            self._capacity_waiters.discard(waiter)
            waiter.cancel()  # no effect once it is done

    def _capacity_changed(self) -> None:
        """Wake every waiting continuation to check capacity and its program again."""
        waiters, self._capacity_waiters = self._capacity_waiters, set()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def skip(self, inter: Inter) -> None:
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        if self.matcher is None:
            return await self._refuse(inter, "Catalog playback is not configured.")
        if self.stopping.is_set():
            return await self._refuse(inter, SHUTTING_DOWN)
        if (player := self._player(inter)) is None:
            return await self._refuse(inter, NOT_JOINED)
        if not self._listening(inter, player):
            return await self._refuse(inter, NOT_LISTENING)
        await inter.response.defer(ephemeral=True)
        await inter.followup.send(
            await self._skip(player), ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    async def _skip(self, player: GuildPlayer) -> str:
        """Check the cooldown, then skip; only a skip under way keeps holding it."""
        claim = self._claim(player)
        if isinstance(claim, str):
            return claim
        reply = None
        try:
            reply = await self._skip_track(player, claim)
            return reply
        finally:
            if reply != SKIPPING:
                self._give_back(player, claim)

    async def _skip_track(self, player: GuildPlayer, claim: Claim) -> str:
        """Switch to the program's next track once it is ready; current audio continues."""
        async with player.lock:
            station = player.station
            if self.stopping.is_set():
                return SHUTTING_DOWN
            heard = station.heard
            if heard is None or station.desired != heard.program:
                return "Nothing from the current program is audible to skip."
            if player.source.cut_pending(claim):
                return SKIPPING  # the prepared or still-preparing next track
            if (reason := station.ending) is not None:
                return f"Nothing follows: {ENDED[reason]}. The current recording continues."
            if not station.needs_next:  # the next track's switch report is in flight
                return "The next recording is already starting."
            player.skip_requested = True
            player.waiting_claim = claim
            task = self._schedule_continue(player)
        # Bounded wait for the preparation; it stays owned by the adapter afterwards.
        await asyncio.wait({task}, timeout=SETTLE_SECONDS)
        async with player.lock:
            if self.stopping.is_set():
                return SHUTTING_DOWN
            if station.desired != heard.program:
                return "The program changed meanwhile; nothing was skipped."
            if not task.done():
                return SKIPPING  # still preparing; it switches as soon as it is ready
            if not player.skip_requested:
                return SKIPPING  # offered to switch as soon as it is ready
            player.skip_requested = False
            if (reason := station.ending) is not None:
                return f"Nothing follows: {ENDED[reason]}. The current recording continues."
        return "The next recording could not be prepared; the current recording continues."

    async def stop(self, inter: Inter) -> None:
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        if (player := self._player(inter)) is None:
            return await self._refuse(inter, NOT_JOINED)
        if not (self._listening(inter, player) or inter.permissions.manage_guild):
            return await self._refuse(inter, NOT_LISTENING)
        acknowledged = await self._defer(inter, "stop")
        async with player.lock:
            tracks = self._silence(player)
        if not acknowledged:
            return  # the interaction is gone; retirements stay owned by the adapter
        if tracks:  # join the stopped producers, bounded; their retirement stays owned
            await asyncio.wait(tracks, timeout=SETTLE_SECONDS)
        await inter.followup.send(
            "Stopped. SobaFM stays connected and idle.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    def _silence(
        self, player: GuildPlayer, ended: EndReason | None = None
    ) -> set[asyncio.Task[None]]:
        """End the program, remove all audio from the callback and start retirements."""
        self._cancel_grace(player)
        self._cancel_refill(player)
        player.silenced += 1
        player.station.stop(ended)
        player.skip_requested = False
        player.waiting_claim = None  # its cooldown runs on; the stop is not its failure
        tracks = player.source.take_all()
        if player.station.switching is not None:
            player.station.switch_failed(player.station.switching)
        if player.station.heard is not None:
            log.info("heard source cleared")
        player.station.silenced()  # the callback can no longer emit any removed frame
        return {self._retire(t) for t in tracks}

    async def leave(self, inter: Inter) -> None:
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        guild_id = inter.guild.id
        if guild_id in self._voice_busy:
            return await self._refuse(inter, VOICE_BUSY)
        # A remembered channel with no connection is still something to forget, for
        # example one a server's outage kept from being rejoined at startup.
        if guild_id not in self.players and self._settings.channel(guild_id) is None:
            return await self._refuse(inter, NOT_CONNECTED)
        tracks: set[asyncio.Task[None]] = set()
        self._voice_busy.add(guild_id)
        try:
            acknowledged = await self._defer(inter, "leave")
            try:
                # Forget first: if this fails, the bot hasn't left and nothing has changed.
                await self._settings.forget_channel(guild_id)
            except SettingsError as exc:
                log.warning("settings write failed: kind=%s", exc)
                if acknowledged:
                    await inter.followup.send(
                        LEAVE_NOT_SAVED, ephemeral=True, allowed_mentions=NO_MENTIONS
                    )
                return
            left = (player := self.players.get(guild_id)) is not None
            if player is not None:
                tracks = await self._disconnect(guild_id, player)
        finally:
            self._voice_busy.discard(guild_id)
        if not acknowledged:
            return  # the interaction is gone; retirements stay owned by the adapter
        if tracks:
            await asyncio.wait(tracks, timeout=SETTLE_SECONDS)
        reply = "Stopped and left." if left else f"{NOT_CONNECTED} It won't rejoin after a restart."
        await inter.followup.send(reply, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def settings(
        self,
        inter: Inter,
        duration: int | None = None,
        volume: int | None = None,
        cooldown: int | None = None,
    ) -> None:
        """Privately show the server's settings or change the supplied values."""
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        guild_id = inter.guild.id
        if duration is None and volume is None and cooldown is None:
            return await self._refuse(inter, _describe(self._settings.get(guild_id)))
        if self.stopping.is_set():
            return await self._refuse(inter, SHUTTING_DOWN)
        await inter.response.defer(ephemeral=True)
        try:
            updated = await self._settings.update(
                guild_id,
                generated_minutes=duration,
                volume_percent=volume,
                cooldown_seconds=cooldown,
            )
        except ValueError:
            reply = SETTINGS_INVALID
        except SettingsError as exc:
            log.warning("settings write failed: kind=%s", exc)
            reply = SETTINGS_UNAVAILABLE
        else:
            log.info(
                "settings saved: generated_minutes=%d volume_percent=%d cooldown_seconds=%d",
                updated.generated_minutes,
                updated.volume_percent,
                updated.cooldown_seconds,
            )
            if (player := self.players.get(guild_id)) is not None:
                # Current audio changes at its next frame. Reading the store rather than
                # this write's result keeps the latest value if two writes finish together.
                player.output.volume = self._settings.get(guild_id).volume_percent / 100
            reply = f"Saved.\n{_describe(updated)}"
        await inter.followup.send(reply, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def _disconnect(
        self, guild_id: int, player: GuildPlayer, *, ended: bool = False
    ) -> set[asyncio.Task[None]]:
        """Silence and disconnect at once; return the retirements still owning audio.

        ``ended`` means the library has already closed the connection. Disconnecting
        again would remove whichever client now holds this server's slot in its table,
        because `cleanup` removes by server. For the same reason this runs once per player.
        """
        player.closing = True
        async with player.lock:
            if player.disconnected:
                return set()
            player.disconnected = True
            tracks = self._silence(player)
            player.voice.stop()
            try:
                if not ended:
                    await player.voice.disconnect(force=True)
            except Exception as exc:
                log.warning("voice disconnect failed: %s", type(exc).__name__)
            else:
                log.info("voice disconnected")
            self.players.pop(guild_id, None)
        return tracks

    # The voice connection's own events.

    def _voice_ready(self, guild_id: int, voice: disnake.VoiceClient) -> None:
        """A connection or reconnection is up: listeners may differ, or a move may be done."""
        player = self.players.get(guild_id)
        if player is not None and player.voice is voice:
            self._review_listeners(player)

    def _voice_gone(self, guild_id: int, voice: disnake.VoiceClient) -> None:
        """The library has ended this connection for good (see `WatchedVoiceClient`)."""
        player = self.players.get(guild_id)
        if (
            player is not None
            and player.voice is voice
            and not player.closing
            and not self.stopping.is_set()
        ):
            self._lose_voice(guild_id, player, "disconnected", ended=True)

    def channel_deleted(self, guild_id: int, channel_id: int) -> None:
        """A deleted channel ends its connection now, without waiting for the library."""
        player = self.players.get(guild_id)
        # A voice client's channel is a vocal channel, or None once its voice is lost.
        channel = cast(
            disnake.VoiceChannel | disnake.StageChannel | None,
            None if player is None else player.voice.channel,
        )
        if player is not None and channel is not None and channel.id == channel_id:
            if not player.closing and not self.stopping.is_set():
                self._lose_voice(guild_id, player, "channel_deleted", ended=False)
        elif self._settings.channel(guild_id) == channel_id:
            self._own(asyncio.create_task(self._forget_channel(guild_id)))

    def _lose_voice(self, guild_id: int, player: GuildPlayer, reason: str, *, ended: bool) -> None:
        """Start ending the program and forgetting the channel; the adapter owns the work."""
        player.closing = True
        log.info("voice connection ended: reason=%s", reason)
        self._own(asyncio.create_task(self._settle_lost_voice(guild_id, player, ended=ended)))

    async def _settle_lost_voice(self, guild_id: int, player: GuildPlayer, *, ended: bool) -> None:
        """Retirements stay owned by `_retire`, with their debt and failed verdicts."""
        await self._disconnect(guild_id, player, ended=ended)
        if guild_id in self._voice_busy:
            return  # a `/join` or `/leave` now under way decides what is remembered
        self._voice_busy.add(guild_id)
        try:
            await self._forget_channel(guild_id)
        finally:
            self._voice_busy.discard(guild_id)

    async def _forget_channel(self, guild_id: int) -> None:
        """Forget a channel that is gone; if that fails, the next start tries it and forgets."""
        try:
            await self._settings.forget_channel(guild_id)
        except SettingsError as exc:
            log.warning("settings write failed: kind=%s", exc)

    async def _adopt_channel(self, guild_id: int, channel_id: int) -> None:
        """Remember where an administrator moved the bot; audio carries on regardless."""
        try:
            await self._settings.remember_channel(guild_id, channel_id)
        except SettingsError as exc:
            log.warning("settings write failed: kind=%s", exc)
        else:
            log.info("voice channel adopted")

    # Rejoining after a restart.

    def start_rejoins(self, get_guild: Callable[[int], disnake.Guild | None]) -> None:
        """Rejoin remembered channels, once per process however often Discord reports ready."""
        if self._rejoins_started or self.stopping.is_set():
            return
        self._rejoins_started = True
        guild_ids = list(self._settings.channels())
        if guild_ids:
            self._rejoins = self._own(asyncio.create_task(self._rejoin_all(guild_ids, get_guild)))

    async def _rejoin_all(
        self, guild_ids: list[int], get_guild: Callable[[int], disnake.Guild | None]
    ) -> None:
        pending = iter(guild_ids)

        async def worker() -> None:
            for guild_id in pending:  # shared, so each server is taken by one worker
                await self._rejoin(guild_id, get_guild(guild_id))

        async with asyncio.TaskGroup() as workers:
            for _ in range(min(MAX_CONCURRENT_REJOINS, len(guild_ids))):
                workers.create_task(worker())

    async def _rejoin(self, guild_id: int, guild: disnake.Guild | None) -> None:
        """Rejoin one remembered channel with no program, or forget it and say why."""
        channel_id = self._settings.channel(guild_id)
        if (
            channel_id is None
            or guild_id in self.players
            or guild_id in self._voice_busy
            or self.stopping.is_set()
        ):
            return  # a manager's `/join` or `/leave` got there first
        if guild is not None and guild.unavailable:
            # An outage, not an answer: this process won't try again, and the next one will.
            log.warning("voice rejoin: outcome=kept reason=guild_unavailable")
            return
        self._voice_busy.add(guild_id)
        try:
            reason = (
                "guild_gone"
                if guild is None
                else await self._rejoin_channel(guild_id, guild, channel_id)
            )
            if reason is None:
                log.info("voice rejoin: outcome=joined")
            elif not self.stopping.is_set():
                log.warning("voice rejoin: outcome=forgotten reason=%s", reason)
                await self._forget_channel(guild_id)
        finally:
            self._voice_busy.discard(guild_id)

    async def _rejoin_channel(
        self, guild_id: int, guild: disnake.Guild, channel_id: int
    ) -> str | None:
        """Connect within the attempt bounds; return why not, or None once connected."""
        found = guild.get_channel(channel_id)
        if found is None or found.type not in VOCAL_CHANNEL_TYPES:
            return "channel_gone"
        channel = cast(disnake.VoiceChannel | disnake.StageChannel, found)
        permissions = channel.permissions_for(guild.me)
        if not (permissions.connect and permissions.speak):
            return "no_permission"
        for _ in range(REJOIN_ATTEMPTS):
            try:
                async with asyncio.timeout(REJOIN_ATTEMPT_SECONDS):
                    player = await self._connect(guild_id, channel)
                return None if player is not None else "shutting_down"
            except Exception as exc:
                log.warning("voice rejoin attempt failed: %s", type(exc).__name__)
                # A timeout here cancels the connect, which leaves its client registered
                # and would make the next attempt report "already connected".
                if (stale := guild.voice_client) is not None:
                    with contextlib.suppress(Exception):
                        await stale.disconnect(force=True)
        return "connect_failed"

    # Listener grace.

    def voice_state_changed(
        self, guild_id: int, user_id: int, channel: disnake.abc.Snowflake | None
    ) -> None:
        """Re-evaluate listeners after any voice state change, and adopt a move of the bot.

        SobaFM's own state with no channel isn't a disconnection: the library's reconnect
        produces it too (see `WatchedVoiceClient`). No grace period runs without a
        connection; `_voice_ready` reviews again when one is back. A refill carries on,
        since a real loss cancels it through `_silence`.
        """
        player = self.players.get(guild_id)
        if player is None:
            return
        if user_id == player.voice.user.id:
            if channel is None:
                self._cancel_grace(player)
                return
            if (
                channel.id != self._settings.channel(guild_id)
                and guild_id not in self._voice_busy  # a `/join` or `/leave` decides that itself
                and not player.closing
                and not self.stopping.is_set()
            ):
                self._own(asyncio.create_task(self._adopt_channel(guild_id, channel.id)))
        self._review_listeners(player)

    @staticmethod
    def _has_listener(player: GuildPlayer) -> bool:
        """Whether a member in the bot's channel is neither a bot nor deafened.

        Voice states, including their deafened flags, are complete under the Voice
        States intent. The member cache is not: a member already in voice when the
        bot connected and unchanged since is uncached, so its bot flag is unknown and
        it counts as a listener rather than risk cutting off a real one.
        """
        # A voice client's channel is a vocal channel, or None once its voice is lost.
        channel = cast(disnake.VoiceChannel | disnake.StageChannel | None, player.voice.channel)
        if channel is None:
            return False
        own_id = player.voice.user.id
        for user_id, state in channel.voice_states.items():
            if user_id == own_id or state.deaf or state.self_deaf:
                continue
            member = channel.guild.get_member(user_id)
            if member is None or not member.bot:
                return True
        return False

    def _review_listeners(self, player: GuildPlayer) -> None:
        """Arm the grace period for the desired program when no listener remains, or
        cancel it when a listener is present, the program ended or voice is gone."""
        program = player.station.desired
        if (
            program is None
            or self.stopping.is_set()
            or not player.voice.is_connected()
            or self._has_listener(player)
        ):
            self._cancel_grace(player)
            return
        if player.grace is not None and player.grace.program == program:
            return  # already counting down for this program; a change does not restart it
        self._cancel_grace(player)
        task = self._own(asyncio.create_task(self._expire_grace(player, program)))
        player.grace = Grace(program, task)
        log.info("listener grace started: seconds=%d", LISTENER_GRACE_SECONDS)

    def _cancel_grace(self, player: GuildPlayer) -> None:
        grace, player.grace = player.grace, None
        if grace is not None:
            grace.task.cancel()
            log.info("listener grace cancelled")

    async def _expire_grace(self, player: GuildPlayer, program: Program) -> None:
        """End ``program`` through the `/stop` path once the grace period passes."""
        await self._sleep(LISTENER_GRACE_SECONDS)
        async with player.lock:
            grace = player.grace
            if grace is None or grace.task is not asyncio.current_task():
                return  # cancelled or superseded while waiting for the lock
            player.grace = None
            if (
                self.stopping.is_set()
                or player not in self.players.values()
                or not player.voice.is_connected()
            ):
                return
            if player.station.desired != program or self._has_listener(player):
                # A stale timer never ends a newer program, but the desired program
                # may have changed with no review, e.g. a failed replacement reverted.
                self._review_listeners(player)
                return
            self._silence(player, EndReason.NO_LISTENERS)  # retirements stay owned
            log.info("program ended: reason=%s", EndReason.NO_LISTENERS.value)

    async def now(self, inter: Inter) -> None:
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        player = self._player(inter)
        station = None if player is None else player.station
        heard = None if station is None else station.heard
        if station is None:
            status = "SobaFM is not in a voice channel."
        elif heard is None:
            status = "Nothing is audible."
            if station.desired is None and (ended := station.ended) is not None:
                status += (
                    f" The last program ended: {ENDED[ended]}. Use `/play` to start a new one."
                )
            elif station.continuing:
                status += " The program's next recording is preparing and is not audible yet."
        else:
            status = "Now heard (full credits follow)."
            if station.desired == heard.program and (ending := station.ending) is not None:
                status += (
                    f" This is the program's last recording: {ENDED[ending]}. "
                    "Use `/play` to start a new program."
                )
        if (
            station is not None
            and station.desired is not None
            and not station.continuing
            and (heard is None or station.desired != heard.program)
        ):
            status += " A new request is starting and is not audible yet."
        await self._refuse(inter, status)
        if heard is not None:
            for number in range(1, len(heard.credits.pages) + 1):
                page = heard.credits.page(number)
                await inter.followup.send(
                    page.text, ephemeral=True, suppress_embeds=True, allowed_mentions=NO_MENTIONS
                )

    # Reports from the voice callback, settled on the loop.

    def _reporter(self, guild_id: int) -> Callable[[str, Track], None]:
        loop = asyncio.get_running_loop()

        def report(kind: str, track: Track) -> None:
            with contextlib.suppress(RuntimeError):  # loop already closed at shutdown
                loop.call_soon_threadsafe(self._settle, guild_id, kind, track)

        return report

    def _settle(self, guild_id: int, kind: str, track: Track) -> None:
        player = self.players.get(guild_id)
        station = None if player is None else player.station
        if station is not None and kind == "switched":
            if station.switched(track.audio) is Outcome.ACCEPTED:
                credits = track.audio.recording.credits  # present: its credits rendered
                log.info(
                    "heard source changed: recording=%s credit_snapshot=%s",
                    _recording_label(track.audio.recording),
                    "none" if credits is None else credits.snapshot_id,
                )
                if station.desired == track.audio.program:
                    assert player is not None
                    player.attempts = 0
        elif station is not None and kind == "failed":
            station.switch_failed(track.audio)
            assert player is not None
            player.cooldown.release(track.claim)  # no effect once a newer request holds it
        elif (
            station is not None
            and kind == "drained"
            and station.drained(track.audio) is Outcome.ACCEPTED
        ):
            log.info("heard source cleared")
            if station.ended is not None and station.desired is None:
                log.info("program ended: reason=%s", station.ended.value)
        if kind != "switched":
            self._retire(track)
        self._capacity_changed()  # e.g. a drained track starts its program's capacity bound
        if player is None or self.stopping.is_set():
            return
        # A failed switch can revert the desired program and a drain can end it.
        self._review_listeners(player)
        if kind in ("switched", "failed") and player.station.ready:
            self._own(asyncio.create_task(self._start_locked(player)))
        else:
            self._resume(player)

    async def _start_locked(self, player: GuildPlayer) -> None:
        async with player.lock:
            if not await self._start(player):
                self._give_back(player, player.waiting_claim)
                self._resume(player)  # a failed replacement hands the place back to the program
            self._review_listeners(player)  # a failed start can revert the desired program

    # Ownership.

    def _own(self, task: asyncio.Task[None]) -> asyncio.Task[None]:
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _retire(self, track: Track) -> asyncio.Task[None]:
        """Start (once) the retirement that owns ``track`` until its bank is released.

        Callers wait on the task without cancelling it, so an interrupted command
        never abandons a producer or a leased bank.
        """
        if track.retirement is None:
            track.retirement = self._own(asyncio.create_task(self._settle_track(track)))
            self.retirements.add(track.retirement)
            track.retirement.add_done_callback(self.retirements.discard)
            track.retirement.add_done_callback(lambda _: self._capacity_changed())
        return track.retirement

    async def _settle_track(self, track: Track) -> None:
        track.bank.cancel()  # keeps the first cancellation time if already cancelled
        cancelled_at = track.bank.facts().cancelled_at
        assert cancelled_at is not None
        track.task.cancel()
        # The pool's default clock is time.monotonic, so the deadline stays the original one.
        remaining = cancelled_at + CLEANUP_SECONDS - time.monotonic()
        done, _ = await asyncio.wait({track.task}, timeout=max(0.0, remaining))
        if not done:
            self.cleanup_failures += 1  # a failed verdict, whatever happens later
            log.error("audio producer missed its cleanup deadline; its bank stays leased")
            await asyncio.wait({track.task})
        facts = track.bank.facts()  # the retirement's settled facts, taken before release
        released = track.bank.release()
        if not released:
            self.debt.append(track.bank)
        log.info(
            "track retired: recording=%s frames_handed_to_voice=%d underruns=%d "
            "transfer_complete=%s decoder_complete=%s pcm_valid=%s audible_drain=%s "
            "failure=%s bank_released=%s cancel_to_release_seconds=%s",
            _recording_label(track.audio.recording),
            track.frames,
            track.underruns,
            facts.transfer_complete,
            facts.decoder_complete,
            facts.pcm_valid,
            facts.audible_drain,
            facts.failure or "none",
            released,
            f"{time.monotonic() - cancelled_at:.3f}" if released else "none",
        )

    async def shutdown(self) -> bool:
        """Stop and disconnect every server, join all owners and report full release.

        Success requires no retained bank, no lease still charged to the pool and no
        failed cleanup verdict. The remembered channels stay, so the next start rejoins them.
        """
        self.stopping.set()
        if self._rejoins is not None:
            # A connect still under way leaves its client registered; closing the bot ends it.
            self._rejoins.cancel()
        self._capacity_changed()  # continuations waiting for a bank return at once
        while self.players or self.tasks:
            for guild_id, player in list(self.players.items()):
                await self._disconnect(guild_id, player)
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            # Gathering tasks that are all done does not yield, so a finished task
            # whose ``tasks.discard`` callback is still queued would spin this loop.
            await asyncio.sleep(0)
        self.debt = [bank for bank in self.debt if not bank.release()]
        if self.debt or self._pool.leased:
            log.error("%d PCM bank lease(s) remain at shutdown", self._pool.leased)
        if self.cleanup_failures:
            log.error("%d cleanup deadline(s) were missed", self.cleanup_failures)
        released = not self.debt and not self._pool.leased and not self.cleanup_failures
        log.info(
            "shutdown: released=%s leases_held=%d cleanup_failures=%d",
            released,
            self._pool.leased,
            self.cleanup_failures,
        )
        return released


def _recording_label(recording: Recording) -> str:
    """The catalog recording key; never request text."""
    return f"{recording.key.source.value}:{recording.key.recording_id}"


def refill_wait(attempt: int, seconds_left: float) -> float:
    """How long a refill waits after failed call ``attempt``, with ``seconds_left`` heard.

    The wait is capped so the next call can finish and the next track prepare before the
    heard track ends. It's never shorter than the first wait, so a short track, a silent
    program and an unknown length all still space their calls.
    """
    fits = seconds_left - REFILL_RESERVE_SECONDS
    return max(REFILL_WAIT_SECONDS[0], min(REFILL_WAIT_SECONDS[attempt - 1], fits))


def _accepted(choice: Ranked) -> str:
    """Say how close the chosen recording is, using only catalog text."""
    title = escaped_text(choice.recording.title)
    artist = escaped_text(choice.recording.primary_artist)
    return (
        f"Request accepted. {FIT_WORDING[choice.fit]}: {title} by {artist}. It is preparing "
        "and becomes audible once enough audio is buffered. Use `/now` to see what is "
        "actually heard."
    )


def _describe(settings: ServerSettings) -> str:
    """The settings with their ranges, as `/settings` shows them."""
    wait = "second" if settings.cooldown_seconds == 1 else "seconds"
    return (
        f"Volume: {settings.volume_percent}% ({MIN_VOLUME_PERCENT} to {MAX_VOLUME_PERCENT}).\n"
        f"Change cooldown: {settings.cooldown_seconds} {wait} "
        f"({MIN_COOLDOWN_SECONDS} to {MAX_COOLDOWN_SECONDS}), across `/play` and `/skip`.\n"
        f"Generated duration: {settings.generated_minutes} minutes "
        f"({MIN_GENERATED_MINUTES} to {MAX_GENERATED_MINUTES}), for generated music only."
    )


def create_bot(adapter: Adapter) -> commands.InteractionBot:
    """Register the slash commands; `/play` and `/skip` are omitted without a catalog."""
    bot = commands.InteractionBot(intents=intents())
    servers = disnake.InteractionContextTypes(guild=True)
    manager = disnake.Permissions(manage_guild=True)

    @bot.slash_command(name="join", contexts=servers, default_member_permissions=manager)
    async def join(inter: Inter) -> None:
        """Join or move to your voice channel."""
        await adapter.join(inter)

    @bot.slash_command(name="leave", contexts=servers, default_member_permissions=manager)
    async def leave(inter: Inter) -> None:
        """Stop and disconnect."""
        await adapter.leave(inter)

    @bot.slash_command(name="stop", contexts=servers)
    async def stop(inter: Inter) -> None:
        """End the program and stay connected."""
        await adapter.stop(inter)

    @bot.slash_command(name="now", contexts=servers)
    async def now(inter: Inter) -> None:
        """Privately show what is actually heard, with full credits."""
        await adapter.now(inter)

    @bot.slash_command(name="settings", contexts=servers, default_member_permissions=manager)
    async def settings(
        inter: Inter,
        duration: commands.Range[int, MIN_GENERATED_MINUTES, MAX_GENERATED_MINUTES] | None = None,
        volume: commands.Range[int, MIN_VOLUME_PERCENT, MAX_VOLUME_PERCENT] | None = None,
        cooldown: commands.Range[int, MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS] | None = None,
    ) -> None:
        """Privately show this server's settings, or change the values you supply.

        Parameters
        ----------
        duration: Minutes generated music lasts.
        volume: Playback volume in percent.
        cooldown: Seconds between changes of music, across /play and /skip.
        """
        await adapter.settings(inter, duration, volume, cooldown)

    @bot.listen("on_voice_state_update")
    async def voice_state_update(
        member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState
    ) -> None:
        adapter.voice_state_changed(member.guild.id, member.id, after.channel)

    # `on_ready` can fire again after a new gateway session; `start_rejoins` runs once.
    @bot.listen("on_ready")
    async def ready() -> None:
        adapter.start_rejoins(bot.get_guild)

    @bot.listen("on_guild_channel_delete")
    async def channel_delete(channel: disnake.abc.GuildChannel) -> None:
        adapter.channel_deleted(channel.guild.id, channel.id)

    if adapter.matcher is not None:

        @bot.slash_command(name="skip", contexts=servers)
        async def skip(inter: Inter) -> None:
            """Move on to the next recording chosen for your request."""
            await adapter.skip(inter)

        @bot.slash_command(name="play", contexts=servers)
        async def play(
            inter: Inter,
            request: commands.String[str, 1, MAX_REQUEST_CHARACTERS],
        ) -> None:
            """Play the existing music that best fits your request.

            Parameters
            ----------
            request: Describe the music you want, any way you like.
            """
            await adapter.play(inter, request)

    else:
        log.warning("catalog is not configured; /play and /skip are not registered")
    return bot


async def run(config: Config, producer: Producer, settings: SettingsStore) -> int:
    """Run until SIGTERM or SIGINT, then disconnect and exit once owners are released."""
    adapter = Adapter(config.matcher, producer, settings)
    bot = create_bot(adapter)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, adapter.stopping.set)
    client = asyncio.create_task(bot.start(config.token))
    stopping = asyncio.create_task(adapter.stopping.wait())
    await asyncio.wait({client, stopping}, return_when=asyncio.FIRST_COMPLETED)
    released = await adapter.shutdown()
    if config.matcher is not None:
        await config.matcher.close()  # also ends any request still being matched
    settings_closed = await settings.close()  # joins any write still queued
    await bot.close()
    stopping.cancel()
    await asyncio.wait({client})
    error = None if client.cancelled() else client.exception()
    if error is not None:
        # Only the type: client errors must not carry the token or interaction data out.
        log.error("Discord client stopped: %s", type(error).__name__)
    return 0 if released and settings_closed and error is None else 1
