"""The two playback engines' responsibilities; station owns results and rollback."""

from collections.abc import Sequence
from typing import Protocol

from sobafm.audio_budget import AudioLedger
from sobafm.mixer import Mixer
from sobafm.program import Mode, Outcome, Program
from sobafm.source import Source


class Engine(Protocol):
    @property
    def mode(self) -> Mode: ...

    @property
    def audio(self) -> AudioLedger: ...

    @property
    def sources(self) -> Sequence[Source]: ...

    def reserve(self, program: Program) -> bool: ...

    def release_if_ended(self, retained: Sequence[Program]) -> None: ...

    def ended_failures(self, program: Program | None) -> Sequence[Source]: ...

    def count_failure(
        self, source: Source, program: Program | None, connected: bool
    ) -> Outcome | None: ...

    def drop_ended(self, program: Program | None, mixer: Mixer) -> None: ...

    def retire(self, program: Program | None, mixer: Mixer) -> None: ...

    def generate(self, program: Program) -> None: ...

    def ready_source(self, program: Program, exclude: Source | None = None) -> Source | None: ...

    def handover_due(self, source: Source) -> bool:
        """Allow natural handover or failed-source recovery.

        Catalog normal completion needs strict verification AND audible drain.
        A failed source may recover after its performance ceases/drains, without
        marking that track complete. Low buffer or producer exit alone is neither.
        """
        ...

    def going_live(self, source: Source) -> None:
        """Record only connected-player settlement, including consecutive catalog tracks."""
        ...

    async def close(self) -> None:
        """Retire work; keep leases/debt until owned resources and storage actually release."""
        ...
