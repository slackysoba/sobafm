"""One server's station facts: desired program, prepared audio and heard source.

Pure single-owner logic with no Discord, network, audio, persistence or clock. The
caller owns preparation work and the connected player; it reports their results
here with the `Program` they belong to. Results for a superseded program are
stale and change nothing. Only the player's report of an actual switch changes
the heard source, so a pending, failed or cancelled replacement never alters the
heard title or credits.

A catalog program continues until stopped, replaced or out of recordings. Its plan
starts as the list `/play` obtained, and `refilled` replaces it once every recording
on it has been played or tried. Every later track comes from `select_next` with the
current plan and the program's bounded no-repeat history (ADR 0011).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from sobafm.catalog import MAX_RECORDS_PER_SOURCE, Recording, RecordingKey, Source
from sobafm.credits import CreditRejected, RenderedCredits, render_credits

if TYPE_CHECKING:
    from sobafm.freestyle_plan import GeneratedPlan

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

    EXHAUSTED = "exhausted"  # every admitted recording was played or tried
    REFILL_FAILED = "refill_failed"  # its plan ran out and no more arrived within the bound
    UNAVAILABLE = "unavailable"  # the next track could not be prepared within its bounds
    CAPACITY_WAIT = "capacity_wait"  # the silent catalog program waited its full bank timeout
    DURATION = "duration"
    FILTERED = "filtered"
    GENERATION_REJECTED = "generation_rejected"
    GENERATION_ACCESS_DENIED = "generation_access_denied"
    GENERATION_RATE_LIMITED = "generation_rate_limited"
    GENERATION_SERVICE_ERROR = "generation_service_error"
    CAPACITY = "capacity"
    VOICE_START = "voice_start"
    MEDIA_MISSING = "media_missing"
    RECORDING_START = "recording_start"
    GENERATION_FAILED = "generation_failed"
    NO_LISTENERS = "no_listeners"  # the listener grace period expired


@dataclass(frozen=True)
class FailedStart:
    command: Literal["/play", "/freestyle"]
    reason: EndReason


@dataclass(frozen=True, eq=False)
class Program:
    """An accepted request; its generation is unique and increasing per station."""

    generation: int
    request: str
    deadline: float | None = None  # generated duration, counted from acceptance


@dataclass(frozen=True)
class Audio:
    """Prepared or heard audio together with the credits rendered at admission."""

    program: Program
    recording: Recording
    credits: RenderedCredits


@dataclass(frozen=True)
class GeneratedAudio:
    program: Program
    plan: GeneratedPlan


type StationAudio = Audio | GeneratedAudio


@dataclass(frozen=True)
class Replacement:
    """A newly desired program and the unheard program whose work the caller cancels."""

    program: Program
    superseded: Program | None


@dataclass(eq=False)
class _Progress:
    """Per-program continuation facts, kept while the program is desired or heard."""

    # Validated recordings for its request, best first. Later tracks come from here.
    plan: tuple[Recording, ...] = ()
    history: deque[RecordingKey] = field(default_factory=lambda: deque(maxlen=HISTORY_LIMIT))
    started: bool = False  # some of its audio has been heard
    ending: EndReason | None = None  # no track follows the one heard


def select_next(plan: tuple[Recording, ...], history: frozenset[RecordingKey]) -> Recording | None:
    """The best-ranked recording in ``plan`` that ``history`` does not hold."""
    return next((r for r in plan if r.key not in history), None)


class Station:
    """Settles the facts for one connected player; callers hold any resources."""

    def __init__(self) -> None:
        self._generation = 0
        self._desired: Program | None = None
        self._preparing = False
        self._ready: StationAudio | None = None
        self._switching: StationAudio | None = None
        self._heard: StationAudio | None = None
        self._progress: dict[int, _Progress] = {}
        self._ended: EndReason | None = None
        self._failed_start: FailedStart | None = None

    @property
    def desired(self) -> Program | None:
        return self._desired

    @property
    def preparing(self) -> bool:
        """Whether the desired program still awaits prepared audio."""
        return self._preparing

    @property
    def ready(self) -> StationAudio | None:
        """Prepared audio for the desired program not yet claimed by the player."""
        return self._ready

    @property
    def switching(self) -> StationAudio | None:
        """Audio the player has claimed but not yet reported as heard."""
        return self._switching

    @property
    def heard(self) -> StationAudio | None:
        """The source the connected player actually plays, with its credits."""
        return self._heard

    @property
    def ending(self) -> EndReason | None:
        """Why no track follows the heard one of the desired program, if none will."""
        progress = None if self._desired is None else self._progress.get(self._desired.generation)
        return None if progress is None else progress.ending

    @property
    def ended(self) -> EndReason | None:
        """Why the last program ended without a command, until a new request or stop."""
        return self._ended

    @property
    def failed_start(self) -> FailedStart | None:
        """An accepted request that failed before its first frame; no request text is kept."""
        return self._failed_start

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
            self._desired is not None
            and self._desired.deadline is None
            and self.continuing
            and self.ending is None
            and not self._preparing
            and self._ready is None
            and self._switching is None
        )

    def plan(self, program: Program) -> tuple[Recording, ...]:
        """The recordings ``program`` currently chooses from, best first."""
        return self._state(program).plan

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

    def intent(self, request: str, *, deadline: float | None = None) -> Program:
        """Identify a request while interpretation still may decline it."""
        if type(request) is not str or not 1 <= len(request) <= MAX_REQUEST_CHARACTERS:
            raise ValueError("request must be 1 to 200 characters")
        self._generation += 1
        return Program(self._generation, request, deadline)

    def request(self, request: str, plan: tuple[Recording, ...] = ()) -> Replacement:
        return self.accept(self.intent(request), plan)

    def accept(self, program: Program, plan: tuple[Recording, ...] = ()) -> Replacement:
        """Accept a validated intent. Heard audio continues until a switch."""
        superseded = self._pending()
        self._desired = program
        self._state(program).plan = plan
        self._preparing = True
        self._ready = None
        self._ended = None
        self._failed_start = None
        self._prune()
        return Replacement(program, superseded)

    def refilled(self, program: Program, recordings: tuple[Recording, ...]) -> Outcome:
        """Replace the plan of the desired, already heard program that awaits a next track.

        A recording its history holds doesn't count, so the program never repeats one.
        With nothing new left, the plan is unchanged and the result is `REJECTED`.
        """
        if program != self._desired or not self.needs_next:
            return Outcome.STALE
        state = self._state(program)
        fresh = tuple(r for r in recordings if r.key not in state.history)
        if not fresh:
            return Outcome.REJECTED
        state.plan = fresh
        return Outcome.ACCEPTED

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

    def generated(self, program: Program, plan: GeneratedPlan) -> Outcome:
        """Offer the validated plan. Only the callback can report its first audio."""
        if program is not self._desired or not self._preparing or program.deadline is None:
            return Outcome.STALE
        self._ready = GeneratedAudio(program, plan)
        self._preparing = False
        return Outcome.ACCEPTED

    def end_generated(self, program: Program, reason: EndReason) -> None:
        """The caller removed this program's audio. Preserve any other live program."""
        if self._switching is not None and self._switching.program is program:
            self._switching = None
        if self._heard is not None and self._heard.program is program:
            self._heard = None
        if self._desired is program:
            self._preparing = False
            self._ready = None
            self._desired = self._heard.program if self._heard is not None else None
            self._ended = reason
        self._prune()

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

    def claim_ready(self) -> StationAudio | None:
        """Hand the ready audio to the player, which then reports switch or failure."""
        if self._ready is None or self._switching is not None:
            return None
        self._switching, self._ready = self._ready, None
        return self._switching

    def switched(self, source: StationAudio) -> Outcome:
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

    def switch_failed(self, source: StationAudio, reason: EndReason | None = None) -> Outcome:
        """The player could not switch to claimed audio; heard audio is unchanged."""
        if source is not self._switching:
            return Outcome.STALE
        self._switching = None
        if source.program == self._desired:
            if reason is not None and not self.continuing:
                self._failed_start = FailedStart(
                    "/freestyle" if isinstance(source, GeneratedAudio) else "/play", reason
                )
            self._revert()
        self._prune()
        return Outcome.ACCEPTED

    def drained(self, source: StationAudio) -> Outcome:
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

    def stop(self, ended: EndReason | None = None) -> Program | None:
        """End the desired program and return an unheard one to cancel.

        ``ended`` records why, when the program ended without a command (its listener
        grace period expired). The heard source changes only when the player reports
        `silenced`.
        """
        superseded = self._pending()
        self._desired = None
        self._preparing = False
        self._ready = None
        self._ended = ended
        self._failed_start = None
        self._prune()
        return superseded

    def silenced(self) -> None:
        """The player actually stopped emitting audio."""
        self._heard = None
        self._prune()
