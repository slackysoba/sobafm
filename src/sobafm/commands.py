"""Slash commands. Replies are private to the caller, except the reply to /play."""

import contextlib
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from sobafm.bot import SobaFM

log = logging.getLogger(__name__)


def add_commands(tree: app_commands.CommandTree[SobaFM], bot: SobaFM) -> None:
    @tree.command(description="Bring SobaFM to your voice channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def join(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            return  # unreachable: the command is guild-only
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await bot.join(interaction.user), ephemeral=True)

    @tree.command(description="Disconnect SobaFM from voice")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def leave(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return  # unreachable: the command is guild-only
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await bot.leave(interaction.guild), ephemeral=True)

    @tree.command(description="Play music in SobaFM's voice channel; requests are sent to Google")
    @app_commands.guild_only()
    @app_commands.describe(request="The music you want, for example: rainy lo-fi with soft piano")
    async def play(
        interaction: discord.Interaction, request: app_commands.Range[str, 1, 200]
    ) -> None:
        if not isinstance(interaction.user, discord.Member):
            return  # unreachable: the command is guild-only
        if problem := bot.play_problem(interaction.user):
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer()
        await interaction.followup.send(await bot.play(interaction.user, request))

    @tree.command(description="Stop the music")
    @app_commands.guild_only()
    async def stop(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            return  # unreachable: the command is guild-only
        problem = bot.stop_problem(interaction.user)
        reply = problem or bot.stop(interaction.user.guild)
        await interaction.response.send_message(reply, ephemeral=True)

    @tree.error
    async def on_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Log a failed command and tell the caller, who would otherwise wait indefinitely."""
        command = interaction.command.name if interaction.command else "unknown"
        log.error("/%s failed", command, exc_info=error)
        reply = "Something went wrong. Try again shortly."
        with contextlib.suppress(discord.HTTPException):  # the interaction may have expired
            if interaction.response.is_done():
                await interaction.followup.send(reply, ephemeral=True)
            else:
                await interaction.response.send_message(reply, ephemeral=True)
