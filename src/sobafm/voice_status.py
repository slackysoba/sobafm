"""Best-effort voice-channel status, owned on the event loop apart from playback."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal, cast

import aiohttp
import disnake
from aiohttp.abc import AbstractStreamWriter
from aiohttp.payload import JsonPayload
from disnake.http import HTTPClient, Route

from sobafm.credits import escaped_text
from sobafm.station import Audio, StationAudio

log = logging.getLogger(__name__)

# disnake 2.12.2 preserves the raw permission bits but has no named bit or setter.
SET_VOICE_CHANNEL_STATUS = 1 << 48
MAX_CHANNELS = 4  # includes previous channels until their last write settles
WRITE_SECONDS = 1.25
# Once admitted to the transport, keep the channel's one writer through the
# response, including past WRITE_SECONDS. A lost acknowledgement has a finite end.
RESPONSE_SECONDS = 30.0
_writing_status = ContextVar("writing_voice_status", default=False)


def _other_http_requests(record: logging.LogRecord) -> bool:
    # The SDK logs the raw URL, channel ID and payload at DEBUG.
    return not _writing_status.get()


class _Refused(Exception):
    """No confirmed write, including a stale request dropped before dispatch."""


class _StatusBody(JsonPayload):
    """Check the desired update when aiohttp has a connection ready to write it."""

    def __init__(self, text: str | None, dispatch: Callable[[], Awaitable[None]]) -> None:
        super().__init__({"status": text})
        self._dispatch = dispatch

    async def write_with_length(
        self, writer: AbstractStreamWriter, content_length: int | None
    ) -> None:
        await self._dispatch()
        await super().write_with_length(writer, content_length)


async def _send_once(
    request: aiohttp.ClientRequest, send: aiohttp.ClientHandlerType
) -> aiohttp.ClientResponse:
    try:
        return await send(request)
    except aiohttp.ClientError, OSError:
        # No SDK/connection retry of a potentially applied PUT.
        log.warning("voice status update unavailable")
        raise _Refused from None


class _RateLimited(Exception):
    def __init__(self, delay: float, *, globally: bool) -> None:
        self.delay = delay
        self.globally = globally


class _GlobalGate(asyncio.Event):
    """The SDK's global gate, with an independently timed hold for status 429s.

    Installed on the fresh client before login. The SDK still owns its ordinary
    clear/set pair. Neither its release nor ours can release the other's hold.
    """

    def __init__(self, sdk: asyncio.Event) -> None:
        super().__init__()
        self._sdk = sdk
        self._resume: asyncio.TimerHandle | None = None
        super().set()

    @property
    def until(self) -> float:
        return self._resume.when() if self._resume is not None else 0.0

    def hold(self, until: float) -> None:
        if until <= self.until:
            return
        if self._resume is not None:
            self._resume.cancel()
        super().clear()
        self._resume = asyncio.get_running_loop().call_at(until, self._release_status)

    def set(self) -> None:
        self._sdk.set()

    def clear(self) -> None:
        self._sdk.clear()

    def is_set(self) -> bool:
        return self._sdk.is_set() and super().is_set()

    async def wait(self) -> Literal[True]:
        while not self.is_set():
            await self._sdk.wait()
            await super().wait()
        return True

    def _release_status(self) -> None:
        self._resume = None
        super().set()


def _description(heard: StationAudio) -> str:
    if not isinstance(heard, Audio):
        return "Generated music"  # the plan can echo a private request
    source = heard.recording.key.source.value
    title = heard.recording.title
    # Status has no allowed_mentions or suppress_embeds flags. Omit a title
    # containing link or mention syntax, including bare domains and invites.
    if any(c in title for c in ".:/@<>\\"):
        return f"Catalog music · {source}"
    return f"{escaped_text(title[:80])}{'…' if len(title) > 80 else ''} · {source}"


def _permitted(channel: disnake.VoiceChannel) -> bool:
    perms = channel.permissions_for(channel.guild.me)
    return perms.administrator or bool(perms.value & SET_VOICE_CHANNEL_STATUS)


@dataclass
class _Channel:
    guild_id: int
    channel: disnake.VoiceChannel
    wanted: str | None = None
    published: bool = False
    waiting_until: float = 0.0
    task: asyncio.Task[None] | None = None


class VoiceStatuses:
    """One writer and one replaceable desired value per channel, across reconnects."""

    def __init__(self, http: HTTPClient) -> None:
        self._http = http
        logging.getLogger("disnake.http").addFilter(_other_http_requests)
        self._channels: dict[int, _Channel] = {}
        self._closing = False
        self._expired = False
        self._deadline = math.inf
        self._last_settlement = -math.inf
        self._global = _GlobalGate(http._global_over)  # pyright: ignore[reportPrivateUsage]
        http._global_over = self._global  # pyright: ignore[reportPrivateUsage]

    def update(
        self, guild_id: int, channel: disnake.VoiceChannel | None, heard: StationAudio | None
    ) -> None:
        if self._closing:
            return
        target = channel.id if channel is not None and heard is not None else None
        for channel_id, state in self._channels.items():
            if state.guild_id == guild_id and channel_id != target:
                self._want(state, None)
        if channel is None or heard is None:
            return
        state = self._channels.get(channel.id)
        if state is None:
            if len(self._channels) >= MAX_CHANNELS:
                return  # clears and already admitted writers keep their slots
            state = self._channels[channel.id] = _Channel(guild_id, channel)
        state.channel = channel
        self._want(state, _description(heard))

    def _want(self, state: _Channel, text: str | None) -> None:
        if state.wanted == text:
            return
        state.wanted = text
        if state.task is None:
            state.task = asyncio.create_task(self._publish(state))

    async def _publish(self, state: _Channel) -> None:
        loop = asyncio.get_running_loop()
        previous: str | None = None
        refusals = 0
        try:
            while not self._expired:
                until = max(state.waiting_until, self._global.until)
                wait = until - loop.time()
                if wait > 0:
                    if self._closing and (
                        not state.published or time.monotonic() + wait >= self._deadline
                    ):
                        return  # final clear cannot be admitted before shutdown
                    state.waiting_until = until
                    await asyncio.sleep(wait)
                    continue  # the delay or desired value may have changed
                state.waiting_until = 0
                text = state.wanted
                if text is None and not state.published:
                    return  # no acknowledged title from us to clear
                if previous != text:
                    previous, refusals = text, 0
                if refusals >= 2:
                    return  # one retry of an unchanged value, respecting both cooldowns
                try:
                    if _permitted(state.channel):
                        await self._write(state, text)
                except _RateLimited as limit:
                    state.waiting_until = loop.time() + limit.delay
                    refusals += 1
                    continue
                except _Refused:
                    pass
                except TimeoutError:
                    if self._global.until > loop.time():
                        continue  # a newly advertised global delay during admission
                    log.warning("voice status update unavailable")
                except Exception:
                    # The SDK or HTTP exception can contain credentials, IDs and URLs.
                    log.warning("voice status update unavailable")
                if state.wanted == text:
                    return
        finally:
            self._last_settlement = time.monotonic()
            state.task = None
            if state.wanted is None or not state.published:
                del self._channels[state.channel.id]

    async def _write(self, state: _Channel, text: str | None) -> None:
        route = Route("PUT", "/channels/{channel_id}/voice-status", channel_id=state.channel.id)
        context = _writing_status.set(True)
        try:
            async with asyncio.timeout(WRITE_SECONDS) as admission:

                async def dispatch() -> None:
                    # The SDK checked its global gate before the bucket and
                    # connector waits. A fresh global hold may now be in effect.
                    await self._global.wait()
                    if state.wanted != text or self._expired or not _permitted(state.channel):
                        raise _Refused
                    admission.reschedule(asyncio.get_running_loop().time() + RESPONSE_SECONDS)

                async def check(response: aiohttp.ClientResponse) -> None:
                    if response.status == 204:
                        # Record the actual ACK even if the SDK later fails to
                        # parse a rate-limit header or an empty JSON response.
                        state.published = text is not None
                    else:
                        await self._refusal(response)

                await self._http.request(
                    route,
                    data=_StatusBody(text, dispatch),
                    allow_redirects=False,
                    auto_decompress=False,
                    read_bufsize=1024,
                    middlewares=(_send_once,),
                    raise_for_status=check,
                )
        finally:
            _writing_status.reset(context)

    async def _refusal(self, response: aiohttp.ClientResponse) -> None:
        # Let disnake handle its ordinary global wait, shared session, route lock
        # and successful bucket headers. Its error path reads an unbounded body
        # and cancellation during a global 429 sleep can strand every command.
        log.warning("voice status update refused: status=%d", response.status)
        try:
            if response.status == 429:
                limit = await _rate_limit(response)
                if limit.globally:
                    self._global.hold(asyncio.get_running_loop().time() + limit.delay)
                raise limit
        finally:
            response.close()
        raise _Refused

    async def close(self, deadline: float) -> bool:
        """Finish final clears within the original cutoff; retain all late owners."""
        self._closing = True
        self._deadline = deadline = min(self._deadline, deadline)
        for state in self._channels.values():
            self._want(state, None)
            remaining = state.waiting_until - asyncio.get_running_loop().time()
            if (
                state.task is not None
                and remaining > 0
                and (not state.published or time.monotonic() + remaining >= deadline)
            ):
                state.task.cancel()  # sleeping after a refusal; no PUT in flight
        tasks = {state.task for state in self._channels.values() if state.task is not None}
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - time.monotonic()))
            self._expired |= bool(pending)
            for task in pending:
                task.cancel()
        if self._expired or self._last_settlement > deadline:
            self._expired = True
            log.error("voice status update missed its shutdown deadline")
            return False  # tasks still own their slots through actual settlement
        return True


async def _rate_limit(response: aiohttp.ClientResponse) -> _RateLimited:
    scope = response.headers.get("X-RateLimit-Scope")
    globally = scope == "global" or response.headers.get("X-RateLimit-Global") == "true"
    delay = _retry_seconds(response.headers.get("Retry-After"))
    if delay is None or (scope is None and not globally):
        # Some replies carry scope or delay only in JSON. Read at most 1 KiB.
        try:
            await response.content.readexactly(1025)
        except asyncio.IncompleteReadError as exc:
            try:
                body = json.loads(exc.partial)
            except ValueError, RecursionError, UnicodeDecodeError:
                body = None
            if isinstance(body, dict):
                fields = cast(dict[str, object], body)
                globally = globally or fields.get("global") is True
                if delay is None:
                    delay = _retry_seconds(fields.get("retry_after"))
    return _RateLimited(60.0 if delay is None else delay, globally=globally)


def _retry_seconds(value: object) -> float | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except ValueError, OverflowError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None
