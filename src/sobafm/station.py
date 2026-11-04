"""One server's station facts: desired program, prepared audio and heard source.

Pure single-owner logic with no Discord, network, audio, persistence or clock. The
caller owns preparation work and the connected player; it reports their results
here with the `Program` they belong to. Results for a superseded program are
stale and change nothing. Only the player's report of an actual switch changes
the heard source, so a pending, failed or cancelled replacement never alters the
heard title or credits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sobafm.catalog import Recording
from sobafm.credits import CreditRejected, RenderedCredits, render_credits

MAX_REQUEST_CHARACTERS = 200


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    STALE = "stale"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Program:
    """An accepted request; its generation is unique and increasing per station."""

    generation: int
    request: str


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


class Station:
    """Settles the facts for one connected player; callers hold any resources."""

    def __init__(self) -> None:
        self._generation = 0
        self._desired: Program | None = None
        self._preparing = False
        self._ready: Audio | None = None
        self._switching: Audio | None = None
        self._heard: Audio | None = None

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

    def _pending(self) -> Program | None:
        """The desired program while its preparation or ready audio is caller-owned work."""
        return self._desired if self._preparing or self._ready is not None else None

    def _revert(self) -> None:
        """Abandon the desired program; audio still heard remains the program."""
        self._preparing = False
        self._ready = None
        self._desired = None if self._heard is None else self._heard.program

    def request(self, request: str) -> Replacement:
        """Accept a new desired program; heard audio continues until a switch."""
        if type(request) is not str or not 1 <= len(request) <= MAX_REQUEST_CHARACTERS:
            raise ValueError("request must be 1 to 200 characters")
        superseded = self._pending()
        self._generation += 1
        self._desired = Program(self._generation, request)
        self._preparing = True
        self._ready = None
        return Replacement(self._desired, superseded)

    def prepared(self, program: Program, recording: Recording) -> Outcome:
        """Record prepared audio for the current program after rendering its credits.

        Credits that cannot be rendered completely and safely reject the candidate
        and change nothing; the caller may try another or report failure.
        """
        if not self._preparing or program != self._desired:
            return Outcome.STALE
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
        return Outcome.ACCEPTED

    def switch_failed(self, source: Audio) -> Outcome:
        """The player could not switch to claimed audio; heard audio is unchanged."""
        if source != self._switching:
            return Outcome.STALE
        self._switching = None
        if source.program == self._desired:
            self._revert()
        return Outcome.ACCEPTED

    def stop(self) -> Program | None:
        """End the desired program and return an unheard one to cancel.

        The heard source changes only when the player reports `silenced`.
        """
        superseded = self._pending()
        self._desired = None
        self._preparing = False
        self._ready = None
        return superseded

    def silenced(self) -> None:
        """The player actually stopped emitting audio."""
        self._heard = None
