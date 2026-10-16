"""Exercise the interpreter seam with a plan unrelated to Lyria or a catalog."""

import asyncio
import json
import logging
from typing import assert_type

import pytest
from google.genai import errors, types
from pydantic import BaseModel, Field

import sobafm.interpreter
from sobafm.failures import Failure
from sobafm.interpreter import GENERATED_TASK, Interpretation, Interpreter, Outcome, Result, Task
from sobafm.plan import MusicPlan
from tests.doubles import FakeGemini, answer


class OtherPlan(BaseModel):
    """A different plan whose docstring must not reach Gemini."""

    direction: str
    pace: int | None = Field(default=None, ge=0, le=10)


def unavailable(
    _request: str, _current: OtherPlan | None, failure: Failure | None
) -> Result[OtherPlan]:
    return Result(Outcome.UNAVAILABLE, None, failure)


def refine(current: OtherPlan, plan: OtherPlan) -> OtherPlan:
    return plan.model_copy(update={"pace": current.pace}) if plan.pace is None else plan


OTHER_TASK = Task(
    "Use the alternate answer schema.", Interpretation[OtherPlan], unavailable, refine
)
CURRENT = OtherPlan(direction="piano", pace=3)


def response(kind: str, plan: object = None) -> types.GenerateContentResponse:
    return answer(json.dumps({"kind": kind, "plan": plan}))


async def test_tasks_keep_their_own_instruction_schema_and_plan_types() -> None:
    gemini = FakeGemini(response("new", {"direction": "cello", "pace": 5}))
    alternate = Interpreter(gemini, "alternate-model", OTHER_TASK)
    assert_type(alternate, Interpreter[OtherPlan])

    result = await alternate.interpret("cello", CURRENT)

    assert_type(result, Result[OtherPlan])
    assert result == Result(Outcome.INTERPRETED, OtherPlan(direction="cello", pace=5))
    [call] = gemini.calls
    assert call["model"] == "alternate-model"
    assert json.loads(call["contents"]) == {
        "request": "cello",
        "current_plan": CURRENT.model_dump(mode="json"),
    }
    config = call["config"]
    assert config.system_instruction == OTHER_TASK.instruction
    assert config.response_json_schema == sobafm.interpreter.without_descriptions(
        Interpretation[OtherPlan].model_json_schema()
    )
    schema = json.dumps(config.response_json_schema)
    assert '"direction"' in schema
    assert '"prompts"' not in schema
    assert '"description"' not in schema
    assert config.response_mime_type == "application/json"
    assert config.thinking_config == sobafm.interpreter.CONFIG.thinking_config
    assert config.automatic_function_calling == types.AutomaticFunctionCallingConfig(disable=True)

    generated_gemini = FakeGemini()
    generated = Interpreter(generated_gemini, "generated-model")
    assert_type(generated, Interpreter[MusicPlan])
    generated_result = await generated.interpret("piano", None)
    assert_type(generated_result, Result[MusicPlan])
    assert generated_result.plan == MusicPlan.from_request("piano")
    assert generated_gemini.calls[0]["config"] == sobafm.interpreter.CONFIG
    assert generated_gemini.calls[0]["config"].system_instruction == GENERATED_TASK.instruction


@pytest.mark.parametrize(
    ("kind", "has_current", "pace"),
    [("new", True, None), ("refine", False, None), ("refine", True, 3)],
)
async def test_task_controls_the_refinement_merge(
    kind: str, has_current: bool, pace: int | None
) -> None:
    gemini = FakeGemini(response(kind, {"direction": "cello"}))

    result = await Interpreter(gemini, "test", OTHER_TASK).interpret(
        "cello", CURRENT if has_current else None
    )

    assert result == Result(Outcome.INTERPRETED, OtherPlan(direction="cello", pace=pace))
    assert OtherPlan(direction="piano", pace=3) == CURRENT


@pytest.mark.parametrize("current", [None, CURRENT])
@pytest.mark.parametrize(
    ("failure", "cause"),
    [
        (errors.ServerError(503, {"error": {"code": 503}}), Failure.UNAVAILABLE),
        (errors.ClientError(429, {"error": {"code": 429}}), Failure.EXHAUSTED),
        (errors.ClientError(403, {"error": {"code": 403}}), Failure.REJECTED),
        (RuntimeError("unexpected"), None),
        ("not JSON", None),
        ('{"kind":"new","plan":{"pace":4}}', None),
        ('{"kind":"new"}', None),
        ('{"kind":"refine"}', None),
        ("timeout", Failure.UNAVAILABLE),
    ],
    ids=["outage", "quota", "key", "bug", "JSON", "invalid", "new", "refine", "timeout"],
)
async def test_task_can_fail_without_turning_the_request_into_a_plan(
    monkeypatch: pytest.MonkeyPatch,
    current: OtherPlan | None,
    failure: Exception | str,
    cause: Failure | None,
) -> None:
    gemini = FakeGemini()
    if isinstance(failure, Exception):
        gemini.error = failure
    elif failure == "timeout":
        monkeypatch.setattr(sobafm.interpreter, "TIMEOUT_S", 0.01)
        gemini.held = asyncio.Event()
    else:
        gemini.response = answer(failure)

    result = await Interpreter(gemini, "test", OTHER_TASK).interpret("raw member words", current)

    assert result == Result(Outcome.UNAVAILABLE, None, cause)
    assert len(gemini.calls) == 1
    assert OtherPlan(direction="piano", pace=3) == CURRENT


@pytest.mark.parametrize(
    ("text", "reply", "outcome", "calls"),
    [
        ("\u202e \u2066", None, Outcome.NOT_MUSIC, 0),
        ("weather", response("not_music", {"pace": 100}), Outcome.NOT_MUSIC, 1),
        ("blocked", answer("", finish=types.FinishReason.SAFETY), Outcome.BLOCKED, 1),
        (
            "blocked",
            types.GenerateContentResponse(
                prompt_feedback=types.GenerateContentResponsePromptFeedback(
                    block_reason=types.BlockedReason.SAFETY
                )
            ),
            Outcome.BLOCKED,
            1,
        ),
    ],
    ids=["blank", "not_music with invalid plan", "candidate safety", "prompt safety"],
)
async def test_refusals_do_not_use_the_task_failure_or_merge_policy(
    text: str, reply: types.GenerateContentResponse | None, outcome: Outcome, calls: int
) -> None:
    def forbidden_fallback(
        _request: str, _current: OtherPlan | None, _failure: Failure | None
    ) -> Result[OtherPlan]:
        pytest.fail("a refusal reached the task's failure policy")

    def forbidden_merge(_current: OtherPlan, _plan: OtherPlan) -> OtherPlan:
        pytest.fail("a refusal reached the task's refinement merge")

    task = Task(
        OTHER_TASK.instruction, OTHER_TASK.answer_model, forbidden_fallback, forbidden_merge
    )
    gemini = FakeGemini(reply)

    result = await Interpreter(gemini, "test", task).interpret(text, CURRENT)

    assert result == Result(outcome, None)
    assert len(gemini.calls) == calls


async def test_unavailable_failure_logs_do_not_claim_a_raw_text_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gemini = FakeGemini()
    gemini.error = errors.ClientError(
        403, {"error": {"code": 403, "message": "api_key:AIzaSecretKey"}}
    )

    with caplog.at_level(logging.WARNING, logger="sobafm.interpreter"):
        await Interpreter(gemini, "test", OTHER_TASK).interpret("private request", CURRENT)

    assert "403" in caplog.text
    assert "unavailable" in caplog.text
    assert "using the request text" not in caplog.text
    assert "private request" not in caplog.text
    assert "AIzaSecretKey" not in caplog.text


async def test_cancellation_does_not_invoke_a_task_fallback() -> None:
    gemini = FakeGemini()
    gemini.held = asyncio.Event()
    pending = asyncio.create_task(
        Interpreter(gemini, "test", OTHER_TASK).interpret("piano", CURRENT)
    )
    await asyncio.sleep(0)
    pending.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending
