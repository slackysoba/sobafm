"""One server's station facts: desired program, prepared audio and heard source.

Pure single-owner logic with no Discord, network, audio, persistence or clock. The
caller owns preparation work and the connected player; it reports their results
here with the `Program` they belong to. Results for a superseded program are
stale and change nothing. Only the player's report of an actual switch changes
the heard source, so a pending, failed or cancelled replacement never alters the
heard title or credits.

A catalog program continues until stopped or replaced. Its ranked plan is fixed
when `/play` interprets the request, and every later track comes from `select_next`
with that plan and the program's bounded no-repeat history; the request is never
interpreted again.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from sobafm.catalog import MAX_RECORDS_PER_SOURCE, Recording, RecordingKey, Source
from sobafm.credits import CreditRejected, RenderedCredits, render_credits

MAX_REQUEST_CHARACTERS = 200
# Every recording a valid snapshot can hold, so a program's history never forgets one
# it has played or tried and the program never repeats a recording.
HISTORY_LIMIT = len(Source) * MAX_RECORDS_PER_SOURCE


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    STALE = "stale"
    REJECTED = "rejected"


class EndReason(StrEnum):
    """Why a program has no further track."""

    EXHAUSTED = "exhausted"  # every recording in its plan was played or tried
    UNAVAILABLE = "unavailable"  # the next track could not be prepared within its bounds


@dataclass(frozen=True)
class Program:
    """An accepted request; its generation is unique and increasing per station."""

    generation: int
    request: str
    # Validated recordings for this request, best first. Later tracks come from here.
    plan: tuple[Recording, ...] = ()


@dataclass(frozen=True)
class Audio:
    """Prepared or heard audio together with the credits rendered at admission."""

    program: Program
    recording: Recording
    credits: RenderedCredits


@dataclass(frozen=True)
class Replacement:
    """A newly desired program and the unheard program whose work the caller cancels."""

    program: Program
    superseded: Program | None


@dataclass(eq=False)
class _Progress:
    """Per-program continuation facts, kept while the program is desired or heard."""

    history: deque[RecordingKey] = field(default_factory=lambda: deque(maxlen=HISTORY_LIMIT))
    started: bool = False  # some of its audio has been heard
    ending: EndReason | None = None  # no track follows the one heard


def select_next(
    plan: tuple[Recording, ...], history: frozenset[RecordingKey]
) -> Recording | None:
    """The best-ranked recording in ``plan`` that ``history`` does not hold."""
    return next((r for r in plan if r.key not in history), None)


class Station:
    """Settles the facts for one connected player; callers hold any resources."""

    def __init__(self) -> None:
        self._generation = 0
        self._desired: Program | None = None
        self._preparing = False
        self._ready: Audio | None = None
        self._switching: Audio | None = None
        self._heard: Audio | None = None
        self._progress: dict[int, _Progress] = {}
        self._ended: EndReason | None = None

    @property
    def desired(self) -> Program | None:
        return self._desired

    @property
    def preparing(self) -> bool:
        """Whether the desired program still awaits prepared audio."""
        return self._preparing

    @property
    def ready(self) -> Audio | None:
        """Prepared audio for the desired program not yet claimed by the player."""
        return self._ready

    @property
    def switching(self) -> Audio | None:
        """Audio the player has claimed but not yet reported as heard."""
        return self._switching

    @property
    def heard(self) -> Audio | None:
        """The source the connected player actually plays, with its credits."""
        return self._heard

    @property
    def ending(self) -> EndReason | None:
        """Why no track follows the heard one of the desired program, if none will."""
        progress = None if self._desired is None else self._progress.get(self._desired.generation)
        return None if progress is None else progress.ending

    @property
    def ended(self) -> EndReason | None:
        """Why the last program ended by itself, until a new request or stop."""
        return self._ended

    @property
    def continuing(self) -> bool:
        """Whether the desired program has already been heard (later tracks continue it)."""
        desired = self._desired
        progress = None if desired is None else self._progress.get(desired.generation)
        return progress is not None and progress.started

    @property
    def needs_next(self) -> bool:
        """Whether the desired, already heard program has no next track in progress."""
        return (
            self.continuing
            and self.ending is None
            and not self._preparing
            and self._ready is None
            and self._switching is None
        )

    def history(self, program: Program) -> frozenset[RecordingKey]:
        """Recordings ``program`` has prepared, played or tried."""
        return frozenset(self._state(program).history)

    def _state(self, program: Program) -> _Progress:
        return self._progress.setdefault(program.generation, _Progress())

    def _prune(self) -> None:
        """Keep progress only for programs still desired, heard or holding audio."""
        live = {
            a.program.generation
            for a in (self._ready, self._switching, self._heard)
            if a is not None
        }
        if self._desired is not None:
            live.add(self._desired.generation)
        for generation in self._progress.keys() - live:
            del self._progress[generation]

    def _pending(self) -> Program | None:
        """The desired program while its preparation or ready audio is caller-owned work."""
        return self._desired if self._preparing or self._ready is not None else None

    def _revert(self) -> None:
        """Abandon pending work; an unheard program yields to the audio still heard."""
        self._preparing = False
        self._ready = None
        if not self.continuing:
            self._desired = None if self._heard is None else self._heard.program
        self._prune()

    def _end(self, reason: EndReason) -> None:
        self._desired = None
        self._ended = reason
        self._prune()

    def request(self, request: str, plan: tuple[Recording, ...] = ()) -> Replacement:
        """Accept a new desired program; heard audio continues until a switch."""
        if type(request) is not str or not 1 <= len(request) <= MAX_REQUEST_CHARACTERS:
            raise ValueError("request must be 1 to 200 characters")
        superseded = self._pending()
        self._generation += 1
        self._desired = Program(self._generation, request, plan)
        self._preparing = True
        self._ready = None
        self._ended = None
        self._prune()
        return Replacement(self._desired, superseded)

    def continue_program(self, program: Program) -> Outcome:
        """Begin preparing the next track of the desired, already heard program."""
        if program != self._desired or not self.needs_next:
            return Outcome.STALE
        self._preparing = True
        return Outcome.ACCEPTED

    def prepared(self, program: Program, recording: Recording) -> Outcome:
        """Record prepared audio for the current program after rendering its credits.

        The recording joins the program's history either way, so it is not tried
        again. Credits that cannot be rendered completely and safely reject the
        candidate; the caller may try another or report failure.
        """
        if not self._preparing or program != self._desired:
            return Outcome.STALE
        self._state(program).history.append(recording.key)
        try:
            credits = render_credits(recording)
        except CreditRejected:
            return Outcome.REJECTED
        self._preparing = False
        self._ready = Audio(program, recording, credits)
        return Outcome.ACCEPTED

    def preparation_failed(self, program: Program) -> Outcome:
        """Abandon a failed or cancelled preparation; heard audio and credits remain."""
        if not self._preparing or program != self._desired:
            return Outcome.STALE
        self._revert()
        return Outcome.ACCEPTED

    def exhausted(self, program: Program, reason: EndReason) -> Outcome:
        """No next track can be prepared: the program ends once its heard track drains.

        History is kept, so the program never repeats a recording to continue.
        """
        if program != self._desired or not self.continuing:
            return Outcome.STALE
        self._preparing = False
        self._ready = None
        self._state(program).ending = reason
        if self._heard is None or self._heard.program != program:
            self._end(reason)
        return Outcome.ACCEPTED

    def claim_ready(self) -> Audio | None:
        """Hand the ready audio to the player, which then reports switch or failure."""
        if self._ready is None or self._switching is not None:
            return None
        self._switching, self._ready = self._ready, None
        return self._switching

    def switched(self, source: Audio) -> Outcome:
        """The player actually switched to claimed audio; it becomes the heard source.

        A newer desired program that arrived during the switch is left unchanged.
        """
        if source != self._switching:
            return Outcome.STALE
        self._switching = None
        self._heard = source
        self._state(source.program).started = True
        self._prune()
        return Outcome.ACCEPTED

    def switch_failed(self, source: Audio) -> Outcome:
        """The player could not switch to claimed audio; heard audio is unchanged."""
        if source != self._switching:
            return Outcome.STALE
        self._switching = None
        if source.program == self._desired:
            self._revert()
        self._prune()
        return Outcome.ACCEPTED

    def drained(self, source: Audio) -> Outcome:
        """The heard track played to its verified end and the player emits nothing of it.

        Its program continues with a next track, or ends if none will follow.
        """
        if source != self._heard:
            return Outcome.STALE
        self._heard = None
        ending = self._state(source.program).ending
        if source.program == self._desired and ending is not None:
            self._end(ending)
        self._prune()
        return Outcome.ACCEPTED

    def stop(self) -> Program | None:
        """End the desired program and return an unheard one to cancel.

        The heard source changes only when the player reports `silenced`.
        """
        superseded = self._pending()
        self._desired = None
        self._preparing = False
        self._ready = None
        self._ended = None
        self._prune()
        return superseded

    def silenced(self) -> None:
        """The player actually stopped emitting audio."""
        self._heard = None
        self._prune()
