"""Private /now navigation through disnake with only its HTTP transport replaced."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import disnake
import pytest
from disnake.webhook.async_ import AsyncWebhookAdapter, async_context

from sobafm.credits import MAX_VIEWS, VIEW_LIFETIME_SECONDS, render_credits
from sobafm.discord_adapter import Inter, create_bot
from tests.test_discord_adapter import (
    MANY_NOTICES,
    FakeMatcher,
    Harness,
    harness,
    heard_id,
    offline,
    recording,
    settle,
    until,
)

USER = {"id": "123", "username": "listener", "discriminator": "0", "avatar": None}
BASE: dict[str, object] = {
    "application_id": "50",
    "token": "placeholder",
    "version": 1,
    "locale": "en-US",
    "guild_id": "1",
    "channel": {"id": "20", "name": "room", "type": 0},
    "member": {"user": USER, "roles": [], "flags": 0},
    "attachment_size_limit": 0,
}
MESSAGE: dict[str, object] = {
    "id": "30",
    "channel_id": "20",
    "type": 0,
    "edited_timestamp": None,
    "content": "",
    "attachments": [],
    "embeds": [],
    "pinned": False,
    "mention_everyone": False,
    "tts": False,
}


class CreditClient:
    def __init__(self, h: Harness, http: AsyncMock) -> None:
        self.bot = create_bot(h.adapter)
        self.state = self.bot._connection  # pyright: ignore[reportPrivateUsage]
        self.state._add_guild_from_data({"id": "1", "unavailable": True})  # pyright: ignore[reportPrivateUsage]
        self.http = http

    async def now(self, identifier: int) -> None:
        command = self.bot.get_slash_command("now")
        assert command is not None
        interaction = Inter(
            state=self.state,
            data=cast(
                Any,
                BASE
                | {
                    "type": 2,
                    "id": str(identifier),
                    "data": {
                        "id": "40",
                        "name": "now",
                        "type": 1,
                    },
                },
            ),
        )
        await command(interaction)

    @property
    def page(self) -> dict[str, Any]:
        return self.http.execute_webhook.call_args.kwargs["payload"]

    async def choose(
        self,
        origin: int,
        values: list[str],
        *,
        caller: str = "123",
        private: bool = True,
        custom_id: str | None = None,
    ) -> None:
        menu = self.page["components"][0]["components"][0]
        flags = disnake.MessageFlags(ephemeral=private, suppress_embeds=True)
        message = MESSAGE | {
            "components": self.page["components"],
            "flags": flags.value,
            "interaction_metadata": {
                "type": 2,
                "id": str(origin),
                "user": USER,
                "original_response_message_id": "31",
            },
        }
        self.http.create_interaction_response.reset_mock()
        self.state.parse_interaction_create(
            cast(
                Any,
                BASE
                | {
                    "type": 3,
                    "id": "400",
                    "member": {"user": USER | {"id": caller}, "roles": []},
                    "message": message,
                    "data": {
                        "component_type": menu["type"],
                        "custom_id": custom_id or menu["custom_id"],
                        "values": values,
                    },
                },
            )
        )
        await settle()

    def response(self, kind: disnake.InteractionResponseType) -> str:
        self.http.create_interaction_response.assert_awaited_once()
        reply = self.http.create_interaction_response.call_args.kwargs
        assert reply["type"] == kind.value
        payload = reply["data"]
        assert payload["allowed_mentions"]["parse"] == []
        assert (
            payload["flags"]
            == disnake.MessageFlags(
                suppress_embeds=True,
                ephemeral=kind is disnake.InteractionResponseType.channel_message,
            ).value
        )
        return payload["content"]


@contextmanager
def client(h: Harness) -> Generator[CreditClient]:
    http = AsyncMock(spec=AsyncWebhookAdapter)
    http.create_interaction_response = AsyncMock()
    http.execute_webhook = AsyncMock(return_value=MESSAGE)
    with async_context.set(cast(AsyncWebhookAdapter, http)):
        yield CreditClient(h, http)


@offline
async def test_listener_navigates_the_complete_original_snapshot_after_a_switch() -> None:
    first = recording("a", ("@everyone **notice**\u202e", *MANY_NOTICES))
    expected = render_credits(first)
    h = harness(recordings=(first,))
    source = await h.join()
    with client(h) as c:
        await c.now(90)
        assert c.response(disnake.InteractionResponseType.channel_message) == "Nothing is audible."
        c.http.execute_webhook.assert_not_awaited()
        await h.play()
        await settle()
        await c.now(91)
        assert "starting" in c.http.create_interaction_response.call_args.kwargs["data"]["content"]
        c.http.execute_webhook.assert_not_awaited()
        source.read()
        await until(lambda: heard_id(h) == "a")
        await until(lambda: h.adapter.players[1].station.ending is not None)
        c.http.create_interaction_response.reset_mock()
        await c.now(100)
        assert c.response(disnake.InteractionResponseType.channel_message) == (
            "Now heard (full credits follow). This is the program's last recording: every "
            "recording in the catalog has been played or tried. Use `/play` to start a new program."
        )
        c.http.execute_webhook.assert_awaited_once()
        page = c.page
        assert page["flags"] == disnake.MessageFlags(ephemeral=True, suppress_embeds=True).value
        assert page["allowed_mentions"]["parse"] == []
        assert page["content"] == expected.page(1).text
        assert "\\@everyone \\*\\*notice\\*\\*" in page["content"]
        assert "\u202e" not in page["content"]
        menu = page["components"][0]["components"][0]
        assert menu["type"] == disnake.ComponentType.string_select.value
        assert menu["placeholder"] == "Choose a credit page"
        total = len(expected.pages)
        assert [option["label"] for option in menu["options"]] == [
            f"Page {n} of {total}" for n in range(1, total + 1)
        ]

        h.adapter.matcher = FakeMatcher(recording("b"))
        await h.play("replacement")
        await settle()
        source.read()
        await until(lambda: heard_id(h) == "b")
        for option in reversed(menu["options"]):
            await c.choose(100, [option["value"]])
            assert (
                c.response(disnake.InteractionResponseType.message_update)
                == expected.page(int(option["value"])).text
            )
        for origin, values, caller, private in (
            (100, ["2"], "456", True),
            (999, ["2"], "123", True),
            (100, ["2"], "123", False),
            (100, ["0"], "123", True),
            (100, [str(total + 1)], "123", True),
            (100, ["1", "2"], "123", True),
            (100, ["100:2"], "123", True),
            (100, [], "123", True),
        ):
            await c.choose(origin, values, caller=caller, private=private)
            assert c.response(disnake.InteractionResponseType.channel_message).startswith(
                "This credit page is unavailable."
            )
        await c.choose(100, ["1"], custom_id="other-menu")
        c.http.create_interaction_response.assert_not_awaited()
        await c.now(101)
        assert "Song b" in c.page["content"] and "components" not in c.page
        assert len(h.adapter._credit_views) == 1  # pyright: ignore[reportPrivateUsage]
    assert await h.adapter.shutdown() and h.pool.leased == 0


@offline
async def test_views_release_on_failure_eviction_expiry_and_shutdown() -> None:
    h = harness(recordings=(recording("a", MANY_NOTICES),))
    source = await h.join()
    await h.play()
    await settle()
    source.read()
    await until(lambda: heard_id(h) == "a")
    views = h.adapter._credit_views  # pyright: ignore[reportPrivateUsage]
    with client(h) as c:
        c.http.execute_webhook.side_effect = OSError
        with pytest.raises(OSError):
            await c.now(99)
        assert len(views) == 0
        assert h.adapter._credit_expiry is None  # pyright: ignore[reportPrivateUsage]
        c.http.execute_webhook.side_effect = None
        for identifier in range(100, 101 + MAX_VIEWS):
            await c.now(identifier)
        assert len(views) == MAX_VIEWS
        await c.choose(100, ["1"])
        assert "unavailable" in c.response(disnake.InteractionResponseType.channel_message)
        h.clock.now += VIEW_LIFETIME_SECONDS - 1
        await c.choose(102, ["2"])
        assert "Page 2/" in c.response(disnake.InteractionResponseType.message_update)
        await c.now(1000)
        # Drive the event loop's expiry timer without any listener interaction.
        loop = asyncio.get_running_loop()
        h.clock.now += 1
        with patch.object(loop, "time", return_value=loop.time() + VIEW_LIFETIME_SECONDS + 1):
            await settle()
        assert len(views) == 1
        await c.choose(102, ["1"])
        assert "unavailable" in c.response(disnake.InteractionResponseType.channel_message)
        await c.choose(1000, ["1"])
        assert "Page 1/" in c.response(disnake.InteractionResponseType.message_update)
        timer = h.adapter._credit_expiry  # pyright: ignore[reportPrivateUsage]
        assert timer is not None
        assert await h.adapter.shutdown() and h.pool.leased == 0
        assert len(views) == 0 and timer.cancelled()
        await c.choose(1000, ["1"])
        assert "unavailable" in c.response(disnake.InteractionResponseType.channel_message)
