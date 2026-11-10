"""One bounded call to the Gemini Interactions API, shared by every request interpreter.

A caller supplies a fixed instruction, its input text and a response schema, and gets back
the model's text, which it must still validate in full. `store` is disabled on every call,
the API key appears only in a request header and nothing here logs a request or a response.
When Gemini says it blocked a call, `GeminiFailed.blocked` records that and nothing it wrote.

This uses the documented REST API over the `aiohttp` already in the lock because the
service runs under a 90 MB whole-service limit (ADR 0004). See ADR 0006.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Protocol, cast

import aiohttp

# Pinned after checking official documentation on 2026-11-05. Recheck behavior with the
# opt-in evaluations before changing any of these.
MODEL = "gemini-3.5-flash-lite"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_REVISION = "2026-05-20"

TIMEOUT_SECONDS = 10.0
# Calls in flight across all servers. A request body exists only while its call holds one
# of these, so together they bound the memory that bodies can use.
MAX_CONCURRENT_CALLS = 4
MAX_OUTPUT_TOKENS = 2_048
MAX_RESPONSE_BYTES = 64_000
# Every code Gemini documents as a block on this API, checked on 2026-11-05. `jailbreak` comes
# from the safety settings page and the rest from the API errors page's blocked table:
# https://aistudio.google.com/docs/safety-settings#when-content-is-blocked
# https://aistudio.google.com/docs/api-errors#generation-blocked-codes
BLOCK_CODES = frozenset(
    {
        "safety",
        "jailbreak",
        "prohibited_content",
        "blocklist",
        "recitation",
        "language",
        "spii",
        "image_safety",
        "image_prohibited_content",
        "image_recitation",
        "image_other",
        "content_blocked",
    }
)


class GeminiFailed(Exception):
    """No validated answer exists. ``kind`` is safe to log; it carries no provider text.

    ``blocked`` means Gemini says it blocked the request or its output, which is a verdict on
    the text rather than a fault. Its reason and message are not kept.
    """

    def __init__(self, kind: str, *, blocked: bool = False) -> None:
        super().__init__(kind)
        self.kind = kind
        self.blocked = blocked


class Transport(Protocol):
    async def post(
        self, url: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, bytes]: ...

    async def close(self) -> None: ...


class AiohttpTransport:
    """Bounded POSTs with no redirects, retries or environment proxies; none after close."""

    def __init__(self, *, timeout: float = TIMEOUT_SECONDS) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    async def post(self, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        if self._closed:  # a late request must not open a session nobody would close
            raise aiohttp.ClientConnectionError("transport is closed")
        if self._session is None:  # created here so it binds to the running event loop
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=MAX_CONCURRENT_CALLS, force_close=True),
                timeout=self._timeout,
            )
        async with self._session.post(
            url, data=body, headers=dict(headers), allow_redirects=False
        ) as response:
            # These statuses already determine the outcome. An oversized or stalled
            # error body must not hide it; only HTTP 400 can carry a documented block.
            if response.status in (401, 402, 403, 404, 408, 429) or 500 <= response.status < 600:
                return response.status, b""
            payload = bytearray()
            while chunk := await response.content.read(MAX_RESPONSE_BYTES + 1 - len(payload)):
                payload += chunk
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise GeminiFailed("response_too_large")
            return response.status, bytes(payload)

    async def close(self) -> None:
        self._closed = True
        session, self._session = self._session, None
        if session is not None:
            await session.close()


def load_json(payload: str | bytes, failure: str) -> object:
    try:
        return cast(object, json.loads(payload))
    except (ValueError, RecursionError) as exc:
        raise GeminiFailed(failure) from exc


def as_mapping(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _items(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _is_block(error: object) -> bool:
    fields = as_mapping(error)
    code = fields.get("code") if fields is not None else None
    return isinstance(code, str) and code in BLOCK_CODES


def _body_is_block(payload: bytes) -> bool:
    """Whether a response body's `error.code` is a block; an unreadable body isn't one."""
    try:
        document = as_mapping(load_json(payload, "response_not_json"))
    except GeminiFailed:
        return False
    return document is not None and _is_block(document.get("error"))


def output_text(payload: bytes) -> str:
    """The model's text from an interaction, requiring a completed status."""
    interaction = as_mapping(load_json(payload, "response_not_json"))
    if interaction is None:
        raise GeminiFailed("response_shape")
    status = interaction.get("status")
    if status != "completed":
        # Documented for background calls, which this client doesn't make. No other status has
        # a documented block: `incomplete` with empty text means the tokens ran out.
        blocked = status == "failed" and any(map(_is_block, _items(interaction.get("errors"))))
        raise GeminiFailed("interaction_not_completed", blocked=blocked)
    for step in reversed(_items(interaction.get("steps"))):
        fields = as_mapping(step)
        if fields is None or fields.get("type") != "model_output":
            continue
        texts: list[str] = []
        for part in _items(fields.get("content")):
            block = as_mapping(part)
            if block is not None and block.get("type") == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise GeminiFailed("response_shape")
                texts.append(text)
        if texts:
            return "".join(texts)
        break
    raise GeminiFailed("response_shape")


def interaction_body(*, system_instruction: str, text: str, schema: Mapping[str, object]) -> bytes:
    """A JSON-output request with no tools or conversation, and with `store` off."""
    return json.dumps(
        {
            "model": MODEL,
            "system_instruction": system_instruction,
            "input": text,
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
            },
            "generation_config": {"max_output_tokens": MAX_OUTPUT_TOKENS},
            "store": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class GeminiClient:
    """The API key, the transport and the limit on calls in flight, for the whole process.

    Sharing one client keeps `MAX_CONCURRENT_CALLS` a single bound across every caller.
    """

    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._transport: Transport = transport if transport is not None else AiohttpTransport()
        self._timeout = timeout
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

    async def interact(self, build_body: Callable[[], bytes]) -> str:
        """Make one call, with no retries, and return the model's text or `GeminiFailed`."""
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
            "Api-Revision": API_REVISION,
        }
        try:
            # Waiting for a slot counts toward the budget, and the body is built only once
            # this call holds one.
            async with asyncio.timeout(self._timeout), self._slots:
                status, payload = await self._transport.post(ENDPOINT, headers, build_body())
        except TimeoutError as exc:
            raise GeminiFailed("timeout") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise GeminiFailed("transport_error") from exc
        if status != 200:
            # A block on a foreground call is documented as a 400 and only a 400.
            raise GeminiFailed(f"http_{status}", blocked=status == 400 and _body_is_block(payload))
        return output_text(payload)

    async def close(self) -> None:
        await self._transport.close()
