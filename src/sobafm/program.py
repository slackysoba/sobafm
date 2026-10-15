"""A station's requested program, its start result, and playback accounting."""

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum

from sobafm.plan import MusicPlan


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
    deferred: Outcome | None = None  # the cause of a last-round failure that waits for a deck
    underruns: int = 0  # frames of silence while it played

    @property
    def playing(self) -> bool:
        return self.started.done() and self.started.result() is Outcome.PLAYING

    def settle(self, outcome: Outcome) -> None:
        if not self.started.done():
            self.started.set_result(outcome)
