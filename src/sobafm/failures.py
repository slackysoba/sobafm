"""What SobaFM can tell from Google's errors: causes a requester can act on, and safe text."""

import re
from enum import StrEnum
from typing import cast

import aiohttp
from google.genai import errors

TOKEN = re.compile(r"[A-Z][A-Z0-9_]{0,62}")  # an API status or reason, such as API_KEY_INVALID
# How Google's live APIs word the close for an exhausted quota, which shares code 1011 with outages
EXHAUSTED_REASON = re.compile(r"quota|exhausted", re.IGNORECASE)
REJECTED_CLOSE = 1007  # how Lyria RealTime closes a session whose API key it rejects
UNAVAILABLE_CLOSES = {1006, 1011, 1013}  # a dropped connection, an internal error, try again later


class Failure(StrEnum):
    REJECTED = "rejected"  # Google rejected the API key
    EXHAUSTED = "exhausted"  # a quota or rate limit
    UNAVAILABLE = "unavailable"  # the service is down or unreachable


def as_token(value: object) -> str | None:
    """`value` if it is a token such as PERMISSION_DENIED, which cannot carry free text."""
    return value if isinstance(value, str) and TOKEN.fullmatch(value) else None


def call_failure(error: BaseException) -> Failure | None:
    """Why an API call failed, when the cause is one a requester can act on."""
    match error:
        case errors.APIError() if error.code in (401, 403) or (
            as_token(error_reason(error)) == "API_KEY_INVALID"
        ):
            return Failure.REJECTED
        case errors.APIError() if error.code == 429:
            return Failure.EXHAUSTED
        case errors.APIError() if error.code >= 500:
            return Failure.UNAVAILABLE
        case OSError() | aiohttp.ClientConnectionError():  # including timeouts
            return Failure.UNAVAILABLE
        case _:
            return None


def close_failure(code: int, reason: object) -> Failure | None:
    """Why Lyria RealTime closed a session.

    The reason is free text that could quote the API key, so it is matched but never kept.
    """
    if isinstance(reason, str) and EXHAUSTED_REASON.search(reason):
        return Failure.EXHAUSTED
    if code == REJECTED_CLOSE:
        return Failure.REJECTED
    if code in UNAVAILABLE_CLOSES:
        return Failure.UNAVAILABLE
    return None


def error_reason(error: errors.APIError) -> object:
    """The `reason` of the error's `google.rpc.ErrorInfo`, such as API_KEY_INVALID."""
    details: object = getattr(error, "details", None)
    body = cast(dict[str, object], details).get("error") if isinstance(details, dict) else None
    items = cast(dict[str, object], body).get("details") if isinstance(body, dict) else None
    for item in cast(list[object], items) if isinstance(items, list) else []:
        fields = cast(dict[str, object], item) if isinstance(item, dict) else {}
        if str(fields.get("@type", "")).endswith("ErrorInfo"):
            return fields.get("reason")
    return None
