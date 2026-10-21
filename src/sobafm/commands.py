"""Slash commands.

Replies are private, except a music request's answer once it plays, fails, or is refused.
A slow start's answer is edited once it plays or doesn't.
"""

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from sobafm.program import Mode

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

    async def music(interaction: discord.Interaction, request: str, mode: Mode) -> None:
        if not isinstance(interaction.user, discord.Member):
            return  # unreachable: the command is guild-only
        if problem := await bot.admit(interaction.user, request):
            await interaction.response.send_message(problem, ephemeral=True)
            return
        cooldown = bot.cooldowns.get(interaction.user.guild.id)  # the one admit() started
        try:
            await interaction.response.defer()
        except BaseException:  # expired or cancelled before playback can start
            bot.free_cooldown(interaction.user.guild, cooldown)
            raise
        sent: asyncio.Future[discord.WebhookMessage] = asyncio.get_running_loop().create_future()

        async def announce(answer: str) -> None:  # a slow start's answer, once it plays or doesn't
            # Shielded, so cancelling this at shutdown leaves `sent` for the command to set.
            await (await asyncio.shield(sent)).edit(content=answer)

        reply = await bot.play(interaction.user, request, cooldown, announce, mode=mode)
        try:  # titles are model-written, so links get no preview, then or after an edit
            sent.set_result(await interaction.followup.send(reply, suppress_embeds=True, wait=True))
        except BaseException:
            sent.cancel()  # nothing to edit
            raise

    def register(name: str, description: str, mode: Mode) -> None:
        @app_commands.guild_only()
        @app_commands.describe(
            request="The music you want, for example: rainy lo-fi with soft piano"
        )
        async def request(
            interaction: discord.Interaction, request: app_commands.Range[str, 1, 200]
        ) -> None:
            await music(interaction, request, mode)

        tree.command(name=name, description=description)(request)

    register(
        "freestyle",
        "Play music in SobaFM's voice channel; requests are sent to Google",
        Mode.GENERATED,
    )
    if bot.settings.jamendo_client_id is not None:
        register(
            "play",
            "Play catalog music; requests go to Google and searches to Jamendo",
            Mode.CATALOG,
        )

    @tree.command(description="Show what is playing")
    @app_commands.guild_only()
    async def now(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return  # unreachable: the command is guild-only
        bot.credit_views.close(interaction.guild.id, interaction.user.id)
        try:
            await interaction.response.defer(ephemeral=True)
            reply = bot.now(interaction.guild)
            snapshot = bot.credit_snapshot(interaction.guild)
            if snapshot is not None:
                await bot.credit_views.show(interaction, snapshot, reply)
            else:
                await interaction.followup.send(
                    reply,
                    ephemeral=True,
                    suppress_embeds=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            log.warning("Could not deliver private now-playing reply")

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
        log.error("/%s failed", command, exc_info=error if command not in {"play", "now"} else None)
        reply = "Something went wrong. Try again shortly."
        with contextlib.suppress(discord.HTTPException):  # the interaction may have expired
            if interaction.response.is_done():
                await interaction.followup.send(reply, ephemeral=True)
            else:
                await interaction.response.send_message(reply, ephemeral=True)
