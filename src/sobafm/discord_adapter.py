"""Discord adapter for the first playback slice: slash commands, voice and shutdown.

Commands work only in servers. `/join` and `/leave` require Manage Server by default
through Discord command permissions; `/play` and `/stop` require the caller to share
the bot's voice channel, except that a manager may stop from elsewhere. `/now` is
private and open to ordinary listeners.

Each server's `Station` settles desired, prepared and heard facts on the event loop.
Audio comes from an injected `Producer` that fills a leased `pcm.py` bank. The voice
callback (`StationSource.read`) only copies prepared frames: it never waits on the
network, a process, a database or cleanup, and hands every report back to the loop.
The heard source changes only when the callback emits the first frame of new audio.
Superseded audio is cancelled, its producer joined and its bank released on the loop;
a bank that cannot yet be released stays leased as retained debt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import disnake
from disnake.ext import commands

from sobafm.catalog import (
    MAX_CATALOG_BYTES,
    AdmissionReview,
    Catalog,
    CatalogInputError,
    Recording,
    parse_catalog,
    parse_reviews,
    select,
)
from sobafm.catalog_prompt import normalize_prompt
from sobafm.pcm import FRAME_BYTES, GLOBAL_PCM_BANKS, PcmBank, PcmBankPool, PcmCapacityError
from sobafm.station import MAX_REQUEST_CHARACTERS, Audio, Outcome, Station

log = logging.getLogger(__name__)

TOKEN_VARIABLE = "SOBAFM_DISCORD_TOKEN"
CATALOG_VARIABLE = "SOBAFM_CATALOG"
REVIEWS_VARIABLE = "SOBAFM_REVIEWS"
MAX_REVIEWS_BYTES = 256_000
# Bound on joining a superseded producer before a replacement may lease its bank.
SETTLE_SECONDS = 5.0
SILENCE = bytes(FRAME_BYTES)
NO_MENTIONS = disnake.AllowedMentions.none()
Inter = disnake.ApplicationCommandInteraction[commands.InteractionBot]

NOT_JOINED = "SobaFM is not in a voice channel. A manager must use `/join` first."
NOT_LISTENING = "Join SobaFM's voice channel to use this command."
SERVER_ONLY = "SobaFM commands work in servers, not direct messages."


class ConfigError(Exception):
    """Configuration is missing or invalid; messages never contain values."""


class Producer(Protocol):
    """Fills one bank with a recording's PCM, away from the event loop and voice callback.

    It calls ``end_pcm`` after its completion policy is satisfied, and returns or
    raises only once every borrowed region is committed and its work has settled,
    including after cancellation.
    """

    async def produce(self, recording: Recording, bank: PcmBank) -> None: ...


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    catalog: tuple[Catalog, tuple[AdmissionReview, ...]] | None

    @classmethod
    def from_environ(cls) -> Config:
        """Read configuration, removing the token so child processes cannot inherit it."""
        token = os.environ.pop(TOKEN_VARIABLE, "").strip()
        if not token:
            raise ConfigError(f"{TOKEN_VARIABLE} is required")
        paths = [os.environ.get(name, "") for name in (CATALOG_VARIABLE, REVIEWS_VARIABLE)]
        if not any(paths):
            return cls(token, None)
        if not all(paths):
            raise ConfigError(f"{CATALOG_VARIABLE} and {REVIEWS_VARIABLE} must be set together")
        try:
            catalog = parse_catalog(_read(Path(paths[0]), MAX_CATALOG_BYTES))
            reviews = parse_reviews(_read(Path(paths[1]), MAX_REVIEWS_BYTES))
        except (CatalogInputError, OSError, UnicodeError) as exc:
            raise ConfigError("catalog snapshot or reviews could not be loaded") from exc
        return cls(token, (catalog, reviews))


def _read(path: Path, maximum: int) -> str:
    with path.open("rb") as file:
        payload = file.read(maximum + 1)
    if len(payload) > maximum:
        raise CatalogInputError("input file exceeds byte limit")
    return payload.decode("utf-8")


def intents() -> disnake.Intents:
    """Exactly the Guilds and Voice States gateway intents."""
    return disnake.Intents(guilds=True, voice_states=True)


@dataclass(eq=False)
class Track:
    """Claimed audio with the bank and producer task it owns until actual release."""

    audio: Audio
    bank: PcmBank
    task: asyncio.Task[None]
    retirement: asyncio.Task[None] | None = None


class StationSource(disnake.AudioSource):
    """Voice callback source: plays the current track, switching when new audio is ready."""

    def __init__(self, report: Callable[[str, Track], None]) -> None:
        self._lock = threading.Lock()
        self._report = report
        self._current: Track | None = None
        self._pending: Track | None = None
        self._idle = True

    def read(self) -> bytes:
        if not self._lock.acquire(blocking=False):
            return SILENCE
        try:
            if (pending := self._pending) is not None:
                frame = pending.bank.read_frame()
                if frame:
                    retired, self._current, self._pending = self._current, pending, None
                    self._report("switched", pending)
                    if retired is not None:
                        self._report("retired", retired)
                    return frame
                if frame == b"":
                    self._pending = None
                    self._report("failed", pending)
            if (current := self._current) is not None:
                frame = current.bank.read_frame()
                if frame is None:
                    return SILENCE
                if frame:
                    return frame
                self._current = None
                self._report("drained", current)
            if self._pending is not None:
                return SILENCE
            self._idle = True
            return b""
        finally:
            self._lock.release()

    def offer(self, track: Track) -> bool:
        """Make ``track`` pending; return whether the callback had gone idle."""
        with self._lock:
            assert self._pending is None
            self._pending = track
            idle, self._idle = self._idle, False
            return idle

    def take_pending(self) -> Track | None:
        """Remove the unheard pending track, if the callback has not yet switched to it."""
        with self._lock:
            pending, self._pending = self._pending, None
            return pending

    def take_all(self) -> list[Track]:
        """Remove every track; the callback emits none of their frames afterwards."""
        with self._lock:
            tracks = [t for t in (self._pending, self._current) if t is not None]
            self._pending = self._current = None
            return tracks

    def cleanup(self) -> None:
        """Ownership stays with the adapter, which releases banks on the event loop."""


@dataclass(eq=False)
class GuildPlayer:
    voice: disnake.VoiceClient
    source: StationSource
    station: Station = field(default_factory=Station)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Adapter:
    """Command handlers and per-server players; all state is owned by the event loop."""

    def __init__(
        self,
        catalog: tuple[Catalog, tuple[AdmissionReview, ...]] | None,
        producer: Producer,
        *,
        pool: PcmBankPool = GLOBAL_PCM_BANKS,
    ) -> None:
        self.catalog = catalog
        self._producer = producer
        self._pool = pool
        self.players: dict[int, GuildPlayer] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.debt: list[PcmBank] = []
        self.stopping = asyncio.Event()

    # Access.

    async def _refuse(self, inter: Inter, text: str) -> None:
        await inter.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)

    def _player(self, inter: Inter) -> GuildPlayer | None:
        player = self.players.get(inter.guild_id or 0)
        return player if player is not None and player.voice.is_connected() else None

    @staticmethod
    def _voice_channel(
        inter: Inter,
    ) -> disnake.VoiceChannel | disnake.StageChannel | None:
        state = getattr(inter.author, "voice", None)  # a Member in servers
        return None if state is None else state.channel

    def _listening(self, inter: Inter, player: GuildPlayer) -> bool:
        channel = self._voice_channel(inter)
        return channel is not None and channel == player.voice.channel

    # Commands.

    async def join(self, inter: Inter) -> None:
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        channel = self._voice_channel(inter)
        if channel is None:
            return await self._refuse(inter, "Join a voice channel first, then use `/join`.")
        await inter.response.defer(ephemeral=True)
        player = self.players.get(inter.guild.id)
        if player is not None and not player.voice.is_connected():
            await self._disconnect(inter.guild.id, player)  # settle a lost connection's owners
            player = None
        if player is not None:
            await player.voice.move_to(channel)
        else:
            voice = await channel.connect()
            report = self._reporter(inter.guild.id)
            self.players[inter.guild.id] = GuildPlayer(voice, StationSource(report))
        await inter.followup.send(
            f"Joined {channel.mention}.", ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    async def play(self, inter: Inter, request: str) -> None:
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        if self.catalog is None:
            return await self._refuse(inter, "Catalog playback is not configured.")
        if (player := self._player(inter)) is None:
            return await self._refuse(inter, NOT_JOINED)
        if not self._listening(inter, player):
            return await self._refuse(inter, NOT_LISTENING)
        await inter.response.defer(ephemeral=True)
        await inter.followup.send(
            await self._prepare(player, request), ephemeral=True, allowed_mentions=NO_MENTIONS
        )

    async def _prepare(self, player: GuildPlayer, request: str) -> str:
        assert self.catalog is not None
        try:
            interpretation = normalize_prompt(request)
        except CatalogInputError:
            return f"Requests must be 1 to {MAX_REQUEST_CHARACTERS} printable characters."
        if interpretation.query is None:
            return f"That request cannot be matched: {interpretation.problem}."
        catalog, reviews = self.catalog
        selection = select(catalog, interpretation.query, reviews, limit=1)
        if not selection.matches:
            return "No eligible recording matches that request; nothing was substituted."
        async with player.lock:
            station = player.station
            program = station.request(request).program
            if station.prepared(program, selection.matches[0].recording) is not Outcome.ACCEPTED:
                station.preparation_failed(program)
                return "The matching recording's credits are incomplete; nothing was substituted."
            if not await self._start(player):
                return "Playback capacity is busy; current audio continues. Try again shortly."
        return (
            "Request accepted. A matching recording is preparing and becomes audible once "
            "enough audio is buffered. Use `/now` to see what is actually heard."
        )

    async def _start(self, player: GuildPlayer) -> bool:
        """Claim ready audio, displacing an unheard pending track, and hand it to the player."""
        station = player.station
        if station.switching is not None:
            displaced = player.source.take_pending()
            if displaced is not None:
                station.switch_failed(displaced.audio)
                # Join the displaced producer before its bank's capacity is reused.
                await asyncio.wait({self._retire(displaced)}, timeout=SETTLE_SECONDS)
        audio = station.claim_ready()
        if audio is None:
            return True  # a switch report is in flight; it claims ready audio when settled
        try:
            bank = self._pool.acquire()
        except PcmCapacityError:
            station.switch_failed(audio)
            return False
        task = asyncio.create_task(self._produce(audio.recording, bank))
        idle = player.source.offer(Track(audio, bank, task))
        if idle or not player.voice.is_playing():  # also restarts after a player error
            player.voice.stop()
            player.voice.play(player.source)
        return True

    async def _produce(self, recording: Recording, bank: PcmBank) -> None:
        try:
            await self._producer.produce(recording, bank)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("audio producer failed: %s", type(exc).__name__)
            bank.cancel()  # the voice callback then reports the failure

    async def stop(self, inter: Inter) -> None:
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        if (player := self._player(inter)) is None:
            return await self._refuse(inter, NOT_JOINED)
        if not (self._listening(inter, player) or inter.permissions.manage_guild):
            return await self._refuse(inter, NOT_LISTENING)
        await inter.response.defer(ephemeral=True)
        async with player.lock:
            tracks = self._silence(player)
        if tracks:  # join the stopped producers, bounded; their retirement stays owned
            await asyncio.wait(tracks, timeout=SETTLE_SECONDS)
        await inter.followup.send(
            "Stopped. SobaFM stays connected and idle.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    def _silence(self, player: GuildPlayer) -> set[asyncio.Task[None]]:
        """End the program, remove all audio from the callback and start retirements."""
        player.station.stop()
        tracks = player.source.take_all()
        if player.station.switching is not None:
            player.station.switch_failed(player.station.switching)
        player.station.silenced()  # the callback can no longer emit any removed frame
        return {self._retire(t) for t in tracks}

    async def leave(self, inter: Inter) -> None:
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        if (player := self.players.get(inter.guild.id)) is None:
            return await self._refuse(inter, "SobaFM is not in a voice channel.")
        await inter.response.defer(ephemeral=True)
        if tracks := await self._disconnect(inter.guild.id, player):
            await asyncio.wait(tracks, timeout=SETTLE_SECONDS)
        await inter.followup.send("Stopped and left.", ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def _disconnect(self, guild_id: int, player: GuildPlayer) -> set[asyncio.Task[None]]:
        """Silence and disconnect at once; return the retirements still owning audio."""
        async with player.lock:
            tracks = self._silence(player)
            player.voice.stop()
            try:
                await player.voice.disconnect(force=True)
            except Exception as exc:
                log.warning("voice disconnect failed: %s", type(exc).__name__)
            self.players.pop(guild_id, None)
        return tracks

    async def now(self, inter: Inter) -> None:
        if inter.guild is None:
            return await self._refuse(inter, SERVER_ONLY)
        player = self._player(inter)
        station = None if player is None else player.station
        heard = None if station is None else station.heard
        if station is None:
            status = "SobaFM is not in a voice channel."
        elif heard is None:
            status = "Nothing is audible."
        else:
            status = "Now heard (full credits follow)."
        if (
            station is not None
            and station.desired is not None
            and (heard is None or station.desired != heard.program)
        ):
            status += " A new request is starting and is not audible yet."
        await self._refuse(inter, status)
        if heard is not None:
            for number in range(1, len(heard.credits.pages) + 1):
                page = heard.credits.page(number)
                await inter.followup.send(
                    page.text, ephemeral=True, suppress_embeds=True, allowed_mentions=NO_MENTIONS
                )

    # Reports from the voice callback, settled on the loop.

    def _reporter(self, guild_id: int) -> Callable[[str, Track], None]:
        loop = asyncio.get_running_loop()

        def report(kind: str, track: Track) -> None:
            with contextlib.suppress(RuntimeError):  # loop already closed at shutdown
                loop.call_soon_threadsafe(self._settle, guild_id, kind, track)

        return report

    def _settle(self, guild_id: int, kind: str, track: Track) -> None:
        player = self.players.get(guild_id)
        station = None if player is None else player.station
        if station is not None and kind == "switched":
            station.switched(track.audio)
        elif station is not None and kind == "failed":
            station.switch_failed(track.audio)
        elif station is not None and kind == "drained" and station.heard == track.audio:
            if station.desired == track.audio.program:
                station.stop()  # no continuous selection in this slice: the program ends
            station.silenced()
        if kind != "switched":
            self._retire(track)
        if player is not None and kind in ("switched", "failed") and player.station.ready:
            self._own(asyncio.create_task(self._start_locked(player)))

    async def _start_locked(self, player: GuildPlayer) -> None:
        async with player.lock:
            await self._start(player)

    # Ownership.

    def _own(self, task: asyncio.Task[None]) -> asyncio.Task[None]:
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _retire(self, track: Track) -> asyncio.Task[None]:
        """Start (once) the retirement that owns ``track`` until its bank is released.

        Callers wait on the task without cancelling it, so an interrupted command
        never abandons a producer or a leased bank.
        """
        if track.retirement is None:
            track.retirement = self._own(asyncio.create_task(self._settle_track(track)))
        return track.retirement

    async def _settle_track(self, track: Track) -> None:
        track.bank.cancel()
        track.task.cancel()
        done, _ = await asyncio.wait({track.task}, timeout=SETTLE_SECONDS)
        if not done:
            log.warning("audio producer has not settled; its bank stays leased")
            await asyncio.wait({track.task})
        if not track.bank.release():
            self.debt.append(track.bank)

    async def shutdown(self) -> bool:
        """Stop and disconnect every server, join all owners and report full release."""
        for guild_id, player in list(self.players.items()):
            await self._disconnect(guild_id, player)
        while self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.debt = [bank for bank in self.debt if not bank.release()]
        if self.debt:
            log.error("%d PCM bank(s) remain leased at shutdown", len(self.debt))
        return not self.debt


def create_bot(adapter: Adapter) -> commands.InteractionBot:
    """Register the slash commands; `/play` is omitted when no catalog is configured."""
    bot = commands.InteractionBot(intents=intents())
    servers = disnake.InteractionContextTypes(guild=True)
    manager = disnake.Permissions(manage_guild=True)

    @bot.slash_command(name="join", contexts=servers, default_member_permissions=manager)
    async def join(inter: Inter) -> None:
        """Join or move to your voice channel."""
        await adapter.join(inter)

    @bot.slash_command(name="leave", contexts=servers, default_member_permissions=manager)
    async def leave(inter: Inter) -> None:
        """Stop and disconnect."""
        await adapter.leave(inter)

    @bot.slash_command(name="stop", contexts=servers)
    async def stop(inter: Inter) -> None:
        """End the program and stay connected."""
        await adapter.stop(inter)

    @bot.slash_command(name="now", contexts=servers)
    async def now(inter: Inter) -> None:
        """Privately show what is actually heard, with full credits."""
        await adapter.now(inter)

    if adapter.catalog is not None:

        @bot.slash_command(name="play", contexts=servers)
        async def play(
            inter: Inter,
            request: commands.String[str, 1, MAX_REQUEST_CHARACTERS],
        ) -> None:
            """Play existing music matching your request.

            Parameters
            ----------
            request: Describe the music you want.
            """
            await adapter.play(inter, request)

    else:
        log.warning("catalog is not configured; /play is not registered")
    return bot


async def run(config: Config, producer: Producer) -> int:
    """Run until SIGTERM or SIGINT, then disconnect and exit once owners are released."""
    adapter = Adapter(config.catalog, producer)
    bot = create_bot(adapter)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, adapter.stopping.set)
    client = asyncio.create_task(bot.start(config.token))
    stopping = asyncio.create_task(adapter.stopping.wait())
    await asyncio.wait({client, stopping}, return_when=asyncio.FIRST_COMPLETED)
    released = await adapter.shutdown()
    await bot.close()
    stopping.cancel()
    await asyncio.wait({client})
    error = None if client.cancelled() else client.exception()
    if error is not None:
        # Only the type: client errors must not carry the token or interaction data out.
        log.error("Discord client stopped: %s", type(error).__name__)
    return 0 if released and error is None else 1


def main(producer: Producer) -> int:
    """Entry point for a deployment that supplies the decoder-backed producer."""
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s"
    )
    try:
        config = Config.from_environ()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    return asyncio.run(run(config, producer))
