"""Audit duplicate SobaFM registrations; explicitly remove reviewed guild copies only.

Uses DISCORD_TOKEN from the environment. Never connects a gateway, starts a bot,
syncs a command tree, or changes global commands. See docs/self-hosting.md.
"""

import argparse
import asyncio
import os
import re
import sys
from typing import Annotated

import aiohttp
import discord
from discord import app_commands
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
)

# Only these SobaFM slash commands are eligible; unrelated commands remain intact.
COMMANDS = frozenset({"join", "leave", "play", "now", "stop", "settings"})


class ReconciliationError(Exception):
    """A fixed, safe explanation for an inventory that cannot be reconciled."""


type Snowflake = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]


class CommandSnapshot(BaseModel):
    """Keep identity and access fields that AppCommand coerces, drops, or misdecodes."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    id: Snowflake
    application_id: Snowflake
    guild_id: Snowflake | None = None
    name: Annotated[str, Field(min_length=1, max_length=32)]
    type: Annotated[StrictInt, Field(ge=1, le=4)] = 1
    contexts: list[Annotated[StrictInt, Field(ge=0, le=2)]] | None = None
    integration_types: list[Annotated[StrictInt, Field(ge=0, le=1)]] | None = None
    default_member_permissions: Annotated[str, Field(pattern=r"^[0-9]{1,20}$")] | None = None
    default_permission: StrictBool | None = True
    dm_permission: StrictBool = True
    nsfw: StrictBool = False

    @field_validator("id", "application_id", "guild_id")
    @classmethod
    def bounded_snowflake(cls, value: str | None) -> str | None:
        if value is not None and int(value) >= 2**64:
            raise ValueError("Snowflake exceeds Discord's range.")
        return value


SNAPSHOTS = TypeAdapter(list[CommandSnapshot])


def validate_inventory(
    payload: object, application_id: int, scope: int | None
) -> dict[int, CommandSnapshot]:
    """Reject malformed, ambiguous, or mis-scoped raw records before comparing models."""
    try:
        records = SNAPSHOTS.validate_python(payload, strict=True)
    except ValidationError:
        raise ReconciliationError("Invalid command identity or access inventory.") from None
    if any(
        record.application_id != str(application_id)
        or record.guild_id != (str(scope) if scope is not None else None)
        for record in records
    ):
        raise ReconciliationError("Unexpected application or scope in Discord's inventory.")
    by_id = {int(record.id): record for record in records}
    if len(by_id) != len(records) or len({(r.name, r.type) for r in records}) != len(records):
        raise ReconciliationError("Ambiguous command identity in Discord's inventory.")
    return by_id


def check_parsed_inventory(
    commands: list[app_commands.AppCommand], snapshots: dict[int, CommandSnapshot]
) -> None:
    """Only retain deletion objects whose identity and decoded access match the raw audit."""
    if len(commands) != len(snapshots) or {c.id for c in commands} != snapshots.keys():
        raise ReconciliationError("Command inventory changed; re-audit before retrying.")
    for command in commands:
        raw = snapshots[command.id]
        permissions = command.default_member_permissions
        if (
            raw.application_id != str(command.application_id)
            or raw.guild_id != (str(command.guild_id) if command.guild_id is not None else None)
            or raw.name != command.name
            or raw.type != command.type.value
            or (
                int(raw.default_member_permissions)
                if raw.default_member_permissions is not None
                else None
            )
            != (permissions.value if permissions is not None else None)
            or raw.nsfw is not command.nsfw
            or raw.dm_permission is not command.dm_permission
        ):
            raise ReconciliationError("Command inventory changed; re-audit before retrying.")
        # Do not compare parsed contexts/installations: 2.7.1 shifts these arrays by one bit.
        # default_permission is also dropped by AppCommand; keep all three in raw snapshots.


async def reconcile(
    tree: app_commands.CommandTree[discord.Client], guild: discord.Object, *, apply: bool = False
) -> list[str]:
    """Audit both scopes before deleting anything; return the duplicate command names."""
    globals_ = await tree.fetch_commands()
    guild_commands = await tree.fetch_commands(guild=guild)
    application_id = tree.client.application_id
    if application_id is None:
        raise ReconciliationError("Discord authentication did not provide an application.")
    if any(
        command.application_id != tree.client.application_id or command.guild_id != scope
        for commands, scope in ((globals_, None), (guild_commands, guild.id))
        for command in commands
    ):
        raise ReconciliationError("Unexpected application or scope in Discord's inventory.")
    # Reuse the authenticated library transport; validate both raw scopes before trusting
    # parsed equality. These snapshots are retained for the final pre-deletion check.
    global_snapshot = validate_inventory(
        await tree.client.http.get_global_commands(application_id), application_id, None
    )
    guild_snapshot = validate_inventory(
        await tree.client.http.get_guild_commands(application_id, guild.id),
        application_id,
        guild.id,
    )
    check_parsed_inventory(globals_, global_snapshot)
    check_parsed_inventory(guild_commands, guild_snapshot)
    print(
        f"Inventory: {len(globals_)} global commands, {len(guild_commands)} guild commands; "
        "application and scopes match."
    )
    global_slash = {
        command.name: command
        for command in globals_
        if command.type is discord.AppCommandType.chat_input and command.name in COMMANDS
    }
    duplicates = [
        command
        for command in guild_commands
        if command.type is discord.AppCommandType.chat_input and command.name in global_slash
    ]
    if apply and duplicates:
        current_globals = validate_inventory(
            await tree.client.http.get_global_commands(application_id), application_id, None
        )
        current_guild = validate_inventory(
            await tree.client.http.get_guild_commands(application_id, guild.id),
            application_id,
            guild.id,
        )
        # Check the entire plan before the first deletion. Discord offers no atomic
        # GET-and-delete, so the operator must keep command/access writers quiescent.
        for command in duplicates:
            survivor = global_snapshot[global_slash[command.name].id]
            candidate = guild_snapshot[command.id]
            if (
                current_globals.get(int(survivor.id)) != survivor
                or current_guild.get(command.id) != candidate
            ):
                raise ReconciliationError("Command inventory changed; re-audit before retrying.")
            if (
                survivor.contexts != [0]
                or survivor.integration_types != [0]
                or candidate.contexts not in (None, [0])
                or candidate.integration_types not in (None, [0])
                or candidate.default_member_permissions != survivor.default_member_permissions
                or candidate.nsfw != survivor.nsfw
                or candidate.default_permission is not True
                or survivor.default_permission is not True
            ):
                raise ReconciliationError(
                    "Command access differs or is unknown; review the registrations first."
                )
        for command in duplicates:
            await command.delete()
    return sorted(command.name for command in duplicates)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--guild-id", type=positive_id, required=True, help="Former development server"
    )
    result.add_argument("--apply", action="store_true", help="Delete only duplicate guild commands")
    result.add_argument(
        "--permissions-reviewed",
        action="store_true",
        help="Confirm guild overrides were reviewed/copied to surviving globals in Discord",
    )
    return result


def positive_id(value: str) -> int:
    try:
        guild_id = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a positive numeric server ID.") from None
    if not 0 < guild_id < 2**64:
        raise argparse.ArgumentTypeError("Use a positive numeric server ID.")
    return guild_id


async def run(token: str, guild_id: int, *, apply: bool) -> None:
    async with discord.Client(intents=discord.Intents.none()) as client:
        tree = app_commands.CommandTree(client)
        await client.login(token)  # REST authentication only; no gateway or SobaFM setup_hook
        names = await reconcile(tree, discord.Object(id=guild_id), apply=apply)
        print(
            f"{'Removed guild copies' if apply else 'Duplicate guild/global commands'}: "
            f"{', '.join('/' + name for name in names) or 'none'}"
        )
        if not apply:
            print("Read-only audit. Globals and all guild commands are unchanged.")


def main() -> None:
    arguments = parser()
    args = arguments.parse_args()
    if args.apply and not args.permissions_reviewed:
        arguments.error("--apply requires --permissions-reviewed; see docs/self-hosting.md")
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        sys.exit("DISCORD_TOKEN is not set.")
    if not re.fullmatch(r"[!-~]+", token):
        sys.exit("DISCORD_TOKEN is invalid; use visible ASCII without whitespace inside it.")
    try:
        asyncio.run(run(token, args.guild_id, apply=args.apply))
    except ReconciliationError as error:
        sys.exit(str(error))
    except (
        discord.HTTPException,
        discord.LoginFailure,
        aiohttp.ClientError,
        OSError,
        ValueError,
        TypeError,
    ):
        sys.exit(
            "Discord audit/reconciliation failed. Re-audit before retrying; no further deletions."
        )


if __name__ == "__main__":
    main()
