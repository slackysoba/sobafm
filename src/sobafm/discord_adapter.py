"""Discord adapter for the first playback slice: slash commands, voice and shutdown.

Commands work only in servers. `/join` and `/leave` require Manage Server by default
through Discord command permissions; `/play`, `/skip` and `/stop` require the caller to
share the bot's voice channel, except that a manager may stop from elsewhere. `/now` is
private and open to ordinary listeners.

`/play` asks an injected `Matcher` for a validated plan. A refusal or failure changes
nothing, one request is matched at a time per server, and a stop during that wait wins.

Each server's `Station` settles desired, prepared and heard facts on the event loop.
Audio comes from an injected `Producer` that fills a leased `pcm.py` bank. The voice
callback (`StationSource.read`) only copies prepared frames: it never waits on the
network, a process, a database or cleanup, and hands every report back to the loop.
The heard source changes only when the callback emits the first frame of new audio.
A catalog program continues: once a track is heard, the next one is taken from the
program's ranked plan and history and prepared in the second bank, and the
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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
    Source,
    eligible,
    parse_catalog,
    parse_reviews,
)
from sobafm.credits import escaped_text
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
    GLOBAL_PCM_BANKS,
    PcmBank,
    PcmBankPool,
    PcmCapacityError,
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
# How long a program continues after the last listener leaves (docs/requirements.md).
LISTENER_GRACE_SECONDS = 60.0
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
ENDED = {
    EndReason.EXHAUSTED: "no recording chosen for its request is left to try",
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
    """Turns one request into a validated plan, or raises `MatchFailed`."""

    async def match(self, request: str) -> Matched | Refused: ...

    async def close(self) -> None: ...


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

    def cut_pending(self) -> bool:
        """Make a pending track switch as soon as it is ready; report whether one exists."""
        with self._lock:
            if self._pending is None:
                return False
            self._pending.cut = True
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
class GuildPlayer:
    voice: disnake.VoiceClient
    source: StationSource
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


class Adapter:
    """Command handlers and per-server players; all state is owned by the event loop."""

    def __init__(
        self,
        matcher: Matcher | None,
        producer: Producer,
        *,
        pool: PcmBankPool = GLOBAL_PCM_BANKS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.matcher = matcher
        self._producer = producer
        self._pool = pool
        self._sleep = sleep  # the grace timer's clock; tests inject a controlled one
        self.players: dict[int, GuildPlayer] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.retirements: set[asyncio.Task[None]] = set()
        self.debt: list[PcmBank] = []
        self.cleanup_failures = 0
        self.stopping = asyncio.Event()
        # Continuations waiting for a bank; woken when a retirement or report may have
        # changed capacity, or at shutdown.
        self._capacity_waiters: set[asyncio.Future[None]] = set()

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
        await inter.response.defer(ephemeral=True)
        player = self.players.get(inter.guild.id)
        if player is not None and not player.voice.is_connected():
            await self._disconnect(inter.guild.id, player)  # settle a lost connection's owners
            player = None
        if player is not None:
            await player.voice.move_to(channel)
            self._review_listeners(player)  # the new channel may have no listener
        else:
            voice = await channel.connect()
            if self.stopping.is_set():  # shutdown began while connecting
                await voice.disconnect(force=True)
                return await inter.followup.send(
                    SHUTTING_DOWN, ephemeral=True, allowed_mentions=NO_MENTIONS
                )
            log.info("voice connected")
            report = self._reporter(inter.guild.id)
            self.players[inter.guild.id] = GuildPlayer(voice, StationSource(report))
        await inter.followup.send(
            f"Joined {channel.mention}.", ephemeral=True, allowed_mentions=NO_MENTIONS
        )

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
        player.matching = True
        silenced = player.silenced
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
            # The request is interpreted once: later tracks come from this ranked plan.
            program = station.request(request, tuple(c.recording for c in plan.ranked)).program
            player.attempts = 0
            player.skip_requested = False
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
                return "The matching recordings' credits are incomplete; nothing was substituted."
            if not await self._start(player):
                return (
                    "Playback could not start (capacity is busy or voice is unavailable); "
                    "current audio continues. Try again shortly."
                )
        return _accepted(chosen)

    async def _start(self, player: GuildPlayer, *, cut: bool = True) -> bool:
        """Claim ready audio, displacing an unheard pending track, and hand it to the player.

        ``cut`` audio switches as soon as it is ready; otherwise it follows the
        current track's last frame.
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
        track = Track(audio, bank, task, cut=cut)
        idle = player.source.offer(track)
        if idle or not player.voice.is_playing():  # also restarts after a player error
            try:
                player.voice.stop()
                player.voice.play(player.source)
            except Exception as exc:  # e.g. the voice connection dropped meanwhile
                log.warning("voice player could not start: %s", type(exc).__name__)
                if player.source.take_pending() is track:
                    station.switch_failed(audio)
                    self._retire(track)
                return False
        return True

    async def _produce(self, recording: Recording, bank: PcmBank) -> None:
        try:
            await self._producer.produce(recording, bank)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("audio producer failed: %s", type(exc).__name__)
            bank.cancel()  # the voice callback then reports the failure

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
        program after the heard track instead of repeating a recording.
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
                            recording = select_next(program.plan, station.history(program))
                            if recording is None:
                                station.exhausted(program, EndReason.EXHAUSTED)
                                log.info("program has no further recording in its plan")
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
            self._capacity_changed()  # a bank this program yielded may now go elsewhere
            self._review_listeners(player)  # exhaustion can end the program

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
        """Switch to the program's next track once it is ready; current audio continues."""
        async with player.lock:
            station = player.station
            if self.stopping.is_set():
                return SHUTTING_DOWN
            heard = station.heard
            if heard is None or station.desired != heard.program:
                return "Nothing from the current program is audible to skip."
            if player.source.cut_pending():
                return SKIPPING  # the prepared or still-preparing next track
            if (reason := station.ending) is not None:
                return f"Nothing follows: {ENDED[reason]}. The current recording continues."
            if not station.needs_next:  # the next track's switch report is in flight
                return "The next recording is already starting."
            player.skip_requested = True
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
        player.silenced += 1
        player.station.stop(ended)
        player.skip_requested = False
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
        if (player := self.players.get(inter.guild.id)) is None:
            return await self._refuse(inter, "SobaFM is not in a voice channel.")
        acknowledged = await self._defer(inter, "leave")
        tracks = await self._disconnect(inter.guild.id, player)
        if not acknowledged:
            return  # the interaction is gone; retirements stay owned by the adapter
        if tracks:
            await asyncio.wait(tracks, timeout=SETTLE_SECONDS)
        await inter.followup.send("Stopped and left.", ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def _disconnect(self, guild_id: int, player: GuildPlayer) -> set[asyncio.Task[None]]:
        """Silence and disconnect at once; return the retirements still owning audio."""
        async with player.lock:
            tracks = self._silence(player)
            player.voice.stop()
            try:
                await player.voice.disconnect(force=True)
            except Exception as exc:
                log.warning("voice disconnect failed: %s", type(exc).__name__)
            else:
                log.info("voice disconnected")
            self.players.pop(guild_id, None)
        return tracks

    # Listener grace.

    def voice_state_changed(
        self, guild_id: int, user_id: int, channel: disnake.abc.Snowflake | None
    ) -> None:
        """Re-evaluate listeners after any voice state change in the server."""
        player = self.players.get(guild_id)
        if player is None:
            return
        if user_id == player.voice.user.id and channel is None:
            self._cancel_grace(player)  # voice was lost; settled at the next `/join` or `/leave`
            return
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
        elif self.matcher is not None and player.station.needs_next:
            self._schedule_continue(player)

    async def _start_locked(self, player: GuildPlayer) -> None:
        async with player.lock:
            await self._start(player)
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
        failed cleanup verdict.
        """
        self.stopping.set()
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


def _accepted(choice: Ranked) -> str:
    """Say how close the chosen recording is, using only catalog text."""
    title = escaped_text(choice.recording.title)
    artist = escaped_text(choice.recording.primary_artist)
    return (
        f"Request accepted. {FIT_WORDING[choice.fit]}: {title} by {artist}. It is preparing "
        "and becomes audible once enough audio is buffered. Use `/now` to see what is "
        "actually heard."
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

    @bot.listen("on_voice_state_update")
    async def voice_state_update(
        member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState
    ) -> None:
        adapter.voice_state_changed(member.guild.id, member.id, after.channel)

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


async def run(config: Config, producer: Producer) -> int:
    """Run until SIGTERM or SIGINT, then disconnect and exit once owners are released."""
    adapter = Adapter(config.matcher, producer)
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
    await bot.close()
    stopping.cancel()
    await asyncio.wait({client})
    error = None if client.cancelled() else client.exception()
    if error is not None:
        # Only the type: client errors must not carry the token or interaction data out.
        log.error("Discord client stopped: %s", type(error).__name__)
    return 0 if released and error is None else 1
