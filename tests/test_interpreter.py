import asyncio
import json
import logging
from typing import Any

import pytest
from google import genai
from google.genai import errors, types

import sobafm.interpreter
from sobafm.interpreter import Interpreter, Outcome
from sobafm.plan import MusicPlan, Prompt

LOFI = MusicPlan(
    title="Rainy lo-fi",
    prompts=[Prompt(text="lo-fi hip hop"), Prompt(text="soft piano", weight=0.6)],
    bpm=75,
    scale=types.Scale.C_MAJOR_A_MINOR,
    density=0.4,
    brightness=0.5,
)
NEW_PLAN = {"title": "Night drive", "prompts": [{"text": "synthwave", "weight": 1.0}], "bpm": 110}


class FakeGemini:
    """Answers `generate_content` like the Google Gen AI SDK's async models API."""

    def __init__(self, response: types.GenerateContentResponse | None = None) -> None:
        self.response = response or types.GenerateContentResponse()
        self.error: Exception | None = None
        self.delay = 0.0
        self.calls: list[dict[str, Any]] = []

    async def generate_content(
        self, *, model: str, contents: str, config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.response


def answer(text: str, finish: types.FinishReason = types.FinishReason.STOP) -> Any:
    content = types.Content(role="model", parts=[types.Part(text=text)])
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=content, finish_reason=finish)]
    )


def interpreter(gemini: FakeGemini) -> Interpreter:
    return Interpreter(gemini, "gemini-test")


async def test_interprets_a_new_request() -> None:
    gemini = FakeGemini(answer(json.dumps({"kind": "new", "plan": NEW_PLAN})))

    result = await interpreter(gemini).interpret("night drive", LOFI)

    assert result.outcome is Outcome.INTERPRETED
    assert result.plan == MusicPlan.model_validate(NEW_PLAN)


async def test_a_refinement_keeps_what_it_does_not_change() -> None:
    refined = {"title": "Faster lo-fi", "prompts": [{"text": "lo-fi hip hop"}], "bpm": 95}
    gemini = FakeGemini(answer(json.dumps({"kind": "refine", "plan": refined})))

    result = await interpreter(gemini).interpret("faster", LOFI)

    assert result.plan is not None
    assert (result.plan.bpm, result.plan.scale, result.plan.density, result.plan.brightness) == (
        95,
        LOFI.scale,
        LOFI.density,
        LOFI.brightness,
    )


async def test_refuses_requests_that_are_not_music() -> None:
    gemini = FakeGemini(answer(json.dumps({"kind": "not_music"})))

    result = await interpreter(gemini).interpret("what's the weather?", None)

    assert (result.outcome, result.plan) == (Outcome.NOT_MUSIC, None)


@pytest.mark.parametrize("blocked_by", ["prompt", "answer"])
async def test_refuses_requests_that_safety_filters_block(blocked_by: str) -> None:
    if blocked_by == "prompt":
        feedback = types.GenerateContentResponsePromptFeedback(
            block_reason=types.BlockedReason.SAFETY
        )
        response = types.GenerateContentResponse(prompt_feedback=feedback)
    else:
        response = answer("", finish=types.FinishReason.SAFETY)

    result = await interpreter(FakeGemini(response)).interpret("…", None)

    assert (result.outcome, result.plan) == (Outcome.BLOCKED, None)


@pytest.mark.parametrize(
    "failure",
    [
        errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}}),
        errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}),
        "not JSON",
        json.dumps({"kind": "new", "plan": NEW_PLAN | {"bpm": 300}}),
        json.dumps({"kind": "new"}),
        "timeout",
    ],
)
async def test_falls_back_to_the_request_text(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | str
) -> None:
    gemini = FakeGemini()
    if isinstance(failure, Exception):
        gemini.error = failure
    elif failure == "timeout":
        monkeypatch.setattr(sobafm.interpreter, "TIMEOUT_S", 0.01)
        gemini.delay = 1
    else:
        gemini.response = answer(failure)

    result = await interpreter(gemini).interpret("rainy lo-fi", LOFI)

    assert (result.outcome, result.plan) == (
        Outcome.FALLBACK,
        MusicPlan.from_request("rainy lo-fi"),
    )


async def test_sends_only_the_request_and_the_current_plan() -> None:
    gemini = FakeGemini(answer(json.dumps({"kind": "not_music"})))

    await interpreter(gemini).interpret("faster", LOFI)

    call = gemini.calls[0]
    assert json.loads(call["contents"]) == {
        "request": "faster",
        "current_plan": LOFI.model_dump(mode="json"),
    }
    assert call["model"] == "gemini-test"
    config: types.GenerateContentConfig = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.automatic_function_calling == types.AutomaticFunctionCallingConfig(disable=True)


async def test_logs_request_text_only_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    gemini = FakeGemini(answer("not JSON"))

    with caplog.at_level(logging.INFO, logger="sobafm.interpreter"):
        await interpreter(gemini).interpret("a secret request", None)
    assert caplog.messages
    assert not any("secret" in message for message in caplog.messages)

    with caplog.at_level(logging.DEBUG, logger="sobafm.interpreter"):
        await interpreter(gemini).interpret("a secret request", None)
    assert any("secret" in message for message in caplog.messages)


def test_takes_the_sdk_models_api() -> None:
    client = genai.Client(api_key="key")

    Interpreter(client.aio.models, "gemini-test")  # pyright checks that it fits `Models`
