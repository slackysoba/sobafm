"""Slash commands. Replies are private to the caller."""

from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from sobafm.bot import SobaFM


def add_commands(tree: app_commands.CommandTree[SobaFM], bot: SobaFM) -> None:
    @tree.command(description="Bring SobaFM to your voice channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def join(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            return  # unreachable: the command is guild-only
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await bot.join(interaction.user), ephemeral=True)

    @tree.command(description="Disconnect SobaFM from voice")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def leave(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return  # unreachable: the command is guild-only
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await bot.leave(interaction.guild), ephemeral=True)
