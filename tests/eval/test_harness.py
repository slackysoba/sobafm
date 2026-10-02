"""Tests of the evaluation set's harness, which run without Gemini, unlike the set itself."""

import asyncio
import json
from typing import Any

import aiohttp
import pytest
from google.genai import errors

import tests.eval.test_interpretation as harness  # a module import, so the set is not collected
from sobafm.interpreter import Outcome, Result
from sobafm.plan import MusicPlan

SERVER_ERROR = errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})
NOT_FOUND = errors.ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND"}})


@pytest.mark.parametrize(
    ("error", "verdict"),
    [
        (None, "invalid"),
        (SERVER_ERROR, "retry"),
        (errors.ClientError(429, {"error": {"code": 429}}), "retry"),
        (asyncio.CancelledError(), "retry"),
        (TimeoutError(), "retry"),
        (aiohttp.ServerDisconnectedError(), "retry"),
        (json.JSONDecodeError("Expecting value", "<html>", 0), "retry"),
        (NOT_FOUND, "stop"),
        (RuntimeError("a bug"), "stop"),
    ],
    ids=[
        "invalid",
        "503",
        "429",
        "timeout",
        "TimeoutError",
        "dropped",
        "unparseable",
        "404",
        "bug",
    ],
)
def test_sorts_each_failure(error: BaseException | None, verdict: str) -> None:
    assert harness.verdict(error) == verdict


class FakeInterpreter:
    """Falls back with each of `errors` in turn, recording it as the call's error, then plays."""

    def __init__(self, *failures: BaseException | None) -> None:
        self.models = self  # stands in for the recorder too
        self.error: BaseException | None = None
        self.failures = list(failures)
        self.calls = 0

    async def interpret(self, request: str, current: MusicPlan | None) -> Result:
        self.calls += 1
        plan = MusicPlan.from_request(request)
        if not self.failures:
            return Result(Outcome.INTERPRETED, plan)
        self.error = self.failures.pop(0)
        return Result(Outcome.FALLBACK, plan)


async def interpret(fake: Any, report: harness.Report) -> Outcome:
    result = await harness.interpret(fake, fake, harness.CASES[0], report)
    return result.outcome


@pytest.fixture(autouse=True)
def no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "RETRY_AFTER_S", (0.0, 0.0))


async def test_retries_a_failed_call_twice() -> None:
    fake, report = FakeInterpreter(SERVER_ERROR, SERVER_ERROR), harness.Report()

    assert await interpret(fake, report) is Outcome.INTERPRETED
    assert (fake.calls, report.retries) == (3, 2)


async def test_stops_after_a_third_failure_or_a_configuration_error() -> None:
    with pytest.raises(harness.RunStoppedError, match="503 UNAVAILABLE"):
        await interpret(FakeInterpreter(SERVER_ERROR, SERVER_ERROR, SERVER_ERROR), harness.Report())
    with pytest.raises(harness.RunStoppedError, match="404 NOT_FOUND"):
        await interpret(FakeInterpreter(NOT_FOUND), harness.Report())


async def test_counts_an_invalid_answer_at_once() -> None:
    fake = FakeInterpreter(None)

    assert await interpret(fake, harness.Report()) is Outcome.FALLBACK
    assert fake.calls == 1


def test_a_case_without_valid_output_fails_each_of_its_checks() -> None:
    case = harness.CASES[0]
    report = harness.Report()

    harness.score(case, Result(Outcome.FALLBACK, MusicPlan.from_request(case.request)), report)

    assert report.invalid == [case.name]
    assert len(report.failures) == 1 + len(case.checks) == 1 + report.failed_properties


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("Zimmeresque strings", True),
        ("DaftPunk house", True),
        ("Daft-Punk house", True),
        ("Miles Davis trumpet", True),
        ("smiles and sunshine", False),
        ("dance-punk energy", False),
        ("piano rhapsody", False),
    ],
)
def test_finds_names_at_the_start_of_words(text: str, *, named: bool) -> None:
    _, avoids = harness.avoids("zimmer", "daft punk", "miles davis", "bohemian rhapsody")
    plan = MusicPlan.model_validate({"title": "Title", "prompts": [{"text": text}]})

    assert avoids(plan, None) is not named
