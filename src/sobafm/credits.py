"""Complete private metadata snapshots; never retain an audio source or interaction token."""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, override

import discord

from sobafm.catalog import CreditRecord, _escape, _units  # pyright: ignore[reportPrivateUsage]

VIEW_SECONDS = 600.0
SERVER_VIEWS = 16
PROCESS_VIEWS = 64
EXPIRED = "These credits expired. Use /now again for a new snapshot."
log = logging.getLogger(__name__)


def shorten_rendered(value: str, limit: int) -> str:
    """Shorten an optional escaped summary without cutting an escape or UTF-16 pair."""
    while _units(value) > limit:
        value = value[:-1]
    if (len(value) - len(value.rstrip("\\"))) & 1:
        value = value[:-1]
    return value


def summary(text: str, limit: int) -> str:
    return shorten_rendered(_escape(text), limit)


def first_credit(record: CreditRecord) -> str:
    return (
        f"First heard: **{summary(record.title, 160)}** by **{summary(record.artist, 160)}**.\n"
        f"License: [{record.license.label}](<{record.license.url}>) · "
        f"[Jamendo track](<https://www.jamendo.com/track/{record.track_id}>)\n"
        "Music provided by Jamendo. Complete private credits: /now."
    )


@dataclass(frozen=True)
class CreditSnapshot:
    record: CreditRecord
    source_number: int
    captured: datetime

    def pages(self) -> tuple[str, ...]:
        title = summary(self.record.title, 70)
        header = f"Heard song: {title}\nSnapshot {self.captured:%Y-%m-%d %H:%M:%S} UTC"
        return self.record.pages(header)


class CreditViews:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.views: OrderedDict[tuple[int, int], CreditView] = OrderedDict()

    def create(self, guild: int, caller: int, snapshot: CreditSnapshot) -> CreditView:
        for view in list(self.views.values()):
            if view.deadline <= self.clock() or view.key == (guild, caller):
                view.expire()
        while sum(key[0] == guild for key in self.views) >= SERVER_VIEWS:
            next(view for key, view in self.views.items() if key[0] == guild).expire()
        while len(self.views) >= PROCESS_VIEWS:
            next(iter(self.views.values())).expire()
        view = CreditView(self, (guild, caller), snapshot.pages())
        self.views[view.key] = view
        return view

    def close(self, guild: int | None = None, caller: int | None = None) -> None:
        for key, view in list(self.views.items()):
            if (guild is None or key[0] == guild) and (caller is None or key[1] == caller):
                view.expire()

    async def show(
        self, interaction: discord.Interaction, snapshot: CreditSnapshot, description: str
    ) -> None:
        if interaction.guild_id is None:
            return
        view = self.create(interaction.guild_id, interaction.user.id, snapshot)
        view.active = asyncio.current_task()
        try:
            async with asyncio.timeout(max(0, view.deadline - self.clock())):
                await interaction.followup.send(
                    description,
                    ephemeral=True,
                    suppress_embeds=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await interaction.followup.send(
                    view.pages[0],
                    ephemeral=True,
                    suppress_embeds=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                    view=view,
                )
        except BaseException as error:
            view.expire()
            if not isinstance(error, Exception):
                raise
            log.warning("Could not deliver private catalog credits")
        finally:
            view.active = None


class CreditView(discord.ui.View):
    def __init__(self, owner: CreditViews, key: tuple[int, int], pages: tuple[str, ...]) -> None:
        super().__init__(timeout=VIEW_SECONDS)
        self.owner, self.key = owner, key
        self.pages: tuple[str, ...] = pages
        self.deadline = owner.clock() + VIEW_SECONDS
        self.index = 0
        self.active: asyncio.Task[Any] | None = None
        self._timer = asyncio.get_running_loop().call_later(VIEW_SECONDS, self.expire)
        self._buttons()

    def _buttons(self) -> None:
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index + 1 >= len(self.pages)

    def expire(self) -> None:
        self._timer.cancel()
        self.stop()
        if self.owner.views.get(self.key) is self:
            del self.owner.views[self.key]
        self.pages = ()
        if self.active is not None and self.active is not asyncio.current_task():
            self.active.cancel()

    @override
    async def on_timeout(self) -> None:
        self.expire()

    @override
    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]
    ) -> None:
        self.expire()
        log.warning("Private catalog credits interaction failed")

    @override
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        expired = not self.pages or self.owner.clock() >= self.deadline
        if expired:
            self.expire()
        if expired or (interaction.guild_id, interaction.user.id) != self.key:
            await interaction.response.send_message(
                EXPIRED if expired else "Use /now for your own private credits.",
                ephemeral=True,
                suppress_embeds=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    async def _navigate(self, interaction: discord.Interaction, direction: int) -> None:
        if self.active is not None:
            await interaction.response.defer(ephemeral=True)
            return  # no queue of edits while the library's rate limiter is waiting
        self.active = asyncio.current_task()
        try:
            await interaction.response.defer(ephemeral=True)
            if not self.pages or self.owner.clock() >= self.deadline:
                self.expire()
                return
            self.index = max(0, min(len(self.pages) - 1, self.index + direction))
            self._buttons()
            async with asyncio.timeout(max(0, self.deadline - self.owner.clock())):
                # The initial message already has suppressed previews. This edits that
                # same private message and never saves the response/interaction object.
                await interaction.edit_original_response(
                    content=self.pages[self.index],
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        finally:
            self.active = None

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(
        self, interaction: discord.Interaction, button: discord.ui.Button[CreditView]
    ) -> None:
        await self._navigate(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(
        self, interaction: discord.Interaction, button: discord.ui.Button[CreditView]
    ) -> None:
        await self._navigate(interaction, 1)
