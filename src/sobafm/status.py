"""The voice channel statuses SobaFM sets in a server (FB-2).

They belong to the server rather than to a station, so a station that closes before it can clear
its title leaves it to the next one, and their requests stay in order across stations.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT_S = 10.0  # how long closing or moving waits for status requests
RETRY_S = 10.0  # before checking a status again, doubling while requests for it fail
RETRY_MAX_S = 300.0  # the longest wait between them


class Statuses:
    """The titles SobaFM set as voice channel statuses in one server, and its requests.

    Discord lets SobaFM change a channel's status only while connected there, and clears an
    empty channel's status itself. So each channel's title is kept until SobaFM clears it or the
    channel empties, and is cleared once SobaFM is back there with nothing to show. Requests run
    one at a time and are never cancelled: discord.py's rate limiter mishandles a request
    cancelled while it waits.
    """

    def __init__(
        self,
        channel: Callable[[], int | None],
        set_status: Callable[[int, str | None], Coroutine[Any, Any, bool]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._channel = channel  # the ID of SobaFM's voice channel, if it is connected there
        self._set = set_status  # shows a status on a channel, or says SobaFM may not
        self._clock = clock
        self.shown: dict[int, str] = {}  # the title SobaFM set in each channel, while it may remain
        self._task: asyncio.Task[None] | None = None  # the request in flight
        self._unshown: tuple[int, str | None] | None = None  # the last status not shown, and where
        self._failures = 0  # failed requests for `_unshown`
        self._retry_at = 0.0

    def show(self, title: str | None) -> None:
        """Show `title` in SobaFM's channel, or clear it with None, unless a request is in flight.

        A refused status waits for its retry, and a new one is tried at once.
        """
        channel = self._channel()
        if channel is None or (self._task is not None and not self._task.done()):
            return
        unshown = self._unshown == (channel, title) and self._clock() < self._retry_at
        if self.shown.get(channel) != title and not unshown:
            self._task = asyncio.create_task(self._show(channel, title))

    def clear_soon(self) -> None:
        """Clear the status of SobaFM's channel, if SobaFM set it, after any request in flight."""
        if (channel := self._channel()) is not None:
            self._task = asyncio.create_task(self._clear_after(self._task, channel))

    async def clear(self) -> None:
        """Clear as `clear_soon()` does, and wait at most TIMEOUT_S for it and what it follows."""
        if (channel := self._channel()) is not None:
            self._task = asyncio.create_task(self._clear_after(self._task, channel))
            await asyncio.wait({self._task}, timeout=TIMEOUT_S)

    def forget(self, channel: int) -> None:
        """Forget `channel`'s title, which Discord clears once the channel empties."""
        self.shown.pop(channel, None)

    async def _clear_after(self, request: asyncio.Task[None] | None, channel: int) -> None:
        if request is not None:
            await asyncio.wait({request})
        if channel in self.shown and self._channel() == channel:  # not since moved or dragged
            await self._show(channel, None)

    async def _show(self, channel: int, title: str | None) -> None:
        """Show `title` on `channel`, or schedule another attempt."""
        failures = self._failures if self._unshown == (channel, title) else 0
        try:
            allowed = await self._set(channel, title)
        except Exception:  # the status is cosmetic, so it must never affect playback
            if not failures:  # once for each status, since retries would repeat it
                log.warning("Could not update the voice channel status", exc_info=True)
            failures += 1
            # The exponent is bounded, so a status refused for days can't overflow the delay.
            delay = min(RETRY_S * 2 ** min(failures - 1, 16), RETRY_MAX_S)
        else:
            if allowed:
                if title is None:
                    self.shown.pop(channel, None)
                else:
                    self.shown[channel] = title
                self._unshown = None
                return
            delay = RETRY_S  # checking the permission again sends no request
        self._unshown, self._failures = (channel, title), failures
        self._retry_at = self._clock() + delay
