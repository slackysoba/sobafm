"""Slash commands. Replies are private, except /play's answer once it plays, fails, or is
refused."""

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
        if problem := await bot.admit(interaction.user, request):
            await interaction.response.send_message(problem, ephemeral=True)
            return
        cooldown = bot.cooldowns.get(interaction.user.guild.id)  # the one admit() started
        try:
            await interaction.response.defer()
        except Exception:  # such as an expired interaction, so nothing will play
            bot.free_cooldown(interaction.user.guild, cooldown)
            raise
        reply = await bot.play(interaction.user, request, cooldown)
        await interaction.followup.send(reply, suppress_embeds=True)  # titles are model-written

    @tree.command(description="Stop the music")
    @app_commands.guild_only()
    async def stop(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            return  # unreachable: the command is guild-only
        problem = bot.stop_problem(interaction.user)
        reply = problem or bot.stop(interaction.user.guild)
        await interaction.response.send_message(reply, ephemeral=True)

    @tree.command(description="Show or change SobaFM's settings for this server")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        duration="How long a program plays, in minutes",
        volume="Volume, in percent",
        cooldown="Seconds between program changes",
    )
    async def settings(
        interaction: discord.Interaction,
        duration: app_commands.Range[int, 5, 240] | None = None,
        volume: app_commands.Range[int, 1, 100] | None = None,
        cooldown: app_commands.Range[int, 0, 600] | None = None,
    ) -> None:
        if interaction.guild is None:
            return  # unreachable: the command is guild-only
        reply = await bot.configure(
            interaction.guild,
            duration_minutes=duration,
            volume_percent=volume,
            cooldown_seconds=cooldown,
        )
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
