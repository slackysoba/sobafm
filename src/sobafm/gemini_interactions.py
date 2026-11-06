"""One bounded call to the Gemini Interactions API, shared by every request interpreter.

A caller supplies a fixed instruction, its input text and a response schema, and gets back
the model's text, which it must still validate in full. `store` is disabled on every call,
the API key appears only in a request header and nothing here logs a request or a response.

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


class GeminiFailed(Exception):
    """No validated answer exists. ``kind`` is safe to log; it carries no provider text."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


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


def output_text(payload: bytes) -> str:
    """The model's text from an interaction, requiring a completed status."""
    interaction = as_mapping(load_json(payload, "response_not_json"))
    if interaction is None:
        raise GeminiFailed("response_shape")
    if interaction.get("status") != "completed":
        raise GeminiFailed("interaction_not_completed")
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
            raise GeminiFailed(f"http_{status}")
        return output_text(payload)

    async def close(self) -> None:
        await self._transport.close()
