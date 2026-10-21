import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from sobafm.catalog import EXPIRED_CREDITS, Notice
from sobafm.credits import CreditSnapshot, CreditViews
from tests.doubles import FakeClock, settle
from tests.test_catalog import credit


def snapshot() -> CreditSnapshot:
    return CreditSnapshot(
        credit(notices=(Notice(kind="copyright", text="full supplied notice " * 180),)),
        42,
        datetime(2026, 10, 21, tzinfo=UTC),
    )


def interaction(guild: int = 1, caller: int = 2) -> Any:
    value = MagicMock(spec=discord.Interaction)
    value.guild_id = guild
    value.user.id = caller
    value.response = MagicMock()
    value.response.send_message = AsyncMock()
    value.response.defer = AsyncMock()
    value.followup.send = AsyncMock()
    value.edit_original_response = AsyncMock()
    return value


def test_pages_keep_complete_notices_links_and_utf16_bounds() -> None:
    text = "supplied é🙂 \\ *notice* " * 160
    record = credit(notices=(Notice(kind="license", text=text, links=("https://example.org/",)),))
    pages = record.pages("Header")
    assert 1 < len(pages) <= 10
    assert all(len(page.encode("utf-16-le")) // 2 <= 1900 for page in pages)
    assert sum(len(page.encode("utf-16-le")) // 2 for page in pages) <= 16000
    bodies = [page.split("\n", 2)[2].removesuffix("\n" + EXPIRED_CREDITS) for page in pages]
    assert all((len(body) - len(body.rstrip("\\"))) % 2 == 0 for body in bodies)
    rendered = "".join(bodies)
    for block in record.blocks():
        assert block in rendered
        if "](<" in block:
            assert sum(block in page for page in pages) == 1


@pytest.mark.parametrize(
    ("marker", "escaped"), [("# ", "\\# "), ("> ", "\\> "), ("-# ", "\\-\\# ")]
)
def test_paginated_notice_continuation_cannot_become_line_start_markdown(
    marker: str, escaped: str
) -> None:
    prefix = "\n".join(credit(notices=()).blocks()) + "\nCopyright: "
    padding = 1700 - len(prefix.encode("utf-16-le")) // 2
    record = credit(
        notices=(
            Notice(
                kind="copyright",
                text="x" * padding + marker + "required literal notice",
            ),
        )
    )
    pages = record.pages("Header")
    bodies = [page.split("\n", 2)[2].removesuffix("\n" + EXPIRED_CREDITS) for page in pages]
    assert bodies[1].startswith(escaped)
    assert record.blocks()[-1] in "".join(bodies)


@pytest.mark.parametrize("text", ["x" * 16000, "🙂" * 8000], ids=["ascii", "supplementary"])
def test_labels_and_utf16_expansion_count_before_admission(text: str) -> None:
    with pytest.raises(ValueError, match="bound"):
        credit(notices=(Notice(kind="copyright", text=text),))


async def test_views_are_one_per_caller_and_evict_oldest_at_both_caps() -> None:
    owner = CreditViews(FakeClock())
    old = owner.create(1, 0, snapshot())
    replacement = owner.create(1, 0, snapshot())
    assert old.is_finished()
    assert not old.pages
    for caller in range(1, 17):
        owner.create(1, caller, snapshot())
    assert len(owner.views) == 16
    assert replacement.is_finished()
    for guild in range(2, 6):
        for caller in range(16):
            owner.create(guild, caller, snapshot())
    assert len(owner.views) == 64
    assert not any(key[0] == 1 for key in owner.views)
    owner.close(3)
    assert len(owner.views) == 48
    owner.close()
    assert not owner.views


async def test_navigation_is_caller_bound_private_and_does_not_extend_deadline() -> None:
    clock = FakeClock()
    owner = CreditViews(clock)
    view = owner.create(1, 2, snapshot())
    for wrong in (interaction(caller=3), interaction(guild=3)):
        assert not await view.interaction_check(wrong)
        assert wrong.response.send_message.call_args.kwargs["ephemeral"]
        wrong.edit_original_response.assert_not_awaited()
    correct = interaction()
    assert await view.interaction_check(correct)
    deadline = view.deadline
    await view.next.callback(correct)
    correct.response.defer.assert_awaited_once_with(ephemeral=True)
    assert view.index == 1
    assert correct.edit_original_response.call_args.kwargs["content"] == view.pages[1]
    assert correct.edit_original_response.call_args.kwargs["allowed_mentions"].everyone is False
    assert view.deadline == deadline
    clock.now = deadline
    assert not await view.interaction_check(correct)
    assert not owner.views
    assert not view.pages
    assert "Use /now again" in correct.response.send_message.call_args.args[0]


@pytest.mark.parametrize("end", ["replace", "station close", "expiry"])
async def test_pending_initial_send_is_owned_and_cancelled_on_eviction_or_close(end: str) -> None:
    owner = CreditViews(FakeClock())
    call = interaction()
    entered = asyncio.Event()
    delivered: list[bool] = []

    async def held(*args: object, **kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()
        delivered.append(True)

    call.followup.send.side_effect = held
    pending = asyncio.create_task(owner.show(call, snapshot(), "Program context"))
    await entered.wait()
    previous = owner.views[(1, 2)]
    if end == "replace":
        owner.create(1, 2, snapshot())
    elif end == "station close":
        owner.close(1)
    else:
        previous.expire()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not previous.pages
    assert previous.active is None
    assert not delivered
    owner.close()


async def test_pending_navigation_has_no_queue_and_is_cancelled_at_original_expiry() -> None:
    owner = CreditViews(FakeClock())
    view = owner.create(1, 2, snapshot())
    call, another = interaction(), interaction()
    entered = asyncio.Event()

    async def held(**kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    call.edit_original_response.side_effect = held
    pending = asyncio.create_task(view.next.callback(call))
    await entered.wait()
    await view.next.callback(another)
    another.response.defer.assert_awaited_once()
    another.edit_original_response.assert_not_awaited()
    view.expire()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not view.pages
    assert not owner.views


async def test_initial_failure_discards_metadata_and_logs_no_exception_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = CreditViews(FakeClock())
    call = interaction()
    call.followup.send.side_effect = RuntimeError("opaque-private-value")
    await owner.show(call, snapshot(), "Program context")
    assert not owner.views
    assert "opaque-private-value" not in caplog.text
    assert "Could not deliver private catalog credits" in caplog.text


async def test_cancelled_initial_caller_releases_view_without_swallowing_cancel() -> None:
    owner = CreditViews(FakeClock())
    call = interaction()
    call.followup.send.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await owner.show(call, snapshot(), "Program context")
    await settle()
    assert not owner.views
