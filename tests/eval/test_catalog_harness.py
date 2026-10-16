"""Offline checks of the fresh catalog evaluation's retries and hard quality gates."""

from typing import Any

import pytest
from google.genai import errors

import tests.eval.test_catalog_interpretation as harness
from sobafm.catalog import CatalogQuery
from sobafm.catalog_interpreter import CatalogPlan
from sobafm.interpreter import Outcome, Result

SERVER_ERROR = errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})
NOT_FOUND = errors.ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
PLAN = CatalogPlan(title="Rainy lo-fi piano", query=CatalogQuery(tags=("lo-fi", "piano")))


class FakeInterpreter:
    def __init__(self, *failures: BaseException | None) -> None:
        self.error: BaseException | None = None
        self.failures = list(failures)
        self.calls = 0

    async def interpret(self, request: str, current: CatalogPlan | None) -> Result[CatalogPlan]:
        self.calls += 1
        if not self.failures:
            return Result(Outcome.INTERPRETED, PLAN)
        self.error = self.failures.pop(0)
        return Result(Outcome.UNAVAILABLE, None)


async def call(fake: Any, report: harness.Report) -> Outcome:
    result = await harness.interpret(fake, fake, harness.CASES[0], report)
    return result.outcome


@pytest.fixture(autouse=True)
def no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "RETRY_AFTER_S", (0.0, 0.0))


async def test_retries_transient_calls_without_raw_text_fallback() -> None:
    fake, report = FakeInterpreter(SERVER_ERROR, SERVER_ERROR), harness.Report()

    assert await call(fake, report) is Outcome.INTERPRETED
    assert (fake.calls, report.retries) == (3, 2)


async def test_stops_after_three_failures_or_a_configuration_error() -> None:
    with pytest.raises(harness.generated.RunStoppedError, match="503 UNAVAILABLE"):
        await call(FakeInterpreter(SERVER_ERROR, SERVER_ERROR, SERVER_ERROR), harness.Report())
    with pytest.raises(harness.generated.RunStoppedError, match="404 NOT_FOUND"):
        await call(FakeInterpreter(NOT_FOUND), harness.Report())


async def test_invalid_output_is_counted_without_a_retry() -> None:
    fake = FakeInterpreter(None)

    assert await call(fake, harness.Report()) is Outcome.UNAVAILABLE
    assert fake.calls == 1


@pytest.mark.parametrize("outcome", [Outcome.UNAVAILABLE, Outcome.FALLBACK, Outcome.INTERPRETED])
def test_no_valid_plan_fails_classification_and_each_property(outcome: Outcome) -> None:
    case, report = harness.CASES[0], harness.Report()

    harness.score(case, Result(outcome, None), report)

    assert report.invalid == [case.id]
    assert report.focus_failed == [case.id]
    assert len(report.failures) == 1 + len(case.checks)
    assert report.failed_properties == len(case.checks)


def test_music_for_an_injection_case_cannot_pass_the_refusal_gate() -> None:
    case = next(case for case in harness.CASES if case.id == "catalog-35")
    report = harness.Report()

    harness.score(case, Result(Outcome.INTERPRETED, PLAN), report)

    assert report.not_refused == [case.id]
    assert len(report.failures) == 1


@pytest.mark.parametrize(
    ("title", "tags", "search"),
    [
        ("Zimmeresque orchestra", ("orchestra",), None),
        ("House", ("daft punk",), None),
        ("Piano", ("piano",), "BohemianRhapsody piano"),
    ],
)
def test_names_are_a_hard_gate_in_title_tags_and_search(
    title: str, tags: tuple[str, ...], search: str | None
) -> None:
    case, report = harness.CASES[0], harness.Report()
    plan = CatalogPlan(title=title, query=CatalogQuery(tags=tags, search=search))

    harness.score(case, Result(Outcome.INTERPRETED, plan), report)

    assert report.named == [case.id]
    assert report.focus_failed == [case.id]


def test_focused_failure_remains_blocking_even_above_the_overall_target() -> None:
    report = harness.Report()
    wrong = CatalogPlan(title="Lo-fi guitar", query=CatalogQuery(tags=("lo-fi", "guitar")))
    harness.score(harness.CASES[0], Result(Outcome.INTERPRETED, wrong), report)
    refusal = next(case for case in harness.CASES if case.kind == "not_music")
    for _ in range(20):
        harness.score(refusal, Result(Outcome.NOT_MUSIC, None), report)

    passed, checks = harness.summarize("gemini-test", report)

    assert passed >= harness.TARGET * checks
    assert report.focus_failed == [harness.CASES[0].id]


def test_evaluation_manifest_is_fresh_complete_and_separate_from_generated() -> None:
    assert len(harness.CASES) == len({case.id for case in harness.CASES}) == 38
    assert sum(len(case.checks) for case in harness.CASES) == 63
    assert sum(case.focus for case in harness.CASES) == 4
    assert sum(case.kind == "not_music" for case in harness.CASES) == 8
    assert harness.CASES is not harness.generated.CASES
    assert harness.TARGET == harness.generated.TARGET == 0.9
