import argparse
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from scripts import reconcile_commands as tool

APP_ID = 99
GUILD_ID = 42


def command(name: str, command_id: int, guild_id: int | None = None) -> Any:
    item = MagicMock(spec=app_commands.AppCommand)
    item.configure_mock(
        name=name,
        id=command_id,
        application_id=APP_ID,
        guild_id=guild_id,
        type=discord.AppCommandType.chat_input,
        default_member_permissions=None,
        allowed_contexts=app_commands.AppCommandContext(guild=True),
        allowed_installs=app_commands.AppInstallationType(guild=True),
        nsfw=False,
        delete=AsyncMock(),
    )
    return item


def inventory(globals_: list[Any], guild_commands: list[Any]) -> Any:
    tree = MagicMock(spec=app_commands.CommandTree)
    tree.client = MagicMock(application_id=APP_ID)

    async def fetch(*, guild: discord.Object | None = None) -> list[Any]:
        if guild is None:
            return globals_
        assert guild.id == GUILD_ID
        return guild_commands

    tree.fetch_commands = AsyncMock(side_effect=fetch)
    tree.client.http.get_global_commands = AsyncMock(
        return_value=[
            {
                "id": str(item.id),
                "application_id": str(item.application_id),
                "name": item.name,
                "type": item.type.value,
                "contexts": item.allowed_contexts.to_array()
                if item.allowed_contexts is not None
                else None,
                "integration_types": item.allowed_installs.to_array()
                if item.allowed_installs is not None
                else None,
                "default_member_permissions": str(item.default_member_permissions.value)
                if item.default_member_permissions is not None
                else None,
                "nsfw": item.nsfw,
            }
            for item in globals_
        ]
    )
    return tree


@pytest.mark.parametrize("apply", [False, True])
async def test_reconciles_only_same_application_same_type_duplicates_in_selected_guild(
    apply: bool,
) -> None:
    globals_ = [command(name, i) for i, name in enumerate(["join", "play", "now"], start=1)]
    guild_commands = [
        command("join", 11, GUILD_ID),
        command("play", 12, GUILD_ID),
        command("now", 13, GUILD_ID),
        command("leave", 14, GUILD_ID),  # no global replacement
        command("unrelated", 15, GUILD_ID),
        command("join", 16, GUILD_ID),  # context menu, not a slash command
    ]
    guild_commands[-1].type = discord.AppCommandType.user
    tree = inventory(globals_, guild_commands)
    tree.sync = AsyncMock()

    names = await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=apply)

    assert names == ["join", "now", "play"]
    assert tree.fetch_commands.await_count == 2
    tree.sync.assert_not_awaited()
    for item in globals_ + guild_commands[3:]:
        item.delete.assert_not_awaited()
    for item in guild_commands[:3]:
        assert item.delete.await_count == int(apply)


async def test_cleanup_is_idempotent_and_keeps_global_ids_and_permissions() -> None:
    global_command = command("join", 1)
    global_command.default_member_permissions = discord.Permissions(manage_guild=True)
    guild_command = command("join", 2, GUILD_ID)
    guild_command.default_member_permissions = global_command.default_member_permissions
    guild_commands = [guild_command]
    guild_command.delete.side_effect = lambda: guild_commands.remove(guild_command)
    tree = inventory([global_command], guild_commands)

    assert await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True) == ["join"]
    assert await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True) == []
    assert global_command.id == 1
    assert global_command.default_member_permissions == discord.Permissions(manage_guild=True)
    global_command.delete.assert_not_awaited()


@pytest.mark.parametrize("scope", ["global", "guild"])
@pytest.mark.parametrize("problem", ["application_id", "guild_id"])
async def test_unexpected_inventory_never_deletes(scope: str, problem: str) -> None:
    globals_ = [command("join", 1)]
    guild_commands = [command("join", 2, GUILD_ID)]
    item = globals_[0] if scope == "global" else guild_commands[0]
    setattr(item, problem, 123)

    with pytest.raises(tool.ReconciliationError, match="Unexpected application or scope"):
        await tool.reconcile(
            inventory(globals_, guild_commands), discord.Object(id=GUILD_ID), apply=True
        )

    guild_commands[0].delete.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_member_permissions", discord.Permissions(manage_guild=True)),
        ("allowed_contexts", None),
        ("allowed_contexts", app_commands.AppCommandContext(guild=True, dm_channel=True)),
        ("allowed_contexts", app_commands.AppCommandContext(dm_channel=True)),
        ("allowed_installs", None),
        ("allowed_installs", app_commands.AppInstallationType(guild=True, user=True)),
        ("allowed_installs", app_commands.AppInstallationType(user=True)),
        ("nsfw", True),
    ],
)
async def test_checks_every_survivor_before_first_delete(field: str, value: Any) -> None:
    globals_ = [command("join", 1), command("play", 2)]
    guild_commands = [command("join", 11, GUILD_ID), command("play", 12, GUILD_ID)]
    setattr(globals_[1], field, value)

    with pytest.raises(tool.ReconciliationError, match="access differs or is unknown"):
        await tool.reconcile(
            inventory(globals_, guild_commands), discord.Object(id=GUILD_ID), apply=True
        )

    for item in globals_ + guild_commands:
        item.delete.assert_not_awaited()


async def test_failed_inventory_never_deletes() -> None:
    tree = inventory([command("join", 1)], [command("join", 2, GUILD_ID)])
    tree.fetch_commands.side_effect = [[], RuntimeError("offline")]

    with pytest.raises(RuntimeError, match="offline"):
        await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True)

    tree.sync.assert_not_called()


async def test_failed_delete_stops_cleanup() -> None:
    globals_ = [command("join", 1), command("play", 2)]
    guild_commands = [command("join", 11, GUILD_ID), command("play", 12, GUILD_ID)]
    guild_commands[0].delete.side_effect = RuntimeError("offline")

    with pytest.raises(RuntimeError, match="offline"):
        await tool.reconcile(
            inventory(globals_, guild_commands), discord.Object(id=GUILD_ID), apply=True
        )

    guild_commands[1].delete.assert_not_awaited()


def test_apply_requires_permission_review_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.argv", ["reconcile_commands", "--guild-id", "42", "--apply"])
    run = AsyncMock()
    monkeypatch.setattr(tool, "run", run)

    with pytest.raises(SystemExit) as caught:
        tool.main()

    assert caught.value.code == 2
    run.assert_not_called()


@pytest.mark.parametrize("value", ["0", "-1", "abc", str(2**64)])
def test_rejects_invalid_guild_ids_without_echoing_them(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="positive numeric server ID"):
        tool.positive_id(value)


async def test_real_discord_commands_delete_only_the_guild_endpoint() -> None:
    http = SimpleNamespace(delete_guild_command=AsyncMock(), delete_global_command=AsyncMock())
    state = SimpleNamespace(application_id=APP_ID, http=http)
    common: dict[str, Any] = {
        "application_id": str(APP_ID),
        "type": 1,
        "name": "join",
        "description": "Join",
        "contexts": [0],
        "integration_types": [0],
        "default_member_permissions": "32",
    }
    global_command = app_commands.AppCommand(
        data=cast(Any, common | {"id": "1"}), state=cast(Any, state)
    )
    guild_command = app_commands.AppCommand(
        data=cast(Any, common | {"id": "2", "guild_id": str(GUILD_ID)}), state=cast(Any, state)
    )

    tree = inventory([global_command], [guild_command])
    tree.client.http.get_global_commands.return_value = [common | {"id": "1"}]

    await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True)

    tree.client.http.get_global_commands.assert_awaited_once_with(APP_ID)
    http.delete_guild_command.assert_awaited_once_with(APP_ID, GUILD_ID, 2)
    http.delete_global_command.assert_not_awaited()


@pytest.mark.parametrize("token", ["", "unsafe\nsecret", "unsafe secret", "unsafe\u201csecret"])
def test_invalid_credentials_are_not_echoed_or_used(
    monkeypatch: pytest.MonkeyPatch, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["reconcile_commands", "--guild-id", "42"])
    monkeypatch.setenv("DISCORD_TOKEN", token)
    run = AsyncMock()
    monkeypatch.setattr(tool, "run", run)

    with pytest.raises(SystemExit) as caught:
        tool.main()

    assert "secret" not in str(caught.value)
    assert "secret" not in capsys.readouterr().err
    run.assert_not_called()


async def test_run_authenticates_without_gateway_or_sync(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = MagicMock(spec=discord.Client)
    client.__aenter__.return_value = client
    tree = inventory([command("join", 1)], [command("join", 2, GUILD_ID)])
    tree.sync = AsyncMock()
    create_client = MagicMock(return_value=client)
    monkeypatch.setattr(discord, "Client", create_client)
    monkeypatch.setattr(app_commands, "CommandTree", MagicMock(return_value=tree))

    await tool.run("private-test-token", GUILD_ID, apply=False)

    create_client.assert_called_once_with(intents=discord.Intents.none())
    client.login.assert_awaited_once_with("private-test-token")
    client.connect.assert_not_awaited()
    client.start.assert_not_awaited()
    tree.sync.assert_not_awaited()
    output = capsys.readouterr().out
    assert "Duplicate guild/global commands: /join" in output
    assert "Read-only audit" in output
    assert "private-test-token" not in output


@pytest.mark.parametrize(
    "failure", [discord.LoginFailure("private-test-token"), OSError("private-test-token")]
)
def test_api_errors_are_sanitized(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    monkeypatch.setattr("sys.argv", ["reconcile_commands", "--guild-id", "42"])
    monkeypatch.setenv("DISCORD_TOKEN", "private-test-token")
    monkeypatch.setattr(tool, "run", AsyncMock(side_effect=failure))

    with pytest.raises(SystemExit) as caught:
        tool.main()

    assert "private-test-token" not in str(caught.value)
    assert "Re-audit before retrying" in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contexts", [1]),
        ("contexts", [False]),
        ("contexts", [0, 1]),
        ("integration_types", [1]),
        ("integration_types", [False]),
        ("id", "123"),
        ("application_id", "123"),
        ("guild_id", "42"),
        ("name", "play"),
        ("type", 2),
        ("default_member_permissions", "32"),
        ("nsfw", True),
    ],
)
async def test_raw_global_access_must_match_the_fetched_survivor(field: str, value: Any) -> None:
    guild_command = command("join", 2, GUILD_ID)
    tree = inventory([command("join", 1)], [guild_command])
    tree.client.http.get_global_commands.return_value[0][field] = value

    with pytest.raises(tool.ReconciliationError):
        await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True)

    guild_command.delete.assert_not_awaited()


async def test_default_permission_zero_is_preserved() -> None:
    global_command, guild_command = command("join", 1), command("join", 2, GUILD_ID)
    global_command.default_member_permissions = discord.Permissions(0)
    guild_command.default_member_permissions = discord.Permissions(0)
    tree = inventory([global_command], [guild_command])

    assert await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True) == ["join"]

    guild_command.delete.assert_awaited_once()
    global_command.delete.assert_not_awaited()


async def test_raw_access_fetch_failure_never_deletes() -> None:
    guild_command = command("join", 2, GUILD_ID)
    tree = inventory([command("join", 1)], [guild_command])
    tree.client.http.get_global_commands.side_effect = RuntimeError("offline")

    with pytest.raises(RuntimeError, match="offline"):
        await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True)

    guild_command.delete.assert_not_awaited()
