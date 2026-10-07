import argparse
from copy import deepcopy
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
        dm_permission=True,
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

    def raw(item: Any) -> dict[str, Any]:
        return {
            "id": str(item.id),
            "application_id": str(item.application_id),
            "guild_id": str(item.guild_id) if item.guild_id is not None else None,
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
            "dm_permission": item.dm_permission,
        }

    tree.client.http.get_global_commands = AsyncMock(return_value=[raw(item) for item in globals_])

    def raw_guild_fetch(*_: int) -> list[dict[str, Any]]:
        return [raw(item) for item in guild_commands]

    tree.client.http.get_guild_commands = AsyncMock(side_effect=raw_guild_fetch)
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
    tree.client.http.get_guild_commands.side_effect = None
    tree.client.http.get_guild_commands.return_value = [
        common | {"id": "2", "guild_id": str(GUILD_ID)}
    ]

    await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True)

    assert tree.client.http.get_global_commands.await_count == 2
    tree.client.http.get_guild_commands.assert_awaited_with(APP_ID, GUILD_ID)
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


def raw_command(name: str, command_id: int, guild_id: int | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": str(command_id),
        "application_id": str(APP_ID),
        "name": name,
        "description": "Test command",
        "type": 1,
        "default_member_permissions": "32",
        "nsfw": False,
    }
    if guild_id is None:
        record.update(contexts=[0], integration_types=[0])
    else:
        record["guild_id"] = str(guild_id)
    return record


def real_inventory(
    monkeypatch: pytest.MonkeyPatch,
    globals_: list[dict[str, Any]],
    guild_commands: list[dict[str, Any]],
) -> tuple[app_commands.CommandTree[discord.Client], SimpleNamespace]:
    """Use the locked CommandTree fetch/parser/delete paths with no network transport."""
    client = discord.Client(intents=discord.Intents.none(), application_id=APP_ID)

    def global_fetch(*_: int) -> list[dict[str, Any]]:
        return deepcopy(globals_)

    def guild_fetch(*_: int) -> list[dict[str, Any]]:
        return deepcopy(guild_commands)

    http = SimpleNamespace(
        get_global_commands=AsyncMock(side_effect=global_fetch),
        get_guild_commands=AsyncMock(side_effect=guild_fetch),
        delete_guild_command=AsyncMock(),
        delete_global_command=AsyncMock(),
    )
    for method, mock in vars(http).items():
        monkeypatch.setattr(client.http, method, mock)
    return app_commands.CommandTree(client), http


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "unrelated"),
        ("type", 2),
        ("id", "123"),
        ("application_id", "123"),
        ("guild_id", "123"),
        ("default_member_permissions", "0"),
        ("default_member_permissions", None),
        ("nsfw", True),
        ("default_permission", False),
        ("default_permission", None),
        ("contexts", [0]),
        ("integration_types", [0]),
        ("dm_permission", False),
    ],
)
async def test_real_tree_rechecks_every_guild_candidate_after_final_global_get(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    globals_ = [raw_command("join", 1), raw_command("play", 2)]
    guild_commands = [raw_command("join", 11, GUILD_ID), raw_command("play", 12, GUILD_ID)]
    tree, http = real_inventory(monkeypatch, globals_, guild_commands)

    def global_fetch(application_id: int) -> list[dict[str, Any]]:
        assert application_id == APP_ID
        # A valid PATCH arrives during the final global GET. The first candidate is
        # unchanged; the later candidate must invalidate the whole deletion plan.
        if http.get_global_commands.await_count == 3:
            guild_commands[1][field] = value
        return deepcopy(globals_)

    http.get_global_commands.side_effect = global_fetch

    with pytest.raises(tool.ReconciliationError):
        await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True)

    assert http.get_guild_commands.await_count == 3
    http.delete_guild_command.assert_not_awaited()
    http.delete_global_command.assert_not_awaited()


@pytest.mark.parametrize("scope", ["global", "guild"])
@pytest.mark.parametrize("stage", ["audit", "final"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", True),
        ("id", 12.5),
        ("id", None),
        ("id", str(2**64)),
        ("application_id", False),
        ("guild_id", False),
        ("name", None),
        ("type", True),
        ("type", 1.5),
        ("type", None),
        ("type", 99),
        ("default_member_permissions", 32.5),
        ("default_member_permissions", False),
        ("default_member_permissions", 0),
        ("nsfw", 0),
        ("nsfw", None),
        ("default_permission", 0),
        ("default_permission", "true"),
        ("dm_permission", 0),
        ("dm_permission", None),
        ("contexts", [False]),
        ("contexts", [0.5]),
        ("contexts", [99]),
        ("integration_types", [False]),
        ("integration_types", [0.5]),
        ("integration_types", [99]),
    ],
)
async def test_real_tree_rejects_malformed_raw_records_before_any_delete(
    monkeypatch: pytest.MonkeyPatch, scope: str, stage: str, field: str, value: Any
) -> None:
    globals_ = [raw_command("join", 1), raw_command("play", 2)]
    guild_commands = [raw_command("join", 11, GUILD_ID), raw_command("play", 12, GUILD_ID)]
    tree, http = real_inventory(monkeypatch, globals_, guild_commands)
    method = http.get_global_commands if scope == "global" else http.get_guild_commands
    records = globals_ if scope == "global" else guild_commands

    def fetch(*_: int) -> list[dict[str, Any]]:
        payload = deepcopy(records)
        # Real parsed objects are retained from GET 1, then strict raw validation
        # must reject malformed GET 2 (audit) or GET 3 (pre-deletion).
        if method.await_count == (2 if stage == "audit" else 3):
            payload[1][field] = value
        return payload

    method.side_effect = fetch

    with pytest.raises(tool.ReconciliationError, match="Invalid command identity or access"):
        await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True)

    http.delete_guild_command.assert_not_awaited()
    http.delete_global_command.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value", "permissions"),
    [
        ("type", True, "32"),
        ("default_member_permissions", 32.5, "32"),
        ("default_member_permissions", False, "0"),
        ("nsfw", 0, "32"),
    ],
)
async def test_real_tree_does_not_trust_coerced_guild_access(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any, permissions: str
) -> None:
    global_record, guild_record = raw_command("join", 1), raw_command("join", 11, GUILD_ID)
    global_record["default_member_permissions"] = permissions
    guild_record["default_member_permissions"] = permissions
    guild_record[field] = value
    tree, http = real_inventory(monkeypatch, [global_record], [guild_record])

    with pytest.raises(tool.ReconciliationError, match="Invalid command identity or access"):
        await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True)

    http.delete_guild_command.assert_not_awaited()
    http.delete_global_command.assert_not_awaited()


@pytest.mark.parametrize("scope", ["global", "guild"])
@pytest.mark.parametrize("legacy", [False, None])
async def test_real_tree_refuses_disabled_or_unknown_legacy_access(
    monkeypatch: pytest.MonkeyPatch, scope: str, legacy: bool | None
) -> None:
    globals_ = [raw_command("join", 1), raw_command("play", 2)]
    guild_commands = [raw_command("join", 11, GUILD_ID), raw_command("play", 12, GUILD_ID)]
    records = globals_ if scope == "global" else guild_commands
    records[1]["default_permission"] = legacy
    tree, http = real_inventory(monkeypatch, globals_, guild_commands)

    with pytest.raises(tool.ReconciliationError, match="access differs or is unknown"):
        await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True)

    http.delete_guild_command.assert_not_awaited()
    http.delete_global_command.assert_not_awaited()


async def test_real_tree_preserves_supported_nondeletion_records_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    globals_ = [raw_command("join", 1)]
    guild_commands = [
        raw_command("join", 11, GUILD_ID),
        raw_command("leave", 12, GUILD_ID),
        raw_command("unrelated", 13, GUILD_ID),
        raw_command("join", 14, GUILD_ID) | {"type": 2},
        raw_command("join", 15, GUILD_ID) | {"type": 3},
    ]
    # Legacy restriction on a nonduplicate is preserved, rather than silently ignored
    # or made a cleanup candidate. Entry-point type 4 is global-only and untouched.
    guild_commands[1]["default_permission"] = False
    globals_.append(raw_command("activity", 2) | {"type": 4})
    tree, http = real_inventory(monkeypatch, globals_, guild_commands)

    assert await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True) == ["join"]

    http.delete_guild_command.assert_awaited_once_with(APP_ID, GUILD_ID, 11)
    http.delete_global_command.assert_not_awaited()
    assert http.get_global_commands.await_count == 3
    assert http.get_guild_commands.await_count == 3
    for call in http.get_guild_commands.await_args_list:
        assert call.args == (APP_ID, GUILD_ID)


@pytest.mark.parametrize("scope", ["global", "guild"])
async def test_real_tree_final_inventory_failure_aborts_the_entire_plan(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    globals_ = [raw_command("join", 1), raw_command("play", 2)]
    guild_commands = [raw_command("join", 11, GUILD_ID), raw_command("play", 12, GUILD_ID)]
    tree, http = real_inventory(monkeypatch, globals_, guild_commands)
    method = http.get_global_commands if scope == "global" else http.get_guild_commands
    records = globals_ if scope == "global" else guild_commands
    method.side_effect = [deepcopy(records), deepcopy(records), OSError("offline")]

    with pytest.raises(OSError, match="offline"):
        await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True)

    http.delete_guild_command.assert_not_awaited()
    http.delete_global_command.assert_not_awaited()


@pytest.mark.parametrize("scope", ["global", "guild"])
@pytest.mark.parametrize("stage", ["audit", "final"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("name", "unrelated"), ("type", 2), ("default_member_permissions", "0"), ("nsfw", True)],
)
async def test_real_tree_requires_raw_parsed_and_final_snapshot_consistency(
    monkeypatch: pytest.MonkeyPatch, scope: str, stage: str, field: str, value: Any
) -> None:
    globals_ = [raw_command("join", 1), raw_command("play", 2)]
    guild_commands = [raw_command("join", 11, GUILD_ID), raw_command("play", 12, GUILD_ID)]
    tree, http = real_inventory(monkeypatch, globals_, guild_commands)
    method = http.get_global_commands if scope == "global" else http.get_guild_commands
    records = globals_ if scope == "global" else guild_commands

    def fetch(*_: int) -> list[dict[str, Any]]:
        payload = deepcopy(records)
        if method.await_count == (2 if stage == "audit" else 3):
            payload[1][field] = value
        return payload

    method.side_effect = fetch

    with pytest.raises(tool.ReconciliationError, match="Command inventory changed"):
        await tool.reconcile(tree, discord.Object(id=GUILD_ID), apply=True)

    http.delete_guild_command.assert_not_awaited()
    http.delete_global_command.assert_not_awaited()


async def test_real_tree_audit_is_read_only_with_strict_raw_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree, http = real_inventory(
        monkeypatch, [raw_command("join", 1)], [raw_command("join", 11, GUILD_ID)]
    )

    assert await tool.reconcile(tree, discord.Object(id=GUILD_ID)) == ["join"]

    assert http.get_global_commands.await_count == 2
    assert http.get_guild_commands.await_count == 2
    http.delete_guild_command.assert_not_awaited()
    http.delete_global_command.assert_not_awaited()
