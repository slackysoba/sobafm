"""Buffered source contract shared by playback engines (ADR-0006)."""

import itertools
from collections import deque
from enum import StrEnum
from typing import Protocol

from sobafm.program import Mode, Program

_numbers = itertools.count(1)


def next_source_number() -> int:
    """One diagnostic sequence across generated and catalog deck summaries."""
    return next(_numbers)


class State(StrEnum):
    CONNECTING = "connecting"
    GENERATING = "generating"
    ENDED = "ended"  # producer/cleanup ended; playable PCM may remain


class Source(Protocol):
    @property
    def frames(self) -> deque[bytes]: ...

    @property
    def number(self) -> int: ...

    @property
    def program(self) -> Program | None: ...

    @property
    def mode(self) -> Mode: ...

    @property
    def title(self) -> str: ...

    @property
    def state(self) -> State: ...

    @property
    def ready(self) -> bool: ...

    @property
    def failed(self) -> bool:
        """A known source failure; safe to inspect on the player thread."""
        ...

    @property
    def completed(self) -> bool:
        """Strict catalog producer completion, distinct from drained playback."""
        ...

    @property
    def buffered_seconds(self) -> float: ...

    def start(self) -> None: ...

    def retire(self) -> None: ...

    async def wait_ended(self) -> None: ...

    async def regulate(self) -> None: ...
