import aiohttp
import pytest
from google.genai import errors

from sobafm.failures import Failure, as_token, call_failure, close_failure


def api_error(code: int, status: str, reason: str | None = None) -> errors.APIError:
    """An API error as Gemini reports it, with an ErrorInfo reason when `reason` is set."""
    info = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    body = {
        "code": code,
        "status": status,
        "message": "free text",
        "details": info if reason else [],
    }
    return errors.APIError(code, {"error": body})


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (api_error(400, "INVALID_ARGUMENT", "API_KEY_INVALID"), Failure.REJECTED),
        (api_error(401, "UNAUTHENTICATED"), Failure.REJECTED),
        (api_error(403, "PERMISSION_DENIED"), Failure.REJECTED),
        (api_error(429, "RESOURCE_EXHAUSTED"), Failure.EXHAUSTED),
        (api_error(503, "UNAVAILABLE"), Failure.UNAVAILABLE),
        (api_error(400, "INVALID_ARGUMENT"), None),
        (TimeoutError(), Failure.UNAVAILABLE),
        (ConnectionResetError("reset by peer"), Failure.UNAVAILABLE),
        (aiohttp.ServerDisconnectedError(), Failure.UNAVAILABLE),  # not an OSError
        (ValueError("a bug"), None),
    ],
    ids=[
        "invalid key",
        "401",
        "403",
        "429",
        "503",
        "400",
        "timeout",
        "reset",
        "disconnected",
        "other",
    ],
)
def test_names_why_a_call_failed(error: BaseException, failure: Failure | None) -> None:
    assert call_failure(error) is failure


@pytest.mark.parametrize(
    ("code", "reason", "failure"),
    [
        (1007, "API key not valid. Please pass a valid API key.", Failure.REJECTED),
        (1011, "The service is currently unavailable.", Failure.UNAVAILABLE),
        (
            1011,
            "You exceeded your current quota, please check your plan and billing details.",
            Failure.EXHAUSTED,
        ),
        (1011, "Resource has been exhausted (e.g. check quota).", Failure.EXHAUSTED),
        (1008, "RESOURCE_EXHAUSTED", Failure.EXHAUSTED),
        (1006, "abnormal closure", Failure.UNAVAILABLE),  # as the SDK reports a dropped connection
        (1013, None, Failure.UNAVAILABLE),
        (1008, "Your project has been denied access. Please contact support.", None),
        (1000, "", None),
    ],
    ids=[
        "invalid key",
        "outage",
        "quota",
        "exhausted",
        "token",
        "dropped",
        "try again later",
        "denied",
        "normal",
    ],
)
def test_names_why_lyria_closed_a_session(
    code: int, reason: str | None, failure: Failure | None
) -> None:
    assert close_failure(code, reason) is failure


@pytest.mark.parametrize(
    ("value", "token"),
    [("API_KEY_INVALID", "API_KEY_INVALID"), ("API key not valid", None), (None, None), ("", None)],
)
def test_keeps_only_tokens(value: object, token: str | None) -> None:
    assert as_token(value) == token
