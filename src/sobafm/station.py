"""A station: one server's program, decks, and mixer, run by a reconcile loop (ADR-0004)."""

import asyncio
import contextlib
import logging
import time
import weakref
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

import discord

from sobafm.deck import Connect, Deck, EndReason, State
from sobafm.failures import Failure
from sobafm.mixer import Mixer
from sobafm.pcm import FRAME_SECONDS
from sobafm.plan import MusicPlan

log = logging.getLogger(__name__)

TICK_S = 0.25
SESSIONS_PER_STATION = 2  # the live deck's and the next deck's
SESSION_LIMIT_S = 540.0  # Lyria closes sessions at 600 s
HANDOVER_BELOW_S = 4.0
CROSSFADE_S = 4.0
FADE_IN_S = 2.0
FADE_OUT_S = 3.0
REFUSAL_RETRY_S = 30.0
CONNECT_BACKOFF_S = (2.0, 5.0, 15.0)
PLAYER_RETRY_S = 1.0  # starting discord.py's player, at most once per interval
RECOVERED_S = 1.0  # audio after a run of underruns that ends it, so a stutter is one run
EMPTY_GRACE_S = 60.0  # how long a program plays to an empty channel (PLAY-4)
STATUS_TIMEOUT_S = 10.0  # how long closing or moving waits for voice channel status requests
STATUS_RETRY_S = 10.0  # before checking a status again, doubling while requests for it fail
STATUS_RETRY_MAX_S = 300.0  # the longest wait between them


class Player(Protocol):
    """The part of discord.py's voice client a station uses."""

    def is_connected(self) -> bool: ...

    def is_playing(self) -> bool: ...

    def play(
        self,
        source: discord.AudioSource,
        *,
        after: Callable[[Exception | None], Any] | None = None,
        signal_type: Literal["auto", "voice", "music"] = "auto",
    ) -> None: ...

    def stop(self) -> None: ...


class Outcome(StrEnum):
    PLAYING = "playing"
    BUSY = "busy"  # every program's worth of Lyria sessions is taken
    REFUSED = "refused"  # Lyria refused the prompt twice
    REJECTED = "rejected"  # Google rejected the API key
    EXHAUSTED = "exhausted"  # a quota or rate limit
    UNAVAILABLE = "unavailable"  # Lyria RealTime is down or unreachable
    FAILED = "failed"  # sessions failed for another reason, or the player could not start
    DISCONNECTED = "disconnected"  # SobaFM lost its voice connection
    REPLACED = "replaced"  # a newer request replaced it before it started
    STOPPED = "stopped"


class SessionPool:
    """Caps Lyria RealTime sessions in the process by reserving two for each program."""

    def __init__(self, limit: int) -> None:
        self.programs = limit // SESSIONS_PER_STATION
        self._holders: set[object] = set()

    def reserve(self, holder: object) -> bool:
        """Reserve a program's sessions for `holder`; False when every reservation is taken."""
        if holder not in self._holders:
            if len(self._holders) >= self.programs:
                return False
            self._holders.add(holder)
        return True

    def release(self, holder: object) -> None:
        self._holders.discard(holder)


@dataclass
class Program:
    plan: MusicPlan
    requester: str
    ends_at: float  # on the station's clock
    started: asyncio.Future[Outcome] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    refusals: int = 0
    refused_until: float = 0.0  # a refusal's round, in which another refusal doesn't count
    failures: int = 0
    retry_at: float = 0.0
    underruns: int = 0  # frames of silence while it played

    @property
    def playing(self) -> bool:
        return self.started.done() and self.started.result() is Outcome.PLAYING

    def settle(self, outcome: Outcome) -> None:
        if not self.started.done():
            self.started.set_result(outcome)


# What a start that fails for each cause reports; any other cause reports FAILED
START_OUTCOMES: dict[Failure | None, Outcome] = {
    Failure.REJECTED: Outcome.REJECTED,
    Failure.EXHAUSTED: Outcome.EXHAUSTED,
    Failure.UNAVAILABLE: Outcome.UNAVAILABLE,
}
# A start that fails for these causes fails at once, since retrying within seconds can't help.
FAILED_STARTS = {Failure.REJECTED, Failure.EXHAUSTED}


class Station:
    """Plays one server's program. Only `reconcile()` and `close()` change decks."""

    def __init__(
        self,
        voice: Callable[[], Player | None],
        listeners: Callable[[], int],
        connect: Connect,
        pool: SessionPool,
        *,
        volume: float,
        channel: Callable[[], int | None],
        status: Callable[[int, str | None], Coroutine[Any, Any, bool]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.mixer = Mixer(volume)
        self.program: Program | None = None
        # What played when the program was requested, which resumes if the program can't start
        self._previous: Program | None = None
        self.decks: list[Deck] = []
        # Decks seen to have ended, so each end counts once; weak, so a dropped deck is freed
        self._seen_ended: weakref.WeakSet[Deck] = weakref.WeakSet()
        self._voice = voice
        self._listeners = listeners
        self._empty_since: float | None = None
        # The mixer's counts of underruns and of reads with audio when last watched: at each
        # tick, and before the program heard changes
        self._underruns_seen = 0
        self._played_seen = 0
        self._dry_since: int | None = None  # the count when the current run of silence began
        self._recovered = 0  # reads with audio since the run's last underrun
        self._connect = connect
        self._pool = pool
        self._clock = clock
        self._playing: object | None = None  # identifies the running player, if any
        self._next_play = 0.0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._channel = channel  # the ID of SobaFM's voice channel, if it is in one
        self._set_status = status  # shows a status on a channel, or says SobaFM may not
        self._shown: tuple[int, str] | None = None  # the status SobaFM set, and where
        self._status_task: asyncio.Task[None] | None = None  # the request in flight
        self._status_held = False  # while SobaFM moves
        self._unshown: tuple[int, str | None] | None = None  # the last status not shown, and where
        self._status_failures = 0  # failed requests for `_unshown`
        self._status_retry_at = 0.0

    @property
    def time_left(self) -> float | None:
        """Seconds until the program ends, or None without one."""
        return None if self.program is None else max(0.0, self.program.ends_at - self._clock())

    def play(
        self, plan: MusicPlan, requester: str, *, duration_seconds: float
    ) -> asyncio.Future[Outcome]:
        """Start or replace the program, which ends `duration_seconds` from now.

        The result resolves once it plays or fails.
        """
        program = Program(plan, requester, ends_at=self._clock() + duration_seconds)
        if not self._pool.reserve(self):
            log.info("No Lyria RealTime session capacity left")
            program.settle(Outcome.BUSY)
            return program.started
        if self.program is not None:
            self.program.settle(Outcome.REPLACED)
            if self.program.playing:
                self._previous = self.program
        self.program = program
        self._empty_since = None
        self.wake()
        return program.started

    def stop(self) -> None:
        self._finish(Outcome.STOPPED)
        self.wake()

    def start(self) -> None:
        """Run the reconcile loop in the background until `close()`."""
        self._task = asyncio.create_task(self._run(), name="station")

    def wake(self) -> None:
        """Reconcile now rather than at the next tick."""
        self._wake.set()

    async def close(self, outcome: Outcome = Outcome.STOPPED) -> None:
        """Stop at once: end the program with `outcome`, retire every deck, and stop the loop.

        It also clears the voice channel status, which needs SobaFM still in the channel.
        """
        self._finish(outcome)
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for deck in self.decks:
            deck.retire()
        await asyncio.gather(*(deck.wait_ended() for deck in self.decks))
        self.decks.clear()
        self._pool.release(self)
        self.mixer.switch_to(None, 0)
        if (player := self._voice()) is not None:
            player.stop()
        await self._clear_status()

    @contextlib.asynccontextmanager
    async def moving(self) -> AsyncGenerator[None]:
        """Clear the voice channel status before SobaFM moves, and show it again after."""
        self._status_held = True
        try:
            await self._clear_status()
            yield
        finally:
            self._status_held = False

    async def _clear_status(self) -> None:
        """Clear the voice channel status, if SobaFM set one in the channel it is in.

        The clear follows any request in flight, so requests never overlap, and this waits for
        both at most STATUS_TIMEOUT_S. Neither is cancelled: discord.py's rate limiter
        mishandles a request cancelled while it waits.
        """
        self._status_task = asyncio.create_task(self._clear_after(self._status_task))
        await asyncio.wait({self._status_task}, timeout=STATUS_TIMEOUT_S)

    async def _clear_after(self, request: asyncio.Task[None] | None) -> None:
        if request is not None:
            await asyncio.wait({request})
        channel = self._channel()
        if channel is not None and self._shown is not None and self._shown[0] == channel:
            await self._show_status(channel, None)

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.reconcile()
            except Exception:  # a failed tick must not stop playback; the next tick retries
                log.exception("Reconcile failed")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(TICK_S):
                    await self._wake.wait()

    async def reconcile(self) -> None:
        """Apply the reconcile rules once, in order."""
        self._watch_underruns()
        player = self._voice()
        if self.program is not None and player is None:
            log.info("Voice connection lost; ending the program")
            self._finish(Outcome.DISCONNECTED)
        if self.program is not None and self._clock() >= self.program.ends_at:
            log.info("Play duration elapsed; ending the program")
            self._finish(Outcome.STOPPED)
        if self.program is not None and self._abandoned():
            log.info("No listeners for %d seconds; ending the program", EMPTY_GRACE_S)
            self._finish(Outcome.STOPPED)
        self._discard()
        if self.program is not None:
            self._retire(self.program)
            self._generate(self.program)
        if self.program is not None:
            self._start(self.program, player)
        if self.program is not None:  # starting the player can end the program
            self._hand_over(self.program, player)
        else:
            self._wind_down(player)
        await asyncio.gather(*(deck.regulate() for deck in self.decks))
        self._sync_status()

    def _sync_status(self) -> None:
        """Show the title being heard as the voice channel status, one request at a time (FB-2).

        A program shows its title once it plays, and a replacement leaves the title still heard
        until it plays itself, including after a move. A drag to another channel is caught up on
        the next tick, and the old channel keeps the status until it empties or SobaFM shows
        another title there: only someone connected there, or with Manage Channels, can change it.
        """
        busy = self._status_task is not None and not self._status_task.done()
        channel = self._channel()
        if busy or self._status_held or channel is None:
            return
        if self._shown is not None and self._shown[0] != channel:
            self._shown = None  # dragged without `moving()`
        heard = self._program_heard()
        title = heard.plan.title if heard is not None else None
        shown = self._shown[1] if self._shown is not None else None
        unshown = self._unshown == (channel, title) and self._clock() < self._status_retry_at
        if title != shown and not unshown:
            self._status_task = asyncio.create_task(self._show_status(channel, title))

    async def _show_status(self, channel: int, title: str | None) -> None:
        """Show `title` on `channel`, or schedule another attempt."""
        failures = self._status_failures if self._unshown == (channel, title) else 0
        try:
            allowed = await self._set_status(channel, title)
        except Exception:  # the status is cosmetic, so it must never affect playback
            if not failures:  # once for each status, since retries would repeat it
                log.warning("Could not update the voice channel status", exc_info=True)
            failures += 1
            # The exponent is bounded, so a status refused for days can't overflow the delay.
            delay = min(STATUS_RETRY_S * 2 ** min(failures - 1, 16), STATUS_RETRY_MAX_S)
        else:
            if allowed:
                self._shown = None if title is None else (channel, title)
                self._unshown = None
                return
            delay = STATUS_RETRY_S  # checking the permission again sends no request
        self._unshown, self._status_failures = (channel, title), failures
        self._status_retry_at = self._clock() + delay

    def _abandoned(self) -> bool:
        """Whether the channel has had no listeners for the whole grace period."""
        if self._listeners() > 0:
            self._empty_since = None
            return False
        now = self._clock()
        if self._empty_since is None:
            self._empty_since = now
        return now - self._empty_since >= EMPTY_GRACE_S

    def _finish(self, outcome: Outcome) -> None:
        self._watch_underruns()  # silence since the last tick, so the next tick won't count it
        if (heard := self._program_heard()) is not None:
            self._end_run()
            self._log_ended(heard, outcome)
        if self.program is not None:
            self.program.settle(outcome)
            self.program = None
        self._previous = None

    def _give_up(self, outcome: Outcome) -> None:
        """End a program that could not start, returning to the program still heard (FB-3)
        unless its end time has passed."""
        previous = self._previous
        if previous is None or self.program is None:
            self._finish(outcome)
            return
        self.program.settle(outcome)  # it never played
        if self._clock() >= previous.ends_at:
            log.info("Play duration elapsed; ending the program")
            self._finish(Outcome.STOPPED)
            return
        self.program, self._previous = previous, None
        log.info("Returning to the program still playing")

    def _settle_playing(self, program: Program) -> None:
        """Settle `program` as playing, so the program it replaced is no longer heard."""
        self._watch_underruns()
        program.settle(Outcome.PLAYING)
        if (previous := self._previous) is not None:
            self._log_ended(previous, Outcome.REPLACED)
            self._previous = None

    def _program_heard(self) -> Program | None:
        """The program listeners hear: the program once it plays, or the one it is replacing."""
        if self.program is not None and self.program.playing:
            return self.program
        return self._previous

    def _watch_underruns(self) -> None:
        """Count new underruns toward the program heard (NFR-2), and log each run of silence in
        it as it starts, and its length once audio plays for a while.

        It runs at each tick and before the program heard changes, so each underrun counts
        once. Reads stop while discord.py reconnects to voice, which ends nothing, and
        RECOVERED_S of audio must play before a run ends, so a stutter logs as one run.
        """
        # Written on discord.py's player thread. Each is read once, so a run and a total agree.
        underruns, played = self.mixer.underruns, self.mixer.played
        new_underruns, new_played = underruns - self._underruns_seen, played - self._played_seen
        self._underruns_seen, self._played_seen = underruns, played
        if (heard := self._program_heard()) is not None:
            heard.underruns += new_underruns
        if new_underruns:
            if self._dry_since is None and heard is not None:
                self._dry_since = underruns - new_underruns
                log.warning("The live deck ran dry; playing silence")
            self._recovered = 0
        elif self._dry_since is not None:
            self._recovered += new_played
            if self._recovered * FRAME_SECONDS >= RECOVERED_S:
                self._end_run()

    def _end_run(self) -> None:
        """Log the length of the current run of silence, if there is one."""
        if self._dry_since is not None:
            seconds = (self._underruns_seen - self._dry_since) * FRAME_SECONDS
            log.info("Silence lasted %.2f s", seconds)
            self._dry_since, self._recovered = None, 0

    @staticmethod
    def _log_ended(program: Program, outcome: Outcome) -> None:
        seconds = program.underruns * FRAME_SECONDS
        log.info("Program ended (%s) with %.2f s of underrun", outcome, seconds)

    def _wind_down(self, player: Player | None) -> None:
        """Without a program: drop idle decks, fade out, stop the player, and free sessions."""
        for deck in self.decks:
            if not self.mixer.uses(deck):
                deck.retire()
        if all(deck.state is State.ENDED for deck in self.decks):
            self._pool.release(self)
        if self._playing is None:  # nothing reads the mixer, so a fade would never finish
            self.mixer.switch_to(None, 0)
            return
        if self.mixer.switching:
            return
        if self.mixer.live is not None:
            self.mixer.switch_to(None, FADE_OUT_S)
        elif player is not None and player.is_playing():
            player.stop()
            self._playing = None  # its `after` callback can come much later

    def _retire(self, program: Program) -> None:
        """End sessions near Lyria's limit, and idle sessions of a replaced plan."""
        for deck in self.decks:
            replaced = deck.plan is not program.plan and not self.mixer.uses(deck)
            if deck.state is not State.ENDED and (deck.age >= SESSION_LIMIT_S or replaced):
                deck.retire()

    def _generate(self, program: Program) -> None:
        """Keep the program's two reserved sessions generating for the current plan."""
        if self._clock() < program.retry_at:
            return
        sessions = sum(deck.state is not State.ENDED for deck in self.decks)
        for _ in range(SESSIONS_PER_STATION - sessions):
            deck = Deck(self._connect, program.plan, clock=self._clock)
            deck.start()
            self.decks.append(deck)

    def _start(self, program: Program, player: Player | None) -> None:
        """Keep discord.py's player running, and fade in the first ready deck once it runs."""
        if player is None:
            return
        idle = self.mixer.live is None and not self.mixer.switching
        deck = self._ready_deck(program.plan) if idle else None
        if self._playing is None and (deck is not None or not idle):
            self._play(player)
        if deck is None or not self._audible(player):
            return
        self.mixer.switch_to(deck, FADE_IN_S)
        self._settle_playing(program)
        log.info("Program started on deck %d", deck.number)

    def _hand_over(self, program: Program, player: Player | None) -> None:
        """Crossfade to the next deck when the live one belongs to a replaced plan or runs low.

        Only while it can be heard, so a replacement is never reported as playing during a
        voice outage.
        """
        live = self.mixer.live
        if self.mixer.switching or not self._audible(player) or not isinstance(live, Deck):
            return
        if live.plan is program.plan and live.buffered_seconds >= HANDOVER_BELOW_S:
            return
        deck = self._ready_deck(program.plan, exclude=live)
        if deck is None:
            return
        self.mixer.switch_to(deck, CROSSFADE_S)
        live.retire()
        self._settle_playing(program)

    def _discard(self) -> None:
        """Count how sessions ended, then drop ended decks the mixer no longer uses, except the
        one ready for the current plan with the most audio, while no open deck is ready.

        A session that lasted until SobaFM retired it, at the session limit or at a handover,
        returns the program's backoff to its first step. That happens before the current plan's
        failures seen with it count, and they count in a fixed order: causes that end a start at
        once, then refusals, then other recognized causes, then the rest. So the order of the
        decks doesn't decide a start's outcome. An ended deck can't fill any further: one short
        of the pre-roll could never go live, and a handover needs only one ready deck.
        """
        seen = [d for d in self.decks if d.state is State.ENDED and d not in self._seen_ended]
        self._seen_ended.update(seen)
        plan = None if self.program is None else self.program.plan
        early = [
            d for d in seen if d.end_reason is not EndReason.RETIRED or not d.delivered_preroll
        ]
        lasted = [deck for deck in seen if deck not in early]
        if self.program is not None and any(deck.plan is plan for deck in lasted):
            self.program.failures = 0
        # Only the plan current now: a give-up can return to the program heard mid-loop.
        early = [deck for deck in early if deck.plan is plan]
        early.sort(
            key=lambda d: (
                d.failure not in FAILED_STARTS,
                d.end_reason is not EndReason.FILTERED,
                d.failure is None,
            )
        )
        for deck in early:
            self._count_failure(deck)
        plan = None if self.program is None else self.program.plan
        unused = [deck for deck in self.decks if not self.mixer.uses(deck)]
        ended = [deck for deck in unused if deck.state is State.ENDED]
        ready = [deck for deck in unused if deck.plan is plan and deck.ready]
        spare = None
        if all(deck.state is State.ENDED for deck in ready):
            spare = max(ready, key=lambda deck: deck.buffered_seconds, default=None)
        self.decks = [deck for deck in self.decks if deck not in ended or deck is spare]

    def _count_failure(self, deck: Deck) -> None:
        """Schedule a retry for a session that ended early, or give up.

        A session ends early when it fails, or Lyria ends it, before SobaFM retires it, or when
        it ends short of the pre-roll. Sessions that fail together count once, so both decks of a
        round share one retry, and a refusal counts once a round even when a failure in that round
        counted first. A cause that retrying can't help ends a start whichever deck reports it,
        unless another deck of its plan may still start it. A program that has started keeps
        retrying, because its buffers may outlast an outage.
        """
        program = self.program
        now = self._clock()
        if program is None or deck.plan is not program.plan:
            return
        starting = not program.started.done()
        outcome = START_OUTCOMES.get(deck.failure, Outcome.FAILED)
        if deck.failure in FAILED_STARTS and starting and not self._may_start(deck):
            log.warning("Could not open a Lyria RealTime session (%s): %s", outcome, deck.detail)
            self._give_up(outcome)
            return
        if deck.end_reason is EndReason.FILTERED:
            if now < program.refused_until:  # its round's other session was refused too
                return
            program.refusals += 1
            program.refused_until = program.retry_at = now + REFUSAL_RETRY_S
            if program.refusals >= 2 and starting:
                log.info("Lyria refused the request twice")
                self._give_up(Outcome.REFUSED)
            return
        if now < program.retry_at:
            return
        program.failures += 1
        program.retry_at = (
            now + CONNECT_BACKOFF_S[min(program.failures, len(CONNECT_BACKOFF_S)) - 1]
        )
        if program.failures > len(CONNECT_BACKOFF_S) and starting:
            log.warning("Could not open a Lyria RealTime session (%s): %s", outcome, deck.detail)
            self._give_up(outcome)

    def _may_start(self, failed: Deck) -> bool:
        """Whether another deck of the failed deck's plan is still open, or ready to go live."""
        return any(
            deck is not failed
            and deck.plan is failed.plan
            and (deck.state is not State.ENDED or deck.ready)
            for deck in self.decks
        )

    def _audible(self, player: Player | None) -> bool:
        """Whether a running player sends the mixer's audio to a connected voice channel."""
        return self._playing is not None and player is not None and player.is_connected()

    def _ready_deck(self, plan: MusicPlan, exclude: Deck | None = None) -> Deck | None:
        """The ready deck for `plan` with the most buffered audio."""
        ready = [d for d in self.decks if d is not exclude and d.ready and d.plan is plan]
        return max(ready, key=lambda deck: deck.buffered_seconds, default=None)

    def _play(self, player: Player) -> None:
        """Start discord.py's player and track it until its thread ends.

        A player thread that gives up on a slow voice reconnect ends without updating
        `is_playing()`, so the station relies on the `after` callback instead.
        """
        loop = asyncio.get_running_loop()
        playing = object()

        def stopped(error: Exception | None) -> None:  # runs on discord.py's player thread
            if error is not None:
                log.warning("Voice player stopped: %s", error)
            loop.call_soon_threadsafe(self._player_stopped, playing)

        if self._clock() < self._next_play:
            return
        self._next_play = self._clock() + PLAYER_RETRY_S
        if player.is_playing():
            player.stop()  # its thread has ended
        try:
            player.play(self.mixer, after=stopped, signal_type="music")
        except discord.ClientException as error:  # not connected yet
            log.debug("Could not start the voice player: %s", error)
            return
        except discord.DiscordException:  # such as an Opus encoder error, which retries cannot fix
            log.exception("Could not start the voice player; ending the program")
            self._finish(Outcome.FAILED)
            return
        self._playing = playing

    def _player_stopped(self, playing: object) -> None:
        if self._playing is playing:
            self._playing = None
        self.wake()
