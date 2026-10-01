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


class Player(Protocol):
    """The part of discord.py's voice client a station uses."""

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
    BUSY = "busy"  # no Lyria session capacity left in the process
    REFUSED = "refused"  # Lyria refused the prompt twice
    FAILED = "failed"  # sessions could not be opened, or the voice connection was lost
    REPLACED = "replaced"  # a newer request replaced it before it started
    STOPPED = "stopped"


class SessionPool:
    """Caps concurrent Lyria RealTime sessions across every station in the process."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._decks: set[Deck] = set()

    def open(self, connect: Connect, plan: MusicPlan, clock: Callable[[], float]) -> Deck | None:
        self._decks = {deck for deck in self._decks if deck.state is not State.ENDED}
        if len(self._decks) >= self.limit:
            return None
        deck = Deck(connect, plan, clock=clock)
        deck.start()
        self._decks.add(deck)
        return deck


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
    """Plays one server's program. Only `reconcile()` creates, switches, or closes decks."""

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
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def play(self, plan: MusicPlan, requester: str) -> asyncio.Future[Outcome]:
        """Start or replace the program; the result resolves once it plays or fails."""
        if self.program is not None:
            self.program.settle(Outcome.REPLACED)
        self.program = Program(plan, requester)
        self.wake()
        return self.program.started

    def stop(self) -> None:
        if self.program is not None:
            self.program.settle(Outcome.STOPPED)
            self.program = None
        self.wake()

    def start(self) -> None:
        """Run the reconcile loop in the background until `close()`."""
        self._task = asyncio.create_task(self._run(), name="station")

    def wake(self) -> None:
        """Reconcile now rather than at the next tick."""
        self._wake.set()

    async def close(self) -> None:
        """Stop at once: end the program, retire every deck, and stop the loop."""
        self.stop()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for deck in self.decks:
            deck.retire()
        await asyncio.gather(*(deck.wait_ended() for deck in self.decks))
        self.decks.clear()
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
            self._finish(Outcome.FAILED)
        self._discard()
        if self.program is not None:
            self._retire(self.program)
            self._generate(self.program)
        if self.program is not None:
            self._start(self.program, player)
            self._hand_over(self.program)
        else:
            self._wind_down(player)
        await asyncio.gather(*(deck.regulate() for deck in self.decks))

    def _finish(self, outcome: Outcome) -> None:
        if self.program is not None:
            self.program.settle(outcome)
            self.program = None

    def _wind_down(self, player: Player | None) -> None:
        """Without a program: drop idle decks, fade out, then stop the player."""
        for deck in self.decks:
            if not self.mixer.uses(deck):
                deck.retire()
        if self.mixer.switching:
            return
        if self.mixer.live is not None:
            self.mixer.switch_to(None, FADE_OUT_S)
        elif player is not None and player.is_playing():
            player.stop()

    def _retire(self, program: Program) -> None:
        """End sessions near Lyria's limit, and idle sessions of a replaced plan."""
        for deck in self.decks:
            replaced = deck.plan is not program.plan and not self.mixer.uses(deck)
            if deck.state is not State.ENDED and (deck.age >= SESSION_LIMIT_S or replaced):
                deck.retire()

    def _generate(self, program: Program) -> None:
        """Keep the live and next decks for the current plan generating."""
        if self._clock() < program.retry_at:
            return
        sessions = sum(deck.state is not State.ENDED for deck in self.decks)
        current = sum(
            deck.plan is program.plan and deck.state is not State.ENDED for deck in self.decks
        )
        while current < SESSIONS_PER_STATION and sessions < SESSIONS_PER_STATION:
            deck = self._pool.open(self._connect, program.plan, self._clock)
            if deck is None:
                if not any(d.plan is program.plan for d in self.decks):
                    log.info("No Lyria RealTime session capacity left")
                    self._finish(Outcome.BUSY)
                return
            self.decks.append(deck)
            current += 1
            sessions += 1

    def _start(self, program: Program, player: Player | None) -> None:
        """Fade in the first ready deck, and restart the player if discord.py stopped it."""
        if player is None:
            return
        if self.mixer.live is None and not self.mixer.switching:
            deck = self._ready_deck(program.plan)
            if deck is None:
                return
            self._play(player)
            self.mixer.switch_to(deck, FADE_IN_S)
            program.settle(Outcome.PLAYING)
            log.info("Playing %r for %s", program.plan.title, program.requester)
        elif not player.is_playing():
            self._play(player)

    def _hand_over(self, program: Program) -> None:
        """Crossfade to the next deck when the live one belongs to a replaced plan or runs low."""
        live = self.mixer.live
        if self.mixer.switching or not isinstance(live, Deck):
            return
        replaced = live.plan is not program.plan
        if not replaced and live.buffered_seconds >= HANDOVER_BELOW_S:
            return
        deck = self._ready_deck(program.plan, exclude=live)
        if deck is None and not replaced:
            deck = self._ready_deck(None, exclude=live)
        if deck is None:
            return
        self.mixer.switch_to(deck, CROSSFADE_S)
        live.retire()
        if deck.plan is program.plan:
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
        """
        program = self.program
        now = self._clock()
        if program is None or deck.plan is not program.plan or now < program.retry_at:
            return
        if deck.end_reason is EndReason.FILTERED:
            program.refusals += 1
            program.retry_at = now + REFUSAL_RETRY_S
            if program.refusals >= 2 and not program.started.done():
                log.info("Lyria refused %r: %s", program.plan.title, deck.detail)
                self._finish(Outcome.REFUSED)
            return
        program.failures += 1
        program.retry_at = (
            now + CONNECT_BACKOFF_S[min(program.failures, len(CONNECT_BACKOFF_S)) - 1]
        )
        if program.failures > len(CONNECT_BACKOFF_S) and not program.started.done():
            log.warning("Could not open a Lyria RealTime session for %r", program.plan.title)
            self._finish(Outcome.FAILED)

    def _ready_deck(self, plan: MusicPlan | None, exclude: Deck | None = None) -> Deck | None:
        """The ready deck with the most buffered audio, for `plan` or any plan."""
        ready = [
            deck
            for deck in self.decks
            if deck is not exclude and deck.ready and (plan is None or deck.plan is plan)
        ]
        return max(ready, key=lambda deck: deck.buffered_seconds, default=None)

    def _play(self, player: Player) -> None:
        loop = asyncio.get_running_loop()

        def stopped(error: Exception | None) -> None:  # runs on discord.py's player thread
            if error is not None:
                log.warning("Voice player stopped: %s", error)
            loop.call_soon_threadsafe(self.wake)

        player.play(self.mixer, after=stopped, signal_type="music")
