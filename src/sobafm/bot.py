"""The Discord client: gateway intents, slash commands, and each server's voice channel."""

import logging
from typing import cast

import discord
from discord import app_commands

from sobafm.commands import add_commands
from sobafm.config import Settings
from sobafm.store import Store

log = logging.getLogger(__name__)

REQUIRED_PERMISSIONS = {"view_channel": "View Channel", "connect": "Connect", "speak": "Speak"}


class SobaFM(discord.Client):
    def __init__(self, settings: Settings, store: Store) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.store = store
        self.tree = app_commands.CommandTree(self)
        add_commands(self.tree, self)

    async def setup_hook(self) -> None:
        await self.store.initialize()
        if self.settings.dev_guild_id is None:
            await self.tree.sync()
        else:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        log.info("Connected as %s to %d servers", self.user, len(self.guilds))
        await self.rejoin_remembered_channels()

    async def rejoin_remembered_channels(self) -> None:
        """Reconnect to each server's remembered channel, or forget it if SobaFM can't."""
        for guild_id, channel_id in (await self.store.remembered_channels()).items():
            guild = self.get_guild(guild_id)
            if guild is not None and guild.voice_client is not None:
                continue
            channel = guild.get_channel(channel_id) if guild else None
            if not isinstance(channel, discord.VoiceChannel) or missing_permissions(channel):
                log.info("Forgetting voice channel %d in server %d", channel_id, guild_id)
                await self.store.forget_channel(guild_id)
                continue
            try:
                await channel.connect(self_deaf=True)
            except TimeoutError, discord.ClientException:
                log.warning("Could not rejoin %s in %s", channel, channel.guild, exc_info=True)

    async def join(self, member: discord.Member) -> str:
        """Connect to, or move to, the member's voice channel, and remember it."""
        channel = member.voice.channel if member.voice else None
        if channel is None:
            return "Join a voice channel first, then use /join."
        if isinstance(channel, discord.StageChannel):
            return "SobaFM can't play in Stage channels."
        if missing := missing_permissions(channel):
            return f"SobaFM needs these permissions in {channel.mention}: {', '.join(missing)}."
        voice = cast(discord.VoiceClient | None, channel.guild.voice_client)
        try:
            if voice is None:
                await channel.connect(self_deaf=True)
                reply = f"Joined {channel.mention}."
            elif voice.channel == channel:
                reply = f"SobaFM is already in {channel.mention}."
            else:
                await voice.move_to(channel)
                reply = f"Moved to {channel.mention}."
        except TimeoutError, discord.ClientException:
            log.warning("Could not connect to %s in %s", channel, channel.guild, exc_info=True)
            return f"SobaFM couldn't connect to {channel.mention}. Try again shortly."
        await self.store.remember_channel(channel.guild.id, channel.id)
        return reply

    async def leave(self, guild: discord.Guild) -> str:
        """Disconnect from voice and forget the server's channel."""
        await self.store.forget_channel(guild.id)
        voice = cast(discord.VoiceClient | None, guild.voice_client)
        if voice is None:
            return "SobaFM isn't in a voice channel."
        channel = voice.channel
        await voice.disconnect(force=False)
        return f"Left {channel.mention}."

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """Follow moves by administrators, and forget a channel SobaFM is removed from."""
        # Shutting down disconnects from voice; keep the channel so SobaFM rejoins on restart.
        if self.is_closed() or member.id != member.guild.me.id or before.channel == after.channel:
            return
        if after.channel is None:
            log.info("Disconnected from %s in %s", before.channel, member.guild)
            await self.store.forget_channel(member.guild.id)
        else:
            await self.store.remember_channel(member.guild.id, after.channel.id)


def missing_permissions(channel: discord.VoiceChannel) -> list[str]:
    """Names of the voice permissions SobaFM lacks in `channel`."""
    granted = channel.permissions_for(channel.guild.me)
    return [name for flag, name in REQUIRED_PERMISSIONS.items() if not getattr(granted, flag)]
