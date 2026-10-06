"""The Discord client: gateway intents, slash commands, and each server's voice channel."""

import asyncio
import contextlib
import logging
import math
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import cast

import discord
from discord import app_commands
from discord.ext import tasks
from discord.types.voice import GuildVoiceState
from google import genai

from sobafm.commands import add_commands
from sobafm.config import Settings
from sobafm.deck import Connect, lyria
from sobafm.failures import Failure
from sobafm.interpreter import Interpreter, Result
from sobafm.interpreter import Outcome as Interpreted
from sobafm.plan import MusicPlan
from sobafm.station import Outcome, SessionPool, Station
from sobafm.store import GuildSettings, Store

log = logging.getLogger(__name__)

REQUIRED_PERMISSIONS = {"view_channel": "View Channel", "connect": "Connect", "speak": "Speak"}
START_TIMEOUT_S = 75.0  # covers one refused start and its retry
VOICE_CHECK_S = 10.0  # how often SobaFM looks for voice clients discord.py has stranded
# discord.py can go without a connection task while it closes a voice websocket, which takes up
# to its 30-second close timeout, so a client counts as stranded only after twice that.
STRANDED_FOR_S = 60.0
REJOIN_RETRY_S = 30.0  # between attempts to rejoin after replacing a stranded client

PLAY_REPLIES = {
    Outcome.BUSY: "SobaFM is already playing in as many servers as it can. Try again later.",
    Outcome.REFUSED: (
        "Lyria RealTime couldn't make music from that request. Try describing it differently."
    ),
    Outcome.REJECTED: (
        "Google rejected SobaFM's API key, so it can't make music. Ask whoever runs SobaFM to "
        "check its Gemini API key."
    ),
    Outcome.EXHAUSTED: "SobaFM's Gemini API quota is used up for now. Try again later.",
    Outcome.UNAVAILABLE: "Lyria RealTime is unavailable right now. Try again shortly.",
    Outcome.FAILED: "SobaFM couldn't start the music. Try again shortly.",
    Outcome.DISCONNECTED: "SobaFM lost its voice connection before the music started.",
    Outcome.REPLACED: "A newer request replaced this one before it started.",
    Outcome.STOPPED: "The music was stopped before this request started.",
}
REFUSALS = {  # the current music keeps playing
    Interpreted.NOT_MUSIC: (
        "That doesn't sound like a request for music, so nothing changed. Try something like: "
        "rainy lo-fi with soft piano."
    ),
    Interpreted.BLOCKED: "Gemini's safety filters blocked that request, so nothing changed.",
}
FALLBACK_NOTES: dict[Failure | None, str] = {  # why a request was played as typed (AI-4)
    Failure.EXHAUSTED: "\nGemini's quota is used up for now, so the request was used as typed.",
    Failure.UNAVAILABLE: "\nGemini is unavailable right now, so the request was used as typed.",
    None: "\nGemini couldn't interpret the request, so it was used as typed.",
}
# Discord's inline markup: emphasis, spoilers, code, masked links, mentions, and timestamps
MARKUP = re.compile(r"[\\*_~|`<\[\]]")


class Voice(discord.VoiceClient):
    """A voice client that tells SobaFM when discord.py is finished with it.

    discord.py forgets its voice clients when a new gateway session starts, but an old client
    keeps running and can still disconnect SobaFM, so SobaFM tracks the clients it opens.
    """

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        super().__init__(client, channel)
        self.sobafm = cast("SobaFM", client)
        self.joined = False  # set once the connection completes
        self.stranded_since: float | None = None  # when SobaFM found it stranded
        self.sobafm.voices.add(self)

    def cleanup(self) -> None:
        self.failure()  # marks an error that ended discord.py's runner as handled
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

    def stranded(self) -> bool:
        """Whether discord.py has stopped connecting this client without cleaning it up.

        During a network outage, discord.py's voice runner can exit without calling `cleanup()`,
        leaving the client registered with nothing to connect it (#47). A connected client always
        has a running runner, so a stranded client can still report itself connected.
        """
        return all(task is None or task.done() for task in self._connection_tasks())

    def failure(self) -> BaseException | None:
        """The error that ended discord.py's voice runner, if one did, marked as handled."""
        runner = self._connection_tasks()[1]
        if runner is None or not runner.done() or runner.cancelled():
            return None
        return runner.exception()

    def _connection_tasks(self) -> list[asyncio.Task[None] | None]:
        """discord.py's connector and voice runner."""
        connection = self._connection
        return cast(
            list[asyncio.Task[None] | None],
            [connection._connector, connection._runner],  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]
        )

    async def finish_connecting(self) -> None:
        """Wait until discord.py's connector connects or gives up.

        discord.py cancels and restarts its connector when SobaFM is moved, or the voice server
        changes, during the handshake.
        """
        while True:
            connector = cast(
                asyncio.Task[None] | None,
                self._connection._connector,  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]
            )
            if connector is None:
                return
            if connector.done():
                if not connector.cancelled():
                    connector.exception()  # discord.py logged it already
                return
            await asyncio.wait({connector})


class SobaFM(discord.Client):
    def __init__(
        self,
        settings: Settings,
        store: Store,
        connect: Connect | None = None,
        *,
        interpreter: Interpreter | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.store = store
        self.open_session = connect or lyria(settings.gemini_api_key.get_secret_value())
        self.interpreter = interpreter or Interpreter(
            genai.Client(api_key=settings.gemini_api_key.get_secret_value()).aio,
            settings.gemini_model,
        )
        self.pool = SessionPool(settings.max_sessions)
        self.stations: dict[int, Station] = {}
        # Statuses that closed stations set and couldn't clear, and where, by server
        self.left_statuses: dict[int, tuple[int, str]] = {}
        self.tree = app_commands.CommandTree(
            self, allowed_installs=app_commands.AppInstallationType(guild=True, user=False)
        )
        add_commands(self.tree, self)
        self.voices: set[Voice] = set()
        self.voice_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.cooldowns: dict[int, float] = {}  # when each server's change cooldown started
        # Each server's requests being interpreted, oldest first
        self.interpreting: defaultdict[int, list[asyncio.Future[Outcome]]] = defaultdict(list)
        self.settings_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.recoveries: dict[int, asyncio.Task[None]] = {}  # servers replacing a stranded client
        self.announcements: set[asyncio.Task[None]] = set()  # answers to slow starts, pending
        self._clock = clock

    async def close(self) -> None:
        """End the voice check, recoveries, pending edits of slow-start answers, requests, and
        stations, before disconnecting."""
        self.check_voice.cancel()
        for announcement in self.announcements:
            announcement.cancel()
        for recovery in self.recoveries.values():
            recovery.cancel()
        for guild_id in list(self.interpreting):
            self.supersede(guild_id, Outcome.STOPPED)
        await asyncio.gather(*self.recoveries.values(), return_exceptions=True)
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
                channel=lambda: voice_channel_id(guild),
                status=self.show_voice_status,
                shown=self.left_statuses.pop(guild.id, None),
            )
            station.start()
            self.stations[guild.id] = station
        return station

    async def close_station(self, guild: discord.Guild, outcome: Outcome = Outcome.STOPPED) -> None:
        self.supersede(guild.id, outcome)
        if (station := self.stations.pop(guild.id, None)) is not None:
            await station.close(outcome)
            # SobaFM had left the channel, so a station clears the status once it is back
            if station.shown is not None:
                self.left_statuses[guild.id] = station.shown

    async def clear_left_status(self, guild: discord.Guild) -> None:
        """Have a station clear the status a closed one left, now that SobaFM is back in voice.

        The station shows a new program's title instead if one plays first, and drops the record
        if SobaFM is in another channel, where it can't change the status.
        """
        if guild.id in self.left_statuses and guild.id not in self.stations:
            self.station(guild, (await self.store.settings(guild.id)).volume)

    def supersede(self, guild_id: int, outcome: Outcome) -> bool:
        """End the server's requests being interpreted with `outcome`, and say if there were any."""
        requests = self.interpreting.pop(guild_id, [])
        for request in requests:
            request.set_result(outcome)
        return bool(requests)

    def supersede_older(self, guild: discord.Guild, request: asyncio.Future[Outcome]) -> None:
        """Stop tracking a request the station has taken, and end the older ones as replaced."""
        requests = self.interpreting[guild.id]
        arrival = requests.index(request)
        for older in requests[:arrival]:
            older.set_result(Outcome.REPLACED)
        del requests[: arrival + 1]

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

    async def play(
        self,
        member: discord.Member,
        request: str,
        cooldown: float | None,
        announce: Callable[[str], Awaitable[object]] | None = None,
    ) -> str:
        """Interpret the request, play it, and describe the outcome once it plays or fails.

        A relative request refines the current program's plan. A request that reaches the
        station ends the older ones still being interpreted, and /stop or leaving the channel
        ends them all. `cooldown` is when `admit()` started this request's cooldown, if it did.
        A request that ends without playing, including one Gemini refuses, frees it, even after
        the reply. When the start outlasts START_TIMEOUT_S, the answer says so, and `announce`
        receives the answer once it plays or doesn't (FB-1).
        """
        playing = False  # or may still play
        interpreting: asyncio.Future[Outcome] = asyncio.get_running_loop().create_future()
        self.interpreting[member.guild.id].append(interpreting)
        try:
            station = self.stations.get(member.guild.id)
            current = station.program.plan if station and station.program else None
            result = await self.interpreter.interpret(request, current)
            settings = await self.store.settings(member.guild.id)
            # No await from this check to the station, so nothing can end the request unseen.
            if interpreting.done():
                return PLAY_REPLIES[interpreting.result()]
            if (plan := result.plan) is None:
                return REFUSALS[result.outcome]
            if member.guild.voice_client is None:  # left before this request was tracked
                return PLAY_REPLIES[Outcome.DISCONNECTED]
            ends = discord.utils.utcnow() + timedelta(seconds=settings.duration_seconds)
            started = self.station(member.guild, settings.volume).play(
                plan, member.mention, duration_seconds=settings.duration_seconds
            )
            self.supersede_older(member.guild, interpreting)
            try:
                outcome = await asyncio.wait_for(asyncio.shield(started), START_TIMEOUT_S)
            except TimeoutError:

                def settled(done: asyncio.Future[Outcome]) -> None:
                    if done.result() is not Outcome.PLAYING:
                        self.free_cooldown(member.guild, cooldown)

                started.add_done_callback(settled)
                if announce is not None:
                    answer = note(f"Now playing {describe(plan, member.mention, ends)}", result)
                    self.announce_when_settled(started, answer, announce)
                playing = True
                return note("The music is taking longer than usual to start.", result)
            playing = outcome is Outcome.PLAYING
        finally:
            with contextlib.suppress(ValueError):  # ended or handed over already
                self.interpreting[member.guild.id].remove(interpreting)
            if not playing:
                self.free_cooldown(member.guild, cooldown)
        if outcome is Outcome.PLAYING:
            return note(f"Now playing {describe(plan, member.mention, ends)}", result)
        return PLAY_REPLIES[outcome]

    def announce_when_settled(
        self,
        started: asyncio.Future[Outcome],
        playing: str,
        announce: Callable[[str], Awaitable[object]],
    ) -> None:
        """Pass `announce` the answer to a slow start once it settles: `playing`, or why not."""

        async def run() -> None:
            outcome = await asyncio.shield(started)  # cancelling this leaves the program alone
            try:
                await announce(playing if outcome is Outcome.PLAYING else PLAY_REPLIES[outcome])
            except Exception:  # the answer is cosmetic, so a failed edit changes nothing else
                log.warning("Could not update the answer to a slow start", exc_info=True)

        task = asyncio.create_task(run(), name="announcement")
        self.announcements.add(task)
        task.add_done_callback(self.announcements.discard)

    async def show_voice_status(self, channel_id: int, status: str | None) -> bool:
        """Show `status` on voice channel `channel_id`, or return False if SobaFM may not (FB-2).

        Discord's errors propagate, for the station to log and retry.
        """
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            return False  # gone, or a Stage channel
        if not channel.permissions_for(channel.guild.me).set_voice_channel_status:
            return False
        await channel.edit(status=status)
        return True

    def now(self, guild: discord.Guild) -> str:
        """Describe the server's program for /now (CMD-3)."""
        station = self.stations.get(guild.id)
        program = station.program if station is not None else None
        if station is None or program is None or (time_left := station.time_left) is None:
            return "Nothing is playing."
        ends = discord.utils.utcnow() + timedelta(seconds=time_left)
        phase = "Now playing" if program.playing else "Starting"
        return f"{phase} {describe(program.plan, program.requester, ends)}"

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
        cancelled = self.supersede(guild.id, Outcome.STOPPED)
        station = self.stations.get(guild.id)
        if station is None or station.program is None:
            return "Stopped the request before it played." if cancelled else "Nothing is playing."
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
        self.check_voice.start()

    async def on_ready(self) -> None:
        log.info("Connected as %s to %d servers", self.user, len(self.guilds))

    async def on_guild_available(self, guild: discord.Guild) -> None:
        """Rejoin at startup, after a new gateway session, and when an outage ends."""
        await self.rejoin(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.store.forget_channel(guild.id)
        self.left_statuses.pop(guild.id, None)

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
            except Exception as error:  # the channel stays remembered for another attempt
                expected = isinstance(error, OSError | discord.ClientException)  # as in an outage
                log.warning(
                    "Could not rejoin %s in %s: %r", channel, guild, error, exc_info=not expected
                )

    async def join(self, member: discord.Member) -> str:
        """Connect to, or move to, the member's voice channel, and remember where SobaFM ends up."""
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
                    destination = (await self.connect_to(channel)).channel
                else:
                    station = self.stations.get(channel.guild.id)
                    async with station.moving() if station else contextlib.nullcontext():
                        await move(voice, channel)
                    destination = channel
            except TimeoutError, discord.ClientException:
                log.warning("Could not connect to %s in %s", channel, channel.guild, exc_info=True)
                return f"SobaFM couldn't connect to {channel.mention}. Try again shortly."
            await self.store.remember_channel(channel.guild.id, destination.id)
        return f"{'Joined' if voice is None else 'Moved to'} {destination.mention}."

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
            voice = current.guild.voice_client  # a restarted connector that gave up unregistered it
        if not isinstance(voice, Voice) or not voice.is_connected():
            if voice is not None:
                # After five rejected handshakes, discord.py has left already, so this waits out
                # its timeout.
                await voice.disconnect(force=True)
            raise TimeoutError
        voice.joined = True
        await self.clear_left_status(current.guild)
        return voice

    @tasks.loop(seconds=VOICE_CHECK_S)
    async def check_voice(self) -> None:
        """Start recovering each voice client discord.py strands, once it stays stranded."""
        now = self._clock()
        for voice in list(self.voices):
            guild_id = voice.guild.id
            if not voice.joined or voice is not voice.guild.voice_client or not voice.stranded():
                voice.stranded_since = None
            elif voice.stranded_since is None:
                voice.stranded_since = now
            elif now - voice.stranded_since >= STRANDED_FOR_S and guild_id not in self.recoveries:
                self.recoveries[guild_id] = asyncio.create_task(self.recover_voice(voice))

    async def recover_voice(self, voice: Voice) -> None:
        """Replace a stranded voice client, then rejoin until back in voice or nothing to rejoin.

        The outage that strands a client can also fail the first attempts to rejoin.
        """
        guild = voice.guild
        try:
            if not await self.replace_voice(voice):
                return
            while True:
                await self.rejoin(guild)
                # discord.py waits for a departure after a failed attempt, swallowing a cancel.
                raise_swallowed_cancel()
                if guild.voice_client is not None:
                    return
                if await self.store.remembered_channel(guild.id) is None:
                    return
                await asyncio.sleep(REJOIN_RETRY_S)
        except Exception:
            log.exception("Could not recover the voice connection in %s", guild)
        finally:
            self.recoveries.pop(guild.id, None)

    async def replace_voice(self, voice: Voice) -> bool:
        """End the program on a stranded voice client and close it, keeping the channel.

        Returns whether it did, since the client can recover or be replaced meanwhile.
        """
        guild = voice.guild
        async with self.voice_locks[guild.id]:
            if guild.voice_client is not voice or not voice.stranded():
                return False
            log.warning(
                "discord.py stopped reconnecting to voice in %s; rejoining",
                guild,
                exc_info=voice.failure(),
            )
            await self.close_station(guild, Outcome.DISCONNECTED)
            voice.joined = False  # replaced rather than left, so the channel stays remembered
            # discord.py waits up to 30 seconds for Discord to confirm the departure. A later
            # confirmation would reach the new connection and end it.
            await voice.disconnect(force=True)
            raise_swallowed_cancel()
        return True

    async def close_stale_voice(self, guild: discord.Guild) -> None:
        """Disconnect clients from earlier gateway sessions, which discord.py no longer tracks."""
        for voice in [v for v in self.voices if v.guild.id == guild.id]:
            if voice is not guild.voice_client:
                self.voices.discard(voice)
                # discord.py no longer routes events to this client, so this waits out its timeout.
                log.info("Closing the voice connection from an earlier session in %s", guild)
                await voice.disconnect(force=True)
                raise_swallowed_cancel()

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


def raise_swallowed_cancel() -> None:
    """Raise a cancel that discord.py swallowed while it waited for a voice departure."""
    if (task := asyncio.current_task()) is not None and task.cancelling():
        raise asyncio.CancelledError


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


def describe(plan: MusicPlan, requester: str, ends: datetime) -> str:
    """A program's title, requester, style, and end time, with model-written text escaped."""
    style = ", ".join(prompt.text for prompt in plan.prompts)
    if plan.bpm is not None:
        style += f", {plan.bpm} BPM"
    return (
        f"**{escape(plan.title)}**, requested by {requester}.\n"
        f"Style: {escape(style)}\n"
        f"Ends {discord.utils.format_dt(ends, 'R')}."
    )


def escape(text: str) -> str:
    """Model-written text with each character of Discord's inline markup escaped (FB-4).

    Escaping each character keeps masked links, mentions, and timestamps from forming, which
    `discord.utils.escape_markdown` misses inside a masked link. Markup that needs a line start
    can't form, since the text never starts a line.
    """
    return MARKUP.sub(r"\\\g<0>", text)


def voice_channel_id(guild: discord.Guild) -> int | None:
    """SobaFM's voice channel, while it is connected there."""
    voice = cast(discord.VoiceClient | None, guild.voice_client)
    return voice.channel.id if voice is not None and voice.is_connected() else None


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


def note(reply: str, interpreted: Result) -> str:
    """`reply`, saying why the request was used as typed, if it was (AI-4)."""
    if interpreted.outcome is not Interpreted.FALLBACK:
        return reply
    return reply + FALLBACK_NOTES.get(interpreted.failure, FALLBACK_NOTES[None])


def seconds(count: int) -> str:
    return f"{count} second" if count == 1 else f"{count} seconds"


def missing_permissions(channel: discord.VoiceChannel) -> list[str]:
    """Names of the voice permissions SobaFM lacks in `channel`."""
    granted = channel.permissions_for(channel.guild.me)
    return [name for flag, name in REQUIRED_PERMISSIONS.items() if not getattr(granted, flag)]
