"""The Discord client: gateway intents, slash commands, and each server's voice channel."""

import asyncio
import logging
import math
import time
from collections import defaultdict
from collections.abc import Callable
from typing import cast

import discord
from discord import app_commands
from discord.types.voice import GuildVoiceState

from sobafm.commands import add_commands
from sobafm.config import Settings
from sobafm.deck import Connect, lyria
from sobafm.plan import MusicPlan
from sobafm.station import Outcome, SessionPool, Station
from sobafm.store import GuildSettings, Store

log = logging.getLogger(__name__)

REQUIRED_PERMISSIONS = {"view_channel": "View Channel", "connect": "Connect", "speak": "Speak"}
START_TIMEOUT_S = 75.0  # covers one refused start and its retry

PLAY_REPLIES = {
    Outcome.BUSY: "SobaFM is already playing in as many servers as it can. Try again later.",
    Outcome.REFUSED: (
        "Lyria RealTime couldn't make music from that request. Try describing it differently."
    ),
    Outcome.FAILED: "SobaFM couldn't start the music. Try again shortly.",
    Outcome.DISCONNECTED: "SobaFM lost its voice connection before the music started.",
    Outcome.REPLACED: "A newer request replaced this one before it started.",
    Outcome.STOPPED: "The music was stopped before this request started.",
}


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

    async def on_voice_state_update(self, data: GuildVoiceState) -> None:
        await super().on_voice_state_update(data)
        if cast(object, data["channel_id"]) is not None:  # None, despite its type, on leaving
            # discord.py's general voice reconnect leaves the channel and rejoins it. Leaving sets
            # this flag and nothing clears it after the rejoin, so discord.py 2.7.1 would take a
            # later move, which closes the voice websocket with 4014, for a disconnection
            # (voice_state.py, #46).
            self._connection._disconnected.clear()  # pyright: ignore[reportPrivateUsage]

    async def finish_connecting(self) -> None:
        """Wait until discord.py's connector, which it restarts when SobaFM is moved or the voice
        server changes during the handshake, connects or gives up."""
        while True:
            connector = cast(
                asyncio.Task[None] | None,
                self._connection._connector,  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]
            )
            if connector is None or connector.done():
                return
            await asyncio.wait({connector})


class SobaFM(discord.Client):
    def __init__(
        self,
        settings: Settings,
        store: Store,
        connect: Connect | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.store = store
        self.open_session = connect or lyria(settings.gemini_api_key.get_secret_value())
        self.pool = SessionPool(settings.max_sessions)
        self.stations: dict[int, Station] = {}
        self.tree = app_commands.CommandTree(
            self, allowed_installs=app_commands.AppInstallationType(guild=True, user=False)
        )
        add_commands(self.tree, self)
        self.voices: set[Voice] = set()
        self.voice_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.cooldowns: dict[int, float] = {}  # when each server's change cooldown started
        self.settings_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._clock = clock

    async def close(self) -> None:
        """Close every station, ending its Lyria sessions, before disconnecting."""
        await asyncio.gather(*(station.close() for station in self.stations.values()))
        self.stations.clear()
        await super().close()

    def station(self, guild: discord.Guild, volume: float) -> Station:
        """The guild's station, created at `volume` and started on first use."""
        station = self.stations.get(guild.id)
        if station is None:
            station = Station(
                lambda: cast(discord.VoiceClient | None, guild.voice_client),
                lambda: listeners(guild),
                self.open_session,
                self.pool,
                volume=volume,
            )
            station.start()
            self.stations[guild.id] = station
        return station

    async def close_station(self, guild: discord.Guild, outcome: Outcome = Outcome.STOPPED) -> None:
        if (station := self.stations.pop(guild.id, None)) is not None:
            await station.close(outcome)

    async def admit(self, member: discord.Member, request: str) -> str | None:
        """Admit a request and start the server's change cooldown, or say why it can't play.

        The check and the start of the cooldown happen without an await between them, so
        concurrent requests cannot both get past it. The start time in `cooldowns` identifies
        the request: one that ends without playing frees it with `free_cooldown()`.
        """
        if not request.strip():
            return "Describe the music you want, for example: rainy lo-fi with soft piano."
        voice = cast(discord.VoiceClient | None, member.guild.voice_client)
        if voice is None:
            return "SobaFM isn't in a voice channel. Ask a server manager to use /join."
        if member.voice is None or member.voice.channel != voice.channel:
            return f"Join {voice.channel.mention} to request music."
        settings = await self.store.settings(member.guild.id)
        now = self._clock()
        wait = settings.cooldown_seconds - (now - self.cooldowns.get(member.guild.id, -math.inf))
        if wait > 0:
            return f"The music changed recently. Try again in {seconds(math.ceil(wait))}."
        self.cooldowns[member.guild.id] = now
        return None

    def free_cooldown(self, guild: discord.Guild, cooldown: float | None) -> None:
        """Free the cooldown that started at `cooldown`, unless another has started since."""
        if cooldown is not None and self.cooldowns.get(guild.id) == cooldown:
            del self.cooldowns[guild.id]

    async def play(self, member: discord.Member, request: str, cooldown: float | None) -> str:
        """Start or replace the program, and describe the outcome once it plays or fails.

        `cooldown` is when `admit()` started this request's cooldown, if it did. A request that
        ends without playing frees it, even after the reply.
        """
        playing = False  # or may still play
        try:
            plan = MusicPlan.from_request(request)
            settings = await self.store.settings(member.guild.id)
            started = self.station(member.guild, settings.volume).play(plan, member.mention)
            try:
                outcome = await asyncio.wait_for(asyncio.shield(started), START_TIMEOUT_S)
            except TimeoutError:

                def settled(done: asyncio.Future[Outcome]) -> None:
                    if done.result() is not Outcome.PLAYING:
                        self.free_cooldown(member.guild, cooldown)

                started.add_done_callback(settled)
                playing = True
                return "The music is taking longer than usual to start."
            playing = outcome is Outcome.PLAYING
        finally:
            if not playing:
                self.free_cooldown(member.guild, cooldown)
        if outcome is Outcome.PLAYING:
            title = discord.utils.escape_markdown(plan.title)
            return f"Now playing **{title}**, requested by {member.mention}."
        return PLAY_REPLIES[outcome]

    def stop_problem(self, member: discord.Member) -> str | None:
        """Why `member` cannot stop the music, if they cannot."""
        voice = cast(discord.VoiceClient | None, member.guild.voice_client)
        if voice is None:
            return "SobaFM isn't in a voice channel."
        in_channel = member.voice is not None and member.voice.channel == voice.channel
        if not in_channel and not member.guild_permissions.manage_guild:
            return f"Join {voice.channel.mention} to stop the music."
        return None

    def stop(self, guild: discord.Guild) -> str:
        station = self.stations.get(guild.id)
        if station is None or station.program is None:
            return "Nothing is playing."
        station.stop()
        return "Stopped the music."

    async def configure(
        self,
        guild: discord.Guild,
        *,
        duration_minutes: int | None = None,
        volume_percent: int | None = None,
        cooldown_seconds: int | None = None,
    ) -> str:
        """Apply the given settings, and describe the server's settings."""
        changes = {
            "duration_minutes": duration_minutes,
            "volume_percent": volume_percent,
            "cooldown_seconds": cooldown_seconds,
        }
        updates = {name: value for name, value in changes.items() if value is not None}
        async with self.settings_locks[guild.id]:  # so concurrent changes cannot undo each other
            settings = await self.store.settings(guild.id)
            if updates:
                settings = GuildSettings.model_validate(settings.model_dump() | updates)
                await self.store.save_settings(guild.id, settings)
                if (station := self.stations.get(guild.id)) is not None:
                    station.mixer.set_volume(settings.volume)
        heading = "Saved. SobaFM's settings" if updates else "SobaFM's settings"
        return (
            f"{heading} for this server:\n"
            f"- Play duration: {settings.duration_minutes} minutes\n"
            f"- Volume: {settings.volume_percent}%\n"
            f"- Change cooldown: {seconds(settings.cooldown_seconds)}"
        )

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
                if voice is None:  # a move during the handshake can end elsewhere
                    joined = (await self.connect_to(channel)).channel
                else:
                    await move(voice, channel)
                    joined = channel
            except TimeoutError, discord.ClientException:
                log.warning("Could not connect to %s in %s", channel, channel.guild, exc_info=True)
                return f"SobaFM couldn't connect to {channel.mention}. Try again shortly."
            await self.store.remember_channel(channel.guild.id, joined.id)
        return f"{'Joined' if voice is None else 'Moved to'} {joined.mention}."

    async def leave(self, guild: discord.Guild) -> str:
        """End any program, disconnect from voice, and forget the server's channel."""
        async with self.voice_locks[guild.id]:
            await self.store.forget_channel(guild.id)
            await self.close_station(guild)
            await self.close_stale_voice(guild)
            voice = cast(discord.VoiceClient | None, guild.voice_client)
            if voice is None:
                return "SobaFM isn't in a voice channel."
            channel = voice.channel
            await voice.disconnect(force=True)
        return f"Left {channel.mention}."

    async def connect_to(self, channel: discord.VoiceChannel) -> Voice:
        """Connect to `channel`, first closing any client from an earlier gateway session.

        Closing can outlast the gateway session `channel` comes from, so the channel is looked up
        again. The client counts as joined only once discord.py reports it connected: it can give
        up on rejected handshakes without an error, and a move or a voice server change during
        the handshake restarts its connector, cancelling `connect()`.
        """
        await self.close_stale_voice(channel.guild)
        current = self.get_channel(channel.id)
        if not isinstance(current, discord.VoiceChannel):
            raise discord.ClientException(f"Voice channel {channel.id} isn't available")
        try:
            await current.connect(self_deaf=True, cls=Voice)
        except asyncio.CancelledError:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise  # the command itself is cancelled
        voice = current.guild.voice_client
        if isinstance(voice, Voice):
            await voice.finish_connecting()  # after a restart, the new connector is still running
        if not isinstance(voice, Voice) or not voice.is_connected():
            if voice is not None:
                # If discord.py gave up, it has left already and this waits out its timeout.
                await voice.disconnect(force=True)
            raise TimeoutError
        voice.joined = True
        return voice

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
            await self.close_station(guild, Outcome.DISCONNECTED)

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
    undeafens SobaFM. A move can also end the connection, so the client must still be the
    server's, and connected.
    """
    await voice.move_to(channel)
    moved = voice.channel == channel and channel.guild.voice_client is voice
    if not moved or not voice.is_connected():
        raise TimeoutError
    await channel.guild.change_voice_state(channel=channel, self_deaf=True)


def listeners(guild: discord.Guild) -> int:
    """Members in SobaFM's voice channel who are neither bots nor deafened.

    Members are looked up through the channel, because `guild` can be an object from an
    earlier gateway session whose member cache no longer updates. Members missing from the
    cache count, so an incomplete cache never ends a program.
    """
    voice = cast(discord.VoiceClient | None, guild.voice_client)
    if voice is None:
        return 0
    count = 0
    for user_id, state in voice.channel.voice_states.items():
        member = voice.channel.guild.get_member(user_id)
        if (member is None or not member.bot) and not (state.self_deaf or state.deaf):
            count += 1
    return count


def seconds(count: int) -> str:
    return f"{count} second" if count == 1 else f"{count} seconds"


def missing_permissions(channel: discord.VoiceChannel) -> list[str]:
    """Names of the voice permissions SobaFM lacks in `channel`."""
    granted = channel.permissions_for(channel.guild.me)
    return [name for flag, name in REQUIRED_PERMISSIONS.items() if not getattr(granted, flag)]
