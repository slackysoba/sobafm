"""Best-effort voice-channel status, owned on the event loop apart from playback."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextvars import ContextVar
from dataclasses import dataclass

import aiohttp
import disnake
from disnake.http import HTTPClient, Route

from sobafm.credits import escaped_text
from sobafm.station import Audio, StationAudio

log = logging.getLogger(__name__)

# disnake 2.12.2 preserves the raw permission bits but has no named bit or setter.
SET_VOICE_CHANNEL_STATUS = 1 << 48
MAX_CHANNELS = 4  # includes previous channels until their last write settles
WRITE_SECONDS = 1.25
_writing_status = ContextVar("writing_voice_status", default=False)


def _other_http_requests(record: logging.LogRecord) -> bool:
    # The SDK logs the raw URL, channel ID and payload at DEBUG.
    return not _writing_status.get()


class _Refused(Exception):
    """The server returned a status other than the documented empty success."""


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


@dataclass
class _Channel:
    guild_id: int
    channel: disnake.VoiceChannel
    wanted: str | None = None
    attempted: bool = False
    task: asyncio.Task[None] | None = None


class VoiceStatuses:
    """One writer and one replaceable desired value per channel, across reconnects."""

    def __init__(self, http: HTTPClient) -> None:
        self._http = http
        logging.getLogger("disnake.http").addFilter(_other_http_requests)
        self._channels: dict[int, _Channel] = {}
        self._closing = False
        self._expired = False
        self._last_settlement = -math.inf
        self._rate_limited_until = -math.inf

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
        try:
            while True:
                text = state.wanted
                try:
                    perms = state.channel.permissions_for(state.channel.guild.me)
                    if (
                        (text is not None or state.attempted)
                        and (perms.administrator or perms.value & SET_VOICE_CHANNEL_STATUS)
                        and time.monotonic() >= self._rate_limited_until
                    ):
                        previously_attempted = state.attempted
                        state.attempted = True  # even a timeout may have reached Discord
                        async with asyncio.timeout(WRITE_SECONDS):
                            applied = await self._write(state.channel.id, text)
                        state.attempted = previously_attempted or applied
                except Exception:
                    # The SDK or HTTP exception can contain credentials, IDs and URLs.
                    log.warning("voice status update unavailable")
                if state.wanted == text or self._expired:
                    return
        finally:
            self._last_settlement = time.monotonic()
            state.task = None
            if state.wanted is None or not state.attempted:
                del self._channels[state.channel.id]

    async def _write(self, channel_id: int, text: str | None) -> bool:
        route = Route("PUT", "/channels/{channel_id}/voice-status", channel_id=channel_id)
        context = _writing_status.set(True)
        try:
            await self._http.request(
                route,
                json={"status": text},
                allow_redirects=False,
                auto_decompress=False,
                read_bufsize=1024,
                raise_for_status=self._check_response,
            )
        except _Refused:
            return False
        finally:
            _writing_status.reset(context)
        return True

    async def _check_response(self, response: aiohttp.ClientResponse) -> None:
        # Let disnake handle its ordinary global wait, shared session, route lock
        # and successful bucket headers. Its error path reads an unbounded body
        # and cancellation during a global 429 sleep can strand every command.
        if response.status == 204:
            return
        response.close()
        if response.status == 429:
            try:
                delay = float(response.headers.get("Retry-After", ""))
            except ValueError:
                delay = math.nan
            until = time.monotonic() + (delay if math.isfinite(delay) and delay > 0 else 60)
            self._rate_limited_until = max(self._rate_limited_until, until)
        log.warning("voice status update refused: status=%d", response.status)
        raise _Refused

    async def close(self, deadline: float) -> bool:
        """Finish final clears within the original cutoff; retain all late owners."""
        self._closing = True
        for state in self._channels.values():
            self._want(state, None)
        tasks = {state.task for state in self._channels.values() if state.task is not None}
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - time.monotonic()))
            self._expired = bool(pending)
            for task in pending:
                task.cancel()
            if pending or self._last_settlement > deadline:
                log.error("voice status update missed its shutdown deadline")
                return False  # tasks still own their slots through actual settlement
        return True
