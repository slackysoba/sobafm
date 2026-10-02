import asyncio
import json
import logging
from typing import Any

import pytest
from google import genai
from google.genai import errors, types

import sobafm.interpreter
from sobafm.interpreter import CONFIG, Interpreter, Outcome
from sobafm.plan import MusicPlan, Prompt

LOFI = MusicPlan(
    title="Rainy lo-fi",
    prompts=[Prompt(text="lo-fi hip hop"), Prompt(text="soft piano", weight=0.6)],
    bpm=75,
    scale=types.Scale.C_MAJOR_A_MINOR,
    density=0.4,
    brightness=0.5,
    mute_drums=True,
)
NEW_PLAN = {"title": "Night drive", "prompts": [{"text": "synthwave", "weight": 1.0}], "bpm": 110}


class FakeGemini:
    """Answers `models.generate_content` like the Google Gen AI SDK's async client."""

    def __init__(self, response: types.GenerateContentResponse | None = None) -> None:
        self.models = self
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


def answer(
    text: str, finish: types.FinishReason = types.FinishReason.STOP
) -> types.GenerateContentResponse:
    content = types.Content(role="model", parts=[types.Part(text=text)])
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=content, finish_reason=finish)]
    )


def answer_json(**fields: object) -> types.GenerateContentResponse:
    return answer(json.dumps(fields))


def interpreter(gemini: FakeGemini) -> Interpreter:
    return Interpreter(gemini, "gemini-test")


async def test_interprets_a_new_request() -> None:
    gemini = FakeGemini(answer_json(kind="new", plan=NEW_PLAN))

    result = await interpreter(gemini).interpret("night drive", LOFI)

    assert result.outcome is Outcome.INTERPRETED
    assert result.plan == MusicPlan.model_validate(NEW_PLAN)


async def test_a_refinement_keeps_what_it_leaves_out() -> None:
    refined = {"title": "Darker lo-fi", "prompts": [{"text": "lo-fi hip hop"}], "brightness": 0.0}
    gemini = FakeGemini(answer_json(kind="refine", plan=refined))

    result = await interpreter(gemini).interpret("darker", LOFI)

    assert result.plan is not None
    assert result.plan.brightness == 0.0
    kept = ("bpm", "scale", "density", "mute_drums")
    assert [getattr(result.plan, f) for f in kept] == [getattr(LOFI, f) for f in kept]


async def test_a_refinement_with_an_unspecified_scale_keeps_the_current_one() -> None:
    refined = NEW_PLAN | {"scale": "SCALE_UNSPECIFIED"}
    gemini = FakeGemini(answer_json(kind="refine", plan=refined))

    result = await interpreter(gemini).interpret("faster", LOFI)

    assert result.plan is not None
    assert result.plan.scale is LOFI.scale


async def test_refuses_requests_that_are_not_music() -> None:
    gemini = FakeGemini(answer_json(kind="not_music", plan=NEW_PLAN))

    result = await interpreter(gemini).interpret("what's the weather?", None)

    assert (result.outcome, result.plan) == (Outcome.NOT_MUSIC, None)


async def test_refuses_a_blank_request_without_a_call() -> None:
    gemini = FakeGemini()

    result = await interpreter(gemini).interpret(" 　", None)

    assert (result.outcome, gemini.calls) == (Outcome.NOT_MUSIC, [])


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
        RuntimeError("an unexpected bug"),
        "not JSON",
        json.dumps({"kind": "new", "plan": NEW_PLAN | {"bpm": 300}}),
        json.dumps({"kind": "new", "plan": NEW_PLAN | {"scale": "E_MINOR"}}),
        json.dumps({"kind": "new"}),
        "timeout",
    ],
    ids=["503", "429", "bug", "not JSON", "bpm", "scale", "no plan", "timeout"],
)
async def test_falls_back_to_the_request_text(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | str
) -> None:
    gemini = FakeGemini(answer_json(kind="new", plan=NEW_PLAN))
    if isinstance(failure, Exception):
        gemini.error = failure
    elif failure == "timeout":
        monkeypatch.setattr(sobafm.interpreter, "TIMEOUT_S", 0.01)
        gemini.delay = 1  # the answer is valid but late
    else:
        gemini.response = answer(failure)

    result = await interpreter(gemini).interpret("rainy lo-fi", LOFI)

    assert (result.outcome, result.plan) == (
        Outcome.FALLBACK,
        MusicPlan.from_request("rainy lo-fi"),
    )


async def test_sends_only_the_request_and_the_current_plan() -> None:
    gemini = FakeGemini(answer_json(kind="not_music"))

    await interpreter(gemini).interpret("雨の日のローファイ", LOFI)

    call = gemini.calls[0]
    assert "雨の日のローファイ" in call["contents"]  # as text, not escapes
    assert json.loads(call["contents"]) == {
        "request": "雨の日のローファイ",
        "current_plan": LOFI.model_dump(mode="json"),
    }
    assert (call["model"], call["config"]) == ("gemini-test", CONFIG)


def test_asks_for_the_interpretation_schema_without_docstrings() -> None:
    schema = json.dumps(CONFIG.response_json_schema)

    assert '"kind"' in schema
    assert '"description"' not in schema
    assert CONFIG.model_dump()["system_instruction"] == sobafm.interpreter.INSTRUCTION
    assert CONFIG.thinking_config == types.ThinkingConfig(
        thinking_level=types.ThinkingLevel.MINIMAL
    )
    assert CONFIG.automatic_function_calling == types.AutomaticFunctionCallingConfig(disable=True)


async def test_logs_request_text_only_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    echo = answer_json(kind="new", plan={"title": "a secret request"})  # invalid: no prompts
    gemini = FakeGemini(echo)

    with caplog.at_level(logging.INFO, logger="sobafm.interpreter"):
        await interpreter(gemini).interpret("a secret request", None)
    assert caplog.messages
    assert not any("secret" in message for message in caplog.messages)

    with caplog.at_level(logging.DEBUG, logger="sobafm.interpreter"):
        await interpreter(gemini).interpret("a secret request", None)
    assert any("secret" in message for message in caplog.messages)


async def test_logs_why_the_service_rejected_the_call(caplog: pytest.LogCaptureFixture) -> None:
    gemini = FakeGemini()
    gemini.error = errors.ClientError(
        400, {"error": {"code": 400, "message": "API key not valid.", "status": "INVALID_ARGUMENT"}}
    )

    with caplog.at_level(logging.WARNING, logger="sobafm.interpreter"):
        await interpreter(gemini).interpret("ambient", None)

    assert "400 INVALID_ARGUMENT: API key not valid." in caplog.text


def test_takes_the_sdk_async_client() -> None:
    client = genai.Client(api_key="key")

    Interpreter(client.aio, "gemini-test")  # pyright checks that it fits `Gemini`
