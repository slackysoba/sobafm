"""One bounded catalog selection attempt; no player, retry loop or journal writes.

The station owns this pipeline, generation predicate, program budget and each
refill budget. It reserves station capacity before calling start and retains its
owners until cancellation settles. Search decisions authorize ranking only.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

from sobafm.catalog import (
    MAX_CATALOG_BYTES,
    MAX_RECORDS_PER_SOURCE,
    AdmissionReview,
    Catalog,
    CatalogInputError,
    Recording,
    RecordingKey,
    SnapshotState,
    Source,
    eligible,
)
from sobafm.gemini_interactions import GeminiClient
from sobafm.gemini_match import (
    Fit,
    MatchFailed,
    Refused,
    interpret_catalog_intent,
    rank_catalog,
)
from sobafm.jamendo_admission import Candidate, Policy, Reason
from sobafm.jamendo_catalog import (
    DISABLED_POLICY,
    MAX_OFFSET,
    Coverage,
    Failure,
    Query,
    Refusal,
    Request,
    SearchWindow,
)
from sobafm.jamendo_transport import Kind, Report

WINDOW_SECONDS = 20.0
MAX_CYCLES = 768
MAX_HISTORY = 768
MAX_NO_PROGRESS = 3
MAX_REFILL_ATTEMPTS = 3


class MetadataFetch(Protocol):
    """The existing StationTransport interface, also supplied by offline fixtures.

    Fetch owns the 10s request bound and joins cancellation/cleanup. The caller
    must serialize search and refresh when reading last_report: it is per scope.
    """

    @property
    def last_report(self) -> Report | None: ...

    async def fetch(
        self, request: Request, *, deadline: float | None = None
    ) -> bytes | Refusal: ...


class End(StrEnum):
    HISTORY = "history_limit"
    CYCLES = "cycle_limit"
    NO_PROGRESS = "no_progress"
    REFILL = "refill_failed"
    CURSOR = "cursor_limit"
    STALE = "stale"
    CAPACITY = "capacity"
    REFUSED = "refused"
    INTENT = "intent_failed"
    MATCH = "matching_failed"
    EMPTY = "no_choices_in_window"
    PROMPT = "matching_pool_too_large"


@dataclass
class ProgramBudget:
    """Caller-owned lifetime accounting; never evict history or refund a start."""

    cycles: int = field(default=0, init=False)
    no_progress: int = field(default=0, init=False)
    _tried: set[RecordingKey] = field(default_factory=set[RecordingKey], init=False, repr=False)

    @property
    def tried(self) -> frozenset[RecordingKey]:
        return frozenset(self._tried)

    @property
    def end(self) -> End | None:
        if len(self._tried) >= MAX_HISTORY:
            return End.HISTORY
        if self.cycles >= MAX_CYCLES:
            return End.CYCLES
        if self.no_progress >= MAX_NO_PROGRESS:
            return End.NO_PROGRESS
        return None

    def begin(self) -> End | None:
        if reason := self.end:
            return reason
        self.cycles += 1
        # Failure, cancellation and stale completion already count. Only a new
        # admitted untried result may reset this, never cycles or refill debt.
        self.no_progress += 1
        return None

    def progress(self) -> None:
        self.no_progress = 0

    def mark_tried(self, key: RecordingKey) -> End | None:
        """Call before preparation/playing. Refuse the 769th key without eviction."""
        if key not in self._tried and len(self._tried) >= MAX_HISTORY:
            return End.HISTORY
        self._tried.add(key)
        return None


@dataclass
class RefillBudget:
    """One list-depletion episode, retained across cancellation/replacement."""

    attempts: int = field(default=0, init=False)

    def begin(self) -> End | None:
        if self.attempts >= MAX_REFILL_ATTEMPTS:
            return End.REFILL
        self.attempts += 1
        return None

    def delay(self, seconds_left: float | None = None) -> float | None:
        """Wait before the next attempt; None means no retry remains.

        The existing 16s reserve is preserved, not a search-latency guarantee.
        No sleeping, provider retry or new retry allowance lives here.
        """
        if self.attempts >= MAX_REFILL_ATTEMPTS:
            return None
        if self.attempts == 0:
            return 0.0
        if seconds_left is None or not math.isfinite(seconds_left):
            return 15.0
        return max(15.0, min(15.0 if self.attempts == 1 else 60.0, seconds_left - 16.0))


class PreparedCoverage(StrEnum):
    MISSING = "missing"
    PARTIAL = "partial"
    COMPLETE = "complete_prepared_subset"
    UNAVAILABLE = "unavailable"
    EMPTY = "empty_prepared_subset"


class Stage(StrEnum):
    INTENT = "intent"
    SEARCH = "search"
    RANK = "ranking"


@dataclass(frozen=True)
class PageEvidence:
    """Safe counters only; None is unknown, never a fabricated zero."""

    rows: int | None
    report: Report | None
    seconds: float


@dataclass(frozen=True)
class Evidence:
    pages: tuple[PageEvidence, ...] = ()
    failures: tuple[tuple[Failure | Reason, int], ...] = ()
    admitted: int = 0
    refused: int = 0
    response_bytes: int = 0
    candidate_bytes: int = 0
    seconds: float = 0.0
    stage: Stage = Stage.INTENT
    # Model failures are deliberately fixed, not arbitrary exception.kind strings.
    model_failed: bool = False
    model_blocked: bool = False


@dataclass(frozen=True)
class PoolCoverage:
    jamendo: Coverage
    fma: PreparedCoverage
    jamendo_untried: int = 0
    fma_untried: int = 0
    window_overflow: bool = False
    prompt_overflow: bool = False


@dataclass(frozen=True)
class Choice:
    recording: Recording
    fit: Fit
    # None means the separately reviewed FMA recording, not a Jamendo approval.
    candidate: Candidate | None = None


@dataclass(frozen=True)
class Selection:
    intent: Query | None
    next_offset: int
    coverage: PoolCoverage
    choices: tuple[Choice, ...] = ()
    reason: End | None = None
    evidence: Evidence = Evidence()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _current() -> bool:
    return True


def _serialized_size(candidates: tuple[Candidate, ...]) -> int:
    """Canonical JSON array size including complete credits and decision bindings.

    Encode one bounded record at a time instead of copying a whole window.
    """
    size = 2
    for index, candidate in enumerate(candidates):
        value = asdict(candidate)
        decision = cast(dict[str, object], value["decision"])
        decision["refreshed_at"] = candidate.decision.refreshed_at.isoformat()
        size += bool(index) + len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
    return size


def _rows_received(payload: bytes | Refusal, advanced: bool) -> int | None:
    # Only inspect a page whose success envelope/structure SearchWindow already
    # accepted; this count cannot grant admission or retain raw metadata.
    if not advanced or isinstance(payload, Refusal):
        return None
    data = cast(dict[str, object], json.loads(payload))
    return len(cast(list[object], data["results"]))


class CatalogSearch:
    """One instance per reserved station, with the process-wide GeminiClient.

    FMA catalog/reviews must come from the existing validated local boundary.
    The metadata scope belongs to the caller, as do its journal/quotas/cleanup.
    """

    def __init__(
        self,
        catalog: Catalog,
        reviews: tuple[AdmissionReview, ...],
        *,
        client: GeminiClient,
        metadata: MetadataFetch,
        policy: Policy = DISABLED_POLICY,
        observed_at: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._client = client
        self._metadata = metadata
        self._policy = policy
        self._observed_at = observed_at
        self._busy = False
        partitions = tuple(p for p in catalog.partitions if p.source == Source.FMA)
        self._fma = eligible(Catalog(catalog.synthetic, partitions), reviews)
        if len(partitions) > 1 or len(self._fma) > MAX_RECORDS_PER_SOURCE:
            raise CatalogInputError("invalid FMA partition")
        if not partitions:
            self._fma_coverage = PreparedCoverage.MISSING
        elif partitions[0].state == SnapshotState.UNAVAILABLE:
            self._fma_coverage = PreparedCoverage.UNAVAILABLE
            self._fma = ()
        elif partitions[0].state == SnapshotState.PARTIAL:
            self._fma_coverage = PreparedCoverage.PARTIAL
        elif not self._fma:
            self._fma_coverage = PreparedCoverage.EMPTY
        else:
            self._fma_coverage = PreparedCoverage.COMPLETE

    def _empty(self, intent: Query | None, offset: int, reason: End) -> Selection:
        return Selection(
            intent,
            offset,
            PoolCoverage(
                Coverage.UNAVAILABLE if self._policy.enabled else Coverage.DISABLED,
                self._fma_coverage,
            ),
            reason=reason,
        )

    async def start(
        self,
        request: str,
        *,
        budget: ProgramBudget,
        current: Callable[[], bool] = _current,
    ) -> Selection:
        """Interpret exactly once, then perform one search/rank cycle if accepted."""
        return await self._attempt(None, request, 0, budget, None, current)

    async def refill(
        self,
        intent: Query,
        offset: int,
        *,
        budget: ProgramBudget,
        refill: RefillBudget,
        current: Callable[[], bool] = _current,
    ) -> Selection:
        """One attempt using prior validated intent; the caller schedules backoff."""
        intent.parameters()
        if type(offset) is not int or not 0 <= offset <= MAX_OFFSET or offset % 100:
            raise CatalogInputError("invalid search cursor")
        return await self._attempt(intent, None, offset, budget, refill, current)

    async def _attempt(
        self,
        intent: Query | None,
        request: str | None,
        offset: int,
        budget: ProgramBudget,
        refill: RefillBudget | None,
        current: Callable[[], bool],
    ) -> Selection:
        if not current():
            return self._empty(intent, offset, End.STALE)
        if self._busy:
            return self._empty(intent, offset, End.CAPACITY)
        if reason := budget.end:
            return self._empty(intent, offset, reason)
        if refill is not None and (reason := refill.begin()):
            return self._empty(intent, offset, reason)
        budget.begin()
        self._busy = True
        try:
            if intent is None:
                assert request is not None
                try:
                    interpreted = await interpret_catalog_intent(request, client=self._client)
                except MatchFailed as exc:
                    result = self._empty(None, offset, End.INTENT)
                    return Selection(
                        None,
                        offset,
                        result.coverage,
                        reason=End.INTENT if current() else End.STALE,
                        evidence=Evidence(model_failed=True, model_blocked=exc.blocked),
                    )
                if not current():
                    return self._empty(None, offset, End.STALE)
                if isinstance(interpreted, Refused):
                    return self._empty(None, offset, End.REFUSED)
                intent = interpreted
            result = await self._select(intent, offset, budget, current)
            if result.choices:
                budget.progress()
            elif result.reason != End.STALE:
                ending = budget.end
                if ending is None and refill is not None and refill.attempts >= MAX_REFILL_ATTEMPTS:
                    ending = End.REFILL
                if ending is not None:
                    result = replace(result, reason=ending)
            return result
        finally:
            # Cancellation propagates only after the injected owner has settled;
            # it cannot free this station pipeline or refund its attempt early.
            self._busy = False

    async def _select(
        self, intent: Query, offset: int, budget: ProgramBudget, current: Callable[[], bool]
    ) -> Selection:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + WINDOW_SECONDS
        window = SearchWindow(intent, self._policy, offset=offset)
        pages: list[PageEvidence] = []
        overflow = False
        late_admission = False
        size = 2
        # SearchWindow is the sole page-sequence/admission authority. A terminal
        # refusal from request() ends this window; it is not another failed row.
        while offset < MAX_OFFSET and isinstance(request := window.request(), Request):
            before = loop.time()
            if before >= deadline:
                payload: bytes | Refusal = Refusal(Failure.TRANSPORT)
                report = Report(Kind.TIMEOUT, 0, 0, 0, None, None, False)
            else:
                payload = await self._metadata.fetch(request, deadline=deadline)
                report = self._metadata.last_report
            if loop.time() > deadline:
                payload = Refusal(Failure.TRANSPORT)
                report = (
                    Report(Kind.TIMEOUT, 0, 0, None, None, None, False)
                    if report is None
                    else replace(report, kind=Kind.TIMEOUT)
                )
            previous = window.result().next_offset
            received = window.receive(request, payload, self._observed_at())
            late_admission = isinstance(payload, bytes) and loop.time() > deadline
            pages.append(
                PageEvidence(
                    _rows_received(payload, received.next_offset > previous),
                    report,
                    loop.time() - before,
                )
            )
            del payload
            size = _serialized_size(received.candidates)
            if len(received.candidates) > MAX_RECORDS_PER_SOURCE or size > MAX_CATALOG_BYTES:
                overflow = True
                break
            if not current():
                break
            # Parsing/admission is bounded work but still consumes the window's
            # clock. Do not start ranking with a response settled after it.
            if loop.time() > deadline:
                break
        found = window.result()
        candidates = (
            ()
            if overflow or late_admission
            else tuple(c for c in found.candidates if c.recording.key not in budget.tried)
        )
        fma = tuple(r for r in self._fma if r.key not in budget.tried)
        coverage = PoolCoverage(
            Coverage.UNAVAILABLE if overflow or late_admission else found.coverage,
            self._fma_coverage,
            len(candidates),
            len(fma),
            overflow,
        )
        counts = Counter(r.reason for r in found.refusals)
        if overflow:
            counts[Failure.LIMIT] += 1
        if late_admission and not counts[Failure.TRANSPORT]:
            counts[Failure.TRANSPORT] += 1
        evidence = Evidence(
            tuple(pages),
            tuple(sorted(counts.items())),
            len(found.candidates) if not overflow and not late_admission else 0,
            len(found.refusals),
            found.response_bytes,
            size,
            loop.time() - started,
            Stage.SEARCH,
        )

        def result(reason: End | None, choices: tuple[Choice, ...] = ()) -> Selection:
            return Selection(intent, found.next_offset, coverage, choices, reason, evidence)

        if not current():
            return result(End.STALE)
        if len(budget.tried) >= MAX_HISTORY:
            return result(End.HISTORY)
        recordings = fma + tuple(c.recording for c in candidates)
        if not recordings:
            return result(End.CURSOR if offset >= MAX_OFFSET else End.EMPTY)
        try:
            matched = await rank_catalog(intent, recordings, client=self._client)
        except CatalogInputError:
            coverage = PoolCoverage(
                coverage.jamendo, coverage.fma, len(candidates), len(fma), overflow, True
            )
            evidence = replace(evidence, seconds=loop.time() - started, stage=Stage.RANK)
            return result(End.PROMPT if current() else End.STALE)
        except MatchFailed as exc:
            evidence = replace(
                evidence,
                seconds=loop.time() - started,
                stage=Stage.RANK,
                model_failed=True,
                model_blocked=exc.blocked,
            )
            return result(End.STALE if not current() else End.MATCH)
        evidence = replace(evidence, seconds=loop.time() - started, stage=Stage.RANK)
        if not current():
            return result(End.STALE)
        if len(budget.tried) >= MAX_HISTORY:
            return result(End.HISTORY)
        if isinstance(matched, Refused):
            return result(End.REFUSED)
        by_key = {c.recording.key: c for c in candidates}
        choices = tuple(
            Choice(r.recording, r.fit, by_key.get(r.recording.key))
            for r in matched.ranked
            if r.recording.key not in budget.tried
        )
        return result(None if choices else End.EMPTY, choices)
