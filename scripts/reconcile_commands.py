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
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

# Only these SobaFM slash commands are eligible; unrelated commands remain intact.
COMMANDS = frozenset({"join", "leave", "play", "now", "stop", "settings"})


class ReconciliationError(Exception):
    """A fixed, safe explanation for an inventory that cannot be reconciled."""


class GlobalAccess(BaseModel):
    """Validate raw access fields; discord.py 2.7.1 misdecodes zero-based flag arrays."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    id: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
    application_id: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
    guild_id: str | None = None
    name: str
    type: StrictInt = 1
    contexts: list[StrictInt] | None = None
    integration_types: list[StrictInt] | None = None
    default_member_permissions: Annotated[str, Field(pattern=r"^[0-9]{1,20}$")] | None = None
    nsfw: StrictBool = False


async def reconcile(
    tree: app_commands.CommandTree[discord.Client], guild: discord.Object, *, apply: bool = False
) -> list[str]:
    """Audit both scopes before deleting anything; return the duplicate command names."""
    globals_ = await tree.fetch_commands()
    guild_commands = await tree.fetch_commands(guild=guild)
    if any(
        command.application_id != tree.client.application_id or command.guild_id != scope
        for commands, scope in ((globals_, None), (guild_commands, guild.id))
        for command in commands
    ):
        raise ReconciliationError("Unexpected application or scope in Discord's inventory.")
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
        application_id = tree.client.application_id
        if application_id is None:
            raise ReconciliationError("Discord authentication did not provide an application.")
        # The 2.7.1 AppCommand decoder shifts contexts/install arrays by one bit. Reuse
        # its authenticated HTTP transport to validate the untouched numeric access fields.
        raw_globals = await tree.client.http.get_global_commands(application_id)
        try:
            access = {int(item.id): item for item in map(GlobalAccess.model_validate, raw_globals)}
        except ValidationError:
            raise ReconciliationError("Invalid global command access inventory.") from None
        # Validate every survivor before the first delete, rather than removing a partial set.
        for command in duplicates:
            survivor = global_slash[command.name]
            raw = access.get(survivor.id)
            permissions = survivor.default_member_permissions
            if (
                raw is None
                or raw.application_id != str(application_id)
                or raw.guild_id is not None
                or raw.name != survivor.name
                or raw.type != survivor.type.value
                or raw.contexts != [0]
                or raw.integration_types != [0]
                or raw.default_member_permissions
                != (str(permissions.value) if permissions is not None else None)
                or raw.nsfw != survivor.nsfw
                or command.default_member_permissions != permissions
                or command.nsfw != survivor.nsfw
            ):
                raise ReconciliationError(
                    "Global command access differs or is unknown; review the registrations first."
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
