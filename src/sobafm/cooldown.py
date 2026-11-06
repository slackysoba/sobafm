"""One server's change cooldown: which request holds it and whether it still blocks.

Pure single-owner logic on the event loop, with no clock, settings or Discord: the
caller supplies the time and the configured length. The length is read on every
check, so a changed setting applies at once, including to a cooldown already running.

Each accepted request holds its own `Claim`. A request that fails or changes nothing
gives back only that claim, so a late report from an older request can't free the
cooldown a newer request now holds.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(eq=False, slots=True)
class Claim:
    """One request's hold on the cooldown; its identity makes a release its own."""

    claimed_at: float


class Cooldown:
    def __init__(self) -> None:
        self._holder: Claim | None = None

    def remaining(self, now: float, seconds: int) -> float:
        """Seconds until a request may claim, or 0 when one may now."""
        if self._holder is None:
            return 0.0
        return max(0.0, self._holder.claimed_at + seconds - now)

    def claim(self, now: float, seconds: int) -> Claim | None:
        """Take the cooldown for one request, or return None while it still blocks.

        A refused request changes nothing, so it never extends the wait.
        """
        if self.remaining(now, seconds) > 0:
            return None
        self._holder = Claim(now)
        return self._holder

    def release(self, claim: Claim | None) -> None:
        """Free the cooldown only if ``claim`` still holds it; otherwise nothing changes."""
        if claim is not None and self._holder is claim:
            self._holder = None
