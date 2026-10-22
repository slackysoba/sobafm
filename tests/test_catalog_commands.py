import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from pydantic import SecretStr

from sobafm.bot import PLAY_REPLIES, SobaFM
from sobafm.catalog import CatalogQuery
from sobafm.catalog_interpreter import CatalogPlan
from sobafm.catalog_playback import CatalogPlayback
from sobafm.commands import add_commands
from sobafm.program import Mode, Outcome, Program
from sobafm.station import Station
from tests.doubles import FakeGemini, FakeSource, answer
from tests.test_bot import (
    CATALOG,
    LOFI,
    in_voice,
    make_channel,
    make_guild,
    make_interaction,
)
from tests.test_bot import (
    bot as bot,
)
from tests.test_bot import (
    gemini as gemini,
)
from tests.test_catalog import credit


def enable_catalog(bot: SobaFM) -> Any:
    bot.settings.jamendo_client_id = SecretStr("catalog-fixture-id")
    owner = MagicMock(spec=CatalogPlayback, ready=True, close=AsyncMock())
    bot.catalog = owner
    bot.tree.clear_commands(guild=None)
    add_commands(bot.tree, bot)
    return owner


def test_no_catalog_configuration_never_registers_generated_play_fallback(bot: SobaFM) -> None:
    assert bot.tree.get_command("play") is None
    assert bot.tree.get_command("skip") is None
    assert bot.tree.get_command("freestyle") is not None


async def test_configured_play_is_catalog_and_discloses_both_services(
    bot: SobaFM, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_catalog(bot)
    command: Any = bot.tree.get_command("play")
    assert "Google" in command.description
    assert "Jamendo" in command.description
    assert command.default_permissions is None
    guild = make_guild()
    channel = make_channel(guild)
    call = make_interaction(in_voice(channel, channel))
    monkeypatch.setattr(bot, "admit", AsyncMock(return_value=None))
    play = AsyncMock(return_value="Catalog reply")
    monkeypatch.setattr(bot, "play", play)
    await command.callback(call, request="soft piano")
    assert play.call_args.kwargs["mode"] is Mode.CATALOG
    call.response.defer.assert_awaited_once_with()
    assert call.followup.send.call_args.kwargs["suppress_embeds"]


async def test_catalog_unavailable_never_calls_a_model_or_disrupts_generated_music(
    bot: SobaFM, gemini: FakeGemini
) -> None:
    owner = enable_catalog(bot)
    owner.ready = False
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    reply = await bot.play(member, "piano", None, mode=Mode.CATALOG)
    assert reply == PLAY_REPLIES[Mode.CATALOG][Outcome.UNAVAILABLE]
    assert not gemini.calls
    owner.engine.assert_not_called()


async def test_catalog_first_reply_uses_validated_plan_and_immutable_accepted_credit(
    bot: SobaFM, gemini: FakeGemini, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_catalog(bot)
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    gemini.response = answer(
        json.dumps(
            {
                "kind": "new",
                "plan": {"title": "Piano", "query": {"tags": ["piano"]}},
            }
        )
    )
    record = credit()
    station = MagicMock(spec=Station)

    def accept(plan: Any, requester: str, *, duration_seconds: None) -> asyncio.Future[Outcome]:
        program = Program(plan, requester, None)
        program.first_credit = record
        program.settle(Outcome.PLAYING)
        source = FakeSource(program)
        source.credits = record
        station.program, station.heard_source = program, source
        bot.stations[guild.id] = station
        bot.station_owners[station] = (guild, guild.voice_client)
        return program.started

    station.play.side_effect = accept
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    reply = await bot.play(member, "piano", None, mode=Mode.CATALOG)
    assert "First heard" in reply
    assert "Song" in reply
    assert "Artist" in reply
    assert "https://www.jamendo.com/track/42" in reply
    assert "CC BY 3.0 Unported" in reply
    assert "Complete private credits: /now" in reply
    assert "Ends" not in reply
    assert station.play.call_args.kwargs == {"duration_seconds": None}
    definitions = gemini.calls[0]["config"].response_json_schema["$defs"]
    assert "CatalogPlan" in definitions
    assert "MusicPlan" not in definitions


@pytest.mark.parametrize("change", ["voice", "guild", "disconnect", "owner missing"])
async def test_new_credit_snapshot_rejects_stale_owner_but_keeps_old_snapshot(
    bot: SobaFM, change: str
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    in_voice(channel, channel)
    program = Program(
        CatalogPlan(title="Piano", query=CatalogQuery(tags=("piano",))), "Member", None
    )
    source = FakeSource(program)
    source.credits = credit()
    station = MagicMock(spec=Station, heard_source=source)
    bot.stations[guild.id] = station
    bot.station_owners[station] = (guild, guild.voice_client)
    previous = bot.credit_snapshot(guild)
    assert previous is not None
    if change == "voice":
        guild.voice_client = MagicMock(spec=discord.VoiceClient)
    elif change == "guild":
        make_guild()  # replaces current gateway identity with the same numeric id
    elif change == "disconnect":
        guild.voice_client.is_connected.return_value = False
    else:
        del bot.station_owners[station]
    assert bot.credit_snapshot(guild) is None
    assert previous.record is source.credits
    assert "Song" in previous.pages()[0]


@pytest.mark.parametrize("current", [CATALOG, LOFI], ids=["catalog", "generated"])
@pytest.mark.parametrize("failure", ["refusal", "unavailable"])
async def test_catalog_failure_uses_same_mode_context_and_never_raw_fallback(
    bot: SobaFM,
    gemini: FakeGemini,
    current: CatalogPlan | Any,
    failure: str,
) -> None:
    from google.genai import errors

    enable_catalog(bot)
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    previous = Program(current, "Other", None if isinstance(current, CatalogPlan) else 3600)
    previous.settle(Outcome.PLAYING)
    station = MagicMock(spec=Station, program=previous)
    bot.stations[guild.id] = station
    if failure == "unavailable":
        gemini.error = errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})
    else:
        gemini.response = answer('{"kind":"not_music"}')
    call = make_interaction(member)
    command: Any = bot.tree.get_command("play")
    await command.callback(call, request="opaque user input")
    contents = json.loads(gemini.calls[0]["contents"])
    assert contents["current_plan"] == (
        current.model_dump(mode="json") if isinstance(current, CatalogPlan) else None
    )
    assert station.program is previous
    assert previous.playing
    station.play.assert_not_called()
    reply = call.followup.send.call_args.args[0]
    assert "opaque user input" not in reply
    assert await bot.admit(member, "another") is None


@pytest.mark.parametrize("first", ["play", "freestyle"])
async def test_catalog_and_generated_requests_share_the_existing_cooldown(
    bot: SobaFM,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    enable_catalog(bot)
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    play = AsyncMock(return_value="Playing")
    monkeypatch.setattr(bot, "play", play)
    first_call, second_call = make_interaction(member), make_interaction(member)
    a: Any = bot.tree.get_command(first)
    b: Any = bot.tree.get_command("freestyle" if first == "play" else "play")
    await a.callback(first_call, request="piano")
    await b.callback(second_call, request="jazz")
    assert play.await_count == 1
    assert "Try again in 30 seconds" in second_call.response.send_message.call_args.args[0]
    assert second_call.response.send_message.call_args.kwargs["ephemeral"]


async def test_now_uses_actual_heard_credits_privately_without_play_access(
    bot: SobaFM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(None, channel)  # requester need not be listening or a manager
    member.guild = guild
    current = Program(CATALOG, "First requester", None)
    current.settle(Outcome.PLAYING)
    desired = Program(LOFI, "Other requester", 3600)
    heard = FakeSource(current)
    heard.credits = credit()
    station = MagicMock(spec=Station, program=desired, heard_source=heard, time_left=600)
    bot.stations[guild.id] = station
    bot.station_owners[station] = (guild, guild.voice_client)
    call = make_interaction(member)
    call.guild = guild
    call.guild_id = guild.id
    # No voice/access/admission operation is relevant to reading private credits.
    admit = AsyncMock(side_effect=AssertionError("must not admit playback"))
    monkeypatch.setattr(bot, "admit", admit)
    command: Any = bot.tree.get_command("now")
    await command.callback(call)
    call.response.defer.assert_awaited_once_with(ephemeral=True)
    assert call.followup.send.await_count == 2
    context, page = call.followup.send.call_args_list
    assert "Starting" in context.args[0]
    assert "Still heard" in context.args[0]
    assert "First requester" not in page.args[0]
    assert "Song" in page.args[0]
    assert "CC BY 3.0" in page.args[0]
    assert all(c.kwargs["ephemeral"] and c.kwargs["suppress_embeds"] for c in (context, page))
    assert all(c.kwargs["allowed_mentions"].to_dict()["parse"] == [] for c in (context, page))
    assert "view" in page.kwargs
    assert not page.kwargs["view"].is_finished()
    await bot.close_station(guild)
    assert not bot.credit_views.views
    assert not page.kwargs["view"].pages


async def test_slow_catalog_failure_does_not_evaluate_a_stale_success_or_log_private_error(
    bot: SobaFM,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.doubles import settle

    started: asyncio.Future[Outcome] = asyncio.get_running_loop().create_future()
    getter = MagicMock(side_effect=AssertionError("stale success must not be evaluated"))
    announce = AsyncMock(side_effect=RuntimeError("opaque interaction token"))
    bot.announce_when_settled(started, getter, announce, Mode.CATALOG)
    started.set_result(Outcome.STOPPED)
    await settle()
    getter.assert_not_called()
    announce.assert_awaited_once_with(PLAY_REPLIES[Mode.CATALOG][Outcome.STOPPED])
    assert "opaque interaction token" not in caplog.text


async def test_omission_replaces_stale_play_registration_without_logging_client_id(
    bot: SobaFM,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    synced: list[str] = []

    async def sync() -> None:
        synced.extend(c.name for c in bot.tree.get_commands())

    monkeypatch.setattr(bot.tree, "sync", sync)
    with caplog.at_level(logging.INFO, logger="sobafm.bot"):
        await bot.setup_hook()
    bot.check_voice.cancel()
    assert "play" not in synced
    assert "freestyle" in synced
    assert "SOBAFM_JAMENDO_CLIENT_ID" in caplog.text
    assert "fixture-client" not in caplog.text


async def test_catalog_stop_during_interpretation_cannot_start_the_source(
    bot: SobaFM,
    gemini: FakeGemini,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.doubles import settle

    enable_catalog(bot)
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    held = gemini.held = asyncio.Event()
    gemini.response = answer('{"kind":"new","plan":{"title":"Piano","query":{"tags":["piano"]}}}')
    factory = MagicMock(side_effect=AssertionError("stopped request cannot create a station"))
    monkeypatch.setattr(bot, "station", factory)
    pending = asyncio.create_task(bot.play(member, "piano", None, mode=Mode.CATALOG))
    await settle()
    assert len(gemini.calls) == 1
    assert bot.stop(guild) == "Stopped the request before it played."
    held.set()
    assert await pending == PLAY_REPLIES[Mode.CATALOG][Outcome.STOPPED]
    factory.assert_not_called()


@pytest.mark.parametrize("superseded", [False, True])
async def test_slow_catalog_reply_keeps_first_heard_record_and_rejects_stale_program(
    bot: SobaFM,
    gemini: FakeGemini,
    monkeypatch: pytest.MonkeyPatch,
    *,
    superseded: bool,
) -> None:
    from tests.doubles import settle

    enable_catalog(bot)
    monkeypatch.setattr("sobafm.bot.START_TIMEOUT_S", 0.001)
    guild = make_guild()
    channel = make_channel(guild)
    member = in_voice(channel, channel)
    gemini.response = answer('{"kind":"new","plan":{"title":"Piano","query":{"tags":["piano"]}}}')
    station = MagicMock(spec=Station)
    first = credit(title="First song")
    later = credit(title="Later song")

    def start(plan: Any, requester: str, *, duration_seconds: None) -> asyncio.Future[Outcome]:
        program = Program(plan, requester, None)
        station.program = program
        source = FakeSource(program)
        source.credits = later
        station.heard_source = source
        bot.stations[guild.id] = station
        return program.started

    station.play.side_effect = start
    monkeypatch.setattr(bot, "station", MagicMock(return_value=station))
    announce = AsyncMock()
    reply = await bot.play(member, "piano", None, announce, mode=Mode.CATALOG)
    assert reply.startswith("The music is taking longer")
    program = station.program
    program.first_credit = first
    program.settle(Outcome.PLAYING)
    if superseded:
        del bot.stations[guild.id]
    await settle()
    result = announce.call_args.args[0]
    if superseded:
        assert "no longer playing" in result
        assert "First song" not in result
    else:
        assert "First song" in result
        assert "Later song" not in result
        assert "Catalog direction: piano" in result
        assert len(result.encode("utf-16-le")) // 2 <= 2000


async def test_catalog_factory_cannot_bypass_unknown_retained_audio(bot: SobaFM) -> None:
    from sobafm.audio_budget import AudioLedger

    owner = enable_catalog(bot)
    guild = make_guild()
    channel = make_channel(guild)
    in_voice(channel, channel)
    ledger = AudioLedger()
    _ = ledger.pcm  # the real server admits its persistent parent before unknown work
    debt = ledger.unbounded()
    bot.audio_ledgers[guild.id] = ledger
    station = bot.station(guild, 1)
    owner.engine.assert_not_called()
    assert Mode.CATALOG not in station.engines
    assert not ledger.bounded
    await bot.close_station(guild)
    assert not ledger.bounded
    assert debt.size is None


async def test_private_now_failure_has_no_public_fallback_or_exception_payload(
    bot: SobaFM,
    caplog: pytest.LogCaptureFixture,
) -> None:
    guild = make_guild()
    channel = make_channel(guild)
    call = make_interaction(in_voice(None, channel))
    call.guild, call.guild_id = guild, guild.id
    call.followup.send.side_effect = discord.HTTPException(
        MagicMock(status=429, reason="opaque-token"),
        "opaque-token",
    )
    command: Any = bot.tree.get_command("now")
    await command.callback(call)
    call.response.defer.assert_awaited_once_with(ephemeral=True)
    call.followup.send.assert_awaited_once()
    assert call.followup.send.call_args.kwargs["ephemeral"]
    assert "opaque-token" not in caplog.text
