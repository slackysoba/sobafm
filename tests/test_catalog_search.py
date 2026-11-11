"""Bounded selection schedules using real intent/admission/ranking and fake I/O."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace

import pytest

from sobafm import catalog_search, gemini_match
from sobafm.catalog import Catalog, CatalogInputError, RecordingKey, Source
from sobafm.catalog_search import (
    MAX_CYCLES,
    MAX_HISTORY,
    CatalogSearch,
    End,
    PreparedCoverage,
    ProgramBudget,
    RefillBudget,
)
from sobafm.gemini_interactions import GeminiClient
from sobafm.jamendo_admission import Reason
from sobafm.jamendo_catalog import MAX_OFFSET, Coverage, Failure, Query, Refusal, Request
from sobafm.jamendo_transport import Kind, Report
from tests.test_catalog import catalog, partition, recording, reviews
from tests.test_catalog_intent import KEY, intent, interaction
from tests.test_gemini_match import plan
from tests.test_jamendo_catalog import NOW, ON, response, row


class Model:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.bodies: list[bytes] = []
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.closed = False

    async def post(self, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        self.bodies.append(body)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        return 200, interaction(self.answers.pop(0))

    async def close(self) -> None:
        self.closed = True


class Metadata:
    def __init__(self, *payloads: bytes | Refusal, kind: Kind | None = None) -> None:
        self.payloads = list(payloads)
        self.requests: list[Request] = []
        self.deadlines: list[float | None] = []
        self.last_report: Report | None = None
        self.kind = kind
        self.entered = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.stopping = asyncio.Event()
        self.settle: asyncio.Event | None = None

    async def fetch(self, request: Request, *, deadline: float | None = None) -> bytes | Refusal:
        self.requests.append(request)
        self.deadlines.append(deadline)
        self.entered.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.stopping.set()
            if self.settle is not None:
                # Mimic the transport's joined owner, including repeated cancellation.
                task = asyncio.create_task(self.settle.wait())
                while not task.done():
                    with suppress(asyncio.CancelledError):
                        await asyncio.shield(task)
            raise
        payload = self.payloads.pop(0)
        self.last_report = Report(
            self.kind,
            1,
            1,
            len(payload) if isinstance(payload, bytes) else None,
            200 if isinstance(payload, bytes) else 429,
            0.0,
            False,
        )
        return payload


EMPTY_CATALOG = Catalog(True, ())


def search(model: Model, metadata: Metadata, local: Catalog = EMPTY_CATALOG) -> CatalogSearch:
    return CatalogSearch(
        local,
        reviews(local),
        client=GeminiClient(KEY, transport=model),
        metadata=metadata,
        policy=ON,
        observed_at=lambda: NOW,
    )


@pytest.mark.parametrize("answer", ['{"outcome":"refuse","query":null}', "{}", "not json"])
def test_refused_or_invalid_intent_never_searches(answer: str) -> None:
    async def run() -> None:
        model, metadata = Model(answer), Metadata()
        result = await search(model, metadata).start("piano", budget=ProgramBudget())
        assert result.reason in (End.REFUSED, End.INTENT)
        assert result.intent is None and not result.choices
        assert len(model.bodies) == 1 and not metadata.requests and not model.closed

    asyncio.run(run())


def test_bad_request_never_calls_model_or_source() -> None:
    async def run() -> None:
        model, metadata = Model(), Metadata()
        with pytest.raises(CatalogInputError):
            await search(model, metadata).start("", budget=ProgramBudget())
        assert not model.bodies and not metadata.requests

    asyncio.run(run())


def test_singles_albums_fma_complete_credits_and_reused_intent() -> None:
    async def run() -> None:
        local = catalog(partition("fma", [recording("1"), recording("2"), recording("3")]))
        model = Model(intent(), plan((0, "close"), (2, "loose")), plan((0, "close")))
        metadata = Metadata(
            response([row("10"), row("11", album_id="7", album_name="An album")]), response([])
        )
        service = search(model, metadata, local)
        budget = ProgramBudget()
        budget.mark_tried(RecordingKey(Source.FMA, "1"))
        result = await service.start("moonlit jazz", budget=budget)
        assert result.reason is None and result.intent == Query(search="moonlit jazz")
        assert result.coverage.fma == PreparedCoverage.COMPLETE
        assert (result.coverage.fma_untried, result.coverage.jamendo_untried) == (2, 2)
        assert result.choices[0].recording.key == RecordingKey(Source.FMA, "2")
        chosen = result.choices[1]
        assert chosen.candidate and chosen.recording is chosen.candidate.recording
        assert chosen.recording.credits is not None
        assert "Jamendo" in str(chosen.recording.credits)
        assert result.next_offset == 100 and result.evidence.pages[0].rows == 2
        refill = RefillBudget()
        assert result.intent is not None
        next_result = await service.refill(
            result.intent, result.next_offset, budget=budget, refill=refill
        )
        assert next_result.choices and len(model.bodies) == 3
        assert dict(metadata.requests[1].parameters)["offset"] == "100"
        assert dict(metadata.requests[0].parameters)["type"] == "single albumtrack"
        assert budget.cycles == 2 and budget.no_progress == 0 and refill.attempts == 1
        bodies = b"\n".join(model.bodies)
        for forbidden in (
            b"credits",
            b"snapshot_id",
            b"client_id",
            b"storage.jamendo",
            KEY.encode(),
        ):
            assert forbidden not in bodies
        assert all(json.loads(body)["store"] is False for body in model.bodies)
        assert not model.closed

    asyncio.run(run())


@pytest.mark.parametrize("change", [{"name": "Changed content"}, {"duration": "invalid"}])
def test_page_two_conflict_revokes_page_one_before_ranking(change: dict[str, object]) -> None:
    async def run() -> None:
        first = response([row(str(n)) for n in range(1, 101)])
        metadata = Metadata(first, response([row("1", **change), row("2")]))
        model = Model(intent(), plan((0, "close")))
        result = await search(model, metadata).start("piano", budget=ProgramBudget())
        assert len(metadata.requests) == 2 and metadata.deadlines[0] == metadata.deadlines[1]
        assert result.next_offset == 200 and result.coverage.jamendo_untried == 99
        assert result.choices[0].recording.key.recording_id == "2"
        assert result.evidence.refused == 1
        assert [p.rows for p in result.evidence.pages] == [100, 2]
        assert len(model.bodies) == 2

    asyncio.run(run())


def test_partial_page_failure_preserves_candidates_and_safe_transport_kind() -> None:
    async def run() -> None:
        metadata = Metadata(
            response([row(str(n)) for n in range(1, 101)]),
            Refusal(Failure.TRANSPORT),
            kind=Kind.RATE_LIMITED,
        )
        model = Model(intent(), plan((0, "close")))
        result = await search(model, metadata).start("piano", budget=ProgramBudget())
        assert result.choices and result.coverage.jamendo == Coverage.PARTIAL
        assert result.next_offset == 100
        assert dict(result.evidence.failures)[Failure.TRANSPORT] == 1
        report = result.evidence.pages[1].report
        assert report and report.kind == Kind.RATE_LIMITED and report.body_bytes is None
        assert result.evidence.pages[1].rows is None

    asyncio.run(run())


def test_http_200_all_refused_is_actionable_unavailable_not_success() -> None:
    async def run() -> None:
        metadata = Metadata(response([row("1", name="Preview"), row("2", license_ccurl="bad")]))
        model = Model(intent())
        result = await search(model, metadata).start("piano", budget=ProgramBudget())
        assert not result.choices and result.reason == End.EMPTY
        assert result.coverage.jamendo == Coverage.UNAVAILABLE
        assert result.evidence.pages[0].rows == 2
        assert result.evidence.admitted == 0 and result.evidence.refused == 2
        assert dict(result.evidence.failures) == {Reason.EXCLUDED: 1, Reason.LICENSE: 1}
        assert len(model.bodies) == 1
        assert "Preview" not in repr(result.evidence)

    asyncio.run(run())


def test_fma_still_ranks_with_jamendo_failure_and_unreviewed_fma_is_absent() -> None:
    async def run() -> None:
        local = catalog(partition("fma", [recording("1"), recording("2")]))
        model, metadata = Model(intent(), plan((0, "close"))), Metadata(Refusal(Failure.TRANSPORT))
        service = CatalogSearch(
            local,
            reviews(local)[:1],
            client=GeminiClient(KEY, transport=model),
            metadata=metadata,
            policy=ON,
        )
        result = await service.start("piano", budget=ProgramBudget())
        assert result.coverage.fma_untried == 1
        assert result.choices[0].recording.key == RecordingKey(Source.FMA, "1")

    asyncio.run(run())


def test_window_overflow_rejects_whole_source_without_truncating_credits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        metadata = Metadata(response([row()]))
        model = Model(intent(), plan((0, "loose")))
        local = catalog(partition("fma", [recording("1")]))
        monkeypatch.setattr(catalog_search, "MAX_CATALOG_BYTES", 2)
        result = await search(model, metadata, local).start("piano", budget=ProgramBudget())
        assert result.coverage.window_overflow and result.coverage.jamendo_untried == 0
        assert result.choices[0].recording.key.source == Source.FMA
        assert dict(result.evidence.failures)[Failure.LIMIT] == 1

    asyncio.run(run())


def test_prompt_overflow_is_explicit_and_makes_no_ranking_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        model, metadata = Model(intent()), Metadata(response([row()]))
        monkeypatch.setattr(gemini_match, "MAX_SONG_CHARACTERS", 2)
        result = await search(model, metadata).start("piano", budget=ProgramBudget())
        assert result.reason == End.PROMPT and result.coverage.prompt_overflow
        assert not result.choices and len(model.bodies) == 1

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["intent", "search", "ranking"])
def test_superseded_results_cannot_progress_or_rank_late_source(stage: str) -> None:
    async def run() -> None:
        model = Model(intent(), plan((0, "close")))
        metadata = Metadata(response([row()]))
        budget = ProgramBudget()
        live = True
        if stage == "search":
            metadata.gate = asyncio.Event()
            gate, entered = metadata.gate, metadata.entered
        else:
            model.gate = asyncio.Event()
            gate, entered = model.gate, model.entered
        service = search(model, metadata)
        if stage == "ranking":
            model.answers.pop(0)
            task = asyncio.create_task(
                service.refill(
                    Query(search="piano"),
                    0,
                    budget=budget,
                    refill=RefillBudget(),
                    current=lambda: live,
                )
            )
        else:
            task = asyncio.create_task(service.start("piano", budget=budget, current=lambda: live))
        await entered.wait()
        live = False
        gate.set()
        result = await task
        assert result.reason == End.STALE and not result.choices
        assert budget.cycles == 1 and budget.no_progress == 1
        if stage == "intent":
            assert not metadata.requests
        if stage == "search":
            assert len(model.bodies) == 1

    asyncio.run(run())


def test_cancelled_owner_retains_pipeline_and_budget_until_settlement() -> None:
    async def run() -> None:
        model, metadata = Model(intent()), Metadata(response([row()]))
        metadata.gate, metadata.settle = asyncio.Event(), asyncio.Event()
        service, budget = search(model, metadata), ProgramBudget()
        task = asyncio.create_task(service.start("piano", budget=budget))
        await metadata.entered.wait()
        task.cancel()
        await metadata.stopping.wait()
        task.cancel()
        denied = await service.start("new", budget=ProgramBudget())
        assert denied.reason == End.CAPACITY and len(model.bodies) == 1
        metadata.settle.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert budget.cycles == 1 and budget.no_progress == 1

    asyncio.run(run())


def test_exact_history_cycle_and_no_progress_boundaries() -> None:
    budget = ProgramBudget()
    for cycle in range(MAX_CYCLES):
        assert budget.begin() is None
        budget.progress()
        assert budget.cycles == cycle + 1
    assert budget.begin() == End.CYCLES and budget.cycles == 768
    history = ProgramBudget()
    for number in range(MAX_HISTORY):
        assert history.mark_tried(RecordingKey(Source.FMA, str(number))) is None
    assert history.mark_tried(RecordingKey(Source.FMA, "769")) == End.HISTORY
    assert len(history.tried) == 768 and history.begin() == End.HISTORY
    stopped = ProgramBudget()
    for _ in range(3):
        assert stopped.begin() is None
    assert stopped.begin() == End.NO_PROGRESS and stopped.cycles == 3


def test_three_attempts_no_nested_retry_and_existing_backoff() -> None:
    async def run() -> None:
        model = Model()
        metadata = Metadata(response([]), response([]), response([]))
        service, budget, refill = search(model, metadata), ProgramBudget(), RefillBudget()
        results: list[catalog_search.Selection] = []
        for attempt in range(1, 4):
            result = await service.refill(
                Query(search="piano"), (attempt - 1) * 100, budget=budget, refill=refill
            )
            results.append(result)
            assert refill.attempts == attempt
            if attempt == 1:
                assert refill.delay(200) == 15
            if attempt == 2:
                assert refill.delay(200) == 60 and refill.delay(59) == 43
                assert refill.delay(20) == 15 and refill.delay() == 15
        assert results[-1].reason == End.NO_PROGRESS and refill.delay() is None
        denied = await service.refill(Query(search="piano"), 300, budget=budget, refill=refill)
        assert denied.reason == End.NO_PROGRESS
        assert len(metadata.requests) == 3 and not model.bodies and budget.cycles == 3

    asyncio.run(run())


def test_cursor_ceiling_never_wraps_but_keeps_fma() -> None:
    async def run() -> None:
        metadata, model = Metadata(), Model(plan((0, "close")))
        local = catalog(partition("fma", [recording("1")]))
        result = await search(model, metadata, local).refill(
            Query(search="piano"), MAX_OFFSET, budget=ProgramBudget(), refill=RefillBudget()
        )
        assert result.choices and not metadata.requests and result.next_offset == MAX_OFFSET

    asyncio.run(run())


def test_shared_client_keeps_four_call_gate_across_intent_and_ranking() -> None:
    async def run() -> None:
        class BlockedModel(Model):
            active = 0
            peak = 0
            four = asyncio.Event()
            release = asyncio.Event()

            async def post(
                self, url: str, headers: Mapping[str, str], body: bytes
            ) -> tuple[int, bytes]:
                self.active += 1
                self.peak = max(self.peak, self.active)
                if self.active == 4:
                    self.four.set()
                await self.release.wait()
                self.active -= 1
                return 200, interaction(plan((0, "close")) if b"SONGS:" in body else intent())

        model = BlockedModel()
        client = GeminiClient(KEY, transport=model)
        local = catalog(partition("fma", [recording("1")]))
        services = [
            CatalogSearch(local, reviews(local), client=client, metadata=Metadata())
            for _ in range(5)
        ]
        tasks = [asyncio.create_task(s.start("piano", budget=ProgramBudget())) for s in services]
        await model.four.wait()
        assert model.peak == 4 and not any(t.done() for t in tasks)
        model.release.set()
        results = await asyncio.gather(*tasks)
        assert all(r.choices for r in results) and model.peak == 4 and not model.closed

    asyncio.run(run())


def test_exact_serialized_utf8_window_boundary_preserves_full_credits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_jamendo_catalog import candidate

    value = row(notices=["創作者 " + "é" * 100])
    admitted = candidate(value)

    async def run() -> None:
        baseline = await search(
            Model(intent(), plan((0, "close"))), Metadata(response([value]))
        ).start("piano", budget=ProgramBudget())
        size = baseline.evidence.candidate_bytes
        assert size > 1000
        for limit, accepted in ((size, True), (size - 1, False)):
            monkeypatch.setattr(catalog_search, "MAX_CATALOG_BYTES", limit)
            model = Model(intent(), plan((0, "close")))
            result = await search(model, Metadata(response([value]))).start(
                "piano", budget=ProgramBudget()
            )
            assert bool(result.choices) == accepted
            assert result.evidence.candidate_bytes == size
            if accepted:
                assert result.choices[0].recording.credits == admitted.recording.credits
            else:
                assert result.coverage.window_overflow and len(model.bodies) == 1

    asyncio.run(run())


def test_expired_window_has_no_source_dispatch_and_keeps_fma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        monkeypatch.setattr(catalog_search, "WINDOW_SECONDS", 0.0)
        model, metadata = Model(intent(), plan((0, "close"))), Metadata()
        local = catalog(partition("fma", [recording("1")]))
        result = await search(model, metadata, local).start("piano", budget=ProgramBudget())
        assert not metadata.requests and result.choices
        assert result.evidence.pages[0].report is not None
        assert result.evidence.pages[0].report.kind == Kind.TIMEOUT
        assert result.evidence.pages[0].report.dispatches == 0

    asyncio.run(run())


def test_history_changes_while_ranking_cannot_replay_or_exceed_limit() -> None:
    async def run() -> None:
        model = Model(plan((0, "close")))
        model.gate = asyncio.Event()
        local = catalog(partition("fma", [recording("1")]))
        service, budget = search(model, Metadata(response([])), local), ProgramBudget()
        task = asyncio.create_task(
            service.refill(Query(search="piano"), 0, budget=budget, refill=RefillBudget())
        )
        await model.entered.wait()
        for number in range(MAX_HISTORY):
            budget.mark_tried(RecordingKey(Source.FMA, str(number)))
        model.gate.set()
        result = await task
        assert result.reason == End.HISTORY and not result.choices
        assert budget.no_progress == 1

    asyncio.run(run())


def test_success_resets_only_no_progress_and_never_refill_or_cycle_debt() -> None:
    async def run() -> None:
        model = Model(plan((0, "close")), plan((0, "close")), plan((0, "close")))
        service = search(model, Metadata(response([row()]), response([row()]), response([row()])))
        budget, refill = ProgramBudget(), RefillBudget()
        for number in range(3):
            result = await service.refill(Query(search="piano"), 0, budget=budget, refill=refill)
            assert result.choices and budget.no_progress == 0
            assert budget.cycles == refill.attempts == number + 1
        denied = await service.refill(Query(search="piano"), 0, budget=budget, refill=refill)
        assert denied.reason == End.REFILL and budget.cycles == 3

    asyncio.run(run())


@pytest.mark.parametrize(
    "answer",
    [
        '{"outcome":"match","ranked":[{"song":0,"song":1,"fit":"close"}]}',
        plan((0, "close"), (0, "loose")),
        plan((False, "close")),
        plan((2, "close")),
        plan(*((n, "close") for n in range(9))),
    ],
)
def test_pooled_ranking_rejects_invalid_positions_and_duplicate_fields(answer: str) -> None:
    async def run() -> None:
        model = Model(intent(), answer)
        result = await search(model, Metadata(response([row()]))).start(
            "piano", budget=ProgramBudget()
        )
        assert result.reason == End.MATCH and not result.choices
        assert result.evidence.model_failed and len(model.bodies) == 2

    asyncio.run(run())


def test_rank_catalog_exact_character_cap_and_plus_one() -> None:
    from tests.test_discord_adapter import recording as prepared_recording

    async def run() -> None:
        base = replace(prepared_recording("1"), title="")
        overhead = len(gemini_match.render_songs((base,)))
        bounded = replace(base, title="é" * (gemini_match.MAX_SONG_CHARACTERS - overhead))
        model = Model(plan((0, "close")))
        client = GeminiClient(KEY, transport=model)
        result = await gemini_match.rank_catalog(Query(search="piano"), (bounded,), client=client)
        assert isinstance(result, gemini_match.Matched)
        with pytest.raises(CatalogInputError):
            await gemini_match.rank_catalog(
                Query(search="piano"), (replace(bounded, title=bounded.title + "é"),), client=client
            )
        assert len(model.bodies) == 1

    asyncio.run(run())
