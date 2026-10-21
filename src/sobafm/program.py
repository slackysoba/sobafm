"""A station's requested program, its start result, and playback accounting."""

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum

from sobafm.catalog_interpreter import CatalogPlan
from sobafm.plan import MusicPlan


class Mode(StrEnum):
    GENERATED = "generated"
    CATALOG = "catalog"


class Outcome(StrEnum):
    PLAYING = "playing"
    BUSY = "busy"  # every program's worth of Lyria sessions is taken
    REFUSED = "refused"  # Lyria refused the prompt twice
    REJECTED = "rejected"  # Google rejected the API key
    EXHAUSTED = "exhausted"  # a quota or rate limit
    UNAVAILABLE = "unavailable"  # Lyria RealTime is down or unreachable
    FAILED = "failed"  # sessions failed for another reason, or the player could not start
    NO_MATCH = "no_match"  # the bounded catalog cycle found no eligible non-recent track
    DISCONNECTED = "disconnected"  # SobaFM lost its voice connection
    REPLACED = "replaced"  # a newer request replaced it before it started
    STOPPED = "stopped"


@dataclass(eq=False)
class Program:
    plan: MusicPlan | CatalogPlan
    requester: str
    ends_at: float | None  # on the station's clock; catalog has no deadline
    started: asyncio.Future[Outcome] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    underruns: int = 0  # frames of silence while it played

    def __post_init__(self) -> None:
        if (self.mode is Mode.CATALOG) != (self.ends_at is None):
            raise ValueError("only generated programs have a deadline")

    @property
    def mode(self) -> Mode:
        return Mode.GENERATED if isinstance(self.plan, MusicPlan) else Mode.CATALOG

    @property
    def playing(self) -> bool:
        return self.started.done() and self.started.result() is Outcome.PLAYING

    def settle(self, outcome: Outcome) -> None:
        if not self.started.done():
            self.started.set_result(outcome)
