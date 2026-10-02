"""The Discord client: gateway intents, slash commands, and each server's voice channel."""

import asyncio
import logging
from collections import defaultdict
from typing import cast

import discord
from discord import app_commands

from sobafm.commands import add_commands
from sobafm.config import Settings
from sobafm.store import Store

log = logging.getLogger(__name__)

REQUIRED_PERMISSIONS = {"view_channel": "View Channel", "connect": "Connect", "speak": "Speak"}


class Voice(discord.VoiceClient):
    """A voice client that tells SobaFM when discord.py is finished with it.

    discord.py forgets its voice clients when a new gateway session starts, but an old client
    keeps running and can still disconnect SobaFM, so SobaFM tracks the clients it opens.
    """

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        super().__init__(client, channel)
        self.sobafm = cast("SobaFM", client)
        self.joined = False  # set once the connection completes
        self.sobafm.voices.add(self)

    def cleanup(self) -> None:
        current = self.guild.voice_client is self
        if current:  # an old client must not unregister its replacement
            super().cleanup()
        self.sobafm.voice_ended(self, left=current and self.joined)


class SobaFM(discord.Client):
    def __init__(self, settings: Settings, store: Store) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.store = store
        self.tree = app_commands.CommandTree(
            self, allowed_installs=app_commands.AppInstallationType(guild=True, user=False)
        )
        add_commands(self.tree, self)
        self.voices: set[Voice] = set()
        self.voice_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

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

    async def on_guild_available(self, guild: discord.Guild) -> None:
        """Rejoin at startup, after a new gateway session, and when an outage ends."""
        await self.rejoin(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.store.forget_channel(guild.id)

    async def rejoin(self, guild: discord.Guild) -> None:
        """Reconnect to the server's remembered channel, or forget it if SobaFM can't."""
        async with self.voice_locks[guild.id]:
            if guild.voice_client is not None:
                return
            await self.close_stale_voice(guild)
            channel_id = await self.store.remembered_channel(guild.id)
            if channel_id is None:
                return
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.VoiceChannel) or missing_permissions(channel):
                log.info("Forgetting voice channel %d in %s", channel_id, guild)
                await self.store.forget_channel(guild.id)
                return
            try:
                await self.connect_to(channel)
            except TimeoutError, discord.ClientException:
                log.warning("Could not rejoin %s in %s", channel, guild, exc_info=True)

    async def join(self, member: discord.Member) -> str:
        """Connect to, or move to, the member's voice channel, and remember it."""
        channel = member.voice.channel if member.voice else None
        if channel is None:
            return "Join a voice channel first, then use /join."
        if isinstance(channel, discord.StageChannel):
            return "SobaFM can't play in Stage channels."
        if missing := missing_permissions(channel):
            return f"SobaFM needs these permissions in {channel.mention}: {', '.join(missing)}."
        async with self.voice_locks[channel.guild.id]:
            voice = cast(discord.VoiceClient | None, channel.guild.voice_client)
            if voice is not None and voice.channel == channel:
                return f"SobaFM is already in {channel.mention}."
            try:
                if voice is None:
                    await self.connect_to(channel)
                else:
                    await move(voice, channel)
            except TimeoutError, discord.ClientException:
                log.warning("Could not connect to %s in %s", channel, channel.guild, exc_info=True)
                return f"SobaFM couldn't connect to {channel.mention}. Try again shortly."
            await self.store.remember_channel(channel.guild.id, channel.id)
        return f"{'Joined' if voice is None else 'Moved to'} {channel.mention}."

    async def leave(self, guild: discord.Guild) -> str:
        """Disconnect from voice and forget the server's channel."""
        async with self.voice_locks[guild.id]:
            await self.store.forget_channel(guild.id)
            await self.close_stale_voice(guild)
            voice = cast(discord.VoiceClient | None, guild.voice_client)
            if voice is None:
                return "SobaFM isn't in a voice channel."
            channel = voice.channel
            await voice.disconnect(force=True)
        return f"Left {channel.mention}."

    async def connect_to(self, channel: discord.VoiceChannel) -> None:
        """Connect to `channel`, first closing any client from an earlier gateway session."""
        await self.close_stale_voice(channel.guild)
        voice = await channel.connect(self_deaf=True, cls=Voice)
        voice.joined = True

    async def close_stale_voice(self, guild: discord.Guild) -> None:
        """Disconnect clients from earlier gateway sessions, which discord.py no longer tracks."""
        for voice in [v for v in self.voices if v.guild.id == guild.id]:
            if voice is not guild.voice_client:
                self.voices.discard(voice)
                # discord.py no longer routes events to this client, so this waits out its timeout.
                log.info("Closing the voice connection from an earlier session in %s", guild)
                await voice.disconnect(force=True)

    def voice_ended(self, voice: Voice, *, left: bool) -> None:
        """Stop tracking `voice`, and handle SobaFM leaving its channel unless shutting down."""
        self.voices.discard(voice)
        # Shutting down disconnects from voice; keep the channel so SobaFM rejoins on restart.
        if left and not self.is_closed():
            self.dispatch("voice_lost", voice.guild)

    async def on_voice_lost(self, guild: discord.Guild) -> None:
        """Forget the channel after SobaFM leaves it, unless it has rejoined since (PLAY-7).

        discord.py's own reconnects keep the client, so this runs only when the client ends:
        after /leave, a disconnect by a member, a deleted channel, or a reconnect discord.py
        gives up on. Discord reports these alike, so all count as removals.
        """
        async with self.voice_locks[guild.id]:
            if guild.voice_client is not None:
                return
            log.info("Left voice in %s", guild)
            await self.store.forget_channel(guild.id)

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """Adopt the channel an administrator moves SobaFM to."""
        if member.id != member.guild.me.id or before.channel is None or after.channel is None:
            return  # joins and departures are handled where SobaFM connects and disconnects
        if before.channel != after.channel:
            await self.store.remember_channel(member.guild.id, after.channel.id)


async def move(voice: discord.VoiceClient, channel: discord.VoiceChannel) -> None:
    """Move to `channel` and stay deafened.

    discord.py's move_to logs a timeout instead of raising it, and its voice state update
    undeafens SobaFM.
    """
    await voice.move_to(channel)
    if voice.channel != channel:
        raise TimeoutError
    await channel.guild.change_voice_state(channel=channel, self_deaf=True)


def missing_permissions(channel: discord.VoiceChannel) -> list[str]:
    """Names of the voice permissions SobaFM lacks in `channel`."""
    granted = channel.permissions_for(channel.guild.me)
    return [name for flag, name in REQUIRED_PERMISSIONS.items() if not getattr(granted, flag)]
