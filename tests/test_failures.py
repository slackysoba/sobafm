import aiohttp
import pytest
from google.genai import errors
from websockets.datastructures import Headers
from websockets.exceptions import InvalidMessage, InvalidStatus
from websockets.http11 import Response

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


def refused_upgrade(status: int) -> InvalidStatus:
    """How connecting fails when the server refuses Lyria RealTime's WebSocket upgrade."""
    return InvalidStatus(Response(status, "Refused", Headers()))


def malformed_upgrade(*, dropped: bool) -> InvalidMessage:
    """How connecting fails on a malformed upgrade response, or a connection that drops first."""
    error = InvalidMessage("did not receive a valid HTTP response")
    if dropped:
        error.__cause__ = EOFError("connection closed while reading HTTP status line")
    return error


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (api_error(400, "INVALID_ARGUMENT", "API_KEY_INVALID"), Failure.REJECTED),
        (api_error(401, "UNAUTHENTICATED"), Failure.REJECTED),
        (api_error(403, "PERMISSION_DENIED"), Failure.REJECTED),
        (api_error(429, "RESOURCE_EXHAUSTED"), Failure.EXHAUSTED),
        (api_error(503, "UNAVAILABLE"), Failure.UNAVAILABLE),
        (api_error(500, "INTERNAL"), Failure.UNAVAILABLE),
        (api_error(400, "INVALID_ARGUMENT"), None),
        (TimeoutError(), Failure.UNAVAILABLE),
        (ConnectionResetError("reset by peer"), Failure.UNAVAILABLE),
        (aiohttp.ServerDisconnectedError(), Failure.UNAVAILABLE),  # not an OSError
        (aiohttp.ClientPayloadError("Response payload is not completed"), Failure.UNAVAILABLE),
        (malformed_upgrade(dropped=True), Failure.UNAVAILABLE),
        (malformed_upgrade(dropped=False), None),
        (refused_upgrade(403), Failure.REJECTED),
        (refused_upgrade(429), Failure.EXHAUSTED),
        (refused_upgrade(503), Failure.UNAVAILABLE),
        (refused_upgrade(404), None),
        (ValueError("a bug"), None),
    ],
    ids=[
        "invalid key",
        "401",
        "403",
        "429",
        "503",
        "500",
        "400",
        "timeout",
        "reset",
        "disconnected",
        "cut off",
        "upgrade dropped",
        "upgrade malformed",
        "upgrade 403",
        "upgrade 429",
        "upgrade 503",
        "upgrade 404",
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
        (1011, "Quota exceeded for this API key.", Failure.EXHAUSTED),  # the quota decides
        (1008, "RESOURCE_EXHAUSTED", Failure.EXHAUSTED),
        (1006, "abnormal closure", Failure.UNAVAILABLE),  # as the SDK reports a dropped connection
        (1013, None, Failure.UNAVAILABLE),
        (1008, "Your project has been denied access. Please contact support.", None),
        (1007, "Request contains an invalid argument.", None),
        (1008, "Consumer 'api_key:AIzaFakeKey' has been suspended.", Failure.REJECTED),
        (1000, "", None),
        (1000, "API key not valid.", None),
    ],
    ids=[
        "invalid key",
        "outage",
        "quota",
        "exhausted",
        "quota and key",
        "token",
        "dropped",
        "try again later",
        "denied",
        "invalid argument",
        "suspended key",
        "normal",
        "normal, mentioning a key",
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
