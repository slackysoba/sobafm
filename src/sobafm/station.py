"""A station: one server's program, decks, and mixer, run by a reconcile loop (ADR-0004)."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

import discord

from sobafm.deck import Connect, Deck, EndReason, State
from sobafm.mixer import Mixer
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
    FAILED = "failed"  # sessions could not be opened, or the player could not start
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
    started: asyncio.Future[Outcome] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    refusals: int = 0
    failures: int = 0
    retry_at: float = 0.0

    def settle(self, outcome: Outcome) -> None:
        if not self.started.done():
            self.started.set_result(outcome)


class Station:
    """Plays one server's program. Only `reconcile()` and `close()` change decks."""

    def __init__(
        self,
        voice: Callable[[], Player | None],
        connect: Connect,
        pool: SessionPool,
        *,
        volume: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.mixer = Mixer(volume)
        self.program: Program | None = None
        self.decks: list[Deck] = []
        self._voice = voice
        self._connect = connect
        self._pool = pool
        self._clock = clock
        self._playing: object | None = None  # identifies the running player, if any
        self._next_play = 0.0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def play(self, plan: MusicPlan, requester: str) -> asyncio.Future[Outcome]:
        """Start or replace the program; the result resolves once it plays or fails."""
        program = Program(plan, requester)
        if not self._pool.reserve(self):
            log.info("No Lyria RealTime session capacity left")
            program.settle(Outcome.BUSY)
            return program.started
        if self.program is not None:
            self.program.settle(Outcome.REPLACED)
        self.program = program
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
        """Stop at once: end the program with `outcome`, retire every deck, and stop the loop."""
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
        player = self._voice()
        if self.program is not None and player is None:
            log.info("Voice connection lost; ending the program")
            self._finish(Outcome.DISCONNECTED)
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

    def _finish(self, outcome: Outcome) -> None:
        if self.program is not None:
            self.program.settle(outcome)
            self.program = None

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
        program.settle(Outcome.PLAYING)
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
        program.settle(Outcome.PLAYING)

    def _discard(self) -> None:
        """Drop ended decks the mixer no longer uses once they are empty or replaced."""
        keep: list[Deck] = []
        for deck in self.decks:
            if deck.state is State.ENDED and not self.mixer.uses(deck):
                if not deck.has_audio:
                    self._count_failure(deck)
                replaced = self.program is None or deck.plan is not self.program.plan
                if replaced or not deck.frames:
                    continue
            keep.append(deck)
        self.decks = keep

    def _count_failure(self, deck: Deck) -> None:
        """Schedule a retry for a session that ended without audio, or give up.

        Sessions that fail together count once, so both decks of a round share one retry.
        A program that has started keeps retrying, because its buffers may outlast an outage.
        """
        program = self.program
        now = self._clock()
        if program is None or deck.plan is not program.plan or now < program.retry_at:
            return
        if deck.end_reason is EndReason.FILTERED:
            program.refusals += 1
            program.retry_at = now + REFUSAL_RETRY_S
            if program.refusals >= 2 and not program.started.done():
                log.info("Lyria refused the request twice: %s", deck.detail)
                self._finish(Outcome.REFUSED)
            return
        program.failures += 1
        program.retry_at = (
            now + CONNECT_BACKOFF_S[min(program.failures, len(CONNECT_BACKOFF_S)) - 1]
        )
        if program.failures > len(CONNECT_BACKOFF_S) and not program.started.done():
            log.warning("Could not open a Lyria RealTime session: %s", deck.detail)
            self._finish(Outcome.FAILED)

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
