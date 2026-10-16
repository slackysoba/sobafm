"""Lyria-specific deck lifecycle and process-wide session reservations (ADR-0004)."""

import asyncio
import logging
import time
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING

from sobafm.deck import Connect, Deck, EndReason, State
from sobafm.failures import Failure
from sobafm.mixer import Mixer
from sobafm.plan import MusicPlan
from sobafm.program import Outcome, Program

if TYPE_CHECKING:
    from sobafm.station import Player

# Keep the operational log identity when moving the rules out of station.py.
log = logging.getLogger("sobafm.station")

SESSIONS_PER_STATION = 2  # the live deck's and the next deck's
SESSION_LIMIT_S = 540.0  # Lyria closes sessions at 600 s
REFUSAL_RETRY_S = 30.0
CONNECT_BACKOFF_S = (2.0, 5.0, 15.0)

# What a start that fails for each cause reports; any other cause reports FAILED
START_OUTCOMES: dict[Failure | None, Outcome] = {
    Failure.REJECTED: Outcome.REJECTED,
    Failure.EXHAUSTED: Outcome.EXHAUSTED,
    Failure.UNAVAILABLE: Outcome.UNAVAILABLE,
}
# A start that fails for these causes fails at once, since retrying within seconds can't help.
FAILED_STARTS = {Failure.REJECTED, Failure.EXHAUSTED}


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


class LyriaEngine:
    """Owns one station's Lyria decks and its reservation from the shared pool."""

    def __init__(
        self, connect: Connect, pool: SessionPool, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.decks: list[Deck] = []
        # Decks seen to have ended, so each end counts once; weak, so a dropped deck is freed
        self._seen_ended: weakref.WeakSet[Deck] = weakref.WeakSet()
        self._connect = connect
        self._pool = pool
        self._clock = clock

    def reserve(self, holder: object) -> bool:
        """Reserve two sessions using the station's existing reservation identity."""
        return self._pool.reserve(holder)

    def release_if_ended(self, holder: object) -> None:
        """Release an idle station's reservation only after its sessions have ended."""
        if all(deck.state is State.ENDED for deck in self.decks):
            self._pool.release(holder)

    async def close(self, holder: object) -> None:
        """Retire every producer and release the reservation after cleanup."""
        for deck in self.decks:
            deck.retire()
        await asyncio.gather(*(deck.wait_ended() for deck in self.decks))
        self.decks.clear()
        self._pool.release(holder)

    @staticmethod
    def retire(deck: Deck, *, replaced: bool) -> None:
        """End a session near Lyria's limit, or an idle session the station replaced."""
        if deck.state is not State.ENDED and (deck.age >= SESSION_LIMIT_S or replaced):
            deck.retire()

    def generate(self, program: Program) -> None:
        """Keep the program's two reserved sessions generating for the current plan."""
        if self._clock() < program.retry_at:
            return
        sessions = sum(deck.state is not State.ENDED for deck in self.decks)
        for _ in range(SESSIONS_PER_STATION - sessions):
            deck = Deck(self._connect, program.plan, clock=self._clock)
            deck.start()
            self.decks.append(deck)

    def ended_failures(self, program: Program | None) -> list[Deck]:
        """Reset backoff for sessions that lasted, then order newly ended early sessions.

        A session that lasted until SobaFM retired it, at the session limit or at a handover,
        returns the program's backoff to its first step. That happens before the current plan's
        failures seen with it count, and they count in a fixed order: causes that end a start at
        once, then refusals, then other recognized causes, then the rest. So the order of the
        decks doesn't decide a start's outcome, except between a rejected key and the quota.
        The station counts each failure against the program current at that point, applying
        any give-up before counting the next one or dropping ended decks.
        """
        seen = [d for d in self.decks if d.state is State.ENDED and d not in self._seen_ended]
        self._seen_ended.update(seen)
        plan = None if program is None else program.plan
        early = [
            d for d in seen if d.end_reason is not EndReason.RETIRED or not d.delivered_preroll
        ]
        lasted = [deck for deck in seen if deck not in early]
        if program is not None and any(deck.plan is plan for deck in lasted):
            program.failures = 0
        early = [deck for deck in early if deck.plan is plan]
        early.sort(
            key=lambda d: (
                d.failure not in FAILED_STARTS,
                d.end_reason is not EndReason.FILTERED,
                d.failure is None,
            )
        )
        return early

    def drop_ended(self, program: Program | None, mixer: Mixer) -> None:
        """Drop unused ended decks, keeping one ready spare until an open deck is ready.

        An ended deck can't fill any further: one short of the pre-roll could never go live,
        and a handover needs only one ready deck.
        """
        plan = None if program is None else program.plan
        unused = [deck for deck in self.decks if not mixer.uses(deck)]
        ended = [deck for deck in unused if deck.state is State.ENDED]
        ready = [deck for deck in unused if deck.plan is plan and deck.ready]
        spare = None
        if all(deck.state is State.ENDED for deck in ready):
            spare = max(ready, key=lambda deck: deck.buffered_seconds, default=None)
        self.decks = [deck for deck in self.decks if deck not in ended or deck is spare]

    def count_failure(
        self, deck: Deck, program: Program | None, player: Player | None
    ) -> Outcome | None:
        """Schedule a retry for a session that ended early, or report a start give-up.

        A session ends early when it fails, or Lyria ends it, before SobaFM retires it, or when
        it ends short of the pre-roll. Sessions that fail together count once, so both decks of a
        round share one retry, and a refusal counts once a round even when a failure in that round
        counted first. A cause that retrying can't help ends a start whichever deck reports it, and
        so do a second refusal and the last retry's failure, unless another deck of its plan may
        still start it. Then the start waits, and the next deck of its plan to fail ends it, with
        the cause that retrying can't help if one was seen. A program that has started keeps
        retrying, because its buffers may outlast an outage.
        """
        now = self._clock()
        if program is None or deck.plan is not program.plan:
            return None
        starting = not program.started.done()
        outcome = START_OUTCOMES.get(deck.failure, Outcome.FAILED)
        if deck.failure in FAILED_STARTS and starting and not self._may_start(deck, player):
            log.warning("Could not open a Lyria RealTime session (%s): %s", outcome, deck.detail)
            return outcome
        if deck.end_reason is EndReason.FILTERED:
            if now >= program.refused_until:  # else its round's other session was refused too
                program.refusals += 1
                program.refused_until = program.retry_at = now + REFUSAL_RETRY_S
            # A later refusal re-checks, so a start that waited gives up once no deck can start it.
            if program.refusals >= 2 and starting and not self._may_start(deck, player):
                log.info("Lyria refused the request twice")
                return Outcome.REFUSED
            return None
        if now >= program.retry_at:
            program.failures += 1
            program.retry_at = (
                now + CONNECT_BACKOFF_S[min(program.failures, len(CONNECT_BACKOFF_S)) - 1]
            )
        elif program.failures <= len(CONNECT_BACKOFF_S) or not starting:
            # Its round's other session failed first; a start that waits re-checks below.
            return None
        if program.failures <= len(CONNECT_BACKOFF_S) or not starting:
            return None
        if self._may_start(deck, player):
            # Keep the cause that retrying can't help over a plain outage.
            if program.deferred is None or deck.failure in FAILED_STARTS:
                program.deferred = outcome
            return None
        outcome = program.deferred or outcome
        log.warning("Could not open a Lyria RealTime session (%s): %s", outcome, deck.detail)
        return outcome

    def _may_start(self, failed: Deck, player: Player | None) -> bool:
        """Whether another deck of the failed deck's plan is still open, or ready to go live.

        An ended deck with audio can go live only while the voice is connected, so a start that
        can't be heard gives up instead of waiting for it.
        """
        voice = player is not None and player.is_connected()
        return any(
            deck is not failed
            and deck.plan is failed.plan
            and (deck.state is not State.ENDED or (voice and deck.ready))
            for deck in self.decks
        )

    def ready_deck(self, plan: MusicPlan, exclude: Deck | None = None) -> Deck | None:
        """The ready deck for `plan` with the most buffered audio."""
        ready = [d for d in self.decks if d is not exclude and d.ready and d.plan is plan]
        return max(ready, key=lambda deck: deck.buffered_seconds, default=None)
